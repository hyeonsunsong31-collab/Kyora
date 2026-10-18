"""HTTP surface hardening: Host allow-list, Origin check, JSON content type, file-only engine command,
the separate preview listener for untrusted game code, the HttpOnly token cookie, static traversal and
exclusive port binding.

Regression tests for the review findings security-dns-rebind-settings-rce,
security-preview-same-origin-api-access, security-static-backslash-traversal-windows (and
portability-win-static-path-traversal) and portability-win-reuseaddr-double-bind.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora import server as server_mod
from kyora.config import Config
from kyora.server import (KyoraServer, Response, Router, is_json_type, parse_host, read_resource,
                          safe_resource_rel)

REPO = Path(__file__).resolve().parent.parent
GAME_OK = Path(__file__).resolve().parent / "fixtures" / "game-ok"
HUB_TOKEN = "hub-token-for-security-tests-0123456789"
REBIND = "rebind.attacker.example"


def http_request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                 send_host: bool = True):
    """One request on 127.0.0.1:port; a ``Host`` in ``headers`` replaces the automatic one."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        if send_host:
            conn.request(method, path, body=body, headers=headers or {})
        else:  # an HTTP/1.0-style client that sends no Host header at all
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def game_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(GAME_OK.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(GAME_OK).as_posix())
    return buf.getvalue()


def multipart_file(name: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----KyoraSecurityBoundary0123"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class AppServerCase(unittest.TestCase):
    """A real App + KyoraServer (main + preview listener) on 127.0.0.1 with a temporary config/home."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kyora-security-")
        base = Path(cls.tmp.name)
        cls.home = base / "home"
        cls.config_dir = base / "config"
        shutil.copytree(REPO / "config", cls.config_dir)
        cls._old_env = {k: os.environ.get(k) for k in ("KYORA_HOME", "KYORA_CONFIG_DIR")}
        os.environ["KYORA_HOME"] = str(cls.home)
        os.environ["KYORA_CONFIG_DIR"] = str(cls.config_dir)
        from kyora.app import App
        cls.app = App(Config.load(cls.config_dir), home=cls.home)
        cls.app.run_manager = None  # never start real runs from these tests
        cls.app.config.raw_keys.setdefault("hub", {})["token"] = HUB_TOKEN
        cls.router = Router()
        cls.app.register_routes(cls.router)
        cls.server = KyoraServer(cls.app, "127.0.0.1", 0, cls.router)
        cls.server.start()
        cls.app.server_url = cls.server.url
        cls.port = cls.server.port
        cls.pport = cls.server.preview_port

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

    def main(self, method, path, body=None, headers=None, **kw):
        return http_request(self.port, method, path, body, headers, **kw)

    def prev(self, method, path, body=None, headers=None, **kw):
        return http_request(self.pport, method, path, body, headers, **kw)

    def main_json(self, method, path, obj, headers=None):
        return self.main(method, path, json.dumps(obj).encode(), {"Content-Type": "application/json", **(headers or {})})

    def create_project(self, name: str) -> str:
        status, _, data = self.main_json("POST", "/api/projects", {"name": name})
        self.assertEqual(status, 201, data)
        return json.loads(data)["project"]["id"]


class HostAllowListTests(AppServerCase):
    """security-dns-rebind-settings-rce: a rebinding domain reaches 127.0.0.1 with a foreign Host."""

    def test_rebinding_host_cannot_read_keys(self):
        for path in ("/api/settings?reveal=1", "/api/status", "/", "/nope"):
            status, headers, data = self.main("GET", path, headers={"Host": f"{REBIND}:{self.port}"})
            self.assertEqual(status, 403, path)
            self.assertEqual(json.loads(data)["error"], f"host not allowed: {REBIND}")
            self.assertNotIn(HUB_TOKEN.encode(), data)
            self.assertNotIn("access-control-allow-origin", headers)

    def test_rebinding_put_cannot_run_a_command(self):
        """The reported exploit: text/plain PUT of claude_code.command, executed by the next /api/status."""
        marker = Path(self.tmp.name) / "PWNED_via_settings"
        body = json.dumps({"settings": {"claude_code": {"command": ["sh", "-c", f"touch '{marker}'"]}}}).encode()
        status, _, _ = self.main("PUT", "/api/settings", body, {"Host": f"{REBIND}:{self.port}",
                                                               "Content-Type": "text/plain;charset=UTF-8",
                                                               "Origin": f"http://{REBIND}:{self.port}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.app.config.claude_code.command, ["claude"])
        self.main("GET", "/api/status")
        self.assertFalse(marker.exists())

    def test_loopback_names_and_missing_host_are_allowed(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}", f"[::1]:{self.port}",
                     "localhost", f"localhost.:{self.port}"):
            status, _, _ = self.main("GET", "/api/status", headers={"Host": host})
            self.assertEqual(status, 200, host)
        status, _, _ = self.main("GET", "/api/status", send_host=False)
        self.assertEqual(status, 200)

    def test_malformed_host_is_refused(self):
        for host in ("evil.example@127.0.0.1", f"127.0.0.1:{self.port}:1", "::1", "127.0.0.1/x", "a b"):
            status, _, _ = self.main("GET", "/api/status", headers={"Host": host})
            self.assertEqual(status, 403, host)

    def test_allowed_hosts_setting(self):
        server = self.app.config.raw_settings["server"]
        status, _, _ = self.main("GET", "/api/status", headers={"Host": "kyora.lan"})
        self.assertEqual(status, 403)
        server["allowed_hosts"] = ["Kyora.LAN:8765"]
        try:
            status, _, _ = self.main("GET", "/api/status", headers={"Host": "kyora.lan"})
            self.assertEqual(status, 200)
            status, _, _ = self.prev("GET", "/favicon.ico", headers={"Host": "kyora.lan"})
            self.assertEqual(status, 204)
        finally:
            server["allowed_hosts"] = []

    def test_preview_listener_checks_host_too(self):
        status, _, _ = self.prev("GET", "/favicon.ico", headers={"Host": f"{REBIND}:{self.pport}"})
        self.assertEqual(status, 403)


class OriginAndContentTypeTests(AppServerCase):
    def test_cross_origin_state_change_is_refused(self):
        # the preview listener (game code) is another origin of the same host: refused like any other
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{self.pport}", f"http://localhost:{self.pport}",
                       f"https://127.0.0.1:{self.pport}", f"http://{REBIND}:{self.port}", f"http://127.0.0.1:{self.port}/path"):
            status, _, data = self.main_json("POST", "/api/projects", {"name": "x-origin"}, {"Origin": origin})
            self.assertEqual(status, 403, origin)
            self.assertEqual(json.loads(data)["error"], "cross-origin request refused")
        names = [p["name"] for p in json.loads(self.main("GET", "/api/projects")[2])["projects"]]
        self.assertNotIn("x-origin", names)
        # a game on the preview origin cannot PUT settings on the main origin either (cookies are per host)
        status, _, _ = self.main_json("PUT", "/api/settings", {"settings": {"quality": {"min_pass_rate": 0.1}}},
                                      {"Origin": f"http://127.0.0.1:{self.pport}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.app.config.quality.min_pass_rate, 0.94)

    def test_own_origin_and_no_origin_are_accepted(self):
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", None):
            status, _, data = self.main_json("POST", "/api/projects", {"name": "same-origin"},
                                             {"Origin": origin} if origin else {})
            self.assertEqual(status, 201, (origin, data))

    def test_reads_send_no_cors_headers(self):
        status, headers, _ = self.main("GET", "/api/settings", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 200)
        self.assertFalse(any(k.startswith("access-control-") for k in headers))

    def test_reverse_proxy_origin_matches_host(self):
        server = self.app.config.raw_settings["server"]
        server["allowed_hosts"] = ["kyora.example"]
        try:
            status, _, data = self.main_json("POST", "/api/projects", {"name": "proxied"},
                                             {"Host": "kyora.example", "Origin": "https://kyora.example"})
            self.assertEqual(status, 201, data)
            status, _, _ = self.main_json("POST", "/api/projects", {"name": "proxied"},
                                          {"Host": "kyora.example", "Origin": "https://other.example"})
            self.assertEqual(status, 403)
        finally:
            server["allowed_hosts"] = []

    def test_json_body_must_be_declared_json(self):
        body = json.dumps({"settings": {"quality": {"min_pass_rate": 0.5}}}).encode()
        for ctype in ("text/plain;charset=UTF-8", "application/x-www-form-urlencoded", None):
            status, _, data = self.main("PUT", "/api/settings", body, {"Content-Type": ctype} if ctype else {})
            self.assertEqual(status, 400, ctype)
            self.assertIn("Content-Type must be application/json", json.loads(data)["error"])
        self.assertEqual(self.app.config.quality.min_pass_rate, 0.94)
        status, _, data = self.main("POST", "/api/projects", b'{"name": "plain"}', {"Content-Type": "text/plain"})
        self.assertEqual(status, 400, data)

    def test_is_json_type(self):
        for ok in ("application/json", "application/json; charset=utf-8", "Application/JSON", "application/merge-patch+json"):
            self.assertTrue(is_json_type(ok), ok)
        for bad in ("", "text/plain", "text/json", "multipart/form-data; boundary=x", "application/jsonx"):
            self.assertFalse(is_json_type(bad), bad)


class SettingsCommandTests(AppServerCase):
    def test_put_cannot_change_engine_command_or_args(self):
        for patch in ({"claude_code": {"command": ["sh", "-c", "id"]}}, {"claude_code": {"extra_args": ["--x"]}},
                      {"claude_code.command": ["sh"]}):
            status, _, data = self.main_json("PUT", "/api/settings", {"settings": patch})
            self.assertEqual(status, 400, patch)
            error = json.loads(data)["error"]
            self.assertIn("cannot be changed over HTTP", error)
            self.assertIn(str(self.app.config.settings_path), error)
        self.assertEqual(self.app.config.claude_code.command, ["claude"])
        self.assertEqual(self.app.config.claude_code.extra_args, [])
        on_disk = json.loads((self.config_dir / "settings.json").read_text())
        self.assertEqual(on_disk["claude_code"]["command"], ["claude"])

    def test_settings_lists_them_read_only(self):
        status, _, data = self.main("GET", "/api/settings")
        self.assertEqual(status, 200)
        patchable = json.loads(data)["patchable"]
        self.assertNotIn("claude_code.command", patchable)
        self.assertNotIn("claude_code.extra_args", patchable)
        self.assertIn("claude_code.model", patchable)

    def test_unchanged_command_round_trip_is_accepted(self):
        status, _, data = self.main_json("PUT", "/api/settings", {"settings": {"claude_code": {"command": ["claude"]}}})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["changed"], [])


class PreviewListenerTests(AppServerCase):
    """security-preview-same-origin-api-access: game code runs on its own origin without the API."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        body, ctype = multipart_file("file", "game.zip", game_zip())
        status, _, data = http_request(cls.port, "POST", "/api/projects", json.dumps({"name": "Iso"}).encode(),
                                       {"Content-Type": "application/json"})
        cls.pid = json.loads(data)["project"]["id"]
        status, _, data = http_request(cls.port, "POST", f"/api/projects/{cls.pid}/import", body, {"Content-Type": ctype})
        assert status == 201, data

    def test_listeners_and_status(self):
        self.assertTrue(self.pport and self.pport != self.port)
        self.assertEqual(self.app.preview_url, f"http://127.0.0.1:{self.pport}")
        status, _, data = self.main("GET", "/api/status")
        st = json.loads(data)
        self.assertEqual(st["preview_port"], self.pport)
        self.assertEqual(st["preview_url"], f"http://127.0.0.1:{self.pport}")

    def test_main_origin_redirects_game_and_harness(self):
        for path in (f"/preview/{self.pid}/v1/index.html?kyora=1", f"/preview/{self.pid}/v1", f"/runner/{self.pid}/v1?auto=1&post=1"):
            status, headers, data = self.main("GET", path)
            self.assertEqual(status, 302, path)
            self.assertEqual(headers["location"], f"http://127.0.0.1:{self.pport}{path}")
            self.assertEqual(data, b"")
        status, headers, _ = self.main("GET", f"/preview/{self.pid}/v1/index.html", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(headers["location"], f"http://localhost:{self.pport}/preview/{self.pid}/v1/index.html")

    def test_preview_listener_serves_the_game(self):
        status, headers, data = self.prev("GET", f"/preview/{self.pid}/v1/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"<canvas", data)
        self.assertNotIn("x-frame-options", headers)
        status, headers, data = self.prev("GET", f"/runner/{self.pid}/v1?run=&auto=1")
        self.assertEqual(status, 200)
        self.assertIn(b"kyora-test-harness.js", data)
        self.assertNotIn("x-frame-options", headers)  # the main UI frames the harness
        status, headers, data = self.prev("GET", "/static/runner/kyora-test-harness.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/javascript") or
                        headers["content-type"].startswith("text/javascript"))
        self.assertEqual(self.prev("GET", "/favicon.ico")[0], 204)

    def test_preview_listener_has_no_api(self):
        """The imported game's fetch('/api/settings?reveal=1') and PUT run on the preview origin: nothing there."""
        for method, path in (("GET", "/api/settings?reveal=1"), ("GET", "/api/status"), ("GET", "/"),
                             ("GET", "/api/events.json"), ("GET", "/api/projects"), ("GET", "/static/js/app.js"),
                             ("GET", "/static/index.html"), ("GET", "/static/runner/../index.html"),
                             ("GET", f"/projects/{self.pid}/deck"), ("GET", f"/api/projects/{self.pid}/attachments/a_x")):
            status, _, data = self.prev(method, path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(HUB_TOKEN.encode(), data)
        body = json.dumps({"settings": {"claude_code": {"command": ["sh"]}}}).encode()
        status, _, _ = self.prev("PUT", "/api/settings", body, {"Content-Type": "application/json"})
        self.assertIn(status, (404, 405))
        status, _, _ = self.prev("POST", "/api/projects", b'{"name": "x"}', {"Content-Type": "application/json"})
        self.assertIn(status, (404, 405))

    def test_browser_results_on_the_preview_origin(self):
        payload = json.dumps({"type": "kyora:test-result", "ref": "v1", "results": [{"id": "at-01", "status": "pass"}],
                              "summary": {"passed": 1, "failed": 0, "skipped": 0, "total": 1}}).encode()
        path = f"/api/projects/{self.pid}/slides/1/browser-results"
        status, _, data = self.prev("POST", path, payload, {"Content-Type": "application/json",
                                                           "Origin": f"http://127.0.0.1:{self.pport}"})
        self.assertEqual(status, 200, data)
        status, _, _ = self.prev("POST", path, payload, {"Content-Type": "application/json",
                                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 403)
        # the run endpoint is there too (an unknown run is the handler's 404, not a missing route)
        prouter = self.server.preview_httpd.RequestHandlerClass.router
        self.assertIn(("POST", "/api/runs/{id}/browser-results"), [(r.method, r.pattern) for r in prouter.routes])
        status, _, data = self.prev("POST", "/api/runs/r_nope/browser-results", payload, {"Content-Type": "application/json"})
        self.assertEqual(status, 404)
        self.assertNotEqual(json.loads(data)["error"], "not found")
        # and non-browser clients may keep posting to the main listener
        status, _, _ = self.main("POST", path, payload, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def test_preview_check_reports_without_file_contents(self):
        status, _, data = self.main("GET", f"/api/projects/{self.pid}/preview-check?ref=v1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "ref": "v1", "error": None})
        for ref in ("v9", "bogus", "run-r_nope"):
            status, _, data = self.main("GET", f"/api/projects/{self.pid}/preview-check?ref={ref}")
            self.assertEqual(status, 200, ref)
            body = json.loads(data)
            self.assertFalse(body["ok"], ref)
            self.assertTrue(body["error"])
        self.assertNotIn(b"<canvas", data)
        self.assertEqual(self.main("GET", "/api/projects/p_nope/preview-check?ref=v1")[0], 404)

    def test_main_html_cannot_be_framed_and_cookie_is_http_only(self):
        status, headers, _ = self.main("GET", f"/?token={HUB_TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-frame-options"], "SAMEORIGIN")
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"kyora_token={HUB_TOKEN};"))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        status, headers, _ = self.main("GET", "/?token=wrong")
        self.assertNotIn("set-cookie", headers)

    def test_web_ui_never_reads_document_cookie(self):
        for js in (REPO / "kyora" / "web").rglob("*.js"):
            self.assertNotIn("document.cookie", js.read_text(encoding="utf-8"), js)


class PreviewPortTests(unittest.TestCase):
    def _app(self, preview_port=None):
        app = SimpleNamespace(preview_url="", config=SimpleNamespace(
            server=SimpleNamespace(preview_port=preview_port, allowed_hosts=[]),
            hub=SimpleNamespace(token="", expose_ui=False)))
        return app

    def _router(self):
        r = Router()
        r.add("GET", "/ping", lambda req, p: Response.json({"ok": True}), auth="any")
        return r

    def test_plain_app_gets_no_preview_listener(self):
        srv = KyoraServer(SimpleNamespace(), "127.0.0.1", 0, self._router())
        try:
            self.assertIsNone(srv.preview_port)
            self.assertEqual(srv.preview_url, "")
        finally:
            srv.shutdown()

    def test_explicit_router_and_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        app = self._app()
        srv = KyoraServer(app, "127.0.0.1", 0, self._router(), preview_port=free, preview_router=self._router())
        try:
            srv.start()
            self.assertEqual(srv.preview_port, free)
            self.assertEqual(app.preview_url, f"http://127.0.0.1:{free}")
            self.assertEqual(http_request(free, "GET", "/ping")[0], 200)
        finally:
            srv.shutdown()

    def test_auto_port_is_main_plus_one_or_ephemeral_when_busy(self):
        for _ in range(20):
            blocker = socket.socket()
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy = blocker.getsockname()[1]
            try:
                srv = KyoraServer(self._app(), "127.0.0.1", busy - 1, self._router(), preview_router=self._router())
            except OSError:  # busy - 1 is taken by someone else: try another pair
                blocker.close()
                continue
            try:
                self.assertNotIn(srv.preview_port, (busy, 0, None))
            finally:
                srv.shutdown()
                blocker.close()
            return
        self.skipTest("no free port pair found")

    def test_explicit_busy_port_is_an_error(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy = blocker.getsockname()[1]
            with self.assertRaises(OSError):
                KyoraServer(self._app(preview_port=busy), "127.0.0.1", 0, self._router(), preview_router=self._router())


class LanHostTests(unittest.TestCase):
    """A listener bound beyond loopback also accepts this host's own names (hub mode, LAN UI)."""

    def _serve(self, host: str) -> KyoraServer:
        r = Router()
        r.add("GET", "/ping", lambda req, p: Response.json({"ok": True}), auth="any")
        srv = KyoraServer(SimpleNamespace(), host, 0, r)
        srv.start()
        self.addCleanup(srv.shutdown)
        return srv

    def test_hostname_allowed_only_when_bound_beyond_loopback(self):
        name = socket.gethostname()
        if parse_host(name) is None or parse_host(name) in ("localhost",):
            self.skipTest(f"hostname {name!r} is not usable as a Host header")
        local = self._serve("127.0.0.1")
        self.assertEqual(http_request(local.port, "GET", "/ping", headers={"Host": name})[0], 403)
        try:
            lan = self._serve("0.0.0.0")
        except OSError as exc:
            self.skipTest(f"cannot bind 0.0.0.0: {exc}")
        self.assertEqual(http_request(lan.port, "GET", "/ping", headers={"Host": f"{name}:{lan.port}"})[0], 200)
        self.assertEqual(http_request(lan.port, "GET", "/ping", headers={"Host": REBIND})[0], 403)


class StaticTraversalTests(unittest.TestCase):
    """security-static-backslash-traversal-windows."""

    def test_parse_host(self):
        self.assertEqual(parse_host("127.0.0.1:8765"), "127.0.0.1")
        self.assertEqual(parse_host("[::1]:8765"), "::1")
        self.assertEqual(parse_host("LocalHost."), "localhost")
        for bad in ("", "a@b", "::1", "a b", "x:1:2", "[zz]"):
            self.assertIsNone(parse_host(bad), bad)

    def test_safe_resource_rel(self):
        for good, want in (("js/app.js", "js/app.js"), ("/runner/harness.html", "runner/harness.html"),
                           ("index.html", "index.html")):
            self.assertEqual(safe_resource_rel(good), want)
        for bad in ("..\\..\\config\\keys.json", "..\\config\\keys.json", "js\\..\\..\\x", "a/../b", "../x", "a//b",
                    "./a", "C:\\Windows\\win.ini", "c:/x", "\\\\server\\share\\x", "\\x", "js/app.js\x00.png",
                    "file.txt:stream", "", "/"):
            self.assertIsNone(safe_resource_rel(bad), bad)

    def test_backslash_cannot_escape_on_windows_path_semantics(self):
        """Emulate ``WindowsPath.joinpath`` (a backslash separates segments) over a real directory."""

        class WinLikePath:
            def __init__(self, path: Path):
                self.path = path

            def joinpath(self, part: str) -> "WinLikePath":
                p = self.path
                for seg in re.split(r"[\\/]", part):
                    p = p / seg
                return WinLikePath(p)

            def is_file(self) -> bool:
                return self.path.is_file()

            def read_bytes(self) -> bytes:
                return self.path.read_bytes()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "kyora" / "web").mkdir(parents=True)
            (root / "kyora" / "web" / "ok.js").write_text("ok")
            (root / "config").mkdir()
            (root / "config" / "keys.json").write_text('{"secret": "nvapi-xyz"}')
            web = WinLikePath(root / "kyora" / "web")
            evil = "..\\..\\config\\keys.json"
            self.assertTrue(web.joinpath(evil).is_file())  # the emulation really escapes
            old = server_mod._web_root
            server_mod._web_root = lambda: web
            server_mod.clear_resource_cache()
            try:
                self.assertEqual(read_resource("ok.js"), b"ok")
                self.assertIsNone(read_resource(evil))
                self.assertIsNone(read_resource("js/" + evil))
                self.assertEqual(server_mod.resource_response(evil).status, 404)
            finally:
                server_mod._web_root = old
                server_mod.clear_resource_cache()

    def test_static_route_rejects_encoded_backslashes(self):
        app = SimpleNamespace(config=SimpleNamespace(hub=SimpleNamespace(token="", expose_ui=False)))
        r = Router()
        r.add("GET", "/static/{path:path}", lambda req, p: server_mod.resource_response(p["path"]), auth="any")
        srv = KyoraServer(app, "127.0.0.1", 0, r)
        srv.start()
        try:
            self.assertEqual(http_request(srv.port, "GET", "/static/js/app.js")[0], 200)
            for path in ("/static/..%5c..%5cconfig%5ckeys.json", "/static/js%5c..%5c..%5capp.py", "/static/..%2f__init__.py",
                         "/static/c:%5cwindows%5cwin.ini"):
                status, _, data = http_request(srv.port, "GET", path)
                self.assertEqual(status, 404, path)
                self.assertNotIn(b"api_key", data)
        finally:
            srv.shutdown()


class ExclusiveBindTests(unittest.TestCase):
    """portability-win-reuseaddr-double-bind: a second Kyora must not share a listening port."""

    def _handler(self):
        r = Router()
        r.add("GET", "/ping", lambda req, p: Response.json({"ok": True}), auth="any")
        return server_mod._bound_handler(SimpleNamespace(), r, None)

    def _bind_options(self, windows: bool) -> list:
        """setsockopt calls made by ``_HTTPServer.server_bind`` with Windows or POSIX semantics."""
        fake_exclusive = 0x7F00  # stands in for SO_EXCLUSIVEADDRUSE, which only Windows defines
        srv = server_mod._HTTPServer(("127.0.0.1", 0), self._handler(), bind_and_activate=False)
        self.addCleanup(srv.server_close)
        real, calls = srv.socket, []

        class Spy:
            def __getattr__(self, name):
                return getattr(real, name)

            def setsockopt(self, level, opt, value):
                calls.append((level, opt, value))
                if opt != fake_exclusive:
                    real.setsockopt(level, opt, value)

        srv.socket = Spy()
        with mock.patch.object(server_mod, "IS_WINDOWS", windows), \
                mock.patch.object(socket, "SO_EXCLUSIVEADDRUSE", fake_exclusive, create=True):
            srv.server_bind()
        return [(opt, value) for _, opt, value in calls], fake_exclusive

    def test_windows_claims_the_port_exclusively(self):
        calls, exclusive = self._bind_options(windows=True)
        self.assertIn((exclusive, 1), calls)
        self.assertNotIn((socket.SO_REUSEADDR, 1), calls)  # on Windows it would allow a double bind

    def test_posix_keeps_reuseaddr_for_quick_restarts(self):
        calls, exclusive = self._bind_options(windows=False)
        self.assertIn((socket.SO_REUSEADDR, 1), calls)
        self.assertNotIn((exclusive, 1), calls)
        if hasattr(socket, "SO_REUSEPORT"):
            self.assertNotIn((socket.SO_REUSEPORT, 1), calls)  # would let a second process share the port

    def test_binding_does_no_reverse_dns(self):
        # HTTPServer.server_bind resolves the bind address with getfqdn between bind() and listen(); on the
        # macOS runner that took longer than the release check's 15 s, so the packaged server never answered
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS on bind")):
            srv = KyoraServer(SimpleNamespace(), "127.0.0.1", 0, Router(), preview_port=0)
        srv.shutdown()
        self.assertEqual(srv.httpd.server_name, "127.0.0.1")

    def test_stdlib_server_class_is_not_mutated(self):
        from http.server import ThreadingHTTPServer
        before = ThreadingHTTPServer.allow_reuse_address
        srv = KyoraServer(SimpleNamespace(), "127.0.0.1", 0, Router())
        srv.shutdown()
        self.assertEqual(ThreadingHTTPServer.allow_reuse_address, before)

    def test_second_server_on_a_busy_port_fails(self):
        first = KyoraServer(SimpleNamespace(), "127.0.0.1", 0, Router())
        self.addCleanup(first.shutdown)
        first.start()
        with self.assertRaises(OSError):
            KyoraServer(SimpleNamespace(), "127.0.0.1", first.port, Router())

    def test_unstarted_server_shuts_down(self):
        """shutdown() of a listener whose loop never ran must not wait forever."""
        app = SimpleNamespace(preview_url="", config=SimpleNamespace(
            server=SimpleNamespace(preview_port=None, allowed_hosts=[]), hub=SimpleNamespace(token="", expose_ui=False)))
        srv = KyoraServer(app, "127.0.0.1", 0, Router(), preview_router=Router())
        srv.shutdown()

    def test_serve_on_a_busy_port_exits_clearly_before_starting_the_app(self):
        with tempfile.TemporaryDirectory(prefix="kyora-busy-") as d:
            config_dir = Path(d) / "config"
            shutil.copytree(REPO / "config", config_dir)
            blocker = socket.socket()
            self.addCleanup(blocker.close)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy = blocker.getsockname()[1]
            from kyora import __main__ as cli
            from kyora.app import App
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(App, "start") as start, mock.patch("logging.basicConfig"), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["serve", "--config", str(config_dir), "--home", str(Path(d) / "home"),
                               "--host", "127.0.0.1", "--port", str(busy)])
            self.assertEqual(rc, 2)
            start.assert_not_called()  # app.start() would mark the other instance's runs interrupted
            self.assertIn(f"cannot listen on 127.0.0.1:{busy}", err.getvalue())
            self.assertIn("--port", err.getvalue())


if __name__ == "__main__":
    unittest.main()
