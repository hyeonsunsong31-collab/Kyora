"""Tests of ``kyora.models.openai_agent`` (the Kyora agent engine on an OpenAI-compatible endpoint).

No network: every request goes to ``tests/mock_nim.py`` (an in-process server that speaks the
NVIDIA API Catalog's OpenAI-compatible protocol, streamed and not), scripted per request. The
judge is a fake that records its calls. Run: ``python3 -m unittest tests.test_openai_agent -v``
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora.config import AgentEngineCfg
from kyora.models import agent_tools
from kyora.models.agent_tools import ToolLoopEngine, find_posix_shell
from kyora.models.base import EngineJob
from kyora.models.nim_client import NIMClient
from kyora.models.openai_agent import (
    DISABLED_REASON, OpenAIAgentEngine, check_plan, elide_context, endpoint_host, family_extra, parse_arguments,
    testspec_tests,
)
from tests.mock_nim import MockNIM

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
GOOD, BAD = "nvapi-good-key-0123456789", "nvapi-refused-key-9876543210"
GLM, ULTRA, SUPER, K3 = ("z-ai/glm-5.3", "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-super-120b-a12b",
                         "moonshotai/kimi-k3")
GONE = {"type": "about:blank", "title": "Gone", "status": 410,
        "detail": "The model 'moonshotai/kimi-k3' has reached its end of life on 2026-10-15T08:00:00Z and is no longer available."}
FORBIDDEN = {"status": 403, "title": "Forbidden", "detail": "Authorization failed"}
OVERLOADED = {"error": {"message": "Service temporarily overloaded", "type": "Service Unavailable", "code": 503}}
HAS_SHELL = find_posix_shell() is not None
INDEX_HTML = ('<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8"><title>Dodge</title></head>\n'
              '<body>\n<canvas id="game" width="320" height="480"></canvas>\n<script src="src/game.js"></script>\n'
              '</body>\n</html>\n')
GAME_JS = "(function () {\n  'use strict';\n  window.Game = { start: function () { return 1; } };\n})();\n"


# --------------------------------------------------------------------------- scripting
def tc(name: str, args, call_id: str) -> dict:
    return {"id": call_id, "name": name, "arguments": args}


def answer(*calls: dict, content: str = "", **extra) -> dict:
    spec = {"content": content, "tool_calls": list(calls)} if calls else {"content": content}
    spec.update(extra)
    return spec


def editor(command: str, path: str, call_id: str, **kw) -> dict:
    return tc("str_replace_based_edit_tool", {"command": command, "path": path, **kw}, call_id)


class Script:
    """Answers the i-th chat request with ``steps[i]`` (a spec dict or ``callable(req) -> spec``); the last repeats."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.n = 0
        self.lock = threading.Lock()

    def __call__(self, req: dict) -> dict:
        with self.lock:
            step = self.steps[min(self.n, len(self.steps) - 1)]
            self.n += 1
        return step(req) if callable(step) else dict(step)


class FakeJudge:
    def __init__(self, deny=(), raise_on=(), on_decide=None):
        self.deny = set(deny)
        self.raise_on = set(raise_on)
        self.on_decide = on_decide
        self.contexts: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.forgotten: list[str] = []
        self.unavailable: list[str] = []

    def register_context(self, run_id, ctx):
        self.contexts.append((run_id, dict(ctx)))
        return "token"

    def decide(self, run_id, call):
        self.calls.append((run_id, dict(call)))
        if self.on_decide:
            self.on_decide(call)
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

    def dump(self) -> str:
        return json.dumps([[e.type, e.data] for e in self.events], default=str)


def acfg(url: str, **over) -> AgentEngineCfg:
    base = dict(enabled=True, model=GLM, fallback_models=[SUPER], max_turns=20, max_tokens=4096, temperature=0.6,
                top_p=0.95, thinking=True, timeout_s=300, context_chars=400000, self_check_tool=True, base_url=url,
                api_key=GOOD, key_label="kimi key", alt_keys=[])
    base.update(over)
    return AgentEngineCfg(**base)


def fast_client(c: AgentEngineCfg) -> NIMClient:
    return NIMClient(c.base_url, c.api_key, key_label=c.key_label, alt_keys=list(c.alt_keys or []),
                     user_agent="kyora-agent", backoff_s=(0.01,), max_attempts=2, timeout_s=20)


def free_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def tool_messages(req: dict) -> list[dict]:
    return [m for m in req["messages"] if m.get("role") == "tool"]


def assert_pairing(test: unittest.TestCase, messages: list[dict]) -> None:
    """Every tool message answers a tool call of the assistant message before it, in order, exactly once."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            answered = [x.get("tool_call_id") for x in messages[i + 1:i + 1 + len(ids)]]
            test.assertEqual(answered, ids, f"tool results after message {i} do not pair with its calls")
            test.assertTrue(all(x.get("role") == "tool" for x in messages[i + 1:i + 1 + len(ids)]))
            i += 1 + len(ids)
            continue
        test.assertNotEqual(m.get("role"), "tool", f"orphan tool message at {i}")
        i += 1


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "repo"
        self.ws.mkdir()
        (self.ws / "existing.txt").write_text("hello\n", encoding="utf-8")
        self.mock: MockNIM | None = None

    def tearDown(self):
        if self.mock is not None:
            self.mock.stop()
        self.tmp.cleanup()

    def serve(self, handler) -> MockNIM:
        self.mock = MockNIM(handler).start()
        return self.mock

    def job(self, **over) -> EngineJob:
        base = dict(run_id="r_agent", workspace=self.ws, prompt="Build the game", system_prompt="You build games.",
                    max_turns=99, model="claude-fable-5-1", fallback_models=["claude-opus-5-5"], effort="max",
                    judge_context={"run_dir": str(self.home / "runs" / "r_agent"), "files": []}, project_id="p_test")
        base.update(over)
        return EngineJob(**base)

    def engine(self, judge=None, **cfg_over) -> OpenAIAgentEngine:
        url = cfg_over.pop("base_url", None) or (self.mock.url if self.mock else "http://127.0.0.1:9/v1")
        c = acfg(url, **cfg_over)
        return OpenAIAgentEngine(lambda: c, judge, self.home, client_factory=fast_client)

    def run_engine(self, eng, job=None, cancel=None):
        sink = ListSink()
        res = eng.run(job or self.job(), sink, cancel or threading.Event())
        return res, sink


# --------------------------------------------------------------------------- availability
class AvailabilityTests(_Base):
    def test_available_with_key_and_details_name_host_only(self):
        c = acfg("https://integrate.api.nvidia.com/v1?token=secret-in-url")
        av = OpenAIAgentEngine(lambda: c, None, self.home).availability()
        self.assertTrue(av.available, av.reason)
        self.assertEqual(av.backend, "openai_agent")
        self.assertEqual(av.details["model"], GLM)
        self.assertEqual(av.details["fallback_models"], [SUPER])
        self.assertEqual(av.details["endpoint"], "integrate.api.nvidia.com")
        self.assertEqual(av.details["key_label"], "kimi key")
        self.assertTrue(av.details["enabled"])
        self.assertNotIn("secret-in-url", json.dumps(av.to_dict()))
        self.assertNotIn(GOOD, json.dumps(av.to_dict()))

    def test_disabled_and_no_key(self):
        av = OpenAIAgentEngine(lambda: acfg("https://x.test/v1", enabled=False), None, self.home).availability()
        self.assertFalse(av.available)
        self.assertEqual(av.reason, DISABLED_REASON)
        av = OpenAIAgentEngine(lambda: acfg("https://x.test/v1", api_key=""), None, self.home).availability()
        self.assertFalse(av.available)
        self.assertIn("no API key", av.reason)
        self.assertIn("x.test", av.reason)

    def test_cfg_callable_read_each_time(self):
        box = {"c": acfg("https://x.test/v1", api_key="")}
        eng = OpenAIAgentEngine(lambda: box["c"], None, self.home)
        self.assertFalse(eng.availability().available)
        box["c"] = acfg("https://x.test/v1")
        self.assertTrue(eng.availability().available)

    def test_run_without_key_reports_error(self):
        eng = OpenAIAgentEngine(lambda: acfg("https://x.test/v1", api_key=""), None, self.home)
        res, sink = self.run_engine(eng)
        self.assertFalse(res.ok)
        self.assertEqual(res.backend, "openai_agent")
        self.assertIn("no API key", res.error)
        self.assertIn("no API key", sink.of("engine.error")[0]["error"])

    def test_endpoint_host_and_family_fields(self):
        self.assertEqual(endpoint_host("http://127.0.0.1:8080/v1"), "127.0.0.1:8080")
        self.assertEqual(family_extra(ULTRA, True), {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(family_extra(SUPER, False), {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(family_extra(GLM, True), {})
        self.assertEqual(family_extra("poolside/laguna-xs-2.1", True), {})


# --------------------------------------------------------------------------- the loop
class LoopTests(_Base):
    def test_full_build_through_editor_bash_and_check(self):
        turn1 = [editor("create", "index.html", "t1_a", file_text=INDEX_HTML),
                 editor("create", "src/game.js", "t1_b", file_text=GAME_JS)]
        if HAS_SHELL:
            turn1.append(tc("bash", {"command": "ls src"}, "t1_c"))
        mock_ = self.serve(Script([answer(*turn1, content="Writing the game.", reasoning="plan: two files"),
                                   answer(tc("kyora_check", "{}", "t2_a")),
                                   answer(content="Done: index.html and src/game.js.")]))
        judge = FakeJudge()
        eng = self.engine(judge)
        res, sink = self.run_engine(eng)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.backend, "openai_agent")
        self.assertEqual(res.requested_model, GLM, "the agent's own model, not job.model (a Claude setting)")
        self.assertEqual(res.active_model, GLM)
        self.assertFalse(res.fallback_used)
        self.assertEqual(res.num_turns, 3)
        self.assertIsNone(res.cost_usd)
        self.assertEqual(res.result_text, "Done: index.html and src/game.js.")
        self.assertEqual((self.ws / "index.html").read_text(encoding="utf-8"), INDEX_HTML)
        self.assertEqual((self.ws / "src" / "game.js").read_text(encoding="utf-8"), GAME_JS)
        self.assertNotIn("failure_kind", res.raw)
        self.assertGreater(res.raw["usage"].get("completion_tokens", 0), 0)

        # judge: context registered and forgotten, every write and command judged in order, the check is not
        self.assertEqual(judge.contexts[0][0], "r_agent")
        self.assertEqual(Path(judge.contexts[0][1]["workspace"]), self.ws.resolve())
        self.assertEqual(judge.contexts[0][1]["kind"], "build")
        expected = ["Write", "Write"] + (["Bash"] if HAS_SHELL else [])
        self.assertEqual([c[1]["tool_name"] for c in judge.calls], expected)
        self.assertTrue(judge.calls[0][1]["tool_input"]["file_path"].endswith("/index.html"))
        self.assertEqual(judge.calls[0][1]["tool_use_id"], "t1_a")
        self.assertEqual(judge.forgotten, ["r_agent"])

        # request shape
        first = mock_.requests[0]
        self.assertEqual(first["model"], GLM)
        self.assertTrue(first["stream"])
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["max_tokens"], 4096)
        self.assertEqual(first["temperature"], 0.6)
        self.assertEqual(first["top_p"], 0.95)
        self.assertNotIn("chat_template_kwargs", first, "GLM gets no family fields")
        names = [t["function"]["name"] for t in first["tools"]]
        self.assertEqual(names, (["bash"] if HAS_SHELL else []) + ["str_replace_based_edit_tool", "kyora_check"])
        editor_schema = next(t for t in first["tools"] if t["function"]["name"] == "str_replace_based_edit_tool")
        params = editor_schema["function"]["parameters"]
        self.assertEqual(params["required"], ["command", "path"])
        self.assertEqual(params["properties"]["command"]["enum"], ["view", "create", "str_replace", "insert"])
        self.assertEqual(params["properties"]["insert_line"]["type"], "integer")
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertTrue(first["messages"][0]["content"].startswith("You build games."))
        self.assertIn(str(self.ws.resolve()).replace("\\", "/"), first["messages"][0]["content"])
        self.assertEqual(first["messages"][1], {"role": "user", "content": "Build the game"})
        headers = [c["headers"] for c in mock_.calls if c["method"] == "POST"]
        self.assertEqual(headers[0]["user-agent"], "kyora-agent")
        self.assertEqual(headers[0]["authorization"], f"Bearer {GOOD}")

        # second request: complete assistant message echoed (tool calls + reasoning), one tool message per call
        second = mock_.requests[1]["messages"]
        assistant = second[2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["reasoning_content"], "plan: two files")
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], [c["id"] for c in turn1])
        tools_ = tool_messages(mock_.requests[1])
        self.assertEqual([m["tool_call_id"] for m in tools_], [c["id"] for c in turn1])
        self.assertIn("File created successfully", tools_[0]["content"])
        if HAS_SHELL:
            self.assertIn("game.js", tools_[2]["content"])
        assert_pairing(self, mock_.requests[2]["messages"])
        check_result = tool_messages(mock_.requests[2])[-1]
        self.assertEqual(check_result["tool_call_id"], "t2_a")
        self.assertIn("checks", check_result["content"])

        # events
        session = sink.of("engine.session")[0]
        self.assertEqual(session["backend"], "openai_agent")
        self.assertEqual(session["model"], GLM)
        self.assertEqual(session["permission_mode"], "kyora-judge")
        self.assertEqual(session["effort"], "default")
        self.assertEqual(session["endpoint"], f"127.0.0.1:{mock_.port}")
        self.assertEqual(session["key_label"], "kimi key")
        self.assertEqual(session["tools"], names)
        self.assertEqual(sink.of("engine.model_resolved"), [{"requested": GLM, "active": GLM, "fallback_used": False}])
        uses = sink.of("engine.tool_use")
        self.assertEqual([u["name"] for u in uses], ["Write", "Write"] + (["Bash"] if HAS_SHELL else []) + ["KyoraCheck"])
        self.assertEqual(uses[1]["summary"], "src/game.js")
        self.assertEqual([r["tool_use_id"] for r in sink.of("engine.tool_result")],
                         [c["id"] for c in turn1] + ["t2_a"])
        self.assertIn("plan: two files", [e["text"] for e in sink.of("engine.thinking")])
        self.assertIn("Done: index.html and src/game.js.", [e["text"] for e in sink.of("engine.text")])
        result_ev = sink.of("engine.result")[-1]
        self.assertEqual(result_ev["subtype"], "success")
        self.assertFalse(result_ev["is_error"])
        self.assertEqual(result_ev["num_turns"], 3)
        self.assertIsNone(result_ev["cost_usd"])
        progress = sink.of("stage.progress")
        self.assertTrue(progress and all(p["stage"] == "build" for p in progress))
        self.assertEqual(progress[-1]["pct"], 100)
        self.assertNotIn(GOOD, sink.dump())

        # journal
        transcript = self.home / "runs" / "r_agent" / "engine" / "agent_turns.jsonl"
        entries = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
        turns = [e for e in entries if "finish_reason" in e]
        self.assertEqual([e["turn"] for e in turns], [1, 2, 3])
        self.assertEqual(turns[0]["model"], GLM)
        self.assertTrue(any("tool_results" in e for e in entries))
        self.assertTrue((self.home / "runs" / "r_agent" / "system_prompt.md").is_file())

    def test_nemotron_model_gets_thinking_switch(self):
        mock_ = self.serve(Script([answer(content="ok")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(model=SUPER, fallback_models=[]))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock_.requests[0]["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(sink.of("engine.session")[0]["effort"], "thinking")

    def test_judge_deny_is_returned_to_the_model(self):
        mock_ = self.serve(Script([answer(editor("create", "index.html", "c1", file_text=INDEX_HTML)),
                                   answer(content="I could not write it.")]))
        judge = FakeJudge(deny=("Write",))
        res, sink = self.run_engine(self.engine(judge))
        self.assertTrue(res.ok, res.error)
        self.assertFalse((self.ws / "index.html").exists(), "a denied create must not write the file")
        self.assertEqual(len(res.permission_denials), 1)
        self.assertEqual(res.permission_denials[0]["tool_name"], "Write")
        msg = tool_messages(mock_.requests[1])[0]
        self.assertEqual(msg["tool_call_id"], "c1")
        self.assertIn("Denied by the Kyora judge: not in the plan", msg["content"])
        self.assertTrue(sink.of("engine.tool_result")[0]["is_error"])

    def test_judge_error_fails_open_and_is_counted(self):
        self.serve(Script([answer(editor("create", "index.html", "c1", file_text=INDEX_HTML)), answer(content="ok")]))
        judge = FakeJudge(raise_on=("Write",))
        with self.assertLogs("kyora.agent_tools", "ERROR"):
            res, sink = self.run_engine(self.engine(judge))
        self.assertTrue(res.ok, res.error)
        self.assertTrue((self.ws / "index.html").is_file())
        self.assertEqual(judge.unavailable, ["r_agent"])
        self.assertEqual([d.get("ran_unjudged") for d in sink.of("judge.unavailable")], [True])

    def test_invalid_json_arguments(self):
        broken = '{"command": "create", "path": "index.html", "file_text": "<html'
        mock_ = self.serve(Script([answer(tc("str_replace_based_edit_tool", broken, "bad1"),
                                          editor("view", "existing.txt", "ok1")),
                                   answer(content="ok")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        judge = FakeJudge()
        res, _sink = self.run_engine(self.engine(judge))
        self.assertTrue(res.ok, res.error)
        msgs = tool_messages(mock_.requests[1])
        self.assertEqual([m["tool_call_id"] for m in msgs], ["bad1", "ok1"])
        self.assertTrue(msgs[0]["content"].startswith("Error: invalid JSON arguments:"), msgs[0]["content"])
        self.assertIn("1\thello", msgs[1]["content"])
        self.assertEqual(judge.calls, [], "nothing to judge: the broken call never runs and reads are not judged")
        self.assertEqual(res.raw["invalid_arguments"], 1)
        self.assertEqual((self.ws / "index.html").read_text(encoding="utf-8"), INDEX_HTML)

    def test_parse_arguments(self):
        self.assertEqual(parse_arguments('{"command": "ls"}'), ({"command": "ls"}, None))
        self.assertEqual(parse_arguments(""), ({}, None))
        self.assertEqual(parse_arguments(json.dumps(json.dumps({"a": 1}))), ({"a": 1}, None))
        self.assertEqual(parse_arguments('{"insert_line": "3", "view_range": "[1, 2]"}')[0],
                         {"insert_line": 3, "view_range": [1, 2]})
        self.assertIn("expected a JSON object", parse_arguments("[1, 2]")[1])
        self.assertIn("invalid JSON arguments", parse_arguments("{nope")[1])

    def test_unknown_tool(self):
        mock_ = self.serve(Script([answer(tc("run_python", {"code": "print(1)"}, "u1")), answer(content="ok")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        content = tool_messages(mock_.requests[1])[0]["content"]
        self.assertIn("Error: unknown tool 'run_python'", content)
        self.assertIn("str_replace_based_edit_tool", content)
        self.assertIn("kyora_check", content)
        self.assertTrue(sink.of("engine.tool_result")[0]["is_error"])

    def test_nudge_when_a_planned_file_is_missing(self):
        mock_ = self.serve(Script([answer(editor("create", "index.html", "n1", file_text=INDEX_HTML)),
                                   answer(content="Finished."),
                                   answer(editor("create", "src/game.js", "n2", file_text=GAME_JS)),
                                   answer(content="Finished now.")]))
        job = self.job(judge_context={"run_dir": str(self.home / "runs" / "r_agent"),
                                      "files": [{"path": "index.html", "purpose": "entry"},
                                                {"path": "src/game.js", "purpose": "logic"}]})
        res, sink = self.run_engine(self.engine(FakeJudge()), job)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.num_turns, 4)
        nudge = mock_.requests[2]["messages"][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("src/game.js", nudge["content"])
        self.assertNotIn("index.html", nudge["content"])
        self.assertEqual(res.raw["nudges"], 1)
        self.assertEqual(res.raw["missing_files"], [])
        self.assertEqual(res.result_text, "Finished now.")
        self.assertTrue(any("asking it to continue" in p["message"] for p in sink.of("stage.progress")))

    def test_nudges_are_capped_and_missing_files_reported(self):
        mock_ = self.serve(Script([answer(content="Done.")]))
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(mock_.requests), 3, "the answer plus two nudges")
        self.assertIn("index.html", mock_.requests[1]["messages"][-1]["content"])
        self.assertEqual(res.raw["nudges"], 2)
        self.assertEqual(res.raw["missing_files"], ["index.html"])
        self.assertIn("still missing: index.html", sink.of("stage.progress")[-1]["message"])

    def test_max_turns(self):
        self.serve(Script([answer(editor("view", "existing.txt", "v1"))]))
        res, sink = self.run_engine(self.engine(FakeJudge(), max_turns=2))
        self.assertFalse(res.ok)
        self.assertTrue(res.error.startswith("error_max_turns"), res.error)
        self.assertEqual(res.num_turns, 2)
        self.assertNotIn("failure_kind", res.raw)
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "error_max_turns")

    def test_length_without_tool_calls_continues_at_most_three_times(self):
        mock_ = self.serve(Script([answer(content="part", finish_reason="length")]))
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertFalse(res.ok)
        self.assertIn("max_tokens", res.error)
        self.assertEqual(len(mock_.requests), 4)
        self.assertEqual(mock_.requests[1]["messages"][-1], {"role": "user", "content": "continue"})

    def test_cancel_before_first_turn(self):
        mock_ = self.serve(Script([answer(content="never")]))
        cancel = threading.Event()
        cancel.set()
        res, sink = self.run_engine(self.engine(FakeJudge()), cancel=cancel)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "cancelled")
        self.assertEqual(mock_.requests, [])
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "cancelled")

    def test_cancel_during_tools_skips_the_rest_and_stops(self):
        cancel = threading.Event()
        mock_ = self.serve(Script([answer(editor("create", "a.txt", "c1", file_text="a"),
                                          editor("create", "b.txt", "c2", file_text="b")),
                                   answer(content="never asked")]))
        judge = FakeJudge(on_decide=lambda call: cancel.set())
        res, sink = self.run_engine(self.engine(judge), cancel=cancel)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "cancelled")
        self.assertEqual(len(mock_.requests), 1)
        self.assertFalse((self.ws / "b.txt").exists())
        self.assertIn("Cancelled by the user", sink.of("engine.tool_result")[1]["excerpt"])

    def test_wall_clock_timeout(self):
        self.serve(Script([answer(content="slow", first_delay_s=1.5)]))
        res, sink = self.run_engine(self.engine(FakeJudge(), timeout_s=1))
        self.assertFalse(res.ok)
        self.assertIn("timeout", res.error)
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "error_timeout")
        self.assertNotIn("failure_kind", res.raw)

    def test_tool_call_ids_are_made_present_and_unique(self):
        mock_ = self.serve(Script([answer(editor("view", "existing.txt", "dup"), editor("view", ".", "dup")),
                                   answer(content="ok")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        msgs = mock_.requests[1]["messages"]
        ids = [c["id"] for c in msgs[2]["tool_calls"]]
        self.assertEqual(len(set(ids)), 2)
        assert_pairing(self, msgs)


# --------------------------------------------------------------------------- fallbacks
class FallbackTests(_Base):
    def test_410_model_falls_back_and_the_chain_remembers(self):
        script = Script([answer(editor("create", "index.html", "f1", file_text=INDEX_HTML)), answer(content="done")])

        def handler(req):
            if req["model"] == K3:
                return {"status": 410, "error_body": GONE}
            return script(req)
        mock_ = self.serve(handler)
        eng = self.engine(FakeJudge(), model=K3, fallback_models=[GLM])
        res, sink = self.run_engine(eng)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([r["model"] for r in mock_.requests], [K3, GLM, GLM])
        self.assertEqual(res.requested_model, K3)
        self.assertEqual(res.active_model, GLM)
        self.assertTrue(res.fallback_used)
        resolved = sink.of("engine.model_resolved")
        self.assertEqual(resolved[0]["requested"], K3)
        self.assertEqual(resolved[0]["active"], GLM)
        self.assertTrue(resolved[0]["fallback_used"])
        self.assertIn("retired by the provider on 2026-10-15", resolved[0]["reason"])
        notes = [e for e in sink.of("engine.stderr") if e.get("kind") == "model"]
        self.assertEqual((notes[0]["from_model"], notes[0]["to_model"], notes[0]["status"]), (K3, GLM, 410))
        # the next run of the same engine starts with the model that answers
        mock_.reset()
        script.n = 1
        res2, _sink = self.run_engine(eng, self.job(run_id="r_two"))
        self.assertTrue(res2.ok, res2.error)
        self.assertEqual([r["model"] for r in mock_.requests], [GLM])

    def test_403_key_falls_back_to_the_other_key(self):
        script = Script([answer(editor("create", "index.html", "k1", file_text=INDEX_HTML)), answer(content="done")])

        def handler(req):
            if mock_.calls[-1]["headers"].get("authorization") != f"Bearer {GOOD}":
                return {"status": 403, "error_body": FORBIDDEN}
            return script(req)
        mock_ = self.serve(handler)
        eng = self.engine(FakeJudge(), api_key=BAD, key_label="nemotron key", alt_keys=[("kimi key", GOOD)])
        res, sink = self.run_engine(eng)
        self.assertTrue(res.ok, res.error)
        keys = [c["headers"]["authorization"] for c in mock_.calls if c["method"] == "POST"]
        self.assertEqual(keys, [f"Bearer {BAD}", f"Bearer {GOOD}", f"Bearer {GOOD}"], "the switch is sticky")
        notes = [e for e in sink.of("engine.stderr") if e.get("kind") == "key"]
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["from_key"], notes[0]["to_key"], notes[0]["status"]), ("nemotron key", "kimi key", 403))
        self.assertIn("nemotron key", notes[0]["line"])
        self.assertEqual(res.raw["key_label"], "kimi key")
        dump = sink.dump() + json.dumps(res.raw, default=str)
        self.assertNotIn(GOOD, dump)
        self.assertNotIn(BAD, dump)

    def test_overloaded_model_inside_a_200_stream_falls_back(self):
        script = Script([answer(content="done")])

        def handler(req):
            if req["model"] == ULTRA:
                return {"stream_error": OVERLOADED}
            return script(req)
        mock_ = self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge(), model=ULTRA, fallback_models=[GLM]))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.active_model, GLM)
        self.assertEqual(mock_.requests[-1]["model"], GLM)
        notes = [e for e in sink.of("engine.stderr") if e.get("kind") == "model"]
        self.assertTrue(notes and notes[0]["temporary"])
        self.assertEqual(notes[0]["from_model"], ULTRA)

    def test_model_that_refuses_tools_is_skipped(self):
        refusal = {"error": {"message": "\"auto\" tool choice requires --enable-auto-tool-choice and "
                                        "--tool-call-parser to be set", "code": 400}}
        script = Script([answer(content="done")])

        def handler(req):
            if req["model"] == "google/gemma-4-31b-it":
                return {"status": 400, "error_body": refusal}
            return script(req)
        mock_ = self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        eng = self.engine(FakeJudge(), model="google/gemma-4-31b-it", fallback_models=[GLM])
        res, _sink = self.run_engine(eng)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([r["model"] for r in mock_.requests], ["google/gemma-4-31b-it", GLM])
        self.assertIn("refused function tools", res.raw["model_chain"]["gone"]["google/gemma-4-31b-it"])


# --------------------------------------------------------------------------- failure kinds
class FailureKindTests(_Base):
    def test_auth_when_every_key_is_refused(self):
        self.serve(lambda req: {"status": 403, "error_body": FORBIDDEN})
        res, sink = self.run_engine(self.engine(FakeJudge(), api_key=BAD, key_label="nemotron key",
                                                alt_keys=[("kimi key", GOOD)]))
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "auth")
        self.assertIn("HTTP 403", res.error)
        self.assertIn("keys tried: nemotron key, kimi key", res.error)
        self.assertIn("Public API Endpoints", res.error)
        self.assertIn("403", res.raw["api_error"])
        self.assertEqual(sink.of("engine.error")[-1]["failure_kind"], "auth")
        self.assertNotIn(GOOD, res.error + sink.dump())
        self.assertNotIn(BAD, res.error + sink.dump())

    def test_quota_on_429(self):
        mock_ = self.serve(lambda req: {"status": 429, "error_body": {"error": {"message": "rate limit exceeded"}}})
        res, _sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "quota")
        self.assertIn("HTTP 429", res.error)
        self.assertEqual(len(mock_.requests), 2, "the client's own retry, then the failure")

    def test_unavailable_when_unreachable(self):
        res, _sink = self.run_engine(self.engine(FakeJudge(), base_url=free_port_url()))
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "unavailable")
        self.assertIn("cannot reach the agent endpoint", res.error)

    def test_unavailable_when_every_model_is_overloaded(self):
        self.serve(lambda req: {"status": 503, "error_body": OVERLOADED})
        res, _sink = self.run_engine(self.engine(FakeJudge(), model=ULTRA, fallback_models=[SUPER]))
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "unavailable")
        self.assertIn("temporarily unavailable", res.error)

    def test_model_when_every_model_is_gone(self):
        self.serve(lambda req: {"status": 410, "error_body": GONE})
        eng = self.engine(FakeJudge(), model=K3, fallback_models=["moonshotai/kimi-k2.6"])
        res, _sink = self.run_engine(eng)
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "model")
        self.assertIn(K3, res.error)
        self.assertIn("moonshotai/kimi-k2.6", res.error)
        # all gone: the next run tries the whole chain again instead of only the first model
        self.assertEqual(len(eng._chain_for(eng.cfg).gone()), 0)

    def test_other_failures_carry_no_failure_kind(self):
        self.serve(lambda req: {"status": 400, "error_body": {"error": {"message": "bad request: temperature"}}})
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertFalse(res.ok)
        self.assertNotIn("failure_kind", res.raw)
        self.assertIn("HTTP 400", res.error)


# --------------------------------------------------------------------------- context budget
class ContextTests(_Base):
    def test_elision_keeps_pairing_and_never_removes_messages(self):
        (self.ws / "big.txt").write_text("".join(f"line {i:04d} of the big file\n" for i in range(300)), encoding="utf-8")
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        steps = [answer(editor("view", "big.txt", f"v{i}")) for i in range(12)] + [answer(content="done")]
        mock_ = self.serve(Script(steps))
        res, _sink = self.run_engine(self.engine(FakeJudge(), context_chars=20000, max_turns=30))
        self.assertTrue(res.ok, res.error)
        last = mock_.requests[-1]["messages"]
        self.assertEqual(len(last), 2 + 2 * 12, "no message was removed")
        assert_pairing(self, last)
        tools_ = tool_messages(mock_.requests[-1])
        elided = [m for m in tools_ if m["content"].startswith("[output elided to save context:")]
        self.assertTrue(elided, "old tool output was elided")
        self.assertTrue(all(not m["content"].startswith("[output elided") for m in tools_[-8:]), "8 newest intact")
        self.assertEqual(tools_[0]["tool_call_id"], "v0")
        self.assertGreater(res.raw["elided_chars"], 0)

    def test_elide_context_stages(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(10):
            args = json.dumps({"command": "create", "path": f"f{i}.js", "file_text": "x" * 5000})
            msgs.append({"role": "assistant", "content": "", "reasoning_content": "r" * 3000,
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "str_replace_based_edit_tool", "arguments": args}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "o" * 4000})
        n = len(msgs)
        stats = elide_context(msgs, 10000)
        self.assertEqual(len(msgs), n)
        assert_pairing(self, msgs)
        self.assertGreater(stats["chars"], 0)
        # only the two oldest turns lie before the 8 newest tool results: they are shrunk, the rest is intact
        self.assertTrue(msgs[3]["content"].startswith("[output elided to save context: 4000 chars]"))
        self.assertNotIn("reasoning_content", msgs[2])
        self.assertIn("elided to save context", msgs[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])["path"], "f0.js")
        self.assertEqual(msgs[-1]["content"], "o" * 4000)
        self.assertEqual(msgs[6]["reasoning_content"], "r" * 3000)
        self.assertEqual(elide_context(msgs[:6], 10), {"messages": 0, "chars": 0}, "fewer than 8 tool results: untouched")


# --------------------------------------------------------------------------- kyora_check
class CheckToolTests(_Base):
    def _copy(self, name: str) -> Path:
        ws = self.root / name
        shutil.copytree(FIXTURES / name, ws)
        return ws

    def test_broken_and_correct_workspaces(self):
        eng = self.engine()
        broken = self._copy("game-broken")
        text = eng.kyora_check(self.job(workspace=broken), broken, self.home / "runs" / "x")
        self.assertIn("checks fail", text)
        self.assertIn("FAIL file_exists:.kyora/manifest.json [required]", text)
        self.assertIn("FAIL html_wellformed:index.html", text)
        ok = self._copy("game-ok")
        text = eng.kyora_check(self.job(workspace=ok), ok, self.home / "runs" / "x")
        self.assertRegex(text, r"^all \d+ checks pass")
        self.assertNotIn("FAIL", text)

    def test_without_node_the_js_checks_are_deferred(self):
        ok = self._copy("game-ok")
        with mock.patch("kyora.pipeline.quality.find_node", lambda: None):
            text = self.engine().kyora_check(self.job(workspace=ok), ok, self.home / "runs" / "x")
        self.assertRegex(text, r"^all \d+ checks pass")
        self.assertIn("deferred", text)
        self.assertIn("js_syntax:src/game.js", text)

    def test_check_in_the_loop_writes_nothing_and_is_not_judged(self):
        ws = self._copy("game-broken")
        mock_ = self.serve(Script([answer(tc("kyora_check", "{}", "k1")), answer(content="done")]))
        judge = FakeJudge()
        before = {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()}
        res, sink = self.run_engine(self.engine(judge), self.job(workspace=ws))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(judge.calls, [])
        self.assertEqual({p: p.read_bytes() for p in ws.rglob("*") if p.is_file()}, before)
        self.assertIn("FAIL js_syntax:src/game.js" if shutil.which("node") else "FAIL html_wellformed:index.html",
                      tool_messages(mock_.requests[1])[0]["content"])
        self.assertEqual(sink.of("engine.tool_use")[0]["name"], "KyoraCheck")

    def test_check_disabled_in_settings(self):
        mock_ = self.serve(Script([answer(tc("kyora_check", "{}", "k1")), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge(), self_check_tool=False))
        self.assertTrue(res.ok, res.error)
        self.assertNotIn("kyora_check", [t["function"]["name"] for t in mock_.requests[0]["tools"]])
        self.assertIn("unknown tool 'kyora_check'", tool_messages(mock_.requests[1])[0]["content"])

    def test_plan_from_run_artifact_or_testspec(self):
        run_dir = self.home / "runs" / "p"
        run_dir.mkdir(parents=True)
        (self.ws / "KYORA_TESTSPEC.md").write_text(
            "# Tests\n\n### `at-01` - Player moves\n- kind: browser - REQUIRED\n- steps: press keys\n\n"
            "### `at-02` - Score adds up\n- kind: node\n- steps: (see name)\n", encoding="utf-8")
        self.assertEqual(testspec_tests(self.ws), [
            {"id": "at-01", "name": "Player moves", "kind": "browser", "required": True},
            {"id": "at-02", "name": "Score adds up", "kind": "node", "required": False}])
        job = self.job(judge_context={"files": [{"path": "index.html"}], "constraints": {"max_size_mb": 5}})
        plan = check_plan(job, self.ws, run_dir)
        self.assertEqual(plan["files"], [{"path": "index.html"}])
        self.assertEqual(plan["constraints"], {"max_size_mb": 5})
        self.assertEqual([t["id"] for t in plan["acceptance_tests"]], ["at-01", "at-02"])
        (run_dir / "plan.json").write_text(json.dumps({"files": ["a.js"], "acceptance_tests": [{"id": "x-1"}]}),
                                           encoding="utf-8")
        plan = check_plan(job, self.ws, run_dir)
        self.assertEqual(plan["files"], ["a.js"])
        self.assertEqual(plan["acceptance_tests"], [{"id": "x-1"}])


# --------------------------------------------------------------------------- probe
class ProbeTests(_Base):
    def test_probe_ok_with_tool_call(self):
        mock_ = self.serve(lambda req: answer(tc("kyora_probe", {"ok": True}, "p1")))
        out = self.engine().probe(timeout_s=20)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["tool_calls_ok"])
        self.assertEqual(out["model"], GLM)
        self.assertIsNotNone(out["latency_ms"])
        self.assertEqual(out["key_label"], "kimi key")
        self.assertIsNone(out["error"])
        req = mock_.requests[0]
        self.assertFalse(req["stream"])
        self.assertEqual([t["function"]["name"] for t in req["tools"]], ["kyora_probe"])
        self.assertEqual(req["tool_choice"], "auto")

    def test_probe_model_without_tool_call(self):
        self.serve(lambda req: answer(content="Hello! How can I help?"))
        out = self.engine().probe(timeout_s=20)
        self.assertTrue(out["ok"])
        self.assertFalse(out["tool_calls_ok"])
        self.assertIn("function calling", out["hint"])

    def test_probe_follows_the_chain_and_reports_refusals(self):
        def handler(req):
            if req["model"] == K3:
                return {"status": 410, "error_body": GONE}
            return answer(tc("kyora_probe", '{"ok": true}', "p1"))
        self.serve(handler)
        out = self.engine(model=K3, fallback_models=[ULTRA]).probe(timeout_s=20)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["model"], ULTRA)
        self.assertEqual(out["tried"], [K3, ULTRA])
        self.assertTrue(out["fallback_used"])
        self.assertTrue(out["tool_calls_ok"])

    def test_probe_403_never_raises(self):
        self.serve(lambda req: {"status": 403, "error_body": FORBIDDEN})
        out = self.engine(api_key=BAD, key_label="nemotron key").probe(timeout_s=20)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 403)
        self.assertEqual(out["failure_kind"], "auth")
        self.assertIn("Public API Endpoints", out["hint"])
        self.assertNotIn(BAD, json.dumps(out, default=str))

    def test_probe_with_a_broken_client_factory(self):
        c = acfg("https://x.test/v1")

        def boom(_cfg):
            raise RuntimeError("factory broke")
        with self.assertLogs("kyora.openai_agent", "ERROR"):
            out = OpenAIAgentEngine(lambda: c, None, self.home, client_factory=boom).probe()
        self.assertFalse(out["ok"])
        self.assertIn("factory broke", out["error"])


# --------------------------------------------------------------------------- devices without a shell
class NoShellTests(_Base):
    def test_windows_without_bash_offers_no_bash_and_still_builds(self):
        mock_ = self.serve(Script([answer(tc("bash", {"command": "ls"}, "b1"),
                                          editor("create", "index.html", "e1", file_text=INDEX_HTML)),
                                   answer(content="done")]))
        with mock.patch.object(agent_tools, "find_posix_shell", lambda *a, **k: None):
            res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        names = [t["function"]["name"] for t in mock_.requests[0]["tools"]]
        self.assertEqual(names, ["str_replace_based_edit_tool", "kyora_check"])
        self.assertIn("no POSIX shell", mock_.requests[0]["messages"][0]["content"])
        self.assertIn("no POSIX shell", tool_messages(mock_.requests[1])[0]["content"])
        self.assertTrue((self.ws / "index.html").is_file())
        self.assertEqual(sink.of("engine.session")[0]["tools"], names)

    def test_windows_message_names_git_bash(self):
        with mock.patch.object(agent_tools, "find_posix_shell", lambda *a, **k: None), \
                mock.patch.object(agent_tools.os, "name", "nt"):
            out, is_error = ToolLoopEngine._bash(SimpleNamespace(_shell_argv=ToolLoopEngine._shell_argv), "ls", self.root)
        self.assertTrue(is_error)
        self.assertIn("Git for Windows", out)


# --------------------------------------------------------------------------- the refactor
class SharedMachineryTests(unittest.TestCase):
    def test_anthropic_engine_is_a_tool_loop_engine_and_keeps_its_names(self):
        from kyora.models import anthropic_api
        self.assertTrue(issubclass(anthropic_api.AnthropicAPIEngine, ToolLoopEngine))
        self.assertTrue(issubclass(OpenAIAgentEngine, ToolLoopEngine))
        for name in ("BETAS", "NO_KEY_REASON", "NO_PACKAGE_REASON", "EditorError", "confine", "scrubbed_env",
                     "find_posix_shell", "which", "TOOLS", "MAX_TOKENS", "same_model", "ENGINE_NOTE", "BASH_TOOL",
                     "EDITOR_TOOL", "NO_SHELL_WINDOWS", "SECRET_ENV_RE"):
            self.assertTrue(hasattr(anthropic_api, name), name)
        self.assertIs(anthropic_api.EditorError, agent_tools.EditorError)

    def test_bash_env_is_scrubbed_for_the_agent_too(self):
        if not HAS_SHELL:
            self.skipTest("no POSIX shell")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-x", "KEEP": "1"}):
            eng = OpenAIAgentEngine(lambda: None, None, d)
            out, is_error = eng._bash('env', d)
        self.assertFalse(is_error, out)
        self.assertNotIn("NVIDIA_API_KEY", out)
        self.assertIn("KEEP=1", out)


if __name__ == "__main__":
    unittest.main()
