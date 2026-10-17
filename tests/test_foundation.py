"""Tests for the foundation modules: config, event bus, server (router, multipart, SSE, static)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from kyora.config import Config, DEFAULT_SETTINGS
from kyora.pipeline.events import DeltaCoalescer, EventBus
from kyora.server import KyoraServer, Request, Response, Router, event_stream, parse_multipart
from kyora.util.sse import encode_event, parse_sse
from kyora.util.fs import safe_join, atomic_write_json, read_json
from kyora.util.ids import new_id, slug


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        atomic_write_json(self.dir / "keys.json", {"nvidia": {"nemotron": {"model": "m1", "api_key": "nvapi-secret-key-1234"},
                                                              "kimi": {"model": "m2", "api_key": "k"}}})
        atomic_write_json(self.dir / "settings.json", {"server": {"port": 9999}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_merges_defaults_and_masks(self):
        cfg = Config.load(self.dir)
        self.assertEqual(cfg.server.port, 9999)
        self.assertEqual(cfg.nvidia.base_url, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(cfg.claude_code.effort, "max")
        self.assertEqual(cfg.quality.min_pass_rate, 0.94)
        pub = cfg.to_public()
        self.assertNotIn("secret-key", pub["keys"]["nvidia"]["nemotron"]["api_key"])
        self.assertIn("…", pub["keys"]["nvidia"]["nemotron"]["api_key"])
        self.assertEqual(cfg.to_public(reveal=True)["keys"]["nvidia"]["nemotron"]["api_key"], "nvapi-secret-key-1234")

    def test_patch_validation_and_mask_roundtrip(self):
        cfg = Config.load(self.dir)
        masked = cfg.to_public()["keys"]["nvidia"]["nemotron"]["api_key"]
        changed = cfg.apply_patch({"keys": {"nvidia": {"nemotron": {"api_key": masked}}}, "settings": {"quality": {"min_pass_rate": 0.95}}})
        self.assertEqual(changed, ["settings.quality.min_pass_rate"])
        self.assertEqual(cfg.nvidia.nemotron.api_key, "nvapi-secret-key-1234")
        with self.assertRaises(ValueError):
            cfg.apply_patch({"settings": {"claude_code": {"effort": "ultra"}}})
        with self.assertRaises(ValueError):
            cfg.apply_patch({"settings": {"server": {"workers": 9}}})  # not patchable
        # the engine command is argv Kyora executes: never patchable over HTTP, only in settings.json
        with self.assertRaises(ValueError) as ctx:
            cfg.apply_patch({"settings": {"claude_code": {"command": ["proot-distro", "login", "ubuntu", "--", "claude"]}}})
        self.assertIn("settings.json", str(ctx.exception))
        self.assertEqual(cfg.apply_patch({"settings": {"claude_code": {"command": ["claude"], "extra_args": []}}}), [])
        cfg.save()
        again = Config.load(self.dir)
        self.assertEqual(again.claude_code.command, ["claude"])
        self.assertEqual(again.quality.min_pass_rate, 0.95)
        settings = read_json(self.dir / "settings.json")
        settings["claude_code"]["command"] = ["proot-distro", "login", "ubuntu", "--", "claude"]
        atomic_write_json(self.dir / "settings.json", settings)
        self.assertEqual(Config.load(self.dir).claude_code.command[0], "proot-distro")

    def test_env_overrides(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        try:
            cfg = Config.load(self.dir)
            self.assertEqual(cfg.anthropic_api_key, "sk-ant-test")
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


class EventBusTests(unittest.TestCase):
    def test_publish_replay_subscribe(self):
        bus = EventBus(None)
        sub = bus.subscribe(run_id="r1", types=["stage.*", "run.state"])
        bus.publish("run.state", {"state": "intake"}, run_id="r1")
        bus.publish("stage.progress", {"pct": 10}, run_id="r1")
        bus.publish("stage.progress", {"pct": 10}, run_id="r2")  # filtered out
        bus.publish("model.delta", {"content": "x"}, run_id="r1", persist=False)  # not matched by types
        got = [sub.queue.get(timeout=1).type for _ in range(2)]
        self.assertEqual(got, ["run.state", "stage.progress"])
        self.assertTrue(sub.queue.empty())
        evs = bus.replay(0, run_id="r1")
        self.assertEqual([e.type for e in evs], ["run.state", "stage.progress"])
        self.assertEqual(bus.replay(evs[0].id, run_id="r1")[0].type, "stage.progress")
        self.assertEqual(bus.last_id(), 3)
        sub.close()
        bus.close()

    def test_wait_for_and_coalescer(self):
        bus = EventBus(None)
        threading.Timer(0.1, lambda: bus.publish("gate.result", {"ok": True}, run_id="r9")).start()
        ev = bus.wait_for("gate.result", lambda e: e.data.get("ok"), timeout=3, run_id="r9")
        self.assertIsNotNone(ev)
        sub = bus.subscribe(types=["model.delta"])
        co = DeltaCoalescer(bus, run_id="r9", project_id=None, role="kimi", model="m", interval=10)
        co.add("content", "a"); co.add("content", "b"); co.add("reasoning", "think")
        co.flush()
        items = []
        while not sub.queue.empty():
            items.append(sub.queue.get())
        joined = "".join(i.data.get("content", "") for i in items)
        self.assertEqual(joined, "ab")
        self.assertEqual("".join(i.data.get("reasoning", "") for i in items), "think")
        bus.close()


class MultipartAndSSETests(unittest.TestCase):
    def test_parse_multipart_binary(self):
        boundary = "----KyoraBoundary123"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"role\"\r\n\r\nreference\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\nContent-Type: image/png\r\n\r\n").encode() \
            + b"\x89PNG\r\n\x1a\n\x00\x01\r\n--notaboundary\r\n" + f"\r\n--{boundary}--\r\n".encode()
        parts = parse_multipart(body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(parts[0].name, "role")
        self.assertEqual(parts[0].text(), "reference")
        self.assertEqual(parts[1].filename, "a.png")
        self.assertEqual(parts[1].content_type, "image/png")
        self.assertEqual(parts[1].data, b"\x89PNG\r\n\x1a\n\x00\x01\r\n--notaboundary\r\n")

    def test_sse_roundtrip(self):
        raw = encode_event({"a": "b\nc"}, event="x", event_id=7) + b": keepalive\n\n" + encode_event("plain")
        msgs = list(parse_sse(raw.split(b"\n")))
        self.assertEqual(msgs[0]["event"], "x")
        self.assertEqual(msgs[0]["id"], "7")
        self.assertEqual(json.loads(msgs[0]["data"]), {"a": "b\nc"})
        self.assertEqual(msgs[1]["data"], "plain")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus(None)

        class FakeApp:
            class config:
                class hub:
                    token = "tok123"
                    expose_ui = False
        self.app = FakeApp()
        self.router = Router()
        self.router.add("GET", "/api/ping", lambda req, p: Response.json({"pong": True, "q": req.query}))
        self.router.add("POST", "/api/echo/{name}", lambda req, p: Response.json({"name": p["name"], "body": req.json()}))
        self.router.add("GET", "/files/{path:path}", lambda req, p: Response.text(p["path"]), auth="any")
        self.router.add("GET", "/hubonly", lambda req, p: Response.text("ok"), auth="hub")
        self.router.add("POST", "/loop", lambda req, p: Response.text("ok"), auth="loopback")
        self.router.add("GET", "/api/events", lambda req, p: Response.sse(
            event_stream(self.bus, run_id=req.query.get("run"), project_id=None, types=None,
                         since=int(req.query.get("since") or 0), keepalive_s=0.2)))
        self.router.add("GET", "/boom", lambda req, p: 1 / 0)
        self.srv = KyoraServer(self.app, "127.0.0.1", 0, self.router)
        self.srv.start()

    def tearDown(self):
        self.srv.shutdown()
        self.bus.close()

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.srv.url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_routing_and_errors(self):
        status, body = self._get("/api/ping?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["q"], {"x": "1"})
        req = urllib.request.Request(self.srv.url + "/api/echo/abc", data=b'{"k": 2}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"name": "abc", "body": {"k": 2}})
        self.assertEqual(self._get("/files/a/b/c.js")[1], b"a/b/c.js")
        self.assertEqual(self._get("/nope")[0], 404)
        self.assertEqual(self._get("/boom")[0], 500)
        self.assertEqual(self._get("/hubonly")[0], 401)
        self.assertEqual(self._get("/hubonly", {"Authorization": "Bearer tok123"})[0], 200)
        self.assertEqual(self._get("/hubonly", {"Authorization": "Bearer wrong"})[0], 401)
        req = urllib.request.Request(self.srv.url + "/api/ping", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)

    def test_sse_replay_and_live(self):
        self.bus.publish("run.state", {"state": "plan"}, run_id="r1")
        req = urllib.request.Request(self.srv.url + "/api/events?run=r1")
        resp = urllib.request.urlopen(req, timeout=5)
        threading.Timer(0.3, lambda: self.bus.publish("stage.progress", {"pct": 50}, run_id="r1")).start()
        seen = []
        start = time.time()
        while time.time() - start < 5 and len(seen) < 2:
            line = resp.readline()
            if line.startswith(b"data:"):
                seen.append(json.loads(line[5:].strip()))
        resp.close()
        self.assertEqual([s["type"] for s in seen], ["run.state", "stage.progress"])


class UtilTests(unittest.TestCase):
    def test_safe_join_and_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertTrue(str(safe_join(root, "a", "b.txt")).startswith(str(root.resolve())))
            with self.assertRaises(PermissionError):
                safe_join(root, "..", "x")
            atomic_write_json(root / "x.json", {"a": 1})
            self.assertEqual(read_json(root / "x.json"), {"a": 1})
        self.assertTrue(new_id("r").startswith("r_"))
        self.assertEqual(slug("Sky Runner!! v2"), "sky-runner-v2")


if __name__ == "__main__":
    unittest.main()
