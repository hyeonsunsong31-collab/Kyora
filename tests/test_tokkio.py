"""Tests of ``kyora.models.tokkio``: health probing, presenter events, view normalisation and the route.

Local stub HTTP servers stand in for a deployed Tokkio UI / ACE Configurator;
nothing leaves 127.0.0.1. Run: ``python3 -m unittest tests.test_tokkio -v``
"""
from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from kyora.config import Config, TokkioCfg
from kyora.models.tokkio import DOCS, SCREENSHOT_MAX_BYTES, Tokkio, normalise_view, register_routes
from kyora.pipeline.events import EventBus
from kyora.server import KyoraServer, Router
from kyora.util.fs import atomic_write_json


def free_port() -> int:
    """A port with nothing listening on it (bound, then released)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StubTokkio:
    """Serves ``/`` (HTML 200) and ``/openapi.json`` (200 or 404) like a Tokkio UI + ACE Configurator."""

    def __init__(self, openapi_status: int = 200):
        status = openapi_status

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/openapi.json":
                    if status != 200:
                        self.send_response(status)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = json.dumps({"openapi": "3.1.0", "paths": {"/upload": {}, "/status": {}}}).encode()
                    ctype = "application/json"
                else:
                    body = b"<!doctype html><title>Tokkio</title>"
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = self.tmp.name
        self.bus = EventBus(None)

    def tearDown(self):
        self.bus.close()
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self.tmp.cleanup()


class HealthTests(_Base):
    def test_not_configured(self):
        tk = Tokkio(TokkioCfg(ui_url="", configurator_url=""), self.bus)
        self.assertFalse(tk.configured())
        h = tk.health()
        self.assertFalse(h["configured"])
        self.assertFalse(h["ui"]["configured"])
        self.assertFalse(h["ui"]["reachable"])
        self.assertFalse(h["configurator"]["configured"])
        self.assertEqual(h["docs"], DOCS)
        self.assertEqual(h["docs"], "docs/tokkio-overlay.md")

    def test_unreachable_closed_port(self):
        port = free_port()
        tk = Tokkio(TokkioCfg(ui_url=f"http://127.0.0.1:{port}", configurator_url=f"http://127.0.0.1:{port}"), self.bus)
        self.assertTrue(tk.configured())
        h = tk.health()
        self.assertTrue(h["configured"])
        ui = h["ui"]
        self.assertFalse(ui["reachable"])
        self.assertFalse(ui["ok"])
        self.assertIsNone(ui["status"])
        self.assertIn("unreachable", ui["error"])
        self.assertIsInstance(ui["latency_ms"], float)
        conf = h["configurator"]
        self.assertFalse(conf["reachable"])
        self.assertFalse(conf["openapi_found"])
        self.assertIn("unreachable", conf["error"])

    def test_reachable_stub(self):
        stub = StubTokkio()
        try:
            tk = Tokkio(TokkioCfg(ui_url=stub.url + "/", configurator_url=stub.url), self.bus)
            h = tk.health()
        finally:
            stub.close()
        ui = h["ui"]
        self.assertTrue(ui["reachable"])
        self.assertTrue(ui["ok"])
        self.assertEqual(ui["status"], 200)
        self.assertIsNone(ui["error"])
        self.assertGreaterEqual(ui["latency_ms"], 0)
        self.assertEqual(ui["url"], stub.url)  # trailing slash stripped
        conf = h["configurator"]
        self.assertTrue(conf["reachable"])
        self.assertTrue(conf["openapi_found"])
        self.assertEqual(conf["status"], 200)
        self.assertEqual(conf["paths"], 2)

    def test_configurator_without_openapi(self):
        stub = StubTokkio(openapi_status=404)
        try:
            h = Tokkio(TokkioCfg(ui_url=stub.url, configurator_url=stub.url), self.bus).health()
        finally:
            stub.close()
        conf = h["configurator"]
        self.assertTrue(conf["reachable"])
        self.assertFalse(conf["openapi_found"])
        self.assertEqual(conf["status"], 404)
        self.assertIn("404", conf["error"])

    def test_update_config_and_callable_config(self):
        box = {"cfg": TokkioCfg(ui_url="", configurator_url="")}
        tk = Tokkio(lambda: box["cfg"], self.bus)
        self.assertFalse(tk.configured())
        box["cfg"] = TokkioCfg(ui_url="http://tokkio.local", configurator_url="")
        self.assertTrue(tk.configured())
        tk.update_config(TokkioCfg(ui_url="", configurator_url=""))
        self.assertFalse(tk.configured())


class EventTests(_Base):
    def test_narration_view_clear_events(self):
        tk = Tokkio(TokkioCfg(ui_url="", configurator_url=""), self.bus)
        sub = self.bus.subscribe(types=["tokkio.*"])
        try:
            self.assertIsNone(tk.narrate("r1", "p1", "   "))
            self.assertIsNone(tk.narrate("r1", "p1", None))
            tk.narrate("r1", "p1", "  The build passed the gate.  ")
            png = base64.b64encode(b"\x89PNG fake image bytes").decode()
            sent = tk.show_view("r1", "p1", {
                "title": "Sky Runner " + "x" * 200,
                "preview_url": "/preview/p1/v1/index.html",
                "screenshot_b64": "data:image/png;base64," + png,
                "stages": [{"stage": "build", "status": "done", "pct": 100}, {"name": "gate", "status": "running"}],
                "buttons": [{"id": "approve", "text": "Approve"}, {"id": "kyora:cancel", "label": "Cancel"},
                            {"id": "bad id!", "text": "x"}, "kyora:approve"],
            })
            tk.clear_view("r1", "p1")
            events = [sub.queue.get(timeout=2) for _ in range(3)]
        finally:
            sub.close()
        self.assertEqual([e.type for e in events], ["tokkio.narration", "tokkio.view", "tokkio.clear"])
        self.assertEqual(events[0].data, {"text": "The build passed the gate."})
        self.assertEqual(events[0].run_id, "r1")
        self.assertEqual(events[0].project_id, "p1")
        view = events[1].data
        self.assertEqual(view, sent)
        self.assertLessEqual(len(view["title"]), 80)
        self.assertTrue(view["title"].startswith("Sky Runner"))
        self.assertEqual(view["preview_url"], "/preview/p1/v1/index.html")
        self.assertEqual(view["screenshot_b64"], png)
        self.assertEqual(view["stages"], [{"name": "build", "status": "done", "pct": 100.0}, {"name": "gate", "status": "running"}])
        self.assertEqual(view["buttons"], [{"id": "kyora:approve", "text": "Approve"}, {"id": "kyora:cancel", "text": "Cancel"}])
        self.assertTrue(all(b["id"].startswith("kyora:") for b in view["buttons"]))
        self.assertEqual(events[2].data, {})
        recent = tk.recent_views()
        self.assertEqual(len(recent), 1)
        self.assertTrue(recent[0]["has_screenshot"])
        self.assertNotIn("screenshot_b64", recent[0])

    def test_large_or_invalid_screenshot_dropped(self):
        big = "A" * (SCREENSHOT_MAX_BYTES + 4)
        out = normalise_view({"title": "t", "screenshot_b64": big})
        self.assertNotIn("screenshot_b64", out)
        self.assertIn("2 MB", out["screenshot_dropped"])
        out = normalise_view({"title": "t", "screenshot_b64": "data:image/png;base64,not base64 !!"})
        self.assertNotIn("screenshot_b64", out)
        self.assertIn("base64", out["screenshot_dropped"])
        out = normalise_view({"title": "t"})
        self.assertNotIn("screenshot_b64", out)
        self.assertNotIn("screenshot_dropped", out)

    def test_preview_url_scheme_filter_and_bad_view(self):
        self.assertEqual(normalise_view({"preview_url": "javascript:alert(1)"})["preview_url"], "")
        self.assertEqual(normalise_view({"preview_url": "https://hub.example/preview/p/v1/"})["preview_url"],
                         "https://hub.example/preview/p/v1/")
        with self.assertRaises(ValueError):
            normalise_view(["not", "a", "dict"])

    def test_recent_views_ring_buffer(self):
        tk = Tokkio(TokkioCfg(ui_url="", configurator_url=""), self.bus)
        for i in range(60):
            tk.show_view("r", "p", {"title": f"view {i}"})
        recent = tk.recent_views()
        self.assertEqual(len(recent), 50)
        self.assertEqual(recent[0]["title"], "view 59")  # newest first
        self.assertEqual(recent[-1]["title"], "view 10")


class RouteTests(_Base):
    def test_health_route(self):
        stub = StubTokkio()
        cfg_dir = Path(self.tmp.name) / "config"
        atomic_write_json(cfg_dir / "keys.json", {"tokkio": {"ui_url": stub.url, "configurator_url": stub.url}})
        config = Config.load(cfg_dir)
        tk = Tokkio(TokkioCfg(ui_url="", configurator_url=""), self.bus)  # stale: the route refreshes from config
        tk.show_view("r1", "p1", {"title": "first"})
        app = SimpleNamespace(config=config, tokkio=tk, missing={}, bus=self.bus)
        router = Router()
        register_routes(router, app)
        server = KyoraServer(app, "127.0.0.1", 0, router)
        server.start()
        try:
            with urllib.request.urlopen(server.url + "/api/tokkio/health", timeout=10) as resp:
                plain = json.loads(resp.read().decode("utf-8"))
            with urllib.request.urlopen(server.url + "/api/tokkio/health?views=1", timeout=10) as resp:
                with_views = json.loads(resp.read().decode("utf-8"))
            app.tokkio = None
            app.missing = {"tokkio": "ImportError: boom"}
            with urllib.request.urlopen(server.url + "/api/tokkio/health", timeout=10) as resp:
                missing = json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            stub.close()
        self.assertTrue(plain["configured"])
        self.assertTrue(plain["ui"]["ok"])
        self.assertTrue(plain["configurator"]["openapi_found"])
        self.assertNotIn("recent_views", plain)
        self.assertEqual([v["title"] for v in with_views["recent_views"]], ["first"])
        self.assertFalse(missing["configured"])
        self.assertIn("boom", missing["error"])


class ActionsRouteTests(unittest.TestCase):
    """``POST /api/actions`` answers the Tokkio overlay with absolute URLs it can show."""

    def setUp(self):
        from kyora.pipeline.routes import register_routes as register_pipeline_routes
        runs = {"r_live": SimpleNamespace(id="r_live", project_id="p_1", result_slide=None, iteration=1,
                                          extra={"runner_url": "/runner/p_1/run-r_live?auto=1&post=1"})}
        store = SimpleNamespace(get_run=runs.get, get=lambda pid: SimpleNamespace(current_slide=3))
        self.published = []
        bus = SimpleNamespace(publish=lambda *a, **k: self.published.append(a))
        self.app = SimpleNamespace(store=store, bus=bus, missing={}, server_url="http://127.0.0.1:8765",
                                   preview_url="http://127.0.0.1:8766", preview_port=8766)
        self.router = Router()
        register_pipeline_routes(self.router, self.app)

    def post(self, body: dict, host: str = "192.168.1.20:8765") -> dict:
        from kyora.server import Request
        route, params, _ = self.router.match("POST", "/api/actions")
        req = Request("POST", "/api/actions", {}, {"host": host, "content-type": "application/json"},
                      json.dumps(body).encode(), "192.168.1.50")
        resp = route.handler(req, params)
        self.assertEqual(resp.status, 200)
        return json.loads(resp.body)

    def test_urls_are_absolute_on_the_host_the_caller_used(self):
        self.assertEqual(self.post({"action": "open_preview", "project_id": "p_1"})["url"],
                         "http://192.168.1.20:8766/preview/p_1/v3/index.html")  # game files: the preview origin
        self.assertEqual(self.post({"action": "kyora:open_preview", "run_id": "r_live"})["url"],
                         "http://192.168.1.20:8766/preview/p_1/run-r_live/index.html")
        self.assertEqual(self.post({"action": "download", "project_id": "p_1"})["url"],
                         "http://192.168.1.20:8765/api/projects/p_1/slides/3/download")  # the API: the main origin
        self.assertEqual(self.post({"action": "run_tests", "run_id": "r_live"})["url"],
                         "http://192.168.1.20:8766/runner/p_1/run-r_live?auto=1&post=1")
        self.assertEqual(self.post({"action": "run_tests", "project_id": "p_1"})["url"],
                         "http://192.168.1.20:8766/runner/p_1/v3?auto=1&post=1")
        self.assertEqual(self.post({"action": "open_preview", "project_id": "p_1"}, host="[fe80::1]:8765")["url"],
                         "http://[fe80::1]:8766/preview/p_1/v3/index.html")
        # no Host header: Kyora's own URLs
        from kyora.server import Request
        route, params, _ = self.router.match("POST", "/api/actions")
        resp = route.handler(Request("POST", "/api/actions", {}, {"content-type": "application/json"},
                                     b'{"action": "download", "project_id": "p_1"}', "127.0.0.1"), params)
        self.assertEqual(json.loads(resp.body)["url"], "http://127.0.0.1:8765/api/projects/p_1/slides/3/download")


if __name__ == "__main__":
    unittest.main()
