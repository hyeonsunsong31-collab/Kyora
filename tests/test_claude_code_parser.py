"""Tests of the Claude Code engine without the real CLI.

* ``parse_stream_line`` against fixtures in ``tests/fixtures/stream_json``
  (synthetic ones plus ``real_*.jsonl`` lines captured from the real CLI by
  ``tests/test_claude_code_smoke.py`` and sanitized: ``real_credits_refusal.jsonl``
  is the primary model's refusal, ``real_session.jsonl`` the full served session
  on the next model, ``real_{init,assistant_tool_use,user_tool_result,result}``
  single line types of it).
* argv / settings construction, the pure retry decisions, ``describe_argv`` and
  ``_build_result``.
* ``run()`` end to end against a FAKE ``claude``: a small Python script written
  to a temp dir that prints stream-json lines and can simulate a rejected
  hidden flag, a model error before ``init``, a model refused after ``init``,
  a hang (cancel / idle / wall timeout), a prompt delivered on stdin, and a
  replay of the real smoke run's captured CLI output.

Run: ``python3 -m unittest tests.test_claude_code_parser -v``
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from kyora.config import DEFAULT_SETTINGS, ClaudeCodeCfg
from kyora.models.base import EngineEvent, EngineJob
from kyora.models.claude_code import (
    ALLOWED_TOOLS, HOOK_TIMEOUT_S, PROMPT_ARGV_LIMIT, ClaudeCodeEngine, _Attempt, cmd_shim_unsafe_args,
    primary_model_from_usage, same_model, summarize_tool_input,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stream_json"
PY = sys.executable

# --------------------------------------------------------------------------- fake CLI
FAKE_CLAUDE = textwrap.dedent(r'''
    """Fake 'claude' for Kyora tests: prints stream-json like the real CLI (test-only, never shipped)."""
    import json, os, sys, time

    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_MODE", "ok")
    log_path = os.environ.get("FAKE_LOG")

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None

    prompt = None
    if "-p" in argv:
        nxt = argv.index("-p") + 1
        if nxt < len(argv) and not argv[nxt].startswith("--"):
            prompt = argv[nxt]
    stdin_text = None
    if prompt is None:
        stdin_text = sys.stdin.read()
    if log_path:
        env = {k: v for k, v in os.environ.items() if k.startswith(("KYORA_", "CLAUDE"))}
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "stdin_len": None if stdin_text is None else len(stdin_text),
                                 "cwd": os.getcwd(), "env": env}) + "\n")

    json_mode = opt("--output-format") == "json"  # single result object, like "claude -p --output-format json"

    def emit(obj):
        if json_mode and obj.get("type") != "result":
            return
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    if mode in ("compat", "compat2") and "--max-turns" in argv:
        sys.stderr.write("error: unknown option '--max-turns'\n")
        sys.exit(1)
    if mode == "compat2" and "--append-system-prompt-file" in argv:
        sys.stderr.write("error: unknown option '--append-system-prompt-file'\n(Did you mean --append-system-prompt?)\n")
        sys.exit(1)
    model = opt("--model") or "default-model"
    if mode == "model" and model == "bad-model":
        sys.stderr.write("API Error: model bad-model is restricted for this organization\n")
        sys.exit(1)
    if mode == "crash":
        sys.stderr.write("fatal: something unrelated broke (file not found)\n")
        sys.exit(3)
    if mode == "replay":
        # print lines captured from the REAL CLI for the requested model: FAKE_REPLAY={"model": ["file", rc]}
        path, rc = json.loads(os.environ["FAKE_REPLAY"])[model]
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    sys.stdout.write(line if line.endswith("\n") else line + "\n")
                    sys.stdout.flush()
        sys.exit(rc)

    sid = "fake-session-1"
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": os.getcwd(),
          "tools": ["Bash", "Read", "Write"], "permissionMode": opt("--permission-mode")})
    if mode in ("sleep", "idle"):
        time.sleep(60)
        sys.exit(0)
    if (mode == "credits" and model == "credit-model") or mode == "auth401":
        status, code, text = ((401, "authentication_failed", "Invalid API key - Please run /login") if mode == "auth401"
                              else (429, "credits_required", "You're out of usage credits. Switch to another model to continue."))
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "errorCode": code}, "session_id": sid})
        emit({"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
              "error": "rate_limit", "is_api_error_message": True, "api_error_status": status})
        emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "duration_ms": 500,
              "duration_api_ms": 0, "session_id": sid, "total_cost_usd": 0, "result": text, "modelUsage": {},
              "permission_denials": [], "terminal_reason": "api_error", "api_error_status": status,
              "api_error_code": code, "api_error": "model_requires_usage_credits" if status == 429 else "auth"})
        sys.exit(1)
    if mode == "wall":
        for _ in range(600):
            emit({"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "tick"}]}})
            time.sleep(0.1)
        sys.exit(0)
    if not json_mode:
        sys.stdout.write("this line is not json\n")
    sys.stderr.write("warning: fake stderr line\n")
    sys.stderr.flush()
    served = os.environ.get("FAKE_SERVED_MODEL") or model
    emit({"type": "assistant", "message": {"model": served, "content": [
        {"type": "text", "text": "Writing the file."},
        {"type": "tool_use", "id": "toolu_A", "name": "Write",
         "input": {"file_path": os.path.join(os.getcwd(), "out.txt"), "content": "OK"}}]}})
    with open(os.path.join(os.getcwd(), "out.txt"), "w", encoding="utf-8") as fh:
        fh.write("OK")
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_A", "content": "File created successfully"}]}})
    if mode == "max_turns":
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 2,
              "duration_ms": 50, "session_id": sid, "total_cost_usd": 0.001, "permission_denials": []})
        sys.exit(1)
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "duration_ms": 42,
          "duration_api_ms": 40, "session_id": sid, "total_cost_usd": 0.0123,
          "result": "done; prompt_len=%d" % len(prompt if prompt is not None else stdin_text or ""),
          "modelUsage": {served: {"inputTokens": 5, "outputTokens": 7}},
          "permission_denials": [{"tool_name": "Bash", "tool_use_id": "toolu_X", "tool_input": {"command": "x"}}]})
''')


def make_cfg(command: list[str], **over) -> ClaudeCodeCfg:
    base = dict(DEFAULT_SETTINGS["claude_code"])
    base.update(command=command, timeout_s=60, idle_timeout_s=30)
    base.update(over)
    return ClaudeCodeCfg(**base)


def make_job(workspace: Path, **over) -> EngineJob:
    data = dict(run_id="r_test", workspace=workspace, prompt="Build the game", system_prompt="You are Kyora's engine.",
                max_turns=12, model="claude-fable-5-1", fallback_models=["claude-opus-5-5", "claude-sonnet-5-5"],
                effort="max", project_id="p_test")
    data.update(over)
    return EngineJob(**data)


class ListSink:
    """Collects engine events (thread-safe)."""

    def __init__(self) -> None:
        self.events: list[EngineEvent] = []
        self._lock = threading.Lock()
        self.session_seen = threading.Event()

    def on_event(self, ev: EngineEvent) -> None:
        with self._lock:
            self.events.append(ev)
        if ev.type == "engine.session":
            self.session_seen.set()

    def of(self, type_: str) -> list[dict]:
        with self._lock:
            return [e.data for e in self.events if e.type == type_]

    def types(self) -> list[str]:
        with self._lock:
            return [e.type for e in self.events]


def parse_file(name: str, requested: str = "claude-fable-5-1", max_turns: int = 10) -> tuple[list[EngineEvent], dict]:
    state = ClaudeCodeEngine.new_parse_state(requested, max_turns, "build")
    events: list[EngineEvent] = []
    with open(FIXTURES / name, encoding="utf-8") as fh:
        for line in fh:
            events.extend(ClaudeCodeEngine.parse_stream_line(line, state))
    return events, state


class TempHomeMixin:
    def setUp(self) -> None:  # noqa: D401 - unittest hook
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-cc-")
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self._tmp.cleanup()


# =========================================================================== parser
class ParseStreamLineTests(unittest.TestCase):
    def test_init_emits_session_and_model_resolved(self):
        state = ClaudeCodeEngine.new_parse_state("claude-opus-5-5", 10)
        line = (FIXTURES / "ok_session.jsonl").read_text(encoding="utf-8").splitlines()[0]
        evs = ClaudeCodeEngine.parse_stream_line(line, state)
        self.assertEqual([e.type for e in evs], ["engine.session", "engine.model_resolved"])
        sess = evs[0].data
        self.assertEqual(sess["session_id"], "0b0c9a5e-1111-4a2b-9c3d-000000000001")
        self.assertEqual(sess["model"], "claude-opus-5-5")
        self.assertEqual(sess["permission_mode"], "acceptEdits")
        self.assertEqual(sess["cwd"], "/WORKSPACE")
        self.assertEqual(sess["backend"], "claude_code")
        self.assertIn("Write", sess["tools"])
        self.assertEqual(evs[1].data, {"requested": "claude-opus-5-5", "active": "claude-opus-5-5", "fallback_used": False})
        self.assertTrue(state["saw_init"])
        self.assertEqual(state["session_id"], "0b0c9a5e-1111-4a2b-9c3d-000000000001")

    def test_full_ok_session(self):
        events, state = parse_file("ok_session.jsonl", requested="claude-fable-5-1", max_turns=4)
        types = [e.type for e in events]
        self.assertEqual(types, ["engine.session", "engine.model_resolved", "engine.thinking", "engine.text",
                                 "engine.tool_use", "stage.progress", "engine.tool_result",
                                 "engine.tool_use", "stage.progress", "engine.tool_result", "engine.result"])
        by = lambda t: [e.data for e in events if e.type == t]  # noqa: E731
        self.assertEqual(by("engine.model_resolved"),
                         [{"requested": "claude-fable-5-1", "active": "claude-opus-5-5", "fallback_used": True}])
        self.assertEqual(by("engine.thinking")[0]["text"], "Plan: write index.html then check syntax.")
        self.assertEqual(by("engine.text")[0]["text"], "I will create the game entry point.")
        uses = by("engine.tool_use")
        self.assertEqual(uses[0], {"id": "toolu_01", "name": "Write", "summary": "/WORKSPACE/index.html"})
        self.assertEqual(uses[1], {"id": "toolu_02", "name": "Bash", "summary": "node --check src/game.js"})
        progress = by("stage.progress")
        self.assertEqual([p["pct"] for p in progress], [25, 50])
        self.assertEqual(progress[0]["stage"], "build")
        self.assertTrue(progress[0]["message"].startswith("Write: /WORKSPACE/index.html"))
        results = by("engine.tool_result")
        self.assertEqual(results[0], {"tool_use_id": "toolu_01", "name": "Write", "is_error": False,
                                      "excerpt": "File created successfully at: /WORKSPACE/index.html"})
        self.assertTrue(results[1]["is_error"])
        self.assertEqual(results[1]["name"], "Bash")
        self.assertIn("denied Bash", results[1]["excerpt"])
        res = by("engine.result")[0]
        self.assertEqual(res, {"subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 12345,
                               "cost_usd": 0.0421, "result": "Created index.html."})
        self.assertEqual(state["tool_uses"], 2)
        self.assertEqual(state["malformed"], 0)
        self.assertEqual(state["lines"], 8)
        self.assertEqual(state["result"]["subtype"], "success")

    def test_result_error_max_turns(self):
        events, state = parse_file("error_max_turns.jsonl", requested="claude-sonnet-5-5")
        res = [e.data for e in events if e.type == "engine.result"][0]
        self.assertEqual(res["subtype"], "error_max_turns")
        self.assertTrue(res["is_error"])
        self.assertEqual(res["num_turns"], 2)
        self.assertEqual(res["result"], "")  # no result text in error results
        resolved = [e.data for e in events if e.type == "engine.model_resolved"]
        self.assertEqual(resolved, [{"requested": "claude-sonnet-5-5", "active": "claude-sonnet-5-5", "fallback_used": False}])
        tr = [e.data for e in events if e.type == "engine.tool_result"][0]
        self.assertEqual(tr["name"], "Read")
        self.assertIn("const x = 1;", tr["excerpt"])

    def test_malformed_lines_are_counted_and_ignored(self):
        events, state = parse_file("malformed.jsonl", requested="claude-opus-5-5")
        self.assertEqual(state["malformed"], 3)  # not json, a JSON array, a truncated object
        self.assertEqual([e.type for e in events], ["engine.session", "engine.model_resolved", "engine.result"])
        self.assertEqual(state["result"]["subtype"], "error_during_execution")
        # empty / whitespace lines are neither events nor malformed
        st = ClaudeCodeEngine.new_parse_state("x", 1)
        self.assertEqual(ClaudeCodeEngine.parse_stream_line("", st), [])
        self.assertEqual(ClaudeCodeEngine.parse_stream_line("   \n", st), [])
        self.assertEqual(st["malformed"], 0)

    def test_model_drift_resolved_once_per_change(self):
        events, state = parse_file("fallback_drift.jsonl", requested="claude-fable-5-1")
        resolved = [e.data for e in events if e.type == "engine.model_resolved"]
        self.assertEqual(resolved, [
            {"requested": "claude-fable-5-1", "active": "claude-fable-5-1", "fallback_used": False},
            {"requested": "claude-fable-5-1", "active": "claude-opus-5-5", "fallback_used": True},
            {"requested": "claude-fable-5-1", "active": "claude-fable-5-1", "fallback_used": False},
        ])
        self.assertEqual(state["model_changes"], 2)
        self.assertEqual(state["active_model"], "claude-fable-5-1")
        # the "<synthetic>" API-error message is shown as text but is not a model
        texts = [e.data["text"] for e in events if e.type == "engine.text"]
        self.assertIn("API Error: Repeated 529 Overloaded errors", texts)

    def test_fallback_flag_uses_same_model(self):
        state = ClaudeCodeEngine.new_parse_state("opus", 5)
        evs = ClaudeCodeEngine.parse_stream_line(json.dumps(
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}}), state)
        self.assertEqual(evs[0].data["fallback_used"], False)  # alias
        state = ClaudeCodeEngine.new_parse_state("claude-opus-5-5", 5)
        evs = ClaudeCodeEngine.parse_stream_line(json.dumps(
            {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5-5[1m]"}), state)
        self.assertEqual(evs[1].data, {"requested": "claude-opus-5-5", "active": "claude-opus-5-5[1m]", "fallback_used": False})

    def test_progress_is_capped(self):
        state = ClaudeCodeEngine.new_parse_state("m", 1, "fix")
        line = json.dumps({"type": "assistant", "message": {"model": "m", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": "a"}} for i in range(3)]}})
        evs = ClaudeCodeEngine.parse_stream_line(line, state)
        pcts = [e.data["pct"] for e in evs if e.type == "stage.progress"]
        self.assertEqual(pcts, [95, 95, 95])
        self.assertEqual({e.data["stage"] for e in evs if e.type == "stage.progress"}, {"fix"})

    def test_tool_result_with_image_and_non_list_content(self):
        state = ClaudeCodeEngine.new_parse_state("m", 5)
        evs = ClaudeCodeEngine.parse_stream_line(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "image", "source": {}},
                                                                    {"type": "text", "text": "shot"}]}]}}), state)
        self.assertEqual(evs[0].data["excerpt"], "[image]\nshot")
        self.assertIsNone(evs[0].data["name"])
        self.assertEqual(ClaudeCodeEngine.parse_stream_line(
            json.dumps({"type": "user", "message": {"content": "plain prompt echo"}}), state), [])

    def test_real_cli_fixtures(self):
        """Lines captured from the real CLI by the smoke test (sanitized) parse into the expected events."""
        files = {p.name: p for p in FIXTURES.glob("real_*.jsonl")}
        if not files:
            self.skipTest("no real_*.jsonl fixtures captured yet (run tests.test_claude_code_smoke)")
        expected = {"real_init.jsonl": "engine.session", "real_assistant_tool_use.jsonl": "engine.tool_use",
                    "real_user_tool_result.jsonl": "engine.tool_result", "real_result.jsonl": "engine.result",
                    "real_credits_refusal.jsonl": "engine.result", "real_session.jsonl": "engine.result"}
        for name, path in sorted(files.items()):
            state = ClaudeCodeEngine.new_parse_state("claude-fable-5-1", 8)
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(lines, name)
            self.assertNotIn("/root/", "".join(lines), name)
            self.assertNotIn("kyora-smoke-", "".join(lines), name)
            types: list[str] = []
            for ln in lines:
                types.extend(e.type for e in ClaudeCodeEngine.parse_stream_line(ln, state))
            self.assertEqual(state["malformed"], 0, name)
            if name in expected:
                self.assertIn(expected[name], types, name)
            if name == "real_init.jsonl":
                self.assertTrue(state["saw_init"])
                self.assertTrue(state["session_id"])
                self.assertIn("engine.model_resolved", types)
            if name == "real_result.jsonl":
                res = state["result"]
                self.assertIn(res.get("subtype"), ("success", "error_max_turns", "error_during_execution"))
                self.assertIsInstance(res.get("num_turns"), int)
                self.assertIsInstance(res.get("duration_ms"), int)
            if name == "real_user_tool_result.jsonl":
                # the hook's exit-2 block arrives as an error tool_result carrying the judge's reason
                blocked = [ln for ln in lines if '"is_error": true' in ln or '"is_error":true' in ln]
                self.assertTrue(blocked, "expected the blocked forbidden.txt tool_result in the real capture")
                self.assertIn("Kyora judge", "".join(blocked))
            if name == "real_credits_refusal.jsonl":
                # CLI 2.1.300: init with the requested model, then a <synthetic> API-error message, then an
                # is_error result with subtype "success"; the model is refused before doing any work
                self.assertEqual(state["active_model"], "claude-fable-5-1")
                self.assertEqual(types.count("engine.model_resolved"), 1)  # "<synthetic>" is not a model
                self.assertEqual(state["model_messages"], 0)
                self.assertEqual(state["rate_limits"][0]["status"], "rejected")
                reason = ClaudeCodeEngine.model_unavailable_after_init(state)
                self.assertIsNotNone(reason)
                self.assertIn("Switch to another model", reason)
                self.assertIn("HTTP 429", reason)
                self.assertIn("credits_required", reason)
            if name == "real_session.jsonl":
                # CLI 2.1.300 serving the fallback model: init, thinking_tokens notices, (redacted) thinking,
                # two Write tool_use blocks, rate_limit_event lines, the hook's exit-2 block, final text, result
                self.assertEqual(state["active_model"], "claude-opus-5-5")
                self.assertEqual(state["tool_uses"], 2)
                self.assertIsNone(ClaudeCodeEngine.model_unavailable_after_init(state))
                self.assertEqual(state["result"]["permission_denials"][0]["tool_input"]["file_path"],
                                 "/WORKSPACE/forbidden.txt")
                self.assertEqual([r["status"] for r in state["rate_limits"]], ["allowed", "allowed"])
        for name in ("real_credits_refusal.jsonl", "real_session.jsonl", "real_init.jsonl",
                     "real_assistant_tool_use.jsonl", "real_user_tool_result.jsonl", "real_result.jsonl"):
            self.assertIn(name, files)

    def test_real_fixture_details(self):
        """Exact values observed on the real CLI (2.1.300, subscription login) during the smoke run."""
        events, state = parse_file("real_init.jsonl")
        sess = [e.data for e in events if e.type == "engine.session"][0]
        self.assertEqual(sess["model"], "claude-opus-5-5")
        self.assertEqual(sess["permission_mode"], "acceptEdits")
        self.assertEqual(sess["cwd"], "/WORKSPACE")
        for tool in ALLOWED_TOOLS:
            if tool != "MultiEdit":  # CLI 2.1.300 no longer lists MultiEdit; allowing it is harmless
                self.assertIn(tool, sess["tools"])
        self.assertEqual([e.data for e in events if e.type == "engine.model_resolved"],
                         [{"requested": "claude-fable-5-1", "active": "claude-opus-5-5", "fallback_used": True}])
        events, state = parse_file("real_assistant_tool_use.jsonl")
        uses = [e.data for e in events if e.type == "engine.tool_use"]
        self.assertEqual([(u["name"], u["summary"]) for u in uses],
                         [("Write", "/WORKSPACE/allowed.txt"), ("Write", "/WORKSPACE/forbidden.txt")])
        events, state = parse_file("real_user_tool_result.jsonl")
        results = [e.data for e in events if e.type == "engine.tool_result"]
        self.assertEqual([r["is_error"] for r in results], [False, True])
        self.assertTrue(results[1]["excerpt"].startswith("PreToolUse:Write hook error:"))
        self.assertIn("Kyora judge (rule) denied Write: smoke test rule", results[1]["excerpt"])
        events, state = parse_file("real_result.jsonl")
        res = [e.data for e in events if e.type == "engine.result"][0]
        self.assertEqual((res["subtype"], res["is_error"], res["num_turns"], res["duration_ms"]),
                         ("success", False, 3, 9253))
        self.assertAlmostEqual(res["cost_usd"], 0.0730166)
        self.assertEqual(state["result"]["terminal_reason"], "completed")
        events, state = parse_file("real_session.jsonl")
        thinking = [e.data["text"] for e in events if e.type == "engine.thinking"]
        self.assertEqual(thinking, ["", ""])  # the CLI streamed redacted thinking (signature only)

    def test_model_unavailable_after_init(self):
        f = ClaudeCodeEngine.model_unavailable_after_init
        base = {"saw_init": True, "tool_uses": 0, "model_messages": 0}
        err = {"type": "result", "subtype": "success", "is_error": True, "result": "You're out of usage credits. Switch to another model to continue.",
               "api_error_status": 429, "api_error_code": "credits_required", "api_error": "model_requires_usage_credits"}
        self.assertIn("HTTP 429", f(dict(base, result=err)))
        # older CLIs without api_error fields: the text alone decides
        self.assertIsNotNone(f(dict(base, result={"is_error": True, "result": "API Error: 404 model: claude-x not found"})))
        self.assertIsNotNone(f(dict(base, result={"is_error": True, "result": "Claude AI usage limit reached|1792000000"})))
        self.assertIsNone(f(dict(base, result=dict(err, api_error_status=401))))  # auth: every model fails
        self.assertIsNone(f(dict(base, tool_uses=1, result=err)))  # work already done
        self.assertIsNone(f(dict(base, model_messages=2, result=err)))
        self.assertIsNone(f(dict(base, saw_init=False, result=err)))  # pre-init is model_retry_decision's job
        self.assertIsNone(f(dict(base, result=dict(err, is_error=False))))
        self.assertIsNone(f(dict(base, result={"is_error": True, "result": "API Error: 500 internal error"})))
        self.assertIsNone(f(dict(base, result={"is_error": True, "subtype": "error_max_turns"})))
        self.assertIsNone(f(dict(base, result=None)))

    def test_rate_limit_event_is_recorded_not_emitted(self):
        state = ClaudeCodeEngine.new_parse_state("m", 5)
        evs = ClaudeCodeEngine.parse_stream_line(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "resetsAt": 1, "rateLimitType": "five_hour", "secret": "x"}}), state)
        self.assertEqual(evs, [])
        self.assertEqual(state["rate_limits"], [{"status": "allowed_warning", "resetsAt": 1, "rateLimitType": "five_hour"}])


class HelperTests(unittest.TestCase):
    def test_same_model(self):
        self.assertTrue(same_model("claude-opus-5-5", "claude-opus-5-5"))
        self.assertTrue(same_model("claude-opus-5-5", "Claude-Opus-5-5"))
        self.assertTrue(same_model("claude-opus-5-5", "claude-opus-5-5-20261001"))
        self.assertTrue(same_model("claude-opus-5-5", "claude-opus-5-5[1m]"))
        self.assertTrue(same_model("opus", "claude-opus-5-5"))
        self.assertFalse(same_model("claude-fable-5-1", "claude-opus-5-5"))
        self.assertFalse(same_model("claude-opus-5", "claude-opus-5-5"))
        self.assertFalse(same_model("", "claude-opus-5-5"))
        self.assertFalse(same_model("claude-opus-5-5", None))

    def test_primary_model_from_usage(self):
        usage = {"claude-haiku-4-5": {"outputTokens": 900}, "claude-opus-5-5": {"outputTokens": 10}}
        self.assertEqual(primary_model_from_usage(usage, ["claude-fable-5-1", "claude-opus-5-5"]), "claude-opus-5-5")
        self.assertEqual(primary_model_from_usage(usage, ["claude-fable-5-1"]), "claude-haiku-4-5")  # most output
        self.assertIsNone(primary_model_from_usage({}, ["x"]))
        self.assertIsNone(primary_model_from_usage(None))
        self.assertIsNone(primary_model_from_usage({"<synthetic>": {}}))

    def test_summarize_tool_input(self):
        self.assertEqual(summarize_tool_input("Write", {"file_path": "/a/b.js", "content": "x"}), "/a/b.js")
        self.assertEqual(summarize_tool_input("Bash", {"command": "ls\n-la"}), "ls -la")
        self.assertEqual(summarize_tool_input("Grep", {"pattern": "foo"}), "foo")
        self.assertEqual(len(summarize_tool_input("Bash", {"command": "x" * 500})), 200)
        self.assertEqual(summarize_tool_input("X", {"n": 1}), '{"n": 1}')
        self.assertEqual(summarize_tool_input("X", "raw"), "raw")


# =========================================================================== argv / settings
class ArgvAndSettingsTests(TempHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine = ClaudeCodeEngine(make_cfg(["claude"]), self.home, SimpleNamespace(server_url="", judge=None))
        self.run_dir = self.tmp / "run"

    def test_flags_present(self):
        job = make_job(self.ws, resume_session_id="sess-123")
        argv = self.engine.build_argv(job, self.run_dir)
        self.assertEqual(argv[:3], ["claude", "-p", "Build the game"])

        def val(flag):
            self.assertIn(flag, argv)
            return argv[argv.index(flag) + 1]

        self.assertEqual(val("--model"), "claude-fable-5-1")
        self.assertEqual(val("--fallback-model"), "claude-opus-5-5,claude-sonnet-5-5")
        self.assertEqual(val("--effort"), "max")
        self.assertEqual(val("--max-turns"), "12")
        self.assertEqual(val("--output-format"), "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(val("--permission-mode"), "acceptEdits")
        self.assertEqual(val("--permission-prompts"), "none")
        self.assertEqual(val("--allowedTools"), ",".join(ALLOWED_TOOLS))
        self.assertEqual(val("--allowedTools"), "Read,Write,Edit,MultiEdit,Bash,Glob,Grep")
        self.assertEqual(val("--append-system-prompt-file"), (self.run_dir / "system_prompt.md").as_posix())
        self.assertEqual(val("--settings"), (self.run_dir / "claude_settings.json").as_posix())
        self.assertEqual(val("--resume"), "sess-123")
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--append-system-prompt", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        # the prompt is the positional right after -p, before any variadic option
        self.assertLess(argv.index("-p"), argv.index("--allowedTools"))

    def test_extra_args_appended_and_never_bare(self):
        eng = ClaudeCodeEngine(make_cfg(["proot-distro", "login", "ubuntu", "--", "claude"], extra_args=["--debug-file", "x.log"]),
                               self.home, SimpleNamespace(server_url="", judge=None))
        argv = eng.build_argv(make_job(self.ws), self.run_dir)
        self.assertEqual(argv[:5], ["proot-distro", "login", "ubuntu", "--", "claude"])
        self.assertEqual(argv[-2:], ["--debug-file", "x.log"])
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--resume", argv)

    def test_fallback_list_excludes_active_model_and_empty(self):
        job = make_job(self.ws, model="claude-opus-5-5", fallback_models=["claude-opus-5-5", "", "claude-sonnet-5-5"])
        argv = self.engine.build_argv(job, self.run_dir)
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "claude-sonnet-5-5")
        argv = self.engine.build_argv(make_job(self.ws, fallback_models=[]), self.run_dir)
        self.assertNotIn("--fallback-model", argv)

    def test_prompt_via_stdin_when_long(self):
        long_job = make_job(self.ws, prompt="x" * (PROMPT_ARGV_LIMIT + 1))
        self.assertTrue(self.engine.prompt_via_stdin(long_job))
        argv = self.engine.build_argv(long_job, self.run_dir)
        self.assertEqual(argv[1:3], ["-p", "--model"])
        self.assertNotIn("x" * 100, " ".join(argv))
        exact = make_job(self.ws, prompt="y" * PROMPT_ARGV_LIMIT)
        self.assertFalse(self.engine.prompt_via_stdin(exact))
        self.assertEqual(self.engine.build_argv(exact, self.run_dir)[2], "y" * PROMPT_ARGV_LIMIT)

    def test_cmd_shim_never_gets_free_text_in_argv(self):
        # Windows runs claude.cmd through cmd.exe: a multi-line prompt in argv would be cut at the first line
        # break and drop every later flag (stream-json, --tools, --settings with the judge hook).
        prompt = 'Build the game "Tom & Jerry" at 100%\nUse <canvas> | no deps ^_^'
        job = make_job(self.ws, prompt=prompt)
        for cfg_cmd, compat in ((["claude"], {"cmd_shim": True}),
                                (["C:/Users/me/AppData/Roaming/npm/claude.CMD"], None),
                                (["claude.bat"], None)):
            argv = ClaudeCodeEngine(make_cfg(cfg_cmd), self.home, None).build_argv(job, self.run_dir, compat)
            self.assertEqual(argv[1:3], ["-p", "--model"])
            self.assertNotIn(prompt, argv)
            self.assertIn("--settings", argv)
            self.assertIn("--append-system-prompt-file", argv)
            # the inline system prompt fallback is never used for a shim
            argv = ClaudeCodeEngine(make_cfg(cfg_cmd), self.home, None).build_argv(
                job, self.run_dir, {**(compat or {}), "append_system_prompt_file": False})
            self.assertNotIn("--append-system-prompt", argv)
            self.assertIn("--append-system-prompt-file", argv)
        self.assertTrue(self.engine.prompt_via_stdin(job, "C:/npm/claude.cmd"))
        self.assertFalse(self.engine.prompt_via_stdin(job, "C:/Program Files/claude/claude.exe"))
        self.assertEqual(self.engine.build_argv(job, self.run_dir)[2], prompt)  # a real executable keeps argv
        self.assertIsNone(ClaudeCodeEngine.compat_retry_decision(
            "error: unknown option '--append-system-prompt-file'", {"cmd_shim": True}))
        self.assertEqual(cmd_shim_unsafe_args(["claude.cmd", "-p", "--settings", "C:/Users/a%b/s.json", "x\ny"]),
                         ["C:/Users/a%b/s.json", "x\ny"])
        self.assertEqual(cmd_shim_unsafe_args(["claude.exe", "-p", "a & b\n"]), [])

    def test_prompt_starting_with_dash_is_not_parsed_as_option(self):
        argv = self.engine.build_argv(make_job(self.ws, prompt="--help me"), self.run_dir)
        self.assertEqual(argv[2], " --help me")

    def test_compat_variants(self):
        job = make_job(self.ws)
        argv = self.engine.build_argv(job, self.run_dir, {"max_turns": False})
        self.assertNotIn("--max-turns", argv)
        self.assertIn("--append-system-prompt-file", argv)
        argv = self.engine.build_argv(job, self.run_dir, {"append_system_prompt_file": False})
        self.assertNotIn("--append-system-prompt-file", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "You are Kyora's engine.")
        argv = self.engine.build_argv(job, self.run_dir, {"permission_prompts": False})
        self.assertNotIn("--permission-prompts", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        argv = self.engine.build_argv(job, self.run_dir, {"model": "claude-sonnet-5-5", "fallbacks": []})
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertNotIn("--fallback-model", argv)
        argv = self.engine.build_argv(make_job(self.ws, system_prompt=""), self.run_dir)
        self.assertNotIn("--append-system-prompt-file", argv)
        self.assertNotIn("--append-system-prompt", argv)
        argv = self.engine.build_argv(make_job(self.ws, max_turns=0), self.run_dir)
        self.assertNotIn("--max-turns", argv)

    def test_tool_set_is_restricted_to_judged_builtins(self):
        argv = self.engine.build_argv(make_job(self.ws), self.run_dir)
        self.assertEqual(argv[argv.index("--tools") + 1], ",".join(ALLOWED_TOOLS))
        self.assertIn("--strict-mcp-config", argv)
        argv = self.engine.build_argv(make_job(self.ws), self.run_dir, {"tools": False, "strict_mcp_config": False})
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--strict-mcp-config", argv)

    def test_describe_argv_hides_prompt_text(self):
        job = make_job(self.ws, prompt="SECRET PROMPT TEXT", system_prompt="SECRET SYSTEM")
        argv = self.engine.build_argv(job, self.run_dir, {"append_system_prompt_file": False})
        shown = ClaudeCodeEngine.describe_argv(argv)
        self.assertEqual(len(shown), len(argv))
        joined = " ".join(shown)
        self.assertNotIn("SECRET PROMPT TEXT", joined)
        self.assertNotIn("SECRET SYSTEM", joined)
        self.assertEqual(shown[shown.index("-p") + 1], "<text>")
        self.assertEqual(shown[shown.index("--append-system-prompt") + 1], "<text>")
        self.assertIn("--model", shown)
        # stdin mode: nothing after -p to hide
        long_argv = self.engine.build_argv(make_job(self.ws, prompt="z" * (PROMPT_ARGV_LIMIT + 5)), self.run_dir)
        self.assertEqual(ClaudeCodeEngine.describe_argv(long_argv), long_argv)

    def test_build_settings(self):
        settings = self.engine.build_settings(make_job(self.ws))
        json.dumps(settings)  # serialisable
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "Write|Edit|MultiEdit|NotebookEdit|Bash")
        hook = pre[0]["hooks"][0]
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], HOOK_TIMEOUT_S)
        self.assertEqual(hook["timeout"], 300)
        cmd = hook["command"]
        python = sys.executable.replace("\\", "/")
        hook_path = (self.home / "bin" / "kyora_hook.py").as_posix()
        self.assertEqual(cmd, f'"{python}" "{hook_path}"')
        self.assertNotIn("\\", cmd)
        self.assertEqual(settings["permissions"], {"deny": ["WebFetch", "WebSearch"]})

    def test_settings_matcher_follows_judge_tools(self):
        eng = ClaudeCodeEngine(make_cfg(["claude"], judge_tools=["Bash", "Write"]), self.home, None)
        self.assertEqual(eng.build_settings(make_job(self.ws))["hooks"]["PreToolUse"][0]["matcher"], "Bash|Write")
        eng = ClaudeCodeEngine(make_cfg(["claude"], judge_tools=[]), self.home, None)
        self.assertEqual(eng.build_settings(make_job(self.ws))["hooks"]["PreToolUse"][0]["matcher"],
                         "Write|Edit|MultiEdit|NotebookEdit|Bash")

    def test_install_hook_copies_packaged_script(self):
        path = self.engine.install_hook()
        self.assertEqual(path, self.home / "bin" / "kyora_hook.py")
        self.assertEqual(path.read_bytes(), ClaudeCodeEngine.hook_source())
        if os.name != "nt":
            self.assertTrue(os.access(path, os.X_OK))
        mtime = path.stat().st_mtime_ns
        time.sleep(0.01)
        self.engine.install_hook()  # unchanged content: not rewritten
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        path.write_text("stale", encoding="utf-8")
        self.engine.install_hook()
        self.assertEqual(path.read_bytes(), ClaudeCodeEngine.hook_source())

    def test_resolve_run_dir(self):
        job = make_job(self.ws)
        self.assertEqual(self.engine.resolve_run_dir(job), self.home / "projects" / "p_test" / "runs" / "r_test")
        job = make_job(self.ws, project_id=None)
        self.assertEqual(self.engine.resolve_run_dir(job), self.home / "runs" / "r_test")
        job = make_job(self.ws, judge_context={"run_dir": str(self.tmp / "custom")})
        self.assertEqual(self.engine.resolve_run_dir(job), self.tmp / "custom")


# =========================================================================== pure decisions
class RetryDecisionTests(unittest.TestCase):
    def test_compat_retry_decision(self):
        d = ClaudeCodeEngine.compat_retry_decision("error: unknown option '--max-turns'\n", None)
        self.assertEqual(d["compat"], {"max_turns": False})
        self.assertIn("--max-turns", d["reason"])
        self.assertIn("dropped --max-turns", d["argv_change"])
        d = ClaudeCodeEngine.compat_retry_decision("error: unknown option '--append-system-prompt-file'", None)
        self.assertEqual(d["compat"], {"append_system_prompt_file": False})
        self.assertIn("--append-system-prompt <text>", d["argv_change"])
        d = ClaudeCodeEngine.compat_retry_decision("Error: Unknown option --permission-prompts", None)
        self.assertEqual(d["compat"], {"permission_prompts": False})
        # already disabled flags are not retried again
        self.assertIsNone(ClaudeCodeEngine.compat_retry_decision("error: unknown option '--max-turns'", {"max_turns": False}))
        # first still-enabled flag wins when several are reported
        d = ClaudeCodeEngine.compat_retry_decision(
            "unknown option '--max-turns'\nunknown option '--append-system-prompt-file'", {"max_turns": False})
        self.assertEqual(d["compat"], {"append_system_prompt_file": False})
        d = ClaudeCodeEngine.compat_retry_decision("error: unknown option '--tools'", None)
        self.assertEqual(d["compat"], {"tools": False})
        d = ClaudeCodeEngine.compat_retry_decision("error: unknown option '--strict-mcp-config'", None)
        self.assertEqual(d["compat"], {"strict_mcp_config": False})
        self.assertIsNone(ClaudeCodeEngine.compat_retry_decision("error: unknown option '--frobnicate'", None))
        self.assertIsNone(ClaudeCodeEngine.compat_retry_decision("", None))
        self.assertIsNone(ClaudeCodeEngine.compat_retry_decision(None, None))  # type: ignore[arg-type]

    def test_model_retry_decision(self):
        models = ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"]
        f = ClaudeCodeEngine.model_retry_decision
        self.assertEqual(f("API Error: model claude-fable-5-1 is restricted", False, 0, models), 1)
        self.assertEqual(f("Error: model 'claude-opus-5-5' not found", False, 1, models), 2)
        self.assertEqual(f("The model claude-x is not available for your account", False, 0, models), 1)
        self.assertIsNone(f("model restricted", False, 2, models))  # no model left
        self.assertIsNone(f("model restricted", True, 0, models))  # after init the CLI's own fallback applies
        self.assertIsNone(f("network unreachable", False, 0, models))
        self.assertIsNone(f("fatal: file not found", False, 0, models))  # availability words without "model"
        self.assertIsNone(f("", False, 0, models))


# =========================================================================== _build_result
class BuildResultTests(TempHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine = ClaudeCodeEngine(make_cfg(["claude"]), self.home, None)
        self.job = make_job(self.ws)
        self.run_dir = self.tmp / "run"

    def build(self, state: dict, attempt: _Attempt) -> object:
        return self.engine._build_result(self.job, state, attempt, time.monotonic() - 2.0, 1, [], {"max_turns": True},
                                         self.run_dir)

    def state(self, result: dict | None = None, active: str | None = None) -> dict:
        st = ClaudeCodeEngine.new_parse_state(self.job.model, 10)
        st["result"] = result
        st["active_model"] = active
        st["model_messages"] = 1 if active else 0  # a real model answered
        st["session_id"] = "sess"
        return st

    def test_success(self):
        att = _Attempt()
        att.rc = 0
        res = self.build(self.state({"subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 1500,
                                     "total_cost_usd": 0.5, "result": "all good", "permission_denials": [{"tool_name": "Bash"}],
                                     "modelUsage": {"claude-opus-5-5": {}}, "duration_api_ms": 1200}, "claude-opus-5-5"), att)
        self.assertTrue(res.ok)
        self.assertIsNone(res.error)
        self.assertEqual(res.backend, "claude_code")
        self.assertEqual(res.requested_model, "claude-fable-5-1")
        self.assertEqual(res.active_model, "claude-opus-5-5")
        self.assertTrue(res.fallback_used)
        self.assertEqual(res.session_id, "sess")
        self.assertEqual(res.num_turns, 4)
        self.assertEqual(res.duration_ms, 1500)
        self.assertEqual(res.cost_usd, 0.5)
        self.assertEqual(res.result_text, "all good")
        self.assertEqual(res.permission_denials, [{"tool_name": "Bash"}])
        self.assertEqual(res.raw["rc"], 0)
        self.assertEqual(res.raw["duration_api_ms"], 1200)
        self.assertEqual(res.raw["stream"], (self.run_dir / "engine" / "stream.jsonl").as_posix())
        json.dumps(res.to_dict())

    def test_init_model_that_never_served_is_not_active(self):
        att = _Attempt()
        att.rc = 1
        st = self.state({"subtype": "success", "is_error": True, "result": "Switch to another model to continue.",
                         "modelUsage": {}})
        st["active_model"] = "claude-fable-5-1"  # named by the init line only
        res = self.build(st, att)
        self.assertIsNone(res.active_model)
        self.assertFalse(res.fallback_used)
        st["tool_uses"] = 1  # a tool call proves the model served
        self.assertEqual(self.build(st, att).active_model, "claude-fable-5-1")

    def test_active_model_from_model_usage_when_stream_had_none(self):
        att = _Attempt()
        att.rc = 0
        res = self.build(self.state({"subtype": "success", "is_error": False,
                                     "modelUsage": {"claude-haiku-4-5": {"outputTokens": 3},
                                                    "claude-fable-5-1": {"outputTokens": 1}}}), att)
        self.assertEqual(res.active_model, "claude-fable-5-1")
        self.assertFalse(res.fallback_used)

    def test_error_paths(self):
        att = _Attempt()
        att.cancelled = True
        res = self.build(self.state({"subtype": "success", "is_error": False}), att)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "cancelled")

        att = _Attempt()
        att.timeout_reason = "idle timeout: no output for 901 s"
        self.assertEqual(self.build(self.state(), att).error, "idle timeout: no output for 901 s")

        att = _Attempt()
        att.spawn_error = "cannot start claude: [Errno 2]"
        att.rc = -1
        self.assertEqual(self.build(self.state(), att).error, "cannot start claude: [Errno 2]")

        att = _Attempt()
        att.rc = 1
        res = self.build(self.state({"subtype": "error_during_execution", "is_error": True, "result": "API Error: 500"}), att)
        self.assertEqual(res.error, "error_during_execution: API Error: 500")

        att = _Attempt()
        att.rc = 1
        att.stderr_lines.append("boom on stderr\n")
        res = self.build(self.state({"subtype": "error_max_turns", "is_error": True}), att)
        self.assertEqual(res.error, "error_max_turns: boom on stderr")

        att = _Attempt()
        att.rc = 2
        att.stderr_lines.append("only stderr\n")
        self.assertEqual(self.build(self.state(), att).error, "exit code 2: only stderr")

        att = _Attempt()
        att.rc = 1
        self.assertEqual(self.build(self.state({"subtype": "error_max_turns", "is_error": True}), att).error, "error_max_turns")

        att = _Attempt()
        att.rc = 0
        res = self.build(self.state(), att)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "claude exited with code 0 without a result line")
        self.assertGreaterEqual(res.duration_ms, 1900)  # measured wall time when no result line
        self.assertFalse(res.fallback_used)
        self.assertIsNone(res.active_model)

        # success subtype but is_error (e.g. API error surfaced as the final message) is not ok
        att = _Attempt()
        att.rc = 1
        res = self.build(self.state({"subtype": "success", "is_error": True, "result": "API Error: overloaded"}), att)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "error: API Error: overloaded")
        res = self.build(self.state({"subtype": "success", "is_error": True, "terminal_reason": "api_error",
                                     "api_error_status": 429, "api_error_code": "credits_required",
                                     "result": "You're out of usage credits. Switch to another model to continue."}), att)
        self.assertEqual(res.error, "api_error (HTTP 429): You're out of usage credits. Switch to another model to continue.")
        self.assertEqual(res.raw["api_error"], {"api_error_status": 429, "api_error_code": "credits_required",
                                                "terminal_reason": "api_error"})


# =========================================================================== run() with a fake CLI
class FakeJudge:
    """Records the engine's judge calls (test double for JudgeService)."""

    def __init__(self, verdict_ids: set[str] | None = None) -> None:
        self.contexts: dict[str, dict] = {}
        self.forgotten: list[str] = []
        self.verdict_ids = verdict_ids or set()
        self.unavailable: list[str] = []

    def register_context(self, run_id: str, ctx: dict) -> str:
        self.contexts[run_id] = ctx
        return "tok-123"

    def forget(self, run_id: str) -> None:
        self.forgotten.append(run_id)

    def has_verdict(self, run_id: str, tool_use_id: str) -> bool:
        return tool_use_id in self.verdict_ids

    def record_unavailable(self, run_id: str, *, allowed: bool = True) -> None:
        self.unavailable.append(run_id)


class RunWithFakeCLITests(TempHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.tmp / "fake_claude.py"
        self.fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.log = self.tmp / "fake_log.jsonl"

    def engine(self, app=None, **cfg_over) -> ClaudeCodeEngine:
        app = app if app is not None else SimpleNamespace(server_url="", judge=None)
        return ClaudeCodeEngine(make_cfg([PY, str(self.fake)], **cfg_over), self.home, app)

    def job(self, mode: str = "ok", **over) -> EngineJob:
        env = {"FAKE_MODE": mode, "FAKE_LOG": str(self.log)}
        env.update(over.pop("env", {}))
        return make_job(self.ws, env=env, **over)

    def invocations(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def run_job(self, eng: ClaudeCodeEngine, job: EngineJob, cancel: threading.Event | None = None):
        sink = ListSink()
        res = eng.run(job, sink, cancel or threading.Event())
        return res, sink

    def test_success_run(self):
        eng = self.engine()
        # a parent Claude Code session's markers must not leak into the child; auth/config dirs must
        parent = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent-session", "CLAUDE_EFFORT": "low",
                  "CLAUDE_CODE_ENTRYPOINT": "remote", "CLAUDE_CONFIG_DIR": str(self.tmp / "claude-config")}
        saved = {k: os.environ.get(k) for k in parent}
        os.environ.update(parent)
        try:
            res, sink = self.run_job(eng, self.job("ok", model="claude-opus-5-5"))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.active_model, "claude-opus-5-5")
        self.assertFalse(res.fallback_used)
        self.assertEqual(res.session_id, "fake-session-1")
        self.assertEqual(res.num_turns, 2)
        self.assertEqual(res.duration_ms, 42)
        self.assertEqual(res.cost_usd, 0.0123)
        self.assertEqual(res.result_text, "done; prompt_len=14")
        self.assertEqual(len(res.permission_denials), 1)
        self.assertEqual(res.raw["attempts"], 1)
        self.assertEqual(res.raw["retries"], [])
        self.assertEqual(res.raw["malformed_lines"], 1)
        self.assertEqual(res.raw["tool_uses"], 1)
        types = sink.types()
        self.assertEqual(types[0], "judge.unavailable")  # app.judge is None: visible, never silent
        self.assertIn("not configured", sink.of("judge.unavailable")[0]["reason"])
        self.assertNotIn("ran_unjudged", sink.of("judge.unavailable")[0])  # a notice, not a counted call
        for t in ("engine.session", "engine.model_resolved", "engine.text", "engine.tool_use", "stage.progress",
                  "engine.tool_result", "engine.result", "engine.stderr"):
            self.assertIn(t, types)
        self.assertIn("warning: fake stderr line", [d["line"] for d in sink.of("engine.stderr")])
        # files of the run
        run_dir = self.home / "projects" / "p_test" / "runs" / "r_test"
        self.assertEqual((run_dir / "prompt.txt").read_text(encoding="utf-8"), "Build the game")
        self.assertEqual((run_dir / "system_prompt.md").read_text(encoding="utf-8"), "You are Kyora's engine.")
        settings = json.loads((run_dir / "claude_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"]["deny"], ["WebFetch", "WebSearch"])
        self.assertTrue((self.home / "bin" / "kyora_hook.py").is_file())
        stream = (run_dir / "engine" / "stream.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(stream[0])
        self.assertEqual(first["type"], "kyora.attempt")
        self.assertNotIn("Build the game", json.dumps(first))  # prompt text never logged in argv
        self.assertTrue(any('"subtype": "init"' in ln for ln in stream))
        self.assertTrue(any('"type": "result"' in ln for ln in stream))
        self.assertIn("warning: fake stderr line", (run_dir / "engine" / "stderr.log").read_text(encoding="utf-8"))
        # what the child saw
        inv = self.invocations()
        self.assertEqual(len(inv), 1)
        argv = inv[0]["argv"]
        self.assertEqual(argv[:2], ["-p", "Build the game"])
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "12")
        self.assertEqual(argv[argv.index("--settings") + 1], (run_dir / "claude_settings.json").as_posix())
        self.assertEqual(Path(inv[0]["cwd"]).resolve(), self.ws.resolve())
        self.assertIsNone(inv[0]["stdin_len"])
        env = inv[0]["env"]
        for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.tmp / "claude-config"))
        self.assertEqual(env["KYORA_RUN_ID"], "r_test")
        self.assertEqual(env["KYORA_JUDGE_URL"], "")
        self.assertEqual(env["KYORA_HOME"], str(self.home))
        self.assertTrue((self.ws / "out.txt").is_file())

    def test_judge_wiring_and_unjudged_tool_detection(self):
        judge = FakeJudge()
        app = SimpleNamespace(server_url="http://127.0.0.1:8765/", judge=judge)
        res, sink = self.run_job(self.engine(app), self.job("ok", judge_context={"files": ["out.txt"]}))
        self.assertTrue(res.ok, res.error)
        ctx = judge.contexts["r_test"]
        self.assertEqual(ctx["workspace"], str(self.ws))
        self.assertEqual(ctx["project_id"], "p_test")
        self.assertEqual(ctx["kind"], "build")
        self.assertEqual(ctx["files"], ["out.txt"])
        self.assertTrue(ctx["run_dir"].endswith("r_test"))
        self.assertEqual(judge.forgotten, ["r_test"])
        env = self.invocations()[0]["env"]
        self.assertEqual(env["KYORA_JUDGE_URL"], "http://127.0.0.1:8765/api/judge")
        self.assertEqual(env["KYORA_HOOK_TOKEN"], "tok-123")
        unav = sink.of("judge.unavailable")
        self.assertEqual(len(unav), 1)  # the Write ran without a journaled verdict
        self.assertEqual(unav[0]["tool_use_id"], "toolu_A")
        self.assertEqual(unav[0]["tool_name"], "Write")
        self.assertIs(unav[0]["ran_unjudged"], True)  # the UI counts it allowed + unavailable, like the server
        # the unjudged call is also counted on the run (honesty-run-judge-stats-miss-unjudged-calls)
        self.assertEqual(judge.unavailable, ["r_test"])
        # with a verdict on record nothing is flagged
        self.log.unlink()
        judge2 = FakeJudge(verdict_ids={"toolu_A"})
        res, sink = self.run_job(self.engine(SimpleNamespace(server_url="http://127.0.0.1:1", judge=judge2)), self.job("ok"))
        self.assertTrue(res.ok)
        self.assertEqual(sink.of("judge.unavailable"), [])
        self.assertEqual(judge2.unavailable, [])  # a judged call is never counted unavailable

    def test_judge_without_server_url_is_reported(self):
        judge = FakeJudge(verdict_ids={"toolu_A"})
        res, sink = self.run_job(self.engine(SimpleNamespace(server_url="", judge=judge)), self.job("ok"))
        self.assertTrue(res.ok)
        reasons = [d["reason"] for d in sink.of("judge.unavailable")]
        self.assertEqual(len(reasons), 1)
        self.assertIn("server URL unknown", reasons[0])
        self.assertNotIn("ran_unjudged", sink.of("judge.unavailable")[0])

    def test_fallback_model_served(self):
        res, sink = self.run_job(self.engine(), self.job("ok", env={"FAKE_SERVED_MODEL": "claude-opus-5-5"}))
        self.assertTrue(res.ok)
        self.assertEqual(res.active_model, "claude-opus-5-5")
        self.assertTrue(res.fallback_used)
        self.assertEqual(sink.of("engine.model_resolved"), [
            {"requested": "claude-fable-5-1", "active": "claude-fable-5-1", "fallback_used": False},
            {"requested": "claude-fable-5-1", "active": "claude-opus-5-5", "fallback_used": True}])

    def test_compat_retry_drops_max_turns(self):
        res, sink = self.run_job(self.engine(), self.job("compat"))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()
        self.assertEqual(len(inv), 2)
        self.assertIn("--max-turns", inv[0]["argv"])
        self.assertNotIn("--max-turns", inv[1]["argv"])
        self.assertIn("--append-system-prompt-file", inv[1]["argv"])
        retries = sink.of("engine.compat_retry")
        self.assertEqual(len(retries), 1)
        self.assertIn("--max-turns", retries[0]["reason"])
        self.assertIn("argv_change", retries[0])
        self.assertEqual(res.raw["attempts"], 2)
        self.assertFalse(res.raw["compat"]["max_turns"])
        self.assertEqual(len(res.raw["retries"]), 1)
        stream = (self.home / "projects" / "p_test" / "runs" / "r_test" / "engine" / "stream.jsonl").read_text(encoding="utf-8")
        self.assertEqual(stream.count('"type": "kyora.attempt"'), 2)

    def test_compat_retry_two_hidden_flags(self):
        res, sink = self.run_job(self.engine(), self.job("compat2"))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()
        self.assertEqual(len(inv), 3)
        last = inv[2]["argv"]
        self.assertNotIn("--max-turns", last)
        self.assertNotIn("--append-system-prompt-file", last)
        self.assertEqual(last[last.index("--append-system-prompt") + 1], "You are Kyora's engine.")
        self.assertEqual(len(sink.of("engine.compat_retry")), 2)

    def test_model_retry_before_init(self):
        res, sink = self.run_job(self.engine(), self.job("model", model="bad-model", fallback_models=["good-model"]))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()
        self.assertEqual(len(inv), 2)
        self.assertEqual(inv[0]["argv"][inv[0]["argv"].index("--model") + 1], "bad-model")
        self.assertEqual(inv[0]["argv"][inv[0]["argv"].index("--fallback-model") + 1], "good-model")
        self.assertEqual(inv[1]["argv"][inv[1]["argv"].index("--model") + 1], "good-model")
        self.assertNotIn("--fallback-model", inv[1]["argv"])
        self.assertEqual(res.requested_model, "bad-model")
        self.assertEqual(res.active_model, "good-model")
        self.assertTrue(res.fallback_used)
        retry = sink.of("engine.compat_retry")[0]
        self.assertEqual(retry["argv_change"], "--model good-model")
        self.assertIn("restricted", retry["reason"])

    def test_model_refused_after_init_switches_to_next_model(self):
        res, sink = self.run_job(self.engine(), self.job("credits", model="credit-model",
                                                          fallback_models=["good-model", "other-model"]))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()
        self.assertEqual(len(inv), 2)
        a0, a1 = inv[0]["argv"], inv[1]["argv"]
        self.assertEqual(a0[a0.index("--model") + 1], "credit-model")
        self.assertEqual(a1[a1.index("--model") + 1], "good-model")
        self.assertEqual(a1[a1.index("--fallback-model") + 1], "other-model")
        retry = sink.of("engine.compat_retry")
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["argv_change"], "--model good-model")
        self.assertIn("refused before any work", retry[0]["reason"])
        self.assertIn("credits_required", retry[0]["reason"])
        self.assertEqual(res.active_model, "good-model")
        self.assertTrue(res.fallback_used)
        self.assertEqual(res.raw["attempts"], 2)
        resolved = sink.of("engine.model_resolved")
        self.assertEqual(resolved[0], {"requested": "credit-model", "active": "credit-model", "fallback_used": False})
        self.assertEqual(resolved[-1], {"requested": "credit-model", "active": "good-model", "fallback_used": True})
        self.assertIn("You're out of usage credits. Switch to another model to continue.",
                      [d["text"] for d in sink.of("engine.text")])  # the CLI's real message stays visible

    def test_model_refused_after_init_without_fallback(self):
        res, sink = self.run_job(self.engine(), self.job("credits", model="credit-model", fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertIsNone(res.active_model)  # named in init, but it never served
        self.assertFalse(res.fallback_used)
        self.assertEqual(res.error, "api_error (HTTP 429): You're out of usage credits. Switch to another model to continue.")
        self.assertEqual(res.raw["rate_limits"], [{"status": "rejected", "errorCode": "credits_required"}])
        self.assertEqual(len(self.invocations()), 1)

    def test_replay_of_the_real_smoke_run(self):
        """The real CLI output captured by the smoke run drives run() to the outcome observed there.

        Primary model: init, ``rate_limit_event`` rejected, ``<synthetic>`` "Switch to another model",
        is_error result (HTTP 429 credits_required). Next model: a served session in which the hook's
        exit-2 block of ``forbidden.txt`` came back as an error tool_result.
        """
        table = {"claude-fable-5-1": [str(FIXTURES / "real_credits_refusal.jsonl"), 1],
                 "claude-opus-5-5": [str(FIXTURES / "real_session.jsonl"), 0]}
        judge = FakeJudge(verdict_ids={"toolu_01LGVDVyZiY2JTxfs8iy7DhS", "toolu_01SDxR4nbD8qZZdL8wR6nFfE"})
        app = SimpleNamespace(server_url="http://127.0.0.1:1", judge=judge)
        res, sink = self.run_job(self.engine(app), self.job("replay", max_turns=8,
                                                             env={"FAKE_REPLAY": json.dumps(table)}))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.requested_model, "claude-fable-5-1")
        self.assertEqual(res.active_model, "claude-opus-5-5")
        self.assertTrue(res.fallback_used)
        self.assertEqual(res.session_id, "d32fed4d-5f3a-4d3e-91b6-0a20bef58fe0")
        self.assertEqual((res.num_turns, res.duration_ms), (3, 9253))
        self.assertAlmostEqual(res.cost_usd, 0.0730166)
        self.assertEqual(res.raw["attempts"], 2)
        self.assertEqual(res.raw["model_usage"]["claude-opus-5-5"]["outputTokens"], 570)
        self.assertEqual(res.permission_denials[0]["tool_input"]["file_path"], "/WORKSPACE/forbidden.txt")
        self.assertIn("forbidden.txt", res.result_text)
        argvs = [i["argv"] for i in self.invocations()]
        self.assertEqual([(a[a.index("--model") + 1], a[a.index("--fallback-model") + 1]) for a in argvs],
                         [("claude-fable-5-1", "claude-opus-5-5,claude-sonnet-5-5"),
                          ("claude-opus-5-5", "claude-sonnet-5-5")])
        retry = sink.of("engine.compat_retry")
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["argv_change"], "--model claude-opus-5-5")
        self.assertIn("HTTP 429, model_requires_usage_credits, credits_required", retry[0]["reason"])
        self.assertEqual(sink.of("engine.model_resolved"), [
            {"requested": "claude-fable-5-1", "active": "claude-fable-5-1", "fallback_used": False},
            {"requested": "claude-fable-5-1", "active": "claude-opus-5-5", "fallback_used": True}])
        blocked = [r for r in sink.of("engine.tool_result") if r["is_error"]]
        self.assertEqual(len(blocked), 1)
        self.assertIn("Kyora judge (rule) denied Write", blocked[0]["excerpt"])
        self.assertEqual(sink.of("judge.unavailable"), [])  # both Write calls have a journaled verdict
        self.assertEqual(judge.forgotten, ["r_test"])
        self.assertEqual(len(sink.of("engine.session")), 2)

    def test_auth_error_after_init_does_not_switch_model(self):
        res, sink = self.run_job(self.engine(), self.job("auth401"))
        self.assertFalse(res.ok)
        self.assertIn("HTTP 401", res.error)
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(sink.of("engine.compat_retry"), [])

    def test_model_error_on_last_model_is_reported(self):
        res, sink = self.run_job(self.engine(), self.job("model", model="bad-model", fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertIn("restricted", res.error)
        self.assertEqual(len(self.invocations()), 1)

    def test_unrelated_crash_is_not_retried(self):
        res, sink = self.run_job(self.engine(), self.job("crash"))
        self.assertFalse(res.ok)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("file not found", res.error)
        self.assertEqual(res.raw["rc"], 3)
        self.assertEqual(sink.of("engine.compat_retry"), [])

    def test_error_max_turns_result(self):
        res, sink = self.run_job(self.engine(), self.job("max_turns"))
        self.assertFalse(res.ok)
        self.assertTrue(res.error.startswith("error_max_turns"), res.error)
        self.assertEqual(res.num_turns, 2)

    def test_prompt_via_stdin(self):
        prompt = "p" * (PROMPT_ARGV_LIMIT + 10)
        res, _ = self.run_job(self.engine(), self.job("ok", prompt=prompt))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()[0]
        self.assertEqual(inv["stdin_len"], len(prompt))
        self.assertNotIn(prompt, inv["argv"])
        self.assertEqual(res.result_text, f"done; prompt_len={len(prompt)}")

    @unittest.skipIf(os.name == "nt", "a POSIX script named claude.cmd stands in for the npm shim")
    def test_cmd_shim_gets_multiline_prompt_on_stdin(self):
        shim = self.tmp / "npm" / "claude.cmd"
        shim.parent.mkdir()
        shim.write_text(f'#!/bin/sh\nexec "{PY}" "{self.fake}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        prompt = 'Build the game "Sky Runner" in this directory.\n\nTom & Jerry chase at 100% speed.'
        eng = ClaudeCodeEngine(make_cfg([str(shim)]), self.home, SimpleNamespace(server_url="", judge=None))
        res, _ = self.run_job(eng, self.job("ok", prompt=prompt))
        self.assertTrue(res.ok, res.error)
        inv = self.invocations()[0]
        self.assertEqual(inv["stdin_len"], len(prompt))
        self.assertNotIn(prompt, inv["argv"])
        self.assertEqual(inv["argv"][:2], ["-p", "--model"])
        self.assertIn("--settings", inv["argv"])
        # an argument cmd.exe would mangle is refused before anything starts
        bad = ClaudeCodeEngine(make_cfg([str(shim)], extra_args=["--add-dir", "C:/Tom&Jerry"]), self.home,
                               SimpleNamespace(server_url="", judge=None))
        res, sink = self.run_job(bad, self.job("ok", prompt=prompt))
        self.assertFalse(res.ok)
        self.assertIn("cmd.exe", res.error)
        self.assertIn("C:/Tom&Jerry", res.error)
        self.assertEqual(len(self.invocations()), 1)

    def test_cancel(self):
        eng = self.engine()
        cancel = threading.Event()
        sink = ListSink()
        out: dict = {}

        def target():
            out["res"] = eng.run(self.job("sleep"), sink, cancel)

        t = threading.Thread(target=target)
        started = time.monotonic()
        t.start()
        self.assertTrue(sink.session_seen.wait(20), "fake CLI never printed init")
        cancel.set()
        t.join(30)
        self.assertFalse(t.is_alive())
        res = out["res"]
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "cancelled")
        self.assertEqual(res.session_id, "fake-session-1")
        self.assertLess(time.monotonic() - started, 25)

    def test_idle_timeout(self):
        started = time.monotonic()
        res, sink = self.run_job(self.engine(idle_timeout_s=1), self.job("idle"))
        self.assertFalse(res.ok)
        self.assertIn("idle timeout", res.error)
        self.assertIn("idle_timeout_s=1", res.error)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(any("idle timeout" in d["error"] for d in sink.of("engine.error")))

    def test_wall_timeout(self):
        res, sink = self.run_job(self.engine(timeout_s=1, idle_timeout_s=0), self.job("wall"))
        self.assertFalse(res.ok)
        self.assertIn("wall timeout", res.error)
        self.assertGreater(len(sink.of("engine.text")), 0)

    def test_probe_model_follows_the_chain(self):
        eng = self.engine()
        os.environ["FAKE_MODE"] = "credits"
        os.environ["FAKE_LOG"] = str(self.log)
        try:
            info = eng.probe_model("credit-model", ["good-model"])
        finally:
            os.environ.pop("FAKE_MODE", None)
            os.environ.pop("FAKE_LOG", None)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["requested"], "credit-model")
        self.assertEqual(info["active"], "good-model")
        self.assertTrue(info["fallback_used"])
        self.assertEqual([r["model"] for r in info["refused"]], ["credit-model"])
        self.assertIn("HTTP 429", info["refused"][0]["reason"])
        argvs = [i["argv"] for i in self.invocations()]
        self.assertEqual(len(argvs), 2)
        self.assertEqual(argvs[0][argvs[0].index("--output-format") + 1], "json")
        self.assertEqual(argvs[0][argvs[0].index("--max-turns") + 1], "1")
        self.assertEqual(argvs[1][argvs[1].index("--model") + 1], "good-model")

    def test_probe_model_direct_success(self):
        os.environ["FAKE_MODE"] = "ok"
        try:
            info = self.engine().probe_model("claude-opus-5-5", [])
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["active"], "claude-opus-5-5")
        self.assertFalse(info["fallback_used"])
        self.assertEqual(info["refused"], [])

    def test_missing_cli(self):
        eng = ClaudeCodeEngine(make_cfg([str(self.tmp / "no-such-claude")]), self.home,
                               SimpleNamespace(server_url="", judge=None))
        res, sink = self.run_job(eng, self.job("ok"))
        self.assertFalse(res.ok)
        self.assertIn("not found", res.error)
        self.assertTrue(sink.of("engine.error"))
        avail = eng.availability()
        self.assertFalse(avail.available)
        self.assertIn("not found", avail.reason)


if __name__ == "__main__":
    unittest.main()
