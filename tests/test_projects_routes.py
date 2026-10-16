"""Project/preview/deck routes against a real in-process Kyora server (kyora.app.App + KyoraServer)."""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from kyora.config import Config
from kyora.projects.deck import HONESTY_TEXT
from kyora.projects.routes import preview_content_type
from kyora.server import KyoraServer, Router

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAME_OK = FIXTURES / "game-ok"
PNG_1x1 = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c63f8cfc0f01f0005fe02fe0dc4d7d30000000049454e44ae426082")


def multipart(fields: list[tuple[str, str | None, str | None, bytes]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body by hand: (name, filename, content_type, data)."""
    boundary = "----KyoraTestBoundary7MA4YWxkTrZu0gW"
    out = io.BytesIO()
    for name, filename, ctype, data in fields:
        out.write(f"--{boundary}\r\n".encode())
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out.write((disp + "\r\n").encode())
        if ctype:
            out.write(f"Content-Type: {ctype}\r\n".encode())
        out.write(b"\r\n")
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def game_zip(top: str = "block-catcher/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(GAME_OK.rglob("*")):
            if f.is_file():
                zf.write(f, top + f.relative_to(GAME_OK).as_posix())
    return buf.getvalue()


class FakeRunManager:
    def __init__(self, running):
        self.running = running

    def status(self):
        return {"running": list(self.running), "pending": [], "workers": 1}


class ProjectRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kyora-routes-")
        base = Path(cls.tmp.name)
        cls.home = base / "home"
        cls.config_dir = base / "config"
        shutil.copytree(REPO / "config", cls.config_dir)
        cls._old_env = {k: os.environ.get(k) for k in ("KYORA_HOME", "KYORA_CONFIG_DIR")}
        os.environ["KYORA_HOME"] = str(cls.home)
        os.environ["KYORA_CONFIG_DIR"] = str(cls.config_dir)
        from kyora.app import App
        cls.app = App(Config.load(cls.config_dir), home=cls.home)
        assert cls.app.store is not None, cls.app.missing
        cls.app.run_manager = None  # never start real runs from these tests
        cls.router = Router()
        cls.app.register_routes(cls.router)
        cls.server = KyoraServer(cls.app, "127.0.0.1", 0, cls.router)
        cls.server.start()
        cls.app.server_url = cls.server.url

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        try:
            cls.app.store.close()
            cls.app.bus.close()
        except Exception:  # noqa: BLE001
            pass
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def setUp(self):
        self.app.run_manager = None

    # ------------------------------------------------------------------ http helpers
    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                port: int | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.server.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def json(self, method: str, path: str, obj=None):
        body = json.dumps(obj).encode() if obj is not None else None
        status, headers, data = self.request(method, path, body, {"Content-Type": "application/json"} if body else {})
        return status, (json.loads(data) if data else None)

    def preview(self, path: str):
        """GET on the preview listener (game files and the harness live there, not on the main origin)."""
        return self.request("GET", path, port=self.server.preview_port)

    def create_project(self, name="Route Game") -> str:
        status, body = self.json("POST", "/api/projects", {"name": name})
        self.assertEqual(status, 201, body)
        return body["project"]["id"]

    def import_fixture(self, pid: str) -> dict:
        body, ctype = multipart([("file", "block-catcher.zip", "application/zip", game_zip())])
        status, _, data = self.request("POST", f"/api/projects/{pid}/import", body, {"Content-Type": ctype})
        self.assertEqual(status, 201, data)
        return json.loads(data)

    # ------------------------------------------------------------------ tests
    def test_routes_registered_without_errors(self):
        self.assertNotIn("routes:projects.routes", self.app.missing)

    def test_create_list_get_delete(self):
        pid = self.create_project("Listed")
        status, body = self.json("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertIn(pid, [p["id"] for p in body["projects"]])
        status, body = self.json("GET", f"/api/projects/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["project"]["name"], "Listed")
        self.assertEqual(body["slides"], [])
        self.assertEqual(body["runs"], [])
        status, body = self.json("POST", "/api/projects", {"name": ""})
        self.assertEqual(status, 400)
        status, _ = self.json("GET", "/api/projects/p_doesnotexist")
        self.assertEqual(status, 404)
        # 409 while a run of the project is active
        run = self.app.store.create_run(pid, "generate", "x")
        self.app.run_manager = FakeRunManager([run.id])
        status, body = self.json("DELETE", f"/api/projects/{pid}")
        self.assertEqual(status, 409)
        self.assertEqual(body["runs"], [run.id])
        self.app.run_manager = FakeRunManager([])
        status, body = self.json("DELETE", f"/api/projects/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], pid)
        status, _ = self.json("GET", f"/api/projects/{pid}")
        self.assertEqual(status, 404)

    def test_attachment_upload_and_download(self):
        pid = self.create_project("Attachments")
        body, ctype = multipart([
            ("role", None, None, b"reference"),
            ("file", "ref.png", "image/png", PNG_1x1),
            ("file", "notes.md", "text/markdown", "# Notes\nüñí\r\n".encode("utf-8")),
        ])
        status, _, data = self.request("POST", f"/api/projects/{pid}/attachments", body, {"Content-Type": ctype})
        self.assertEqual(status, 201, data)
        atts = json.loads(data)["attachments"]
        self.assertEqual([a["name"] for a in atts], ["ref.png", "notes.md"])
        self.assertEqual(atts[0]["mime"], "image/png")
        self.assertEqual(atts[0]["size"], len(PNG_1x1))
        status, headers, data = self.request("GET", f"/api/projects/{pid}/attachments/{atts[0]['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data, PNG_1x1)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        status, headers, data = self.request("GET", f"/api/projects/{pid}/attachments/{atts[1]['id']}")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"])
        self.assertEqual(data, "# Notes\nüñí\r\n".encode("utf-8"))
        views = self.app.store.attachment_views(pid, [atts[1]["id"]])
        self.assertIn("üñí", views[0]["text_excerpt"])
        status, _, _ = self.request("GET", f"/api/projects/{pid}/attachments/a_nope")
        self.assertEqual(status, 404)
        body, ctype = multipart([("role", None, None, b"reference")])
        status, _, _ = self.request("POST", f"/api/projects/{pid}/attachments", body, {"Content-Type": ctype})
        self.assertEqual(status, 400)

    def test_import_preview_download_deck(self):
        pid = self.create_project("Block Catcher")
        result = self.import_fixture(pid)
        slide = result["slide"]
        self.assertEqual(slide["number"], 1)
        self.assertEqual(slide["title"], "Imported block-catcher.zip")
        self.assertIsNone(slide["gate"])
        self.assertEqual(result["files"], 4)
        status, body = self.json("GET", f"/api/projects/{pid}/slides")
        self.assertEqual(status, 200)
        self.assertEqual([s["number"] for s in body["slides"]], [1])
        self.assertEqual(body["current_slide"], 1)
        events = self.app.bus.replay(0, project_id=pid, types=["slide.created"])
        self.assertEqual(events[-1].data["number"], 1)

        # preview of the materialized slide (on the preview listener; the main origin only redirects)
        status, headers, _ = self.request("GET", f"/preview/{pid}/v1/index.html?kyora=1")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], f"http://127.0.0.1:{self.server.preview_port}/preview/{pid}/v1/index.html?kyora=1")
        status, headers, data = self.preview(f"/preview/{pid}/v1/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn(b'<script src="src/game.js"></script>', data)
        status, headers, data = self.preview(f"/preview/{pid}/v1/src/game.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/javascript; charset=utf-8")
        self.assertEqual(data, (GAME_OK / "src" / "game.js").read_bytes())
        status, headers, _ = self.preview(f"/preview/{pid}/v1/.kyora/manifest.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        status, _, data = self.preview(f"/preview/{pid}/v1/")
        self.assertEqual(status, 200)
        self.assertIn(b"<canvas", data)
        status, headers, _ = self.preview(f"/preview/{pid}/v1")
        self.assertEqual(status, 301)
        self.assertEqual(headers["location"], f"/preview/{pid}/v1/")
        status, headers, _ = self.preview(f"/preview/{pid}/v1/src?kyora=1")
        self.assertEqual(status, 301)
        self.assertEqual(headers["location"], f"/preview/{pid}/v1/src/?kyora=1")
        status, _, data = self.preview(f"/preview/{pid}/work/tests/browser-tests.js")
        self.assertEqual(status, 200)
        self.assertIn(b"at-06", data)
        run = self.app.store.create_run(pid, "modify", "faster", parent_slide=1)
        status, _, _ = self.preview(f"/preview/{pid}/run-{run.id}/index.html")
        self.assertEqual(status, 200)
        for bad in (f"/preview/{pid}/v1/missing.js", f"/preview/{pid}/v9/index.html", f"/preview/{pid}/bogus/index.html",
                    f"/preview/{pid}/run-r_nope/index.html", "/preview/p_nope/v1/index.html",
                    f"/preview/{pid}/work/.git/config"):
            status, _, _ = self.preview(bad)
            self.assertEqual(status, 404, bad)

        # path escapes (encoded ..) are 404, never content
        for esc in ("%2e%2e/%2e%2e/project.json", "..%2f..%2fproject.json", "%2e%2e%2f%2e%2e%2f%2e%2e%2fkyora.db",
                    "src/%2e%2e/%2e%2e/%2e%2e/%2e%2e/project.json", "%2E%2E/%2E%2E/%2E%2E/repo/.git/HEAD"):
            status, _, data = self.preview(f"/preview/{pid}/v1/{esc}")
            self.assertEqual(status, 404, esc)
            self.assertNotIn(b'"slides"', data)

        # slide download
        status, headers, data = self.request("GET", f"/api/projects/{pid}/slides/1/download")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/zip")
        self.assertIn('filename="block-catcher-v1.zip"', headers["content-disposition"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("index.html", names)
        self.assertIn("src/game.js", names)
        status, _, _ = self.request("GET", f"/api/projects/{pid}/slides/5/download")
        self.assertEqual(status, 404)

        # deck
        status, headers, data = self.request("GET", f"/projects/{pid}/deck")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        text = data.decode("utf-8")
        for sid in ("cover", "concept", "design", "architecture", "screenshots", "tests", "changelog", "honesty"):
            self.assertIn(f'id="{sid}"', text)
        self.assertIn(HONESTY_TEXT, text)
        self.assertIn("Imported block-catcher.zip", text)
        status, headers, _ = self.request("GET", f"/projects/{pid}/deck?slide=1&download=1")
        self.assertEqual(status, 200)
        self.assertIn("block-catcher-deck-v1.html", headers["content-disposition"])

        # browser results for a slide
        payload = {"type": "kyora:test-result", "ref": "v1", "results": [{"id": "at-01", "status": "pass"}],
                   "summary": {"passed": 1, "failed": 0, "skipped": 0, "total": 1}}
        status, body = self.json("POST", f"/api/projects/{pid}/slides/1/browser-results", payload)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        stored = json.loads((self.app.store.paths(pid).cache / "browser-results" / "v1.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["summary"]["passed"], 1)
        status, _ = self.json("POST", f"/api/projects/{pid}/slides/4/browser-results", payload)
        self.assertEqual(status, 404)

        # a second import while a run is active -> 409; afterwards it stacks on v1 when asked
        self.app.run_manager = FakeRunManager([run.id])
        body, ctype = multipart([("file", "again.zip", "application/zip", game_zip(""))])
        status, _, _ = self.request("POST", f"/api/projects/{pid}/import", body, {"Content-Type": ctype})
        self.assertEqual(status, 409)
        self.app.run_manager = None
        body, ctype = multipart([("file", "again.zip", "application/zip", game_zip("")), ("parent", None, None, b"1")])
        status, _, data = self.request("POST", f"/api/projects/{pid}/import", body, {"Content-Type": ctype})
        self.assertEqual(status, 201, data)
        self.assertEqual(json.loads(data)["slide"]["parent"], 1)
        status, body = self.json("GET", f"/api/projects/{pid}")
        self.assertEqual([s["number"] for s in body["slides"]], [1, 2])
        self.assertEqual(body["runs"][0]["id"], run.id)

    def test_import_rejects_zip_slip(self):
        pid = self.create_project("Evil import")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("index.html", "<canvas></canvas>")
            zf.writestr("../../escape.txt", "x")
        body, ctype = multipart([("file", "evil.zip", "application/zip", buf.getvalue())])
        status, _, data = self.request("POST", f"/api/projects/{pid}/import", body, {"Content-Type": ctype})
        self.assertEqual(status, 400, data)
        self.assertFalse((self.app.store.paths(pid).root.parent / "escape.txt").exists())
        self.assertEqual(self.app.store.get(pid).slides, [])

    def test_runner_route(self):
        pid = self.create_project("Runner")
        status, headers, _ = self.request("GET", f"/runner/{pid}/v1?run=r_x&iter=1")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], f"http://127.0.0.1:{self.server.preview_port}/runner/{pid}/v1?run=r_x&iter=1")
        status, headers, data = self.preview(f"/runner/{pid}/v1?run=r_x&iter=1")
        harness = REPO / "kyora" / "web" / "runner" / "harness.html"
        if harness.is_file():
            self.assertEqual(status, 200)
            self.assertTrue(headers["content-type"].startswith("text/html"))
        else:  # written by another module; a 404 is the documented behaviour until then
            self.assertEqual(status, 404)
        status, _, _ = self.preview("/runner/p_nope/v1")
        self.assertEqual(status, 404)

    def test_preview_content_types(self):
        self.assertEqual(preview_content_type("a.js"), "application/javascript; charset=utf-8")
        self.assertEqual(preview_content_type("a.mjs"), "application/javascript; charset=utf-8")
        self.assertEqual(preview_content_type("a.json"), "application/json; charset=utf-8")
        self.assertEqual(preview_content_type("a.wasm"), "application/wasm")
        self.assertEqual(preview_content_type("a.css"), "text/css; charset=utf-8")
        self.assertEqual(preview_content_type("a.png"), "image/png")
        self.assertEqual(preview_content_type("a.glb"), "model/gltf-binary")


if __name__ == "__main__":
    unittest.main()
