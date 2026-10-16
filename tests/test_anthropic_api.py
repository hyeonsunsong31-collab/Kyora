"""Tests of ``kyora.models.anthropic_api`` (optional Messages-API code engine).

No network and no real SDK: a fake ``anthropic`` module is injected into
``sys.modules``; its ``Anthropic(api_key=...).beta.messages.stream(...)``
context manager returns scripted final messages and it exposes the SDK's typed
error classes. Run: ``python3 -m unittest tests.test_anthropic_api -v``
"""
from __future__ import annotations

import importlib.machinery
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora.config import ClaudeCodeCfg
from kyora.models.anthropic_api import (
    BETAS, NO_KEY_REASON, NO_PACKAGE_REASON, AnthropicAPIEngine, EditorError, confine, scrubbed_env,
)
from kyora.models.base import EngineJob


# --------------------------------------------------------------------------- fake SDK
class FakeAPIStatusError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FakeNotFoundError(FakeAPIStatusError):
    def __init__(self, message: str):
        super().__init__(message, 404)


class FakeRateLimitError(FakeAPIStatusError):
    def __init__(self, message: str):
        super().__init__(message, 429)


class FakeAPIConnectionError(Exception):
    def __init__(self, message: str = "Connection error."):
        super().__init__(message)
        self.message = message


class FakeStream:
    def __init__(self, final):
        self.final = final
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        return iter([SimpleNamespace(type="message_start"), SimpleNamespace(type="message_stop")])

    def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kw):
        rec = dict(kw)
        rec["messages"] = list(kw.get("messages") or [])  # snapshot: the engine appends to its list later
        self.owner.calls.append(rec)
        if not self.owner.script:
            raise AssertionError("fake anthropic: no scripted response left")
        item = self.owner.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeStream(item)


class FakeSDK:
    """Holds the script and the recorded calls; builds the fake ``anthropic`` module."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []
        self.clients: list[dict] = []
        sdk = self

        class Anthropic:
            def __init__(self, api_key=None, **kw):
                sdk.clients.append({"api_key": api_key, **kw})
                self.beta = SimpleNamespace(messages=FakeMessages(sdk))

        mod = types.ModuleType("anthropic")
        mod.__spec__ = importlib.machinery.ModuleSpec("anthropic", None)
        mod.Anthropic = Anthropic
        mod.APIStatusError = FakeAPIStatusError
        mod.NotFoundError = FakeNotFoundError
        mod.RateLimitError = FakeRateLimitError
        mod.APIConnectionError = FakeAPIConnectionError
        self.module = mod


def block(type_: str, **kw):
    return SimpleNamespace(type=type_, **kw)


def final(stop: str, content: list, model: str = "claude-fable-5-1", usage=(10, 5), stop_details=None, iterations=None):
    return SimpleNamespace(id="msg_x", model=model, stop_reason=stop, stop_details=stop_details, content=content,
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1], iterations=iterations),
                           _request_id="req_test")


# --------------------------------------------------------------------------- fakes for kyora parts
class FakeJudge:
    """Denies writes (Write/Edit), allows everything else; records the calls."""

    def __init__(self, deny=("Write",), raise_on=()):
        self.deny = set(deny)
        self.raise_on = set(raise_on)
        self.contexts: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.forgotten: list[str] = []
        self.unavailable: list[str] = []
        self._lock = threading.Lock()

    def register_context(self, run_id, ctx):
        self.contexts.append((run_id, dict(ctx)))
        return "token"

    def decide(self, run_id, call):
        with self._lock:
            self.calls.append((run_id, dict(call)))
        if call["tool_name"] in self.raise_on:
            raise RuntimeError("judge boom")
        if call["tool_name"] in self.deny:
            return {"decision": "deny", "reason": "not in the plan", "source": "rule"}
        return {"decision": "allow", "reason": "ok", "source": "rule"}

    def record_unavailable(self, run_id, *, allowed=True):
        self.unavailable.append(run_id)

    def forget(self, run_id):
        self.forgotten.append(run_id)


class ListSink:
    def __init__(self):
        self.events = []

    def on_event(self, ev):
        self.events.append(ev)

    def of(self, type_):
        return [e.data for e in self.events if e.type == type_]


def cfg(**over) -> ClaudeCodeCfg:
    base = dict(command=["claude"], model="claude-fable-5-1", fallback_models=["claude-opus-5-5"], effort="max",
                max_turns=60, timeout_s=3600, idle_timeout_s=900, resume_for_fixes=True,
                judge_tools=["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"])
    base.update(over)
    return ClaudeCodeCfg(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "repo"
        self.ws.mkdir()
        (self.ws / "existing.txt").write_text("hello\n", encoding="utf-8")
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)
        self._old_mod = sys.modules.get("anthropic")

    def tearDown(self):
        if self._old_mod is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._old_mod
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self.tmp.cleanup()

    def install(self, script) -> FakeSDK:
        sdk = FakeSDK(script)
        sys.modules["anthropic"] = sdk.module
        return sdk

    def job(self, **over) -> EngineJob:
        base = dict(run_id="r_test", workspace=self.ws, prompt="Build the game", system_prompt="You build games.",
                    max_turns=10, model="claude-fable-5-1", fallback_models=["claude-opus-5-5"], effort="max",
                    judge_context={"run_dir": str(self.home / "runs" / "r_test"), "plan": {"files": []}},
                    project_id="p_test")
        base.update(over)
        return EngineJob(**base)

    def engine(self, judge=None, key="sk-ant-test") -> AnthropicAPIEngine:
        return AnthropicAPIEngine(key, cfg(), judge, self.home)


# --------------------------------------------------------------------------- availability
class AvailabilityTests(_Base):
    def test_no_key(self):
        av = self.engine(key="").availability()
        self.assertFalse(av.available)
        self.assertEqual(av.reason, NO_KEY_REASON)
        self.assertEqual(av.backend, "anthropic_api")

    def test_key_but_no_package(self):
        sys.modules.pop("anthropic", None)
        with mock.patch("importlib.util.find_spec", return_value=None):
            av = self.engine().availability()
        self.assertFalse(av.available)
        self.assertEqual(av.reason, NO_PACKAGE_REASON)

    def test_key_and_package(self):
        self.install([])
        av = self.engine().availability()
        self.assertTrue(av.available, av.reason)
        self.assertTrue(av.details["package_installed"])

    def test_module_without_spec_counts_as_installed(self):
        mod = types.ModuleType("anthropic")  # __spec__ is None -> find_spec raises ValueError
        sys.modules["anthropic"] = mod
        self.assertTrue(AnthropicAPIEngine.package_installed())

    def test_callable_key_is_read_each_time(self):
        box = {"key": ""}
        eng = AnthropicAPIEngine(lambda: box["key"], cfg(), None, self.home)
        self.install([])
        self.assertFalse(eng.availability().available)
        box["key"] = "sk-ant-later"
        self.assertTrue(eng.availability().available)

    def test_run_without_key_reports_error(self):
        sink = ListSink()
        res = self.engine(key="").run(self.job(), sink, threading.Event())
        self.assertFalse(res.ok)
        self.assertEqual(res.error, NO_KEY_REASON)
        self.assertEqual(sink.of("engine.error")[0]["error"], NO_KEY_REASON)


# --------------------------------------------------------------------------- agent loop
class LoopTests(_Base):
    def test_tool_turn_then_end_turn(self):
        turn1 = [
            block("text", text="I will create the file and list the workspace."),
            block("tool_use", id="toolu_1", name="str_replace_based_edit_tool",
                  input={"command": "create", "path": "src/a.js", "file_text": "console.log(1);\n"}),
            block("tool_use", id="toolu_2", name="bash", input={"command": "ls"}),
        ]
        turn2 = [block("text", text="Done.")]
        sdk = self.install([final("tool_use", turn1, usage=(100, 40)), final("end_turn", turn2, usage=(150, 10))])
        judge = FakeJudge(deny=("Write",))
        sink = ListSink()
        res = self.engine(judge).run(self.job(), sink, threading.Event())

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.backend, "anthropic_api")
        self.assertEqual(res.active_model, "claude-fable-5-1")
        self.assertFalse(res.fallback_used)
        self.assertEqual(res.num_turns, 2)
        self.assertIsNone(res.cost_usd)
        self.assertEqual(res.result_text, "Done.")
        self.assertEqual(res.raw["usage"]["input_tokens"], 250)
        self.assertEqual(res.raw["usage"]["output_tokens"], 50)
        self.assertEqual(len(res.permission_denials), 1)
        self.assertEqual(res.permission_denials[0]["tool_name"], "Write")

        # judge: context registered with the workspace, both calls judged in order, context forgotten
        self.assertEqual(judge.contexts[0][0], "r_test")
        self.assertEqual(Path(judge.contexts[0][1]["workspace"]), self.ws.resolve())
        self.assertEqual([c[1]["tool_name"] for c in judge.calls], ["Write", "Bash"])
        self.assertTrue(judge.calls[0][1]["tool_input"]["file_path"].endswith("/src/a.js"))
        self.assertEqual(judge.calls[0][1]["tool_use_id"], "toolu_1")
        self.assertEqual(judge.calls[1][1]["tool_input"], {"command": "ls"})
        self.assertEqual(judge.forgotten, ["r_test"])
        self.assertFalse((self.ws / "src" / "a.js").exists(), "a denied create must not write the file")

        # request shape (docs: scalar fallbacks + 2026-07-01 beta, effort via output_config, no thinking param)
        self.assertEqual(len(sdk.calls), 2)
        first = sdk.calls[0]
        self.assertEqual(sdk.clients[0]["api_key"], "sk-ant-test")
        self.assertEqual(first["model"], "claude-fable-5-1")
        self.assertEqual(first["max_tokens"], 64000)
        self.assertEqual(first["betas"], list(BETAS))
        self.assertEqual(first["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["output_config"], {"effort": "max"})
        self.assertNotIn("thinking", first)
        self.assertEqual([t["type"] for t in first["tools"]], ["bash_20250124", "text_editor_20250728"])
        self.assertEqual([t["name"] for t in first["tools"]], ["bash", "str_replace_based_edit_tool"])
        self.assertTrue(first["system"].startswith("You build games."))
        self.assertIn(str(self.ws.resolve()).replace("\\", "/"), first["system"])
        self.assertEqual(first["messages"], [{"role": "user", "content": "Build the game"}])

        # second request: assistant turn appended unchanged, then ALL tool results in ONE user message, in order
        msgs = sdk.calls[1]["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertIs(msgs[1]["content"], turn1)
        self.assertEqual(msgs[2]["role"], "user")
        results = msgs[2]["content"]
        self.assertEqual([r["type"] for r in results], ["tool_result", "tool_result"])
        self.assertEqual([r["tool_use_id"] for r in results], ["toolu_1", "toolu_2"])
        self.assertTrue(results[0]["is_error"])
        self.assertIn("Denied by the Kyora judge: not in the plan", results[0]["content"])
        self.assertFalse(results[1]["is_error"])
        self.assertIn("existing.txt", results[1]["content"])

        # events
        session = sink.of("engine.session")[0]
        self.assertEqual(session["backend"], "anthropic_api")
        self.assertEqual(session["model"], "claude-fable-5-1")
        resolved = sink.of("engine.model_resolved")
        self.assertEqual(resolved[0], {"requested": "claude-fable-5-1", "active": "claude-fable-5-1", "fallback_used": False})
        self.assertEqual([(e["id"], e["name"]) for e in sink.of("engine.tool_use")], [("toolu_1", "Write"), ("toolu_2", "Bash")])
        self.assertEqual(sink.of("engine.tool_use")[0]["summary"], "src/a.js")
        self.assertEqual([(e["tool_use_id"], e["is_error"]) for e in sink.of("engine.tool_result")],
                         [("toolu_1", True), ("toolu_2", False)])
        self.assertIn("Done.", [e["text"] for e in sink.of("engine.text")])
        result_ev = sink.of("engine.result")[-1]
        self.assertEqual(result_ev["subtype"], "success")
        self.assertFalse(result_ev["is_error"])
        self.assertEqual(result_ev["num_turns"], 2)
        self.assertIsNone(result_ev["cost_usd"])
        self.assertIn("duration_ms", result_ev)
        progress = sink.of("stage.progress")
        self.assertTrue(progress and all(p["stage"] == "build" for p in progress))
        self.assertEqual(progress[-1]["pct"], 100)
        # audit transcript written next to the run
        transcript = self.home / "runs" / "r_test" / "engine" / "api_turns.jsonl"
        lines = transcript.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["stop_reason"], "tool_use")

    def test_allowed_create_writes_inside_workspace(self):
        turn1 = [block("tool_use", id="t1", name="str_replace_based_edit_tool",
                       input={"command": "create", "path": "src/game.js", "file_text": "let x = 1;\n"})]
        self.install([final("tool_use", turn1), final("end_turn", [block("text", text="ok")])])
        judge = FakeJudge(deny=())
        res = self.engine(judge).run(self.job(), ListSink(), threading.Event())
        self.assertTrue(res.ok, res.error)
        self.assertEqual((self.ws / "src" / "game.js").read_text(encoding="utf-8"), "let x = 1;\n")
        self.assertEqual(res.permission_denials, [])

    def test_judge_failure_counts_unavailable_and_runs_unjudged(self):
        # Regression for honesty-run-judge-stats-miss-unjudged-calls: when the judge raises, the API engine
        # fails open (the tool runs) AND counts the call on the run's unavailable counter.
        turn1 = [block("tool_use", id="t1", name="bash", input={"command": "ls"})]
        self.install([final("tool_use", turn1), final("end_turn", [block("text", text="ok")])])
        judge = FakeJudge(deny=(), raise_on=("Bash",))
        sink = ListSink()
        res = self.engine(judge).run(self.job(), sink, threading.Event())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(judge.unavailable, ["r_test"])  # the unjudged call is counted
        unav = sink.of("judge.unavailable")
        self.assertTrue(any(d.get("tool_name") == "Bash" for d in unav))

    def test_escape_path_rejected_without_judging(self):
        turn1 = [block("tool_use", id="t1", name="str_replace_based_edit_tool",
                       input={"command": "create", "path": "../x", "file_text": "pwned"})]
        sdk = self.install([final("tool_use", turn1), final("end_turn", [block("text", text="ok")])])
        judge = FakeJudge(deny=())
        res = self.engine(judge).run(self.job(), ListSink(), threading.Event())
        self.assertTrue(res.ok)
        self.assertEqual(judge.calls, [])
        tr = sdk.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertIn("escapes the project workspace", tr["content"])
        self.assertFalse((self.root / "x").exists())

    def test_read_is_not_judged(self):
        turn1 = [block("tool_use", id="t1", name="str_replace_based_edit_tool",
                       input={"command": "view", "path": "existing.txt"})]
        sdk = self.install([final("tool_use", turn1), final("end_turn", [block("text", text="ok")])])
        judge = FakeJudge()
        self.engine(judge).run(self.job(), ListSink(), threading.Event())
        self.assertEqual(judge.calls, [])
        tr = sdk.calls[1]["messages"][-1]["content"][0]
        self.assertFalse(tr["is_error"])
        self.assertIn("1\thello", tr["content"])

    def test_refusal_stops_without_running_tools(self):
        content = [block("text", text="partial"),
                   block("tool_use", id="t1", name="bash", input={"command": "touch refused.txt"})]
        sdk = self.install([final("refusal", content, stop_details=SimpleNamespace(category="cyber", explanation="declined"))])
        judge = FakeJudge(deny=())
        sink = ListSink()
        res = self.engine(judge).run(self.job(), sink, threading.Event())
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "refusal:cyber")
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(judge.calls, [])
        self.assertFalse((self.ws / "refused.txt").exists())
        self.assertEqual(sink.of("engine.error")[0]["error"], "refusal:cyber")
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "refusal")
        self.assertEqual(sink.of("engine.tool_use"), [])
        self.assertEqual(sink.of("engine.text"), [], "a refused turn's partial output is discarded")

    def test_refusal_without_category(self):
        self.install([final("refusal", [], stop_details=None)])
        res = self.engine(FakeJudge()).run(self.job(), ListSink(), threading.Event())
        self.assertEqual(res.error, "refusal:unspecified")

    def test_max_tokens_without_tool_use_continues(self):
        sdk = self.install([final("max_tokens", [block("text", text="part 1")]),
                            final("end_turn", [block("text", text="part 2")])])
        res = self.engine(FakeJudge()).run(self.job(), ListSink(), threading.Event())
        self.assertTrue(res.ok)
        self.assertEqual(sdk.calls[1]["messages"][-1], {"role": "user", "content": "continue"})

    def test_max_tokens_inside_tool_use_is_an_error(self):
        content = [block("tool_use", id="t1", name="bash", input={"command": "ls"})]
        self.install([final("max_tokens", content)])
        judge = FakeJudge(deny=())
        res = self.engine(judge).run(self.job(), ListSink(), threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("max_tokens", res.error)
        self.assertEqual(judge.calls, [])

    def test_max_turns_bound(self):
        content = [block("tool_use", id="t1", name="bash", input={"command": "ls"})]
        self.install([final("tool_use", content)])
        res = self.engine(FakeJudge(deny=())).run(self.job(max_turns=1), ListSink(), threading.Event())
        self.assertFalse(res.ok)
        self.assertTrue(res.error.startswith("error_max_turns"))
        self.assertEqual(res.num_turns, 1)

    def test_cancel_before_first_turn(self):
        sdk = self.install([final("end_turn", [block("text", text="never")])])
        cancel = threading.Event()
        cancel.set()
        sink = ListSink()
        res = self.engine(FakeJudge()).run(self.job(), sink, cancel)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "cancelled")
        self.assertEqual(sdk.calls, [])
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "cancelled")

    def test_model_fallback_reported(self):
        iterations = [SimpleNamespace(type="message", input_tokens=5, output_tokens=0),
                      SimpleNamespace(type="fallback_message", input_tokens=7, output_tokens=3)]
        self.install([final("end_turn", [block("text", text="ok")], model="claude-opus-4-8", iterations=iterations)])
        sink = ListSink()
        res = self.engine(FakeJudge()).run(self.job(), sink, threading.Event())
        self.assertTrue(res.ok)
        self.assertEqual(res.active_model, "claude-opus-4-8")
        self.assertTrue(res.fallback_used)
        self.assertEqual(res.raw["usage"], {"input_tokens": 12, "output_tokens": 3})
        self.assertTrue(sink.of("engine.model_resolved")[0]["fallback_used"])

    def test_echo_content_after_mid_output_fallback(self):
        content = [block("thinking", thinking="", signature="s"), block("text", text="partial "),
                   block("tool_use", id="old", name="bash", input={"command": "ls"}),
                   block("fallback", from_=SimpleNamespace(model="claude-fable-5-1"), to=SimpleNamespace(model="claude-opus-4-8")),
                   block("text", text="continued"), block("tool_use", id="new", name="bash", input={"command": "pwd"})]
        echoed = AnthropicAPIEngine.echo_content(content)
        self.assertEqual([b.type for b in echoed], ["text", "fallback", "text", "tool_use"])
        self.assertEqual(echoed[-1].id, "new")
        active = AnthropicAPIEngine.active_blocks(content)
        self.assertEqual([getattr(b, "id", None) for b in active if b.type == "tool_use"], ["new"])
        plain = [block("text", text="x")]
        self.assertIs(AnthropicAPIEngine.echo_content(plain), plain)


class ErrorTests(_Base):
    def test_not_found_on_first_turn_tries_next_model(self):
        sdk = self.install([FakeNotFoundError("model: claude-fable-5-1 not found"),
                            final("end_turn", [block("text", text="ok")], model="claude-opus-5-5")])
        sink = ListSink()
        res = self.engine(FakeJudge()).run(self.job(), sink, threading.Event())
        self.assertTrue(res.ok, res.error)
        self.assertEqual([c["model"] for c in sdk.calls], ["claude-fable-5-1", "claude-opus-5-5"])
        self.assertEqual(sink.of("engine.compat_retry")[0]["model"], "claude-opus-5-5")
        self.assertEqual(res.active_model, "claude-opus-5-5")
        self.assertTrue(res.fallback_used)

    def test_not_found_without_fallback(self):
        self.install([FakeNotFoundError("model not found")])
        sink = ListSink()
        res = self.engine(FakeJudge()).run(self.job(fallback_models=[]), sink, threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("HTTP 404", res.error)
        self.assertTrue(sink.of("engine.error"))

    def test_rate_limit(self):
        self.install([FakeRateLimitError("rate_limit_error")])
        res = self.engine(FakeJudge()).run(self.job(), ListSink(), threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("HTTP 429", res.error)

    def test_status_error_401(self):
        self.install([FakeAPIStatusError("invalid x-api-key", 401)])
        res = self.engine(FakeJudge()).run(self.job(), ListSink(), threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("HTTP 401", res.error)
        self.assertIn("ANTHROPIC_API_KEY", res.error)

    def test_connection_error(self):
        self.install([FakeAPIConnectionError("Connection error.")])
        sink = ListSink()
        res = self.engine(FakeJudge()).run(self.job(), sink, threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("cannot reach the Anthropic API", res.error)
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "error_during_execution")

    def test_unparseable_tool_json_is_retried(self):
        sdk = self.install([ValueError("Unable to parse tool parameter JSON"),
                            final("end_turn", [block("text", text="ok")])])
        res = self.engine(FakeJudge()).run(self.job(), ListSink(), threading.Event())
        self.assertTrue(res.ok)
        self.assertEqual(len(sdk.calls), 2)


# --------------------------------------------------------------------------- tools
class ToolTests(_Base):
    def test_bash_env_is_scrubbed(self):
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-x", "NVIDIA_API_KEY": "nvapi-x", "FOO_API_KEY": "f",
                   "MY_SECRET_TOKEN": "t", "GH_TOKEN": "g", "DB_PASSWORD": "p"}
        with mock.patch.dict(os.environ, {**secrets, "KYORA_KEEP_ME": "1"}):
            eng = self.engine()
            cmd = f'"{sys.executable}" -c "import os, json; print(json.dumps(sorted(os.environ)))"'
            out, is_error = eng._bash(cmd, self.ws, {"EXTRA_TOKEN": "z", "KYORA_EXTRA": "y"})
        self.assertFalse(is_error, out)
        names = set(json.loads(out.strip().splitlines()[-1]))
        for name in list(secrets) + ["EXTRA_TOKEN"]:
            self.assertNotIn(name, names)
        self.assertIn("KYORA_KEEP_ME", names)
        self.assertIn("KYORA_EXTRA", names)
        self.assertIn("PATH", names)

    def test_scrubbed_env_helper(self):
        with mock.patch.dict(os.environ, {"SOME_ACCESS_KEY": "a", "PLAIN": "b"}):
            env = scrubbed_env()
        self.assertNotIn("SOME_ACCESS_KEY", env)
        self.assertEqual(env.get("PLAIN"), "b")

    def test_bash_cwd_exit_code_and_timeout(self):
        eng = self.engine()
        out, is_error = eng._bash("ls", self.ws)
        self.assertFalse(is_error)
        self.assertIn("existing.txt", out)
        out, is_error = eng._bash("exit 3", self.ws)
        self.assertTrue(is_error)
        self.assertIn("[exit code 3]", out)
        out, is_error = eng._bash(f'"{sys.executable}" -c "import time; time.sleep(5)"', self.ws, timeout_s=0.5)
        self.assertTrue(is_error)
        self.assertIn("timed out", out)

    def test_editor_confinement(self):
        eng = self.engine()
        for bad in ("../x", "../../etc/passwd", str(self.root / "outside.txt"), "~/x", "%2e%2e%2fx", ""):
            with self.assertRaises(EditorError, msg=bad):
                eng._editor(self.ws, "create", bad, file_text="x")
        self.assertFalse((self.root / "x").exists())
        self.assertFalse((self.root / "outside.txt").exists())
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "create", ".git/config", file_text="x")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_editor_rejects_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.ws / "link").symlink_to(outside, target_is_directory=True)
        (self.ws / "dangling").symlink_to(self.root / "nowhere.txt")
        eng = self.engine()
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "create", "link/evil.txt", file_text="x")
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "create", "dangling", file_text="x")
        self.assertFalse((outside / "evil.txt").exists())
        self.assertFalse((self.root / "nowhere.txt").exists())
        with self.assertRaises(EditorError):
            confine(self.ws, "link/../link/evil.txt")

    def test_editor_commands(self):
        eng = self.engine()
        backups = self.home / "backups"
        self.assertIn("File created successfully", eng._editor(self.ws, "create", "src/a.js", file_text="a\nb\nc\n"))
        self.assertEqual((self.ws / "src" / "a.js").read_text(encoding="utf-8"), "a\nb\nc\n")
        view = eng._editor(self.ws, "view", "src/a.js")
        self.assertIn("     1\ta", view)
        self.assertIn("     3\tc", view)
        part = eng._editor(self.ws, "view", "src/a.js", view_range=[2, 2])
        self.assertIn("     2\tb", part)
        self.assertNotIn("\ta\n", part)
        out = eng._editor(self.ws, "str_replace", "src/a.js", old_str="b", new_str="B", backup_dir=backups)
        self.assertIn("has been edited", out)
        self.assertEqual((self.ws / "src" / "a.js").read_text(encoding="utf-8"), "a\nB\nc\n")
        self.assertTrue(any(backups.iterdir()), "previous content is backed up outside the workspace")
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "str_replace", "src/a.js", old_str="zzz", new_str="y")
        (self.ws / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        with self.assertRaises(EditorError) as ctx:
            eng._editor(self.ws, "str_replace", "dup.txt", old_str="x", new_str="y")
        self.assertIn("Multiple occurrences", str(ctx.exception))
        eng._editor(self.ws, "insert", "src/a.js", insert_line=0, insert_text="top")
        self.assertEqual((self.ws / "src" / "a.js").read_text(encoding="utf-8"), "top\na\nB\nc\n")
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "insert", "src/a.js", insert_line=99, insert_text="x")
        listing = eng._editor(self.ws, "view", ".")
        self.assertIn("src/", listing)
        self.assertIn("src/a.js", listing)
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "view", "missing.txt")
        with self.assertRaises(EditorError):
            eng._editor(self.ws, "undo_edit", "src/a.js")


if __name__ == "__main__":
    unittest.main()
