"""Runtime engine failover, usage-limit handling, automatic resume and Claude Code session continuation.

Scripted engines stand in for Claude Code and Kyora's agent engine; the real
``run_engine``, ``Runner``, ``RunManager``, ``ProjectStore`` and slide store run, so
each test pins one decision: which failures hand a BUILD/FIX to the next engine,
what the next engine is told, when a run blocks with an automatic resume, and that
the resumed Claude Code session continues where the usage limit cut it off.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import kyora.pipeline.runner as R
import kyora.pipeline.stages as S
from kyora.config import ClaudeCodeCfg
from kyora.hub import HubClientEngine, select_local_engine
from kyora.models.base import (Availability, CodeEngine, EngineJob, EngineResult, classify_failure,
                               engine_candidates, parse_reset_text, reset_time, select_engine)
from kyora.models.claude_code import ClaudeCodeEngine, _Attempt
from kyora.pipeline.events import ENGINE_FAILOVER, RUN_AUTO_RESUME, RUN_BLOCKED, EventBus
from kyora.pipeline.quality import Check
from kyora.pipeline.stages import CONTINUE_PREFIX, StageBlocked, StageContext
from kyora.projects.store import ProjectStore

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc).timestamp()
SESSION_LIMIT = "You've hit your session limit · resets 1:20pm (UTC)"
PLAN = {"title": "Sky Runner", "acceptance_tests": [{"id": "at-01", "name": "boots", "kind": "browser"}]}


def failed(error, **raw) -> EngineResult:
    return EngineResult(ok=False, backend="x", requested_model="m", error=error, raw=raw)


def utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScriptedEngine(CodeEngine):
    """Each call pops one step: ``{ok, error, files, session, raw, model}``; every job is kept."""

    def __init__(self, name: str, steps=(), available: bool = True):
        self.name = name
        self.steps = list(steps)
        self.available = available
        self.jobs: list[EngineJob] = []

    def availability(self) -> Availability:
        return Availability(self.available, self.name, "" if self.available else "switched off")

    def run(self, job, sink, cancel) -> EngineResult:
        self.jobs.append(job)
        step = self.steps.pop(0) if self.steps else {"ok": True, "files": {"index.html": "<canvas></canvas>"}}
        raw = {k: (v() if callable(v) else v) for k, v in (step.get("raw") or {}).items()}  # e.g. a reset "in 1 s"
        for rel, text in (step.get("files") or {}).items():
            p = Path(job.workspace) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return EngineResult(ok=bool(step.get("ok")), backend=self.name, requested_model=job.model,
                            active_model=step.get("model", "model-" + self.name), session_id=step.get("session"),
                            error=step.get("error"), raw=raw)


# ============================================================================= classification
class ClassifyFailureTests(unittest.TestCase):
    TABLE = [
        (SESSION_LIMIT, "quota"),
        ("api_error (HTTP 429): " + SESSION_LIMIT, "quota"),
        ("success (HTTP 429): usage limit reached", "quota"),
        ("You're out of usage credits. Switch to another model to continue.", "quota"),
        ("api_error: credits_required", "quota"),
        ("Your credit balance is too low to access the Anthropic API", "quota"),
        ("Your subscription has ended: upgrade your plan to continue", "quota"),
        ("rate limited (HTTP 429) after SDK retries: Too Many Requests", "quota"),
        ("HTTP 402: payment required", "quota"),
        ("Invalid API key · Please run /login", "auth"),
        ("HTTP 401: invalid x-api-key - check ANTHROPIC_API_KEY", "auth"),
        ("authentication_error: OAuth token has expired", "auth"),
        ("Claude Code is not logged in: run 'claude auth login' (subscription)", "auth"),
        ("HTTP 403: Authorization failed", "auth"),
        ("permission_error: your key may not use this resource", "auth"),
        ("hub rejected the token (401): check hub.token", "auth"),
        ("cannot reach the Anthropic API: Connection error.", "unavailable"),
        ("hub unreachable: [Errno 111] Connection refused", "unavailable"),
        ("Service temporarily overloaded", "unavailable"),
        ("HTTP 503: upstream", "unavailable"),
        ("lost contact with the hub", "unavailable"),
        ("model not found (HTTP 404): claude-x", "model"),
        ("HTTP 410: moonshotai/kimi-k3 reached its end of life on 2026-10-15T08:00:00Z", "model"),
        ("HTTP 404: model z-ai/glm-9 Not found for account", "model"),
        # task failures: another engine would not do better
        ("error_max_turns", None),
        ("error_max_turns: reached max_turns=60 without end_turn", None),
        ("wall timeout after 3600 s (claude_code.timeout_s=3600)", None),
        ("idle timeout: no output for 900 s (claude_code.idle_timeout_s=900)", None),
        ("refusal:cyber", None),
        ("unparseable tool input from the API after 2 retries: x", None),
        ("malformed tool call arguments", None),
        ("Context limit reached · /compact or /clear to continue", None),
        ("denied by the judge: git push is not allowed", None),
        ("cancelled", None),
        ("some unexplained failure", None),
        ("", None),
    ]

    def test_table(self):
        for error, kind in self.TABLE:
            with self.subTest(error=error):
                self.assertEqual(classify_failure(failed(error)), kind)

    def test_ok_and_none_are_not_failures(self):
        self.assertIsNone(classify_failure(None))
        self.assertIsNone(classify_failure(EngineResult(ok=True, backend="x", requested_model="m", error="usage limit")))

    def test_the_engines_own_failure_kind_wins(self):
        self.assertEqual(classify_failure(failed("error_max_turns", failure_kind="quota")), "quota")
        self.assertEqual(classify_failure(failed("odd text", failure_kind="auth")), "auth")
        self.assertEqual(classify_failure(failed("usage limit", failure_kind="bogus")), "quota")  # unknown kind: text

    def test_api_error_fields_and_rate_limit_events(self):
        self.assertEqual(classify_failure(failed("error", api_error={"api_error_status": 401})), "auth")
        self.assertEqual(classify_failure(failed("error", api_error={"api_error_status": 529})), "unavailable")
        self.assertEqual(classify_failure(failed("error", api_error={"api_error_code": "credits_required"})), "quota")
        self.assertEqual(classify_failure(failed("error", rate_limits=[{"status": "rejected", "resetsAt": 1}])), "quota")
        self.assertIsNone(classify_failure(failed("error", rate_limits=[{"status": "allowed"}])))


class ResetTimeTests(unittest.TestCase):
    def at(self, text):
        val = parse_reset_text(text, NOW)
        return utc(val) if val is not None else None

    def test_texts(self):
        table = [
            (SESSION_LIMIT, "2026-10-18T13:20:00Z"),
            ("api_error (HTTP 429): " + SESSION_LIMIT, "2026-10-18T13:20:00Z"),
            ("5-hour limit reached ∙ resets 13:20 (UTC)", "2026-10-18T13:20:00Z"),
            ("resets 9am (UTC)", "2026-10-19T09:00:00Z"),  # already past today: the next occurrence
            ("resets 12am (UTC)", "2026-10-19T00:00:00Z"),
            ("resets 12pm (GMT)", "2026-10-18T12:00:00Z"),
            ("resets 1:20pm (UTC+2)", "2026-10-18T11:20:00Z"),
            ("resets at 2026-10-18T13:20:00Z", "2026-10-18T13:20:00Z"),
            ("limit resets_at=2026-10-18T13:20:00.123+0000", "2026-10-18T13:20:00Z"),
            ("try again in 3h 20m", "2026-10-18T13:20:00Z"),
            ("Your limit will reset in 45 minutes", "2026-10-18T10:45:00Z"),
            ("please try again in 2 hours and 5 minutes", "2026-10-18T12:05:00Z"),
            ("Retry-After: 120", "2026-10-18T10:02:00Z"),
            ("Claude AI usage limit reached|1760800000", "2025-10-18T15:06:40Z"),
            ("usage limit reached", None),
            ("resets 5 times a day", None),
            ("", None),
        ]
        for text, want in table:
            with self.subTest(text=text):
                self.assertEqual(self.at(text), want)

    def test_named_zones(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Europe/Paris")
        except Exception:  # noqa: BLE001 - no tz database on this device (Windows without tzdata, iSH)
            self.skipTest("no IANA time zone database on this device")
        self.assertEqual(self.at("resets 1pm (Europe/Paris)"), "2026-10-18T11:00:00Z")  # CEST, UTC+2
        self.assertEqual(self.at("Weekly limit reached · resets Oct 20, 10am (Europe/Berlin)"), "2026-10-20T08:00:00Z")

    def test_unknown_zone_is_treated_as_utc_and_logged(self):
        with self.assertLogs("kyora.engines", "WARNING") as logs:
            self.assertEqual(self.at("resets 1pm (Mars/Olympus_Mons)"), "2026-10-18T13:00:00Z")
        self.assertIn("treating it as UTC", logs.output[0])

    def test_no_zone_means_this_devices_local_time(self):
        local = datetime.fromtimestamp(NOW).replace(hour=13, minute=20, second=0, microsecond=0)
        want = local.timestamp() if local.timestamp() > NOW else (local + timedelta(days=1)).timestamp()
        self.assertEqual(parse_reset_text("usage limit · resets 1:20pm", NOW), want)

    def test_structured_sources_come_first(self):
        self.assertEqual(reset_time(failed(SESSION_LIMIT, resets_at=NOW + 60), NOW), NOW + 60)
        self.assertEqual(reset_time(failed(SESSION_LIMIT, resets_at=(NOW + 60) * 1000), NOW), NOW + 60)  # ms
        self.assertEqual(reset_time(failed("x", resets_at="2026-10-18T12:00:00Z"), NOW), NOW + 7200)
        limits = [{"status": "allowed", "resetsAt": int(NOW) + 100}, {"status": "rejected", "resetsAt": int(NOW) + 900},
                  {"status": "rejected", "resetsAt": int(NOW) - 900}]
        self.assertEqual(reset_time(failed(SESSION_LIMIT, rate_limits=limits), NOW), NOW + 900)
        self.assertEqual(reset_time(failed("x", rate_limits=[{"status": "allowed_warning", "resetsAt": int(NOW) + 50}]),
                                    NOW), NOW + 50)
        self.assertIsNone(reset_time(failed("x", rate_limits=[{"status": "rejected", "resetsAt": 1}]), NOW))  # past
        self.assertIsNone(reset_time(failed("usage limit reached"), NOW))
        self.assertIsNone(reset_time(None, NOW))
        # garbage never becomes a timer or a date that cannot be formatted
        self.assertIsNone(reset_time(failed("x", resets_at=1e20), NOW))
        self.assertIsNone(reset_time(failed("x", resets_at="9999-01-01T00:00:00Z"), NOW))
        self.assertIsNone(reset_time(failed("x", resets_at=True), NOW))
        self.assertIsNone(parse_reset_text("Retry-After: 999999999999", NOW))

    def test_api_error_text_is_read(self):
        res = failed("api_error (HTTP 429)", api_error={"api_error": "rate_limited", "terminal_reason": "resets 13:20 (UTC)"})
        self.assertEqual(utc(reset_time(res, NOW)), "2026-10-18T13:20:00Z")


# ============================================================================= selection
def _app(order=None, hub_url="", **engines):
    cfg = SimpleNamespace(hub=SimpleNamespace(url=hub_url))
    if order is not None:
        cfg.engines = SimpleNamespace(order=order, failover=True, auto_resume_on_limit=True)
    return SimpleNamespace(config=cfg, **engines)


class SelectEngineTests(unittest.TestCase):
    def test_follows_settings_order(self):
        claude, api, agent = ScriptedEngine("claude_code"), ScriptedEngine("anthropic_api"), ScriptedEngine("openai_agent")
        app = _app(["openai_agent", "claude_code"], claude_engine=claude, anthropic_engine=api, agent_engine=agent)
        self.assertIs(select_engine(app), agent)
        self.assertEqual(engine_candidates(app), [agent, claude])  # anthropic_api is not in the order
        agent.available = False
        self.assertIs(select_engine(app), claude)

    def test_default_order_and_hub_only_with_url(self):
        hub, claude, agent = ScriptedEngine("hub"), ScriptedEngine("claude_code"), ScriptedEngine("openai_agent")
        app = _app(None, hub_client=hub, claude_engine=claude, agent_engine=agent)
        self.assertIs(select_engine(app), claude)  # no hub.url
        app.config.hub.url = "http://192.168.1.5:8765"
        self.assertEqual(engine_candidates(app), [hub, claude, agent])
        self.assertEqual(engine_candidates(app, exclude=["hub", claude]), [agent])

    def test_nothing_available(self):
        app = _app(None, claude_engine=ScriptedEngine("claude_code", available=False))
        self.assertIsNone(select_engine(app))
        self.assertEqual(engine_candidates(app), [])

    def test_a_broken_engine_does_not_hide_the_others(self):
        broken = ScriptedEngine("claude_code")
        broken.availability = mock.Mock(side_effect=RuntimeError("boom"))
        agent = ScriptedEngine("openai_agent")
        self.assertIs(select_engine(_app(None, claude_engine=broken, agent_engine=agent)), agent)


class HubCandidateTests(unittest.TestCase):
    def test_local_order_ends_with_the_agent_engine(self):
        claude, api, agent = (ScriptedEngine("claude_code", available=False), ScriptedEngine("anthropic_api", available=False),
                              ScriptedEngine("openai_agent"))
        app = SimpleNamespace(claude_engine=claude, anthropic_engine=api, agent_engine=agent, missing={})
        eng, av, reasons = select_local_engine(app)
        self.assertIs(eng, agent)
        self.assertEqual(av.backend, "openai_agent")
        self.assertEqual(list(reasons), ["claude_code", "anthropic_api"])
        claude.available = True
        self.assertIs(select_local_engine(app)[0], claude)

    def test_a_hub_never_forwards_to_another_hub(self):
        hub = HubClientEngine(SimpleNamespace(url="http://10.0.0.9:8765", token="t", expose_ui=False), None)
        app = SimpleNamespace(claude_engine=None, anthropic_engine=None, agent_engine=hub, hub_client=hub,
                              missing={"claude_code": "not installed"})
        eng, av, reasons = select_local_engine(app)
        self.assertIsNone(eng)
        self.assertEqual(reasons["openai_agent"], "a hub client cannot run hub jobs")
        self.assertEqual(reasons["claude_code"], "not installed")


# ============================================================================= Claude Code result fields
class ClaudeCodeFailureFieldsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        cfg = ClaudeCodeCfg(command=["claude"], model="claude-opus-5-5", fallback_models=[], effort="max", max_turns=60,
                            timeout_s=3600, idle_timeout_s=900, resume_for_fixes=True, judge_tools=["Write"])
        self.eng = ClaudeCodeEngine(cfg, self.dir, SimpleNamespace())
        self.job = EngineJob(run_id="r1", workspace=self.dir, prompt="p", system_prompt="", max_turns=60,
                             model="claude-opus-5-5", fallback_models=[], effort="max")

    def result(self, res: dict | None, attempt: _Attempt | None = None, limits=None):
        state = ClaudeCodeEngine.new_parse_state("claude-opus-5-5", 60)
        state.update({"saw_init": True, "session_id": "sess-1", "result": res, "rate_limits": limits or []})
        return self.eng._build_result(self.job, state, attempt or _Attempt(), time.monotonic(), 1, [], {}, self.dir)

    def test_session_limit_sets_failure_kind_and_reset(self):
        r = self.result({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                         "terminal_reason": "api_error", "result": SESSION_LIMIT})
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "api_error (HTTP 429): " + SESSION_LIMIT)
        self.assertEqual(r.raw["failure_kind"], "quota")
        self.assertGreater(r.raw["resets_at"], time.time())
        self.assertEqual(datetime.fromtimestamp(r.raw["resets_at"], timezone.utc).strftime("%H:%M"), "13:20")

    def test_rate_limit_event_reset_is_used(self):
        at = int(time.time()) + 3600
        r = self.result({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached"},
                        limits=[{"status": "rejected", "resetsAt": at, "rateLimitType": "five_hour"}])
        self.assertEqual((r.raw["failure_kind"], r.raw["resets_at"]), ("quota", float(at)))

    def test_login_and_task_failures(self):
        r = self.result({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                         "result": "Invalid API key · Please run /login"})
        self.assertEqual(r.raw["failure_kind"], "auth")
        self.assertNotIn("resets_at", r.raw)
        r = self.result({"type": "result", "subtype": "error_max_turns", "is_error": True})
        self.assertNotIn("failure_kind", r.raw)
        cancelled = _Attempt()
        cancelled.cancelled = True
        r = self.result(None, cancelled)
        self.assertEqual(r.error, "cancelled")
        self.assertNotIn("failure_kind", r.raw)
        r = self.result({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
        self.assertTrue(r.ok)
        self.assertNotIn("failure_kind", r.raw)


# ============================================================================= run_engine failover
class _Harness(unittest.TestCase):
    """A real project store and work tree; the app holds scripted Claude Code and agent engines."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.store = ProjectStore(base / "projects", base / "kyora.db")
        self.addCleanup(self.store.close)
        self.bus = EventBus(None)
        self.addCleanup(self.bus.close)
        cc = SimpleNamespace(model="claude-opus-5-5", fallback_models=[], effort="max", max_turns=60, resume_for_fixes=True)
        cfg = SimpleNamespace(quality=SimpleNamespace(min_pass_rate=0.94, max_fix_iterations=3, browser_wait_s=1,
                                                      require_plan_approval=False, auto_run_browser_tests=False),
                              claude_code=cc, hub=SimpleNamespace(url=""), agent_engine=SimpleNamespace(model="z-ai/glm-5.3"),
                              engines=SimpleNamespace(order=["hub", "claude_code", "anthropic_api", "openai_agent"],
                                                      failover=True, auto_resume_on_limit=True))
        self.claude = ScriptedEngine("claude_code")
        self.agent = ScriptedEngine("openai_agent")
        self.app = SimpleNamespace(store=self.store, bus=self.bus, config=cfg, judge=None, browser_results=None,
                                   tokkio=None, kimi=None, nemotron=None, home=base, hub_client=None,
                                   claude_engine=self.claude, anthropic_engine=None, agent_engine=self.agent)
        self.app.engine = lambda: select_engine(self.app)

    def ctx(self, options=None, parent_game=True):
        p = self.store.create("demo")
        sm = self.store.slide_manager(p.id)
        if parent_game:
            (self.store.paths(p.id).repo / "index.html").write_text("<canvas></canvas><script>v1</script>")
            sm.commit_slide(None, "v1", {"ok": True}, {})
        run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1 if parent_game else None,
                                    options=options)
        sm.prepare_run(run)
        ctx = StageContext(app=self.app, run=run, project=p, run_dir=self.store.run_dir(run), workspace=sm.work_tree(),
                           cancel=threading.Event(), slides=sm)
        ctx.write_artifact("plan", dict(PLAN))
        return ctx

    def events(self, ctx, type_):
        return [e.data for e in self.bus.replay(0, run_id=ctx.run_id) if e.type == type_]


class FailoverTests(_Harness):
    QUOTA = {"ok": False, "error": "api_error (HTTP 429): " + SESSION_LIMIT, "session": "sess-limited",
             "raw": {"failure_kind": "quota"}}

    def test_usage_limit_fails_over_and_tells_the_next_engine_about_partial_work(self):
        self.claude.steps = [{**self.QUOTA, "files": {"src/menu.js": "// half a pause menu"}}]
        self.agent.steps = [{"ok": True, "files": {"index.html": "<canvas></canvas><script>v2</script>"}}]
        ctx = self.ctx()
        res = S.run_engine(ctx, "build", "BUILD THE GAME")
        self.assertTrue(res.ok)
        self.assertIs(ctx.engine, self.agent)
        prompt = self.agent.jobs[0].prompt
        self.assertTrue(prompt.startswith("A previous code engine (claude_code) stopped part-way (usage limit, credits "
                                          "or subscription: api_error (HTTP 429): You've hit your session limit"), prompt)
        self.assertIn("The work tree already contains its partial work: inspect it and complete the task below without "
                      "starting over.\n\nBUILD THE GAME", prompt)
        [fo] = self.events(ctx, ENGINE_FAILOVER)
        self.assertEqual({k: fo[k] for k in ("from", "to", "failure_kind", "kind", "iteration", "pinned", "tree_changed")},
                         {"from": "claude_code", "to": "openai_agent", "failure_kind": "quota", "kind": "build",
                          "iteration": 0, "pinned": False, "tree_changed": True})
        self.assertIn("session limit", fo["reason"])
        run = self.store.get_run(ctx.run_id)
        self.assertEqual([(e["engine"], e["ok"], e["failure_kind"]) for e in run.extra["engine_runs"]],
                         [("claude_code", False, "quota"), ("openai_agent", True, None)])
        self.assertEqual(run.extra["engine_runs"][0]["session_id"], "sess-limited")
        self.assertEqual(len(run.extra["engine_failovers"]), 1)
        self.assertEqual(run.extra["engine_failures"][0]["failover_to"], "openai_agent")
        self.assertEqual(run.engine["backend_name"], "openai_agent")
        starts = [e["message"] for e in self.events(ctx, "stage.progress") if "starting" in e.get("message", "")]
        self.assertEqual(starts, ["claude_code: starting (claude-opus-5-5, effort max)",
                                  "openai_agent: starting (z-ai/glm-5.3)"])  # the agent engine has no Claude effort

    def test_no_prefix_when_the_failed_engine_changed_nothing(self):
        self.claude.steps = [dict(self.QUOTA)]
        self.agent.steps = [{"ok": True, "files": {"index.html": "<canvas></canvas><b>2</b>"}}]
        ctx = self.ctx()
        S.run_engine(ctx, "build", "BUILD THE GAME")
        self.assertEqual(self.agent.jobs[0].prompt, "BUILD THE GAME")

    def test_auth_failure_fails_over(self):
        self.claude.steps = [{"ok": False, "error": "Invalid API key · Please run /login"}]
        ctx = self.ctx()
        self.assertTrue(S.run_engine(ctx, "build", "B").ok)
        self.assertEqual(self.events(ctx, ENGINE_FAILOVER)[0]["failure_kind"], "auth")
        self.assertEqual(len(self.agent.jobs), 1)

    def test_task_failure_never_fails_over(self):
        self.claude.steps = [{"ok": False, "error": "error_max_turns"}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertIn("changed no file", cm.exception.reason)
        self.assertIsNone(cm.exception.resume_at)
        self.assertEqual(self.agent.jobs, [])
        self.assertEqual(self.events(ctx, ENGINE_FAILOVER), [])

    def test_a_pinned_engine_still_fails_over_and_the_event_says_so(self):
        self.claude.steps = [dict(self.QUOTA)]
        ctx = self.ctx(options={"engine_pref": "claude_code"})
        S.run_engine(ctx, "build", "B")
        [fo] = self.events(ctx, ENGINE_FAILOVER)
        self.assertTrue(fo["pinned"])
        msgs = [e["message"] for e in self.events(ctx, "stage.progress")]
        self.assertTrue(any(m.startswith("the pinned engine claude_code stopped") for m in msgs), msgs)

    def test_failover_off_keeps_the_old_rules(self):
        self.app.config.engines.failover = False
        self.app.config.engines.auto_resume_on_limit = False
        self.claude.steps = [dict(self.QUOTA)]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertIn("changed no file in the work tree: api_error (HTTP 429)", cm.exception.reason)
        self.assertIsNone(cm.exception.resume_at)
        self.assertEqual(self.agent.jobs, [])

    def test_a_failed_failover_names_every_engine_tried(self):
        self.claude.steps = [{"ok": False, "error": "usage limit reached"}]
        self.agent.steps = [{"ok": False, "error": "HTTP 403: Authorization failed"}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertIsNone(cm.exception.resume_at)  # no reset time is known
        self.assertIn("code engine (openai_agent) failed and changed no file", cm.exception.reason)
        self.assertIn("engines tried: claude_code: usage limit", cm.exception.reason)
        self.assertIn("openai_agent: login or API key refused", cm.exception.reason)
        self.assertIs(ctx.engine, self.agent)

    def test_partial_work_with_index_html_is_measured_when_no_reset_is_known(self):
        self.agent.available = False
        self.claude.steps = [{"ok": False, "error": "usage limit reached", "files": {"index.html": "<b>half</b>"}}]
        ctx = self.ctx()
        res = S.run_engine(ctx, "build", "B")  # the existing rule: changed files are measured by TEST/GATE
        self.assertFalse(res.ok)
        self.assertEqual(self.events(ctx, ENGINE_FAILOVER), [])


class LimitBlockTests(_Harness):
    def quota(self, at, **extra):
        return {"ok": False, "error": "api_error (HTTP 429): " + SESSION_LIMIT, "session": "sess-limited",
                "raw": {"failure_kind": "quota", "resets_at": at}, **extra}

    def test_no_engine_left_blocks_with_the_reset_time(self):
        self.agent.available = False
        at = time.time() + 3600
        self.claude.steps = [self.quota(at, files={"src/half.js": "// partial"})]
        ctx = self.ctx(parent_game=False)
        tip = ctx.run.options.get("tip")
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertEqual(cm.exception.resume_at, at)
        self.assertEqual(cm.exception.stage, "build")
        self.assertIn(f"The limit resets at {utc(at)}", cm.exception.reason)
        self.assertIn("no other code engine is available", cm.exception.reason)
        self.assertIn("the partial work is kept and continued", cm.exception.reason)
        run = self.store.get_run(ctx.run_id)
        self.assertEqual(run.extra["auto_resume_at"], at)
        # the partial work is committed, so the resumed run (which restores its last commit) still has it
        self.assertNotEqual(run.options["tip"], tip)
        self.assertIn("src/half.js", ctx.slides.backend.ls(run.options["tip"]))

    def test_every_engine_limited_uses_the_soonest_reset(self):
        soon, later = time.time() + 600, time.time() + 7200
        self.claude.steps = [self.quota(later)]
        self.agent.steps = [{"ok": False, "error": "rate limited (HTTP 429)", "raw": {"failure_kind": "quota",
                                                                                    "resets_at": soon}}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertEqual(cm.exception.resume_at, soon)
        self.assertIn("code engine (openai_agent) hit a usage limit", cm.exception.reason)
        self.assertIn("the engines tried: claude_code:", cm.exception.reason)

    def test_a_reset_in_the_past_resumes_now_and_auto_resume_off_blocks_plainly(self):
        self.agent.available = False
        self.claude.steps = [self.quota(time.time() - 10, files={"index.html": "<b>half</b>"})]
        ctx = self.ctx()
        before = time.time()
        with self.assertRaises(StageBlocked) as cm:  # not measured as the result: the limit should be over
            S.run_engine(ctx, "build", "B")
        self.assertGreaterEqual(cm.exception.resume_at, before)
        self.assertLessEqual(cm.exception.resume_at, time.time())
        self.app.config.engines.auto_resume_on_limit = False
        self.claude.steps = [self.quota(time.time() + 600)]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked) as cm:
            S.run_engine(ctx, "build", "B")
        self.assertIsNone(cm.exception.resume_at)
        self.assertNotIn("auto_resume_at", self.store.get_run(ctx.run_id).extra)


class SessionContinuationTests(_Harness):
    def test_claude_code_continues_its_limited_session(self):
        self.agent.available = False
        self.claude.steps = [{"ok": False, "error": SESSION_LIMIT, "session": "sess-limited",
                              "raw": {"failure_kind": "quota", "resets_at": time.time() + 60}},
                             {"ok": True, "session": "sess-limited", "files": {"index.html": "<canvas>v2</canvas>"}}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked):
            S.run_engine(ctx, "build", "BUILD")
        self.assertIsNone(self.claude.jobs[0].resume_session_id)
        # the run resumes: a new context (fresh process state), same kind and iteration
        ctx2 = StageContext(app=self.app, run=self.store.get_run(ctx.run_id), project=ctx.project, run_dir=ctx.run_dir,
                            workspace=ctx.workspace, cancel=threading.Event(), slides=ctx.slides)
        self.assertTrue(S.run_engine(ctx2, "build", "BUILD").ok)
        job = self.claude.jobs[1]
        self.assertEqual(job.resume_session_id, "sess-limited")
        self.assertEqual(job.prompt, CONTINUE_PREFIX + "BUILD")
        self.assertEqual(ctx2.run.engine["resumed_session_id"], "sess-limited")

    def test_a_task_failure_is_not_continued_and_fixes_keep_resume_for_fixes(self):
        self.claude.steps = [{"ok": False, "error": "error_max_turns", "session": "sess-1"},
                             {"ok": True, "session": "sess-2", "files": {"index.html": "<b>3</b>"}},
                             {"ok": True, "session": "sess-3", "files": {"index.html": "<b>4</b>"}}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked):
            S.run_engine(ctx, "build", "BUILD")
        S.run_engine(ctx, "build", "BUILD")
        self.assertIsNone(self.claude.jobs[1].resume_session_id)
        self.assertEqual(self.claude.jobs[1].prompt, "BUILD")
        ctx.run.iteration = 1
        S.run_engine(ctx, "fix", "FIX")  # resume_for_fixes: the previous call's session
        self.assertEqual(self.claude.jobs[2].resume_session_id, "sess-2")
        self.assertEqual(self.claude.jobs[2].prompt, "FIX")

    def test_an_expired_session_starts_fresh_and_is_told_about_the_kept_work(self):
        self.agent.available = False
        self.claude.steps = [{"ok": False, "error": SESSION_LIMIT, "session": "sess-limited",
                              "raw": {"failure_kind": "quota", "resets_at": time.time() + 60},
                              "files": {"src/half.js": "// partial"}},
                             {"ok": False, "error": "exit code 1: No conversation found with session ID: sess-limited"},
                             {"ok": True, "session": "sess-new", "files": {"index.html": "<canvas>v2</canvas>"}}]
        ctx = self.ctx()
        with self.assertRaises(StageBlocked):
            S.run_engine(ctx, "build", "BUILD")
        self.assertTrue(S.run_engine(ctx, "build", "BUILD").ok)
        retry = self.claude.jobs[2]
        self.assertIsNone(retry.resume_session_id)
        self.assertTrue(retry.prompt.startswith("A previous code engine (claude_code) stopped part-way (usage limit"),
                        retry.prompt)
        self.assertTrue(retry.prompt.endswith("\n\nBUILD"))


# ============================================================================= RunManager auto resume
class AutoResumeTests(_Harness):
    def blocked_run(self, at, state="blocked"):
        p = self.store.create("limited")
        run = self.store.create_run(p.id, "generate", "a game")
        at = at() if callable(at) else at  # after the (slow on Windows/macOS) project creation
        self.store.set_state(run, state, blocked_reason="usage limit", auto_resume_at=at)
        return run

    def manager(self) -> R.RunManager:
        rm = R.RunManager(self.app, workers=1)  # workers not started: a resumed run waits in the queue
        self.addCleanup(rm.stop, 0.5)
        return rm

    def wait_for(self, cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_timer_resumes_the_run_after_the_reset(self):
        rm = self.manager()
        with mock.patch.object(R, "AUTO_RESUME_GRACE_S", 0.0):
            run = self.blocked_run(lambda: time.time() + 1.0)
            self.assertTrue(rm._arm_auto_resume(run))
            self.assertIn(run.id, rm.status()["auto_resume"])
            self.assertTrue(self.wait_for(lambda: rm.is_active(run.id)), "the timer did not resume the run")
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "created")
        self.assertNotIn("auto_resume_at", after.extra)
        self.assertEqual(len(after.extra["auto_resumes"]), 1)
        [e] = [e.data for e in self.bus.replay(0, run_id=run.id) if e.type == RUN_AUTO_RESUME]
        self.assertEqual(set(e), {"at", "in_s", "reset_at", "reason"})
        self.assertLessEqual(e["in_s"], 1)
        self.assertEqual(rm.status()["auto_resume"], {})

    def test_the_callback_waits_for_the_wall_clock(self):
        rm = self.manager()
        run = self.blocked_run(time.time() + 3600)
        rm._schedule_resume(run.id, run.extra["auto_resume_at"], 0.0)  # e.g. woke up early / after a suspend
        time.sleep(0.2)
        self.assertEqual(self.store.get_run(run.id).state, "blocked")
        self.assertIn(run.id, rm.status()["auto_resume"])  # re-armed for the rest

    def test_restart_rearms_future_and_resumes_past_resets(self):
        future = self.blocked_run(time.time() + 3600)
        past = self.blocked_run(time.time() - 3600)
        plain = self.blocked_run(None)
        rm = self.manager()
        rm._rearm_auto_resumes()
        self.assertTrue(self.wait_for(lambda: rm.is_active(past.id)))
        self.assertEqual(self.store.get_run(past.id).state, "created")
        self.assertEqual(list(rm.status()["auto_resume"]), [future.id])
        self.assertEqual(self.store.get_run(plain.id).state, "blocked")
        [e] = [e.data for e in self.bus.replay(0, run_id=future.id) if e.type == RUN_AUTO_RESUME]
        self.assertGreater(e["in_s"], 3600)
        rm.stop(0.5)
        self.assertEqual(rm.status()["auto_resume"], {})

    def test_start_rearms(self):
        future = self.blocked_run(time.time() + 3600)
        rm = self.manager()
        rm.start()
        self.assertIn(future.id, rm.status()["auto_resume"])

    def test_manual_resume_and_cancel_drop_the_timer(self):
        rm = self.manager()
        a, b = self.blocked_run(time.time() + 3600), self.blocked_run(time.time() + 3600)
        rm._arm_auto_resume(a)
        rm._arm_auto_resume(b)
        self.assertTrue(rm.resume(a.id))
        self.assertNotIn(a.id, rm.status()["auto_resume"])
        self.assertNotIn("auto_resume_at", self.store.get_run(a.id).extra)
        self.assertTrue(rm.cancel(b.id))
        self.assertEqual(rm.status()["auto_resume"], {})
        self.assertEqual(self.store.get_run(b.id).state, "cancelled")

    def test_a_deleted_or_changed_run_is_not_resumed(self):
        rm = self.manager()
        run = self.blocked_run(time.time() - 120)
        token = object()
        rm._resume_timers[run.id] = (threading.Timer(999, lambda: None), run.extra["auto_resume_at"], token)
        self.store.set_state(run, "blocked", blocked_reason="no browser", auto_resume_at=None)
        rm._auto_resume_due(run.id, run.extra.get("auto_resume_at") or time.time() - 120, token)
        self.assertEqual(self.store.get_run(run.id).state, "blocked")
        gone = self.blocked_run(time.time() - 120)
        token = object()
        rm._resume_timers[gone.id] = (threading.Timer(999, lambda: None), gone.extra["auto_resume_at"], token)
        self.store.delete(gone.project_id)
        rm._auto_resume_due(gone.id, gone.extra["auto_resume_at"], token)  # no exception, nothing queued
        self.assertFalse(rm.is_active(gone.id))

    def test_automatic_resumes_are_capped(self):
        rm = self.manager()
        run = self.blocked_run(time.time() + 600)
        run.extra["auto_resumes"] = ["x"] * R.AUTO_RESUME_MAX
        self.store.save_run(run)
        self.assertFalse(rm._arm_auto_resume(self.store.get_run(run.id)))
        self.assertEqual(rm.status()["auto_resume"], {})
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "blocked")
        self.assertNotIn("auto_resume_at", after.extra)
        self.assertIn(f"automatic resume stopped after {R.AUTO_RESUME_MAX} automatic resumes", after.blocked_reason)


class AutoResumeEndToEndTests(_Harness):
    """A real Runner and RunManager: usage limit -> blocked -> resumed by itself -> the session continues -> gate."""

    def setUp(self):
        super().setUp()
        self.plan = dict(PLAN)
        tests = lambda ctx, n: ([Check(f"s{i}", "static", "s", "pass") for i in range(20)], [],  # noqa: E731
                                [Check("at-01", "browser", "boots", "pass")])
        for name, fn in {"intake_stage": lambda ctx: {},
                         "plan_stage": lambda ctx: ctx.write_artifact("plan", self.plan) and self.plan,
                         "design_stage": lambda ctx: ({}, {}), "narrate": lambda *a, **k: None,
                         "show_view": lambda *a, **k: None, "test_stage": tests}.items():
            p = mock.patch.object(R, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(R, "AUTO_RESUME_GRACE_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def test_limited_run_resumes_and_completes(self):
        self.agent.available = False
        self.claude.steps = [{"ok": False, "error": "api_error (HTTP 429): " + SESSION_LIMIT, "session": "sess-limited",
                              "raw": {"failure_kind": "quota", "resets_at": lambda: time.time() + 1.0},
                              "files": {"index.html": "<canvas></canvas><script>half</script>"}},
                             {"ok": True, "session": "sess-limited",
                              "files": {"index.html": "<canvas></canvas><script>whole</script>"}}]
        rm = R.RunManager(self.app, workers=1)
        self.addCleanup(rm.stop, 2.0)
        rm.start()
        p = self.store.create("limit")
        run = self.store.create_run(p.id, "generate", "a runner game")
        rm.submit(run.id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.store.get_run(run.id).state not in ("complete", "failed",
                                                                                      "failed_gate", "cancelled"):
            time.sleep(0.05)
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "complete", after.blocked_reason or after.error)
        types = [e.type for e in self.bus.replay(0, run_id=run.id)]
        self.assertIn(RUN_BLOCKED, types)
        self.assertIn(RUN_AUTO_RESUME, types)
        blocked = [e.data for e in self.bus.replay(0, run_id=run.id) if e.type == RUN_BLOCKED][0]
        self.assertIn("auto_resume_at", blocked)
        self.assertEqual(len(self.claude.jobs), 2)
        self.assertEqual(self.claude.jobs[1].resume_session_id, "sess-limited")
        self.assertTrue(self.claude.jobs[1].prompt.startswith(CONTINUE_PREFIX))
        self.assertEqual(len(after.extra["auto_resumes"]), 1)
        self.assertNotIn("auto_resume_at", after.extra)
        self.assertTrue(after.gate["final"]["ok"])  # measured by the gate, not assumed from the failover

    def test_another_block_never_keeps_a_stale_resume_time(self):
        self.claude.steps = [{"ok": False, "error": "error_max_turns"}]
        p = self.store.create("stale")
        run = self.store.create_run(p.id, "generate", "a game")
        run.extra["auto_resume_at"] = time.time() + 999
        self.store.save_run(run)
        R.Runner(self.app, self.store.get_run(run.id), threading.Event()).execute()
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "blocked")
        self.assertNotIn("auto_resume_at", after.extra)
        self.assertEqual(self.agent.jobs, [])  # a task failure is not failed over


class AgentNarrationTests(_Harness):
    def test_the_agent_engine_is_announced_with_its_model_not_a_claude_effort(self):
        said: list[str] = []

        def build(ctx):
            raise StageBlocked("stop after the announcement", "build")
        patches = mock.patch.multiple(R, intake_stage=lambda ctx: {}, plan_stage=lambda ctx: dict(PLAN),
                                      design_stage=lambda ctx: ({}, {}), build_stage=build, show_view=lambda *a, **k: None,
                                      narrate=lambda ctx, text: said.append(text),
                                      select_engine=lambda ctx: SimpleNamespace(name="openai_agent"))
        patches.start()
        self.addCleanup(patches.stop)
        p = self.store.create("narration")
        R.Runner(self.app, self.store.create_run(p.id, "generate", "a game"), threading.Event()).execute()
        self.assertIn("Kyora's own code engine is now building your game with z-ai/glm-5.3.", said)
        self.assertFalse(any("effort" in t for t in said), said)


if __name__ == "__main__":
    unittest.main()
