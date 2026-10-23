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
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora.config import AgentEngineCfg
from kyora.models import agent_tools, openai_agent
from kyora.models.agent_tools import ToolLoopEngine, find_posix_shell
from kyora.models.base import EngineJob
from kyora.models.nim_client import NIMClient, NIMError
from kyora.models.openai_agent import (
    CHECK_COMMAND, DISABLED_REASON, OpenAIAgentEngine, check_plan, elide_context, endpoint_host, family_extra,
    endpoint_wait_s, parse_arguments, retry_after_s, testspec_tests, text_tool_call,
)
from kyora.util import envsafe
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
        eng = OpenAIAgentEngine(lambda: c, judge, self.home, client_factory=fast_client)
        eng.retry_base_s = 0.01  # transient retries without the real 5 s .. 60 s waits
        return eng

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
        # never the user info or the query, also without a scheme
        self.assertEqual(endpoint_host("https://user:s3cret@Host.Example:8443/v1?api_key=SECRET"), "host.example:8443")
        self.assertEqual(endpoint_host("host.example/v1?api_key=SECRET"), "host.example")
        self.assertEqual(endpoint_host("user:s3cret@host.example"), "host.example")
        self.assertEqual(endpoint_host("localhost:8000/v1"), "localhost:8000")
        self.assertEqual(endpoint_host("http://[::1]:9000/v1"), "[::1]:9000")
        self.assertEqual(endpoint_host("?token=SECRET"), "")
        self.assertEqual(family_extra(ULTRA, True), {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(family_extra(SUPER, False), {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(family_extra(GLM, True), {})  # GLM thinks by default: nothing to send
        self.assertEqual(family_extra(GLM, False), {"chat_template_kwargs": {"enable_thinking": False}})
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

    def test_glm_thinking_off_and_per_turn_timing(self):
        mock_ = self.serve(Script([answer(content="ok")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(thinking=False, fallback_models=[]))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock_.requests[0]["chat_template_kwargs"], {"enable_thinking": False})
        turns = [d for d in sink.of("stage.progress") if d.get("turn_metrics")]
        self.assertEqual(len(turns), 2, "the answer without a tool call, and the one after the single nudge")
        tm = turns[0]["turn_metrics"]
        self.assertEqual((tm["turn"], tm["model"], tm["tool_calls"], tm["finish_reason"]), (1, GLM, 0, "stop"))
        self.assertIsInstance(tm["total_ms"], (int, float))
        self.assertTrue(turns[0]["message"].startswith(f"turn 1: {GLM} answered in "), turns[0]["message"])

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
        self.assertEqual([r["model"] for r in mock_.requests], [GLM, GLM], "K3 is not asked again (+1: the nudge)")

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
        # the second GLM request is the single nudge after a first answer without any tool call
        self.assertEqual([r["model"] for r in mock_.requests], ["google/gemma-4-31b-it", GLM, GLM])
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
        # the client's own retry per request; the turn itself is retried 6 times before it is classified
        self.assertEqual(len(mock_.requests), 2 * (1 + 6))
        self.assertEqual(res.raw["retries"], 6)

    def test_unavailable_when_unreachable(self):
        res, sink = self.run_engine(self.engine(FakeJudge(), base_url=free_port_url()))
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "unavailable")
        self.assertIn("cannot reach the agent endpoint", res.error)
        # classified only after the same turn was retried 6 times, each one said
        retries = [e for e in sink.of("engine.stderr") if e.get("kind") == "retry"]
        self.assertEqual([e["retry"] for e in retries], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(e["turn"] == 1 for e in retries))
        self.assertIn("could not be reached", retries[0]["line"])
        self.assertEqual(res.raw["retries"], 6)

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
        mock_ = self.serve(lambda req: {"status": 400, "error_body": {"error": {"message": "bad request: temperature"}}})
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertFalse(res.ok)
        self.assertNotIn("failure_kind", res.raw)
        self.assertIn("HTTP 400", res.error)
        self.assertEqual(len(mock_.requests), 1, "a 400 is not transient: not retried")

    def test_a_429_without_credits_is_not_retried(self):
        mock_ = self.serve(lambda req: {"status": 429, "error_body": {"error": {"message": "insufficient balance"}}})
        res, _sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))
        self.assertEqual(res.raw["failure_kind"], "quota")
        self.assertEqual(len(mock_.requests), 2, "only the client's own retry: waiting cannot add credits")


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

    LOGIC = ("import { writeFileSync } from 'node:fs';\nimport test from 'node:test';\n"
             "writeFileSync('ran.txt', 'ran');\ntest('at-01 adds up', () => {});\n")

    def _with_logic_tests(self) -> None:
        (self.ws / "tests").mkdir()
        (self.ws / "tests" / "logic-tests.mjs").write_text(self.LOGIC, encoding="utf-8")
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")

    def test_the_logic_test_run_is_judged_and_a_deny_runs_none_of_it(self):
        self._with_logic_tests()
        mock_ = self.serve(Script([answer(tc("kyora_check", "{}", "k1")), answer(content="done")]))
        judge = FakeJudge(deny=("Bash",))
        with mock.patch("kyora.pipeline.quality.find_node", lambda: "node"):  # judged before any Node runs
            res, _sink = self.run_engine(self.engine(judge))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(judge.calls), 1)
        call = judge.calls[0][1]
        self.assertEqual((call["tool_name"], call["tool_use_id"]), ("Bash", "k1"))
        self.assertEqual(call["tool_input"]["command"], CHECK_COMMAND)
        self.assertTrue(call["tool_input"]["file_path"].endswith("/tests/logic-tests.mjs"), call)
        self.assertEqual(call["tool_input"]["content"], self.LOGIC)
        self.assertEqual(tool_messages(mock_.requests[1])[0]["content"], "Denied by the Kyora judge: not in the plan")
        self.assertFalse((self.ws / "ran.txt").exists())
        self.assertEqual(res.permission_denials[0]["tool_name"], "Bash")

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_an_allowed_check_runs_the_logic_tests(self):
        self._with_logic_tests()
        self.serve(Script([answer(tc("kyora_check", "{}", "k1")), answer(content="done")]))
        judge = FakeJudge()
        res, _sink = self.run_engine(self.engine(judge))
        self.assertTrue(res.ok, res.error)
        self.assertEqual([c[1]["tool_name"] for c in judge.calls], ["Bash"])
        self.assertTrue((self.ws / "ran.txt").is_file())

    def test_the_check_is_capped_by_the_run_deadline(self):
        eng = self.engine()
        block = {"type": "tool_use", "id": "k", "name": "kyora_check", "input": {}}
        state = {"tool_names": ["str_replace_based_edit_tool", "kyora_check"], "denials": [],
                 "tool_deadline": time.monotonic() - 1}
        self.assertEqual(eng._handle_tool(self.job(), ListSink(), block, self.ws, self.home / "r", state),
                         (agent_tools.DEADLINE_PASSED, True))
        seen = []

        def fake_check(_self, job, ws, run_dir, *, timeout_s, cancel=None):
            seen.append(timeout_s)
            return "all 1 checks pass"
        state["tool_deadline"] = time.monotonic() + 30
        with mock.patch.object(OpenAIAgentEngine, "kyora_check", fake_check):
            self.assertEqual(eng._handle_tool(self.job(), ListSink(), block, self.ws, self.home / "r", state),
                             ("all 1 checks pass", False))
        self.assertTrue(0 < seen[0] <= 30, seen)
        runner = mock.MagicMock()
        runner.return_value.logic_tests.return_value = []
        checker = mock.MagicMock()
        checker.return_value.run.return_value = []
        with mock.patch("kyora.pipeline.quality.find_node", lambda: "node"), \
                mock.patch("kyora.pipeline.quality.NodeRunner", runner), \
                mock.patch("kyora.pipeline.quality.StaticChecker", checker):
            stop = threading.Event()
            eng.kyora_check(self.job(), self.ws, self.home / "r", timeout_s=12.5, cancel=stop)
        self.assertEqual(runner.call_args.kwargs, {"timeout_s": 12.5, "cancel": stop})  # a cancel stops node at once

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

    def test_probe_retries_an_answer_cut_off_while_thinking(self):
        def handler(req):
            if req["max_tokens"] <= 1024:
                return answer(content="", reasoning="hmm " * 50, finish_reason="length")
            return answer(tc("kyora_probe", {"ok": True}, "p1"))
        mock_ = self.serve(handler)
        out = self.engine().probe(timeout_s=20)
        self.assertTrue(out["tool_calls_ok"], out)
        self.assertEqual([r["max_tokens"] for r in mock_.requests], [1024, 4096])
        self.assertNotIn("cut_off", out)

    def test_probe_says_cut_off_while_thinking(self):
        self.serve(lambda req: answer(content="", reasoning="hmm", finish_reason="length"))
        out = self.engine().probe(timeout_s=20)
        self.assertTrue(out["ok"])
        self.assertFalse(out["tool_calls_ok"])
        self.assertEqual(out["cut_off"], "thinking")
        self.assertIn("cut off after 4096 tokens while thinking", out["hint"])

    def test_probe_does_not_call_a_model_it_skipped_a_fallback(self):
        mock_ = self.serve(lambda req: answer(tc("kyora_probe", {"ok": True}, "p1")))
        eng = self.engine()  # GLM, fallback SUPER
        eng._chain_for(eng.cfg).mark_busy(GLM, 120, "the provider is temporarily overloaded")  # an earlier run
        out = eng.probe(timeout_s=20)
        self.assertTrue(out["ok"] and out["tool_calls_ok"], out)
        self.assertEqual((out["model"], out["tried"]), (SUPER, [SUPER]))
        self.assertFalse(out["fallback_used"], "GLM was never asked, so it did not fail to answer")
        self.assertEqual(out["skipped"], [{"model": GLM, "state": "busy",
                                           "reason": "the provider is temporarily overloaded"}])
        self.assertIn(f"{GLM} was not tried (busy", out["hint"])
        self.assertEqual([r["model"] for r in mock_.requests], [SUPER])

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


# --------------------------------------------------------------------------- what is echoed to the endpoint
class EchoTests(_Base):
    def test_invalid_and_double_encoded_arguments_are_echoed_as_valid_json(self):
        broken = '{"command": "create", "path": "index.html", "file_text": "<html'
        double = json.dumps(json.dumps({"command": "view", "path": "existing.txt"}))
        script = Script([answer(tc("str_replace_based_edit_tool", broken, "bad1"),
                                tc("str_replace_based_edit_tool", double, "dbl1")),
                         answer(editor("view", "existing.txt", "v2")), answer(content="done")])

        def strict(req):  # like vLLM/SGLang: every historical tool-call argument must be a JSON object
            for m in req["messages"]:
                for call in m.get("tool_calls") or []:
                    try:
                        good = isinstance(json.loads(call["function"]["arguments"]), dict)
                    except ValueError:
                        good = False
                    if not good:
                        return {"status": 400, "error_body": {"error": {"message": "invalid tool call arguments"}}}
            return script(req)
        mock_ = self.serve(strict)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(mock_.requests), 3, "no request was refused")
        calls = mock_.requests[1]["messages"][2]["tool_calls"]
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"_invalid_arguments": broken})
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"command": "view", "path": "existing.txt"})
        results = tool_messages(mock_.requests[1])
        self.assertTrue(results[0]["content"].startswith("Error: invalid JSON arguments:"), "the error stays in the result")
        self.assertIn("1\thello", results[1]["content"])
        self.assertEqual(res.raw["invalid_arguments"], 1)

    def test_tool_blocks_cap_the_echo_and_the_calls_of_one_answer(self):
        long_broken = '{"file_text": "' + "x" * 5000
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b0", "type": "function", "function": {"name": "bash", "arguments": long_broken}}] + [
            {"id": f"v{i}", "type": "function",
             "function": {"name": "str_replace_based_edit_tool", "arguments": {"command": "view", "path": "."}}}
            for i in range(40)]}
        blocks = self.engine()._tool_blocks(msg, 1, False, 4096, set())
        self.assertEqual(len(json.loads(msg["tool_calls"][0]["function"]["arguments"])["_invalid_arguments"]), 2000)
        self.assertTrue(blocks[0]["invalid"])
        self.assertEqual(json.loads(msg["tool_calls"][1]["function"]["arguments"]), {"command": "view", "path": "."})
        over = [b["id"] for b in blocks if b.get("error") and not b.get("invalid")]
        self.assertEqual(over, [f"v{i}" for i in range(31, 40)], "calls after the first 32 are not run")

    def test_at_most_32_calls_of_one_answer_run_and_every_call_is_answered(self):
        calls = [editor("view", "existing.txt", f"v{i}") for i in range(40)]
        mock_ = self.serve(Script([answer(*calls), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        results = tool_messages(mock_.requests[1])
        self.assertEqual(len(results), 40)
        assert_pairing(self, mock_.requests[1]["messages"])
        self.assertTrue(all("1\thello" in r["content"] for r in results[:32]))
        self.assertTrue(all("more than 32 tool calls" in r["content"] for r in results[32:]))

    def test_tool_call_ids_stay_unique_across_turns(self):
        # an empty id: the mock (like many gateways) fills in call_1 on every turn
        mock_ = self.serve(Script([answer(editor("view", "existing.txt", "")), answer(editor("view", "existing.txt", "")),
                                   answer(editor("view", "existing.txt", "same")),
                                   answer(editor("view", "existing.txt", "same")), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        last = mock_.requests[-1]["messages"]
        ids = [c["id"] for m in last for c in m.get("tool_calls") or []]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4, ids)
        self.assertEqual(ids[:3], ["call_1_0", "call_2_0", "same"])
        assert_pairing(self, last)
        self.assertEqual([r["tool_use_id"] for r in sink.of("engine.tool_result")], ids)

    def test_calls_of_an_answer_the_endpoint_stopped_are_not_run(self):
        mock_ = self.serve(Script([answer(editor("create", "x.txt", "s1", file_text="x"), finish_reason="content_filter"),
                                   answer(content="never asked")]))
        judge = FakeJudge()
        res, sink = self.run_engine(self.engine(judge))
        self.assertFalse(res.ok)
        self.assertIn("finish_reason=content_filter", res.error)
        self.assertFalse((self.ws / "x.txt").exists())
        self.assertEqual(judge.calls, [], "nothing was judged or run")
        self.assertEqual(len(mock_.requests), 1)
        result = sink.of("engine.tool_result")[0]
        self.assertTrue(result["is_error"])
        self.assertIn("the endpoint stopped this answer (finish_reason=content_filter); the call was not run",
                      result["excerpt"])


# --------------------------------------------------------------------------- transient failures
class TransientRetryTests(_Base):
    def retries(self, sink) -> list[dict]:
        return [e for e in sink.of("engine.stderr") if e.get("kind") == "retry"]

    def test_a_transient_failure_retries_the_same_turn(self):
        script = Script([answer(editor("view", "existing.txt", "v1")), answer(content="done")])
        failed = {"n": 0}

        def handler(req):
            if tool_messages(req) and failed["n"] < 2:  # turn 2: a 503 that outlasts the client's own retry
                failed["n"] += 1
                return {"status": 503, "error_body": OVERLOADED}
            return script(req)
        mock_ = self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))
        self.assertTrue(res.ok, res.error)
        notes = self.retries(sink)
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["turn"], notes[0]["retry"], notes[0]["status"]), (2, 1, 503))
        self.assertIn("temporarily unavailable", notes[0]["line"])
        self.assertEqual(len(mock_.requests), 4)
        self.assertEqual(mock_.requests[3]["messages"], mock_.requests[1]["messages"], "the same turn, nothing added")
        self.assertEqual(res.num_turns, 2)
        self.assertNotIn("failure_kind", res.raw)

    def test_the_retry_count_starts_again_after_each_answer(self):
        script = Script([answer(editor("view", "existing.txt", f"v{i}")) for i in range(3)] + [answer(content="done")])
        failures: dict = {}

        def handler(req):  # every turn fails once (both of the client's attempts), then answers
            n = failures[len(req["messages"])] = failures.get(len(req["messages"]), 0) + 1
            if n <= 2:
                return {"status": 429, "error_body": {"error": {"message": "slow down"}}}
            return script(req)
        self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        eng = self.engine(FakeJudge(), fallback_models=[])
        eng.transient_retries = 1
        res, sink = self.run_engine(eng)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.raw["retries"], 4, "one per turn: more than transient_retries in all, never in a row")
        self.assertEqual([e["retry"] for e in self.retries(sink)], [1, 1, 1, 1])

    def test_an_answer_ended_with_error_or_abort_is_retried_and_its_calls_never_run(self):
        mock_ = self.serve(Script([answer(content="", finish_reason="error"),
                                   answer(editor("create", "x.txt", "a1", file_text="x"), finish_reason="abort"),
                                   answer(editor("view", "existing.txt", "v1")), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertFalse((self.ws / "x.txt").exists())
        self.assertEqual(res.raw["retries"], 2)
        self.assertEqual(mock_.requests[2]["messages"], mock_.requests[0]["messages"])
        self.assertEqual(res.num_turns, 2)
        lines = [e["line"] for e in self.retries(sink)]
        self.assertIn("finish_reason=error", lines[0])
        self.assertIn("finish_reason=abort (its tool calls were not run)", lines[1])

    def test_retry_after_from_the_endpoint_is_honoured(self):
        failed = {"n": 0}

        def handler(req):
            if failed["n"] < 2:
                failed["n"] += 1
                return {"status": 429, "error_body": {"error": {"message": "rate limited: retry after 0.05 seconds"}}}
            return answer(content="done") if tool_messages(req) else answer(editor("view", "existing.txt", "v1"))
        self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        eng = self.engine(FakeJudge(), fallback_models=[])
        eng.retry_base_s = 30  # the endpoint's 0.05 s wins over the own backoff
        res, sink = self.run_engine(eng)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([e["delay_s"] for e in self.retries(sink)], [0.05])

    def test_a_longer_wait_than_a_retry_may_take_fails_over_at_once(self):
        # 429 with Retry-After: 900 and no wording in the body: the header reaches the engine through NIMError,
        # and no retry is sent that the endpoint already said it would refuse
        mock_ = self.serve(lambda req: {"status": 429, "retry_after": 900,
                                        "error_body": {"error": {"message": "Too Many Requests"}}})
        eng = self.engine(FakeJudge(), fallback_models=[])
        eng.retry_base_s = 30
        t = time.monotonic()
        res, sink = self.run_engine(eng)
        self.assertLess(time.monotonic() - t, 20)
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "quota")
        self.assertEqual(self.retries(sink), [])
        self.assertEqual(len(mock_.requests), 1, "neither the client nor the engine retried")

    def test_retry_after_s(self):
        self.assertEqual(retry_after_s(NIMError("x", status=429, body='{"error": {"retry_after": 12}}')), 12.0)
        self.assertEqual(retry_after_s(NIMError("HTTP 429: please try again in 250ms", status=429)), 0.25)
        self.assertEqual(retry_after_s(NIMError("x", status=429, body="Retry-After: 900")), 60.0, "capped")
        exc = NIMError("x", status=503)
        exc.headers = {"Retry-After": "7"}
        self.assertEqual(retry_after_s(exc), 7.0)
        self.assertEqual(retry_after_s(NIMError("x", status=429, retry_after=12.5)), 12.5)  # NIMClient sets it
        self.assertEqual(endpoint_wait_s(NIMError("x", status=429, retry_after=900.0)), 900.0)
        self.assertIsNone(retry_after_s(NIMError("x", status=503, body="Service temporarily overloaded")))

    def test_no_wait_that_would_end_past_the_run_deadline(self):
        mock_ = self.serve(lambda req: {"status": 503, "error_body": OVERLOADED})
        eng = self.engine(FakeJudge(), fallback_models=[], timeout_s=60)
        eng.retry_base_s = 90
        t0 = time.monotonic()
        res, sink = self.run_engine(eng)
        self.assertLess(time.monotonic() - t0, 45, "classified at once instead of waiting 90 s")
        self.assertEqual(res.raw["failure_kind"], "unavailable")
        self.assertEqual(res.raw["retries"], 0)
        self.assertEqual(self.retries(sink), [])
        self.assertEqual(len(mock_.requests), 2)

    def test_cancel_ends_the_wait(self):
        self.serve(lambda req: {"status": 503, "error_body": OVERLOADED})
        cancel = threading.Event()
        timers: list = []

        class CancelSoonAfterTheRetryNote(ListSink):
            def on_event(self, ev):
                super().on_event(ev)
                if ev.type == "engine.stderr" and ev.data.get("kind") == "retry":
                    timers.append(threading.Timer(0.3, cancel.set))
                    timers[-1].start()
        eng = self.engine(FakeJudge(), fallback_models=[])
        eng.retry_base_s = 40
        t0 = time.monotonic()
        res = eng.run(self.job(), CancelSoonAfterTheRetryNote(), cancel)
        for t in timers:
            t.cancel()
        self.assertLess(time.monotonic() - t0, 30, "the 40 s wait ended with the cancel")
        self.assertEqual(res.error, "cancelled")
        self.assertNotIn("failure_kind", res.raw)


# --------------------------------------------------------------------------- final answers that are not final
class FinalAnswerTests(_Base):
    def test_tool_calls_written_as_text_are_not_run_and_the_model_is_told(self):
        as_text = ('<tool_call>{"name": "str_replace_based_edit_tool", "arguments": {"command": "create", '
                   '"path": "text.txt", "file_text": "x"}}</tool_call>')
        mock_ = self.serve(Script([answer(content=as_text),
                                   answer(editor("create", "index.html", "c1", file_text=INDEX_HTML)),
                                   answer(content="Done.")]))
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertFalse((self.ws / "text.txt").exists(), "a call written as text is never run")
        nudge = mock_.requests[1]["messages"][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("function-calling interface", nudge["content"])
        self.assertIn("<tool_call>", nudge["content"])
        self.assertEqual(res.raw["text_call_nudges"], 1)
        self.assertEqual(res.raw["nudges"], 0)
        self.assertTrue(any("tool call as text" in p["message"] for p in sink.of("stage.progress")))

    def test_text_call_nudges_are_capped(self):
        as_json = '{"name": "str_replace_based_edit_tool", "arguments": {"command": "view", "path": "."}}'
        mock_ = self.serve(Script([answer(content=as_json)]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge()))
        # 3 reminders per model, then the next model of the chain; with none left the run fails as a model
        # failure (so the pipeline fails over) instead of ending as a success that changed nothing
        self.assertFalse(res.ok)
        self.assertEqual(res.raw["failure_kind"], "model")
        self.assertIn("as text", res.error)
        self.assertEqual([r["model"] for r in mock_.requests], [GLM] * 4 + [SUPER] * 4)
        self.assertEqual([(s["from_model"], s["to_model"]) for s in res.raw["model_switches"]], [(GLM, SUPER)])
        self.assertTrue(any(d.get("kind") == "model" and "trying " + SUPER in d["line"]
                            for d in sink.of("engine.stderr")), sink.dump())

    def test_text_calls_move_to_the_next_model_that_calls_tools(self):
        as_json = '{"name": "str_replace_based_edit_tool", "arguments": {"command": "view", "path": "."}}'

        def by_model(req):
            if req["model"] == GLM:
                return answer(content=as_json)
            if any(m.get("role") == "tool" for m in req["messages"]):
                return answer(content="Done: viewed the workspace.")
            return answer(editor("view", ".", "c1"))
        self.serve(Script([by_model]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.active_model, SUPER)
        self.assertTrue(res.fallback_used)

    def test_text_tool_call_detection(self):
        names = ["bash", "str_replace_based_edit_tool", "kyora_check"]
        self.assertIsNotNone(text_tool_call("<|tool_call_begin|>functions.bash:0<|tool_call_argument_begin|>{}", names))
        self.assertIsNotNone(text_tool_call("<function=bash><parameter=command>ls</parameter></function>", names))
        self.assertIsNotNone(text_tool_call('Now: {"type": "function", "function": {"name": "kyora_check", '
                                            '"arguments": {}}}', names))
        self.assertIsNotNone(text_tool_call('{"arguments": {"command": "ls"}, "name": "bash"}', names))
        self.assertIsNone(text_tool_call('Done: the config is {"name": "Dodge", "arguments": 2}', names))
        self.assertIsNone(text_tool_call("Done: index.html and src/game.js are written.", names))
        self.assertIsNone(text_tool_call("", names))
        t = time.monotonic()  # an unbalanced quote and many braces: decoded near each key, never scanned again
        self.assertIsNone(text_tool_call('x"' + "{" * 50000 + ' "name" ' * 50, names))
        self.assertLess(time.monotonic() - t, 5)

    def test_a_first_answer_without_any_tool_call_is_nudged_once(self):
        mock_ = self.serve(Script([answer(content="All done.")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(mock_.requests), 2)
        self.assertIn("without calling any tool", mock_.requests[1]["messages"][-1]["content"])
        self.assertEqual(res.raw["idle_nudges"], 1)

    def test_an_empty_final_answer_is_nudged(self):
        mock_ = self.serve(Script([answer(editor("view", "existing.txt", "v1")), answer(content=""),
                                   answer(content="Finished.")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, _sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        nudge = mock_.requests[2]["messages"][-1]["content"]
        self.assertIn("with an empty answer", nudge)
        self.assertNotIn("without calling any tool", nudge)
        self.assertEqual(res.result_text, "Finished.")


# --------------------------------------------------------------------------- answers cut off while thinking
class ThinkingCutTests(_Base):
    CUT = answer(content="", reasoning="let me think " * 30, finish_reason="length")

    def test_cut_off_while_thinking_retries_with_thinking_off_instead_of_continue(self):
        mock_ = self.serve(Script([self.CUT, answer(editor("view", "existing.txt", "v1")), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))  # GLM, thinking on
        self.assertTrue(res.ok, res.error)
        first, second, third = mock_.requests[:3]
        self.assertEqual(second["messages"], first["messages"], "the cut answer is not appended, no 'continue'")
        self.assertNotIn("chat_template_kwargs", first)
        self.assertEqual(second["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(second["max_tokens"], 4096)
        self.assertNotIn("chat_template_kwargs", third, "the next turn thinks again")
        notes = [e for e in sink.of("engine.stderr") if e.get("kind") == "thinking"]
        self.assertEqual(len(notes), 1)
        self.assertIn("thinking switched off", notes[0]["line"])
        self.assertEqual(res.num_turns, 2)

    def test_still_cut_off_doubles_max_tokens_then_fails_without_identical_requests(self):
        mock_ = self.serve(Script([self.CUT]))
        res, _sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertIn(f"while {GLM} was still thinking", res.error)
        self.assertEqual([(r.get("chat_template_kwargs"), r["max_tokens"]) for r in mock_.requests],
                         [(None, 4096), ({"enable_thinking": False}, 4096), ({"enable_thinking": False}, 8192)])

    def test_a_refused_max_tokens_bump_names_the_cut_off_thought_not_the_context(self):
        too_long = {"status": 400, "error_body": {"error": {"message": "This model's maximum context length is 8192 "
                                                                       "tokens. However, you requested 8300 tokens"}}}
        mock_ = self.serve(Script([self.CUT, self.CUT, too_long]))
        res, _sink = self.run_engine(self.engine(FakeJudge(), fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertIn(f"while {GLM} was still thinking", res.error)
        self.assertIn("refused the retry with max_tokens=8192 (HTTP 400", res.error)
        self.assertEqual((res.raw["context_retries"], res.raw["elided_messages"]), (0, 0))
        self.assertEqual(len(mock_.requests), 3, "no elided resend of the same request")

    def test_a_model_without_a_thinking_switch_gets_more_tokens(self):
        mock_ = self.serve(Script([self.CUT]))
        res, _sink = self.run_engine(self.engine(FakeJudge(), model="poolside/laguna-xs-2.1", fallback_models=[]))
        self.assertFalse(res.ok)
        self.assertEqual([r["max_tokens"] for r in mock_.requests], [4096, 8192])


# --------------------------------------------------------------------------- context overflow
class OverflowTests(_Base):
    def test_overflow_with_few_tool_results_elides_old_reasoning(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return answer(editor("view", "existing.txt", "v1"), reasoning="r" * 60000)
            if calls["n"] == 2:
                return answer(editor("view", "existing.txt", "v2"))
            if calls["n"] == 3:  # no context wording: a 413 is an overflow all the same
                return {"status": 413, "error_body": {"error": {"message": "payload too large"}}}
            return answer(content="done")
        mock_ = self.serve(handler)
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        res, sink = self.run_engine(self.engine(FakeJudge()))
        self.assertTrue(res.ok, res.error)
        before, retried = mock_.requests[2]["messages"], mock_.requests[3]["messages"]
        self.assertEqual(before[2]["reasoning_content"], "r" * 60000)
        self.assertNotIn("reasoning_content", retried[2], "the old turn's reasoning was elided")
        self.assertEqual(len(retried), len(before), "no message removed")
        assert_pairing(self, retried)
        notes = [e for e in sink.of("engine.stderr") if e.get("kind") == "context"]
        self.assertTrue(notes and "HTTP 413" in notes[0]["line"], notes)
        self.assertEqual(res.raw["context_retries"], 1)

    def test_413_is_an_overflow(self):
        self.assertTrue(OpenAIAgentEngine._context_overflow(NIMError("x", status=413, body="payload too large")))
        self.assertFalse(OpenAIAgentEngine._context_overflow(NIMError("x", status=400, body="bad temperature")))
        self.assertTrue(OpenAIAgentEngine._context_overflow(NIMError("x", status=400, body="maximum context length")))

    def test_elide_context_deep_with_a_smaller_keep(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(3):
            args = json.dumps({"command": "create", "path": f"f{i}.js", "file_text": "x" * 5000})
            msgs.append({"role": "assistant", "content": "", "reasoning_content": "r" * 3000,
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "str_replace_based_edit_tool", "arguments": args}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "o" * 4000})
        n = len(msgs)
        self.assertEqual(elide_context(msgs, 1000), {"messages": 0, "chars": 0}, "default: 3 results <= 8, untouched")
        stats = elide_context(msgs, 1000, keep=1, deep=True)
        self.assertGreater(stats["chars"], 0)
        self.assertEqual(len(msgs), n)
        assert_pairing(self, msgs)
        self.assertTrue(msgs[3]["content"].startswith("[output elided to save context:"))
        self.assertTrue(msgs[5]["content"].startswith("[output elided to save context:"))
        self.assertEqual(msgs[7]["content"], "o" * 4000, "the newest result is kept")
        self.assertNotIn("reasoning_content", msgs[2])
        self.assertNotIn("reasoning_content", msgs[4])
        self.assertIn("elided to save context", msgs[4]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(msgs[6]["reasoning_content"], "r" * 3000, "the newest assistant message is never touched")
        self.assertNotIn("elided", msgs[6]["tool_calls"][0]["function"]["arguments"])
        # deep: also with no more than `keep` tool results, everything before the newest assistant message
        few = [m for m in msgs[:6]]
        few[2] = {**few[2], "reasoning_content": "r" * 3000}
        self.assertGreater(elide_context(few, 10, keep=8, deep=True)["chars"], 0)
        self.assertNotIn("reasoning_content", few[2])


# --------------------------------------------------------------------------- tools under the run's deadline
class ToolDeadlineTests(_Base):
    def test_the_loop_gives_the_tools_the_run_deadline_and_the_users_cancel(self):
        self.serve(Script([answer(editor("view", "existing.txt", "v1")), answer(content="done")]))
        (self.ws / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        eng = self.engine(FakeJudge(), timeout_s=300)
        seen = []
        real = eng._run_tools

        def spy(job, sink, cancel_, blocks, *args, **kw):
            seen.append((cancel_, kw.get("deadline")))
            return real(job, sink, cancel_, blocks, *args, **kw)
        eng._run_tools = spy
        cancel = threading.Event()
        t0 = time.monotonic()
        res, _sink = self.run_engine(eng, cancel=cancel)
        self.assertTrue(res.ok, res.error)
        self.assertIs(seen[0][0], cancel, "the user's cancel: a running command is killed, the message stays true")
        self.assertGreaterEqual(seen[0][1], t0 + 300)
        self.assertLessEqual(seen[0][1], time.monotonic() + 300)

    @unittest.skipUnless(HAS_SHELL, "no POSIX shell")
    def test_a_command_running_into_the_deadline_is_killed_and_later_calls_do_not_run(self):
        self.serve(Script([answer(tc("bash", {"command": "sleep 120"}, "b1"),
                                  editor("create", "late.txt", "e1", file_text="x")),
                           answer(content="never asked")]))
        t0 = time.monotonic()
        res, sink = self.run_engine(self.engine(FakeJudge(), timeout_s=8))
        self.assertLess(time.monotonic() - t0, 90, "the 120 s sleep was killed at the run deadline")
        self.assertFalse(res.ok)
        self.assertEqual(sink.of("engine.result")[-1]["subtype"], "error_timeout")
        results = sink.of("engine.tool_result")
        self.assertIn("the run deadline was reached", results[0]["excerpt"])
        self.assertIn("the run deadline passed; the call was not run", results[1]["excerpt"])
        self.assertFalse((self.ws / "late.txt").exists())


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
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-x", "KEEP": "1",
                                                                             "NO_COLOR": "1"}):
            eng = OpenAIAgentEngine(lambda: None, None, d)
            out, is_error = eng._bash('env', d)
        self.assertFalse(is_error, out)
        self.assertNotIn("NVIDIA_API_KEY", out)
        self.assertNotIn("KEEP=1", out)  # not allowlisted
        self.assertIn("NO_COLOR=1", out)



# --------------------------------------------------------------------------- tool safety (deadline, cancel, redaction)
class ToolSafetyTests(unittest.TestCase):
    """The shared tool executor: the run deadline, cancel, bounded output and redaction of configured secrets."""

    SECRET = "nvapi-configured-key-0123456789"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-toolsafety-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.run_dir = self.tmp / "run"
        self.eng = OpenAIAgentEngine(lambda: None, FakeJudge(), self.tmp / "home")
        self.job = EngineJob(run_id="r_safe", workspace=self.ws, prompt="p", system_prompt="s", max_turns=5, model=GLM,
                             fallback_models=[], effort="default")
        envsafe.clear_secrets()
        self.addCleanup(envsafe.clear_secrets)

    def state(self) -> dict:
        return {"denials": [], "tool_names": ["bash", "str_replace_based_edit_tool"]}

    def run_tools(self, blocks, *, cancel=None, deadline=None, sink=None):
        return self.eng._run_tools(self.job, sink or ListSink(), cancel or threading.Event(), blocks, self.ws,
                                   self.run_dir, self.state(), deadline=deadline)

    def test_calls_after_the_deadline_are_not_run_but_answered(self):
        blocks = [{"type": "tool_use", "id": "c1", "name": "str_replace_based_edit_tool",
                   "input": {"command": "create", "path": "late.txt", "file_text": "x"}},
                  {"type": "tool_use", "id": "c2", "name": "bash", "input": {"command": "echo hi > late2.txt"}}]
        results = self.run_tools(blocks, deadline=time.monotonic() - 1)
        self.assertEqual([(r["tool_use_id"], r["is_error"]) for r in results], [("c1", True), ("c2", True)])
        for r in results:
            self.assertIn("the run deadline passed; the call was not run", r["content"])
        self.assertFalse((self.ws / "late.txt").exists())
        self.assertFalse((self.ws / "late2.txt").exists())
        # a deadline still ahead does not stop the call
        res = self.run_tools(blocks[:1], deadline=time.monotonic() + 600)[0]
        self.assertFalse(res["is_error"], res)
        self.assertTrue((self.ws / "late.txt").is_file())

    def test_tool_timeout_is_capped_by_the_deadline(self):
        state = {"tool_deadline": time.monotonic() + 5}
        self.assertLessEqual(ToolLoopEngine._tool_timeout(state, 120), 5)
        self.assertEqual(ToolLoopEngine._tool_timeout({"tool_deadline": time.monotonic() - 1}, 120), 0.0)
        self.assertEqual(ToolLoopEngine._tool_timeout({}, 120), 120.0)

    def test_results_events_and_journal_are_redacted(self):
        envsafe.register_secret("keys.agent.api_key", self.SECRET)
        (self.ws / "notes.txt").write_text(f"key={self.SECRET}\n", encoding="utf-8")
        sink = ListSink()
        res = self.run_tools([{"type": "tool_use", "id": "v1", "name": "str_replace_based_edit_tool",
                               "input": {"command": "view", "path": "notes.txt"}}], sink=sink)[0]
        self.assertNotIn(self.SECRET, res["content"])
        self.assertIn("[redacted keys.agent.api_key]", res["content"])
        self.assertNotIn(self.SECRET, sink.dump())
        journal = self.run_dir / "engine" / "agent_turns.jsonl"
        ToolLoopEngine._journal(journal, {"content": f"the model echoed {self.SECRET}"})
        text = journal.read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, text)
        self.assertEqual(json.loads(text)["content"], "the model echoed [redacted keys.agent.api_key]")

    @unittest.skipUnless(HAS_SHELL, "no POSIX shell")
    def test_bash_deadline_cancel_background_and_bounded_output(self):
        t = time.monotonic()
        out, is_error = self.eng._bash("sleep 60", self.ws, deadline=time.monotonic() + 1)
        self.assertTrue(is_error)
        self.assertIn("run deadline", out)
        self.assertEqual(self.eng._bash("echo x", self.ws, deadline=time.monotonic() - 1),
                         ("Error: the run deadline passed; the call was not run", True))
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        out, is_error = self.eng._bash("echo before; sleep 60", self.ws, cancel=cancel)
        self.assertTrue(is_error)
        self.assertTrue(out.endswith("[cancelled]"), out)
        out, is_error = self.eng._bash("sleep 60 & echo started", self.ws)  # the background job is stopped
        self.assertEqual((out, is_error), ("started", False))
        self.assertLess(time.monotonic() - t, 60, "a hang would take the children's 60 s")
        out, is_error = self.eng._bash(f'"{sys.executable}" -c "print(\'y\' * 3000000)"', self.ws)
        self.assertFalse(is_error)
        self.assertLess(len(out), agent_tools.MAX_TOOL_OUTPUT + 200)
        self.assertIn("characters omitted", out)

    def test_bash_tool_note_for_the_description(self):
        # what the model is told must hold on this platform: Windows does not stop background processes
        want = agent_tools.BASH_BACKGROUND_WINDOWS if os.name == "nt" else agent_tools.BASH_BACKGROUND_POSIX
        self.assertEqual(agent_tools.BASH_BACKGROUND_NOTE, want)
        self.assertIn(want, openai_agent.BASH_FUNCTION["function"]["description"])
        self.assertIn("not stopped", agent_tools.BASH_BACKGROUND_WINDOWS)


if __name__ == "__main__":
    unittest.main()
