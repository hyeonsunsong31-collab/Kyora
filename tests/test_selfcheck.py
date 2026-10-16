"""Tests of ``kyora.selfcheck``: report items, statuses, concurrency guards, events and routes.

The app is a ``SimpleNamespace`` with a real :class:`Config` (temp dir), a real
:class:`EventBus` and small fakes for the NIM clients, the Claude Code engine,
the hub client and Tokkio. No model endpoint is contacted.
Run: ``python3 -m unittest tests.test_selfcheck -v``
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from kyora.config import Config, TokkioCfg
from kyora.models.base import Availability
from kyora.models.tokkio import Tokkio
from kyora.pipeline.events import EventBus
from kyora.selfcheck import (
    ISH_CLAUDE, KIMI_EFFORT_HINT, KIMI_EFFORT_HINT_TOP, NO_ENGINE, NODE_MISSING, ORDER, TOKKIO_NOT_DEPLOYED, SelfCheck, _Probe,
    register_routes,
)
from kyora.server import KyoraServer, Router
from kyora.util.fs import atomic_write_json


class FakeNIM:
    """``probe(model, timeout_s)`` returns a scripted dict (or raises); optional ``chat`` for the effort check."""

    def __init__(self, result: dict | None = None, exc: Exception | None = None, chat_exc: Exception | None = None,
                 chat_reasoning: str = ""):
        self.result = result
        self.exc = exc
        self.chat_exc = chat_exc
        self.chat_reasoning = chat_reasoning
        self.probes: list[str] = []
        self.chats: list[dict] = []

    def probe(self, model, timeout_s=30.0):
        self.probes.append(model)
        if self.exc:
            raise self.exc
        base = {"reachable": True, "model_listed": True, "latency_ms": 412.5, "error": None, "status": None,
                "chat_ok": True, "reasoning_seen": False, "model": model}
        base.update(self.result or {})
        return base

    def chat(self, model, messages, **kw):
        self.chats.append({"model": model, **kw})
        if self.chat_exc:
            raise self.chat_exc
        return SimpleNamespace(reasoning=self.chat_reasoning, content="")


class FakeStatusError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class FakeKimi:
    effort_field = "top_level"

    def effort_extra(self, effort):
        return {"reasoning_effort": effort or "max"}


class FakeClaude:
    name = "claude_code"

    def __init__(self, available=True, logged_in=True, probe=None):
        self.available = available
        self.logged_in = logged_in
        self.probe = probe or {"ok": True, "requested": "claude-fable-5-1", "active": "claude-fable-5-1",
                               "fallback_used": False, "duration_ms": 2100, "refused": [], "error": None}
        self.availability_calls = 0
        self.probe_calls: list[tuple] = []

    def availability(self):
        self.availability_calls += 1
        details = {"version": "2.1.300 (Claude Code)", "path": "/usr/local/bin/claude", "logged_in": self.logged_in,
                   "auth_method": "claude.ai", "model": "claude-fable-5-1"}
        reason = "" if self.available else "Claude Code is not logged in: run 'claude auth login' (subscription)"
        return Availability(self.available, self.name, reason, details)

    def probe_model(self, model, fallbacks=None, timeout_s=120):
        self.probe_calls.append((model, list(fallbacks or [])))
        return dict(self.probe)


class FakeHub:
    name = "hub"

    def availability(self):
        return Availability(True, "hub", "", {"url": "http://hub.local:8765", "engine": "claude_code"})


class FakeTokkio:
    def __init__(self):
        self.updated = []

    def update_config(self, cfg):
        self.updated.append(cfg)

    def configured(self):
        return True

    def health(self):
        return {"configured": True,
                "ui": {"configured": True, "url": "https://tokkio.local", "reachable": True, "ok": True, "status": 200,
                       "latency_ms": 35.2, "error": None},
                "configurator": {"configured": True, "url": "http://tokkio.local:30180", "reachable": True,
                                 "openapi_found": True, "status": 200, "latency_ms": 20.1, "error": None, "paths": 12},
                "docs": "docs/tokkio-overlay.md"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.root / "home")
        self.home = self.root / "home"
        self.home.mkdir()
        self.cfg_dir = self.root / "config"
        atomic_write_json(self.cfg_dir / "keys.json", {
            "nvidia": {"nemotron": {"model": "nvidia/nemotron-3-ultra-550b-a55b", "api_key": "nvapi-test-1"},
                       "kimi": {"model": "moonshotai/kimi-k3", "api_key": "nvapi-test-2"}},
            "anthropic": {"api_key": ""},
            "hub": {"url": "http://hub.local:8765", "token": "hubtoken"},
            "tokkio": {"ui_url": "https://tokkio.local", "configurator_url": "http://tokkio.local:30180"},
        })
        self.buses: list[EventBus] = []

    def tearDown(self):
        for bus in self.buses:
            bus.close()
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self.tmp.cleanup()

    def make_app(self, **over) -> SimpleNamespace:
        bus = EventBus(None)
        self.buses.append(bus)
        engine = SimpleNamespace(name="claude_code",
                                 availability=lambda: Availability(True, "claude_code", "", {"model": "claude-fable-5-1"}))
        app = SimpleNamespace(
            config=Config.load(self.cfg_dir), bus=bus, home=self.home, missing={}, server_url="http://127.0.0.1:8765",
            platform={"kind": "linux", "system": "linux", "machine": "x86_64", "python": "3.13.0", "is_ish": False},
            nim_nemotron=FakeNIM(), nim_kimi=FakeNIM(result={"reasoning_seen": True}), kimi=None,
            claude_engine=FakeClaude(), anthropic_engine=None, hub_client=FakeHub(), tokkio=FakeTokkio(),
            engine=lambda: engine,
        )
        for k, v in over.items():
            setattr(app, k, v)
        return app

    @staticmethod
    def by_id(report: dict) -> dict:
        return {it["id"]: it for it in report["items"]}


class ReportTests(_Base):
    def test_all_ok(self):
        app = self.make_app()
        report = SelfCheck(app).run(probe_models=True, probe_claude=True)
        items = self.by_id(report)
        self.assertEqual([it["id"] for it in report["items"]], list(ORDER))
        for it in report["items"]:
            self.assertEqual(set(it), {"id", "label", "status", "detail", "measured"})
            self.assertIn(it["status"], ("ok", "warn", "fail", "skip"))
        for i in ("python", "platform", "claude_cli", "claude_auth", "claude_model", "nemotron", "kimi",
                  "engine_selected", "hub", "tokkio_ui", "tokkio_configurator"):
            self.assertEqual(items[i]["status"], "ok", f"{i}: {items[i]['detail']}")
        self.assertEqual(items["anthropic_api"]["status"], "skip")
        self.assertEqual(report["summary"]["fail"], 0, [it for it in report["items"] if it["status"] == "fail"])
        self.assertEqual(sum(report["summary"].values()), len(ORDER))
        self.assertEqual(items["nemotron"]["measured"]["latency_ms"], 412.5)
        self.assertIn("412.5 ms", items["nemotron"]["detail"])
        self.assertTrue(items["kimi"]["measured"]["reasoning_seen"])
        self.assertIn("reasoning_content seen", items["kimi"]["detail"])
        self.assertIn("2.1.300", items["claude_cli"]["detail"])
        self.assertIn("claude.ai", items["claude_auth"]["detail"])
        self.assertIn("active model claude-fable-5-1", items["claude_model"]["detail"])
        self.assertIn("claude_code", items["engine_selected"]["detail"])
        self.assertEqual(report["platform"]["kind"], "linux")
        self.assertIsInstance(report["lan_urls"], list)
        self.assertTrue(report["generated_at"].endswith("Z"))
        self.assertIs(SelfCheck(app).last(), None)
        # the FakeTokkio was refreshed with the configured URLs
        self.assertEqual(app.tokkio.updated[-1], app.config.tokkio)

    def test_last_caches_report(self):
        sc = SelfCheck(self.make_app())
        self.assertIsNone(sc.last())
        report = sc.run(probe_models=False)
        self.assertIs(sc.last(), report)
        items = self.by_id(report)
        self.assertEqual(items["nemotron"]["status"], "skip")
        self.assertEqual(items["kimi"]["status"], "skip")
        self.assertEqual(items["claude_model"]["status"], "skip")
        self.assertEqual(items["claude_model"]["detail"], "run with probe_claude to resolve the active model")

    def test_nemotron_401_detail(self):
        err = ('HTTP 401 from integrate.api.nvidia.com for nvidia/nemotron-3-ultra-550b-a55b: '
               '{"status":401,"title":"Unauthorized","detail":"Authentication failed"}')
        app = self.make_app(nim_nemotron=FakeNIM(result={"chat_ok": False, "status": 401, "error": err,
                                                         "latency_ms": 120.0, "reasoning_seen": None}))
        it = self.by_id(SelfCheck(app).run(probe_models=True))["nemotron"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("HTTP 401", it["detail"])
        self.assertIn("Unauthorized", it["detail"])
        self.assertIn("API key", it["detail"])
        self.assertEqual(it["measured"]["status"], 401)

    def test_nemotron_unreachable_and_unlisted(self):
        app = self.make_app(
            nim_nemotron=FakeNIM(result={"reachable": False, "chat_ok": False,
                                         "error": "integrate.api.nvidia.com unreachable: [Errno -2] Name or service not known"}),
            nim_kimi=FakeNIM(result={"model_listed": False}))
        items = self.by_id(SelfCheck(app).run(probe_models=True))
        self.assertEqual(items["nemotron"]["status"], "fail")
        self.assertIn("Name or service not known", items["nemotron"]["detail"])
        self.assertEqual(items["kimi"]["status"], "warn")
        self.assertIn("not listed", items["kimi"]["detail"])

    def test_kimi_reasoning_effort_hint_from_probe_error(self):
        err = 'HTTP 400 from integrate.api.nvidia.com for moonshotai/kimi-k3: {"detail":"Unknown parameter: reasoning_effort"}'
        app = self.make_app(nim_kimi=FakeNIM(result={"chat_ok": False, "status": 400, "error": err}))
        it = self.by_id(SelfCheck(app).run(probe_models=True))["kimi"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("HTTP 400", it["detail"])
        self.assertIn(KIMI_EFFORT_HINT, it["detail"])
        self.assertEqual(it["detail"].count(KIMI_EFFORT_HINT), 1)

    def test_kimi_effort_check_with_configured_field(self):
        rejected = FakeStatusError('HTTP 400 from integrate.api.nvidia.com for moonshotai/kimi-k3: '
                                   '{"detail":"extra field reasoning_effort not permitted"}', 400)
        nim = FakeNIM(chat_exc=rejected)
        it = self.by_id(SelfCheck(self.make_app(nim_kimi=nim, kimi=FakeKimi())).run(probe_models=True))["kimi"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("set kimi.effort_field to extra_body in Settings", it["detail"])
        self.assertEqual(it["measured"]["effort_check"], "failed")
        self.assertEqual(nim.chats[0]["extra"], {"reasoning_effort": "max"})
        self.assertEqual(nim.chats[0]["max_tokens"], 1)
        ok_nim = FakeNIM(chat_reasoning="thinking...")
        it = self.by_id(SelfCheck(self.make_app(nim_kimi=ok_nim, kimi=FakeKimi())).run(probe_models=True))["kimi"]
        self.assertEqual(it["status"], "ok")
        self.assertEqual(it["measured"]["effort_check"], "ok")
        self.assertTrue(it["measured"]["reasoning_seen"])
        self.assertEqual(it["measured"]["effort_sent"], "reasoning_effort=max")
        self.assertIn("request with reasoning_effort=max accepted (top_level)", it["detail"])

    def test_kimi_effort_check_reports_extra_body_field_actually_sent(self):
        # The real Kimi wrapper builds the body; self-check must name what it put on the wire.
        from kyora.config import KimiCfg
        from kyora.models.kimi import Kimi
        kimi = Kimi(object(), "moonshotai/kimi-k3", KimiCfg(reasoning_effort="high", judge_reasoning_effort="low",
                                                         effort_field="extra_body"), None)
        nim = FakeNIM()
        it = self.by_id(SelfCheck(self.make_app(nim_kimi=nim, kimi=kimi)).run(probe_models=True))["kimi"]
        self.assertEqual(nim.chats[0]["extra"], {"chat_template_kwargs": {"reasoning_effort": "high"}})
        self.assertNotIn("extra_body", nim.chats[0]["extra"])
        self.assertEqual(it["measured"]["effort_sent"], "chat_template_kwargs.reasoning_effort=high")
        self.assertIn("request with chat_template_kwargs.reasoning_effort=high accepted (extra_body)", it["detail"])
        # When that form is rejected too, the hint must not point back at the mode that just failed.
        rejected = FakeStatusError('HTTP 400: {"detail":"chat_template_kwargs.reasoning_effort not permitted"}', 400)
        it = self.by_id(SelfCheck(self.make_app(nim_kimi=FakeNIM(chat_exc=rejected), kimi=kimi))
                        .run(probe_models=True))["kimi"]
        self.assertEqual(it["status"], "warn")
        self.assertIn(KIMI_EFFORT_HINT_TOP, it["detail"])
        self.assertNotIn(KIMI_EFFORT_HINT, it["detail"])

    def test_no_engine_fail_message(self):
        app = self.make_app(engine=lambda: None)
        it = self.by_id(SelfCheck(app).run(probe_models=False))["engine_selected"]
        self.assertEqual(it["status"], "fail")
        self.assertEqual(it["detail"], NO_ENGINE)
        self.assertIn("claude login", it["detail"])
        self.assertIn("hub.url", it["detail"])
        self.assertIn("ANTHROPIC_API_KEY + pip install anthropic", it["detail"])

    def test_ish_skips_claude(self):
        claude = FakeClaude()
        app = self.make_app(platform={"kind": "ish", "system": "linux", "machine": "i686", "is_ish": True},
                            claude_engine=claude)
        items = self.by_id(SelfCheck(app).run(probe_models=False, probe_claude=True))
        self.assertEqual(items["claude_cli"]["status"], "skip")
        self.assertEqual(items["claude_cli"]["detail"], ISH_CLAUDE)
        self.assertEqual(items["claude_auth"]["status"], "skip")
        self.assertEqual(items["claude_model"]["status"], "skip")
        self.assertEqual(claude.probe_calls, [])
        self.assertIn("ish", items["platform"]["detail"])

    def test_claude_unavailable_and_logged_out(self):
        missing_cli = SimpleNamespace(availability=lambda: Availability(
            False, "claude_code", "'claude' not found on PATH (install Claude Code and run 'claude auth login')",
            {"command": ["claude"]}))
        items = self.by_id(SelfCheck(self.make_app(claude_engine=missing_cli)).run(probe_models=False))
        self.assertEqual(items["claude_cli"]["status"], "fail")
        self.assertIn("not found on PATH", items["claude_cli"]["detail"])
        self.assertEqual(items["claude_auth"]["status"], "skip")
        logged_out = FakeClaude(available=False, logged_in=False)
        items = self.by_id(SelfCheck(self.make_app(claude_engine=logged_out)).run(probe_models=False, probe_claude=True))
        self.assertEqual(items["claude_cli"]["status"], "ok")
        self.assertEqual(items["claude_auth"]["status"], "fail")
        self.assertIn("not logged in", items["claude_auth"]["detail"])
        self.assertEqual(items["claude_model"]["status"], "skip")
        self.assertEqual(logged_out.probe_calls, [])

    def test_probe_claude_fallback_note(self):
        claude = FakeClaude(probe={"ok": True, "requested": "claude-fable-5-1", "active": "claude-opus-5-5",
                                   "fallback_used": True, "duration_ms": 3200, "error": None,
                                   "refused": [{"model": "claude-fable-5-1", "reason": "out of usage credits"}]})
        app = self.make_app(claude_engine=claude)
        it = self.by_id(SelfCheck(app).run(probe_models=False, probe_claude=True))["claude_model"]
        self.assertEqual(it["status"], "ok")
        self.assertIn("active model claude-opus-5-5", it["detail"])
        self.assertIn("fallback from claude-fable-5-1", it["detail"])
        self.assertTrue(it["measured"]["fallback_used"])
        self.assertEqual(claude.probe_calls, [("claude-fable-5-1", ["claude-opus-5-5", "claude-sonnet-5-5"])])

    def test_probe_claude_failure(self):
        claude = FakeClaude(probe={"ok": False, "requested": "claude-fable-5-1", "active": None, "fallback_used": False,
                                   "error": "probe timed out after 150 s", "refused": []})
        it = self.by_id(SelfCheck(self.make_app(claude_engine=claude)).run(probe_models=False, probe_claude=True))["claude_model"]
        self.assertEqual(it["status"], "fail")
        self.assertIn("timed out", it["detail"])

    def test_one_probe_raising_does_not_abort(self):
        app = self.make_app(nim_nemotron=FakeNIM(exc=RuntimeError("boom")), hub_client=SimpleNamespace(
            availability=lambda: (_ for _ in ()).throw(OSError("hub exploded"))))
        report = SelfCheck(app).run(probe_models=True)
        items = self.by_id(report)
        self.assertEqual(len(report["items"]), len(ORDER))
        self.assertEqual(items["nemotron"]["status"], "fail")
        self.assertIn("RuntimeError: boom", items["nemotron"]["detail"])
        self.assertEqual(items["hub"]["status"], "fail")
        self.assertIn("hub exploded", items["hub"]["detail"])
        self.assertEqual(items["kimi"]["status"], "ok")
        self.assertEqual(items["python"]["status"], "ok")
        self.assertEqual(report["overall"], "fail")

    def test_per_item_timeout(self):
        sc = SelfCheck(self.make_app())

        def slow():
            time.sleep(1.5)
            return {"id": "nemotron", "label": "x", "status": "ok", "detail": "late", "measured": {}}

        t0 = time.monotonic()
        out = sc._execute([_Probe(("nemotron",), slow, 0.2), _Probe(("python",), sc._p_python, 5.0)])
        self.assertLess(time.monotonic() - t0, 1.2)
        self.assertEqual(out["nemotron"]["status"], "fail")
        self.assertIn("timed out", out["nemotron"]["detail"])
        self.assertEqual(out["python"]["status"], "ok")

    def test_tokkio_not_deployed_and_no_hub(self):
        atomic_write_json(self.cfg_dir / "keys.json", {"hub": {"url": ""}, "tokkio": {"ui_url": ""}})
        bus = EventBus(None)
        self.buses.append(bus)
        app = self.make_app(tokkio=Tokkio(TokkioCfg(ui_url="", configurator_url=""), bus))
        items = self.by_id(SelfCheck(app).run(probe_models=False))
        self.assertEqual(items["tokkio_ui"]["status"], "skip")
        self.assertEqual(items["tokkio_ui"]["detail"], TOKKIO_NOT_DEPLOYED)
        self.assertEqual(items["tokkio_configurator"]["status"], "skip")
        self.assertEqual(items["hub"]["status"], "skip")

    def test_anthropic_key_without_package(self):
        atomic_write_json(self.cfg_dir / "keys.json", {"anthropic": {"api_key": "sk-ant-test"}})
        import importlib.util
        from unittest import mock
        with mock.patch("importlib.util.find_spec", return_value=None):
            it = self.by_id(SelfCheck(self.make_app()).run(probe_models=False))["anthropic_api"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("pip install anthropic", it["detail"])
        self.assertTrue(importlib.util.find_spec("json") is not None)

    def test_node_missing_warns(self):
        from unittest import mock
        real_which = __import__("kyora.util.fs", fromlist=["which"]).which
        with mock.patch("kyora.selfcheck.which", side_effect=lambda c: None if c == "node" else real_which(c)):
            it = self.by_id(SelfCheck(self.make_app()).run(probe_models=False))["node"]
        self.assertEqual(it["status"], "warn")
        self.assertEqual(it["detail"], NODE_MISSING)

    def test_selfcheck_update_published(self):
        app = self.make_app()
        sub = app.bus.subscribe(types=["selfcheck.update"])
        try:
            report = SelfCheck(app).run(probe_models=False)
            ev = sub.queue.get(timeout=2)
        finally:
            sub.close()
        self.assertEqual(ev.type, "selfcheck.update")
        self.assertEqual(ev.data["summary"], report["summary"])
        self.assertEqual(ev.data["generated_at"], report["generated_at"])
        self.assertEqual(set(ev.data["statuses"]), set(ORDER))

    def test_concurrent_runs_are_serialised(self):
        sc = SelfCheck(self.make_app())
        reports: list[dict] = []
        threads = [threading.Thread(target=lambda: reports.append(sc.run(probe_models=False))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(len(reports), 2)
        self.assertIs(sc.last(), reports[-1])


class RouteTests(_Base):
    def _request(self, url: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_routes(self):
        app = self.make_app()
        app.selfcheck = SelfCheck(app)
        router = Router()
        register_routes(router, app)
        server = KyoraServer(app, "127.0.0.1", 0, router)
        server.start()
        try:
            st, quick = self._request(server.url + "/api/selfcheck")
            self.assertEqual(st, 200)
            self.assertFalse(quick["probe_models"])
            self.assertEqual(self.by_id(quick)["nemotron"]["status"], "skip")
            self.assertEqual(app.nim_nemotron.probes, [])

            st, full = self._request(server.url + "/api/selfcheck/run", "POST", {"probe_models": True, "probe_claude": False})
            self.assertEqual(st, 200)
            self.assertTrue(full["probe_models"])
            self.assertEqual(self.by_id(full)["nemotron"]["status"], "ok")
            self.assertEqual(len(app.nim_nemotron.probes), 1)

            st, last = self._request(server.url + "/api/selfcheck")
            self.assertEqual(st, 200)
            self.assertEqual(last["generated_at"], full["generated_at"])
            self.assertEqual(len(app.nim_nemotron.probes), 1, "GET returns the cached report")

            st, bad = self._request(server.url + "/api/selfcheck/run", "POST", {"probe_models": "maybe"})
            self.assertEqual(st, 400)
            self.assertIn("probe_models", bad["error"])

            app.selfcheck = None
            app.missing = {"selfcheck": "ImportError: x"}
            st, unavailable = self._request(server.url + "/api/selfcheck")
            self.assertEqual(st, 503)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
