"""Tests for kyora.models.nim_client against the in-process MockNIM (never a real endpoint)."""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import tempfile
import threading
import time
import unittest

from kyora.models.base import Metrics
from kyora.models.nim_client import (
    DELTA_CONTENT, DELTA_REASONING, DELTA_TOOL_CALL, ChatResult, NIMCancelled, NIMClient, NIMError, NIMUnreachable,
    build_assistant_message, chat_with_events, extract_json_object, extract_structured, force_tool, function_tool,
    is_tool_rejection, json_for_prompt, render_prompt, strip_think, tool_call_arguments,
)
from kyora.pipeline.events import (
    MODEL_DELTA, MODEL_DONE, MODEL_ERROR, MODEL_METRICS, MODEL_REQUEST, EventBus,
)
from tests.mock_nim import MockNIM, scripted_handler, tool_response

MODEL = "moonshotai/kimi-k3"
_STATE: dict = {}


def setUpModule() -> None:
    _STATE["tmp"] = tempfile.TemporaryDirectory(prefix="kyora-test-nim-")
    _STATE["home"] = os.environ.get("KYORA_HOME")
    os.environ["KYORA_HOME"] = _STATE["tmp"].name
    # retries and skipped chunks are logged as warnings by design; keep the test output readable
    lg = logging.getLogger("kyora.models.nim")
    _STATE["level"] = lg.level
    lg.setLevel(logging.ERROR)


def tearDownModule() -> None:
    logging.getLogger("kyora.models.nim").setLevel(_STATE.get("level", logging.NOTSET))
    if _STATE.get("home") is None:
        os.environ.pop("KYORA_HOME", None)
    else:
        os.environ["KYORA_HOME"] = _STATE["home"]
    _STATE["tmp"].cleanup()


def _free_closed_port() -> int:
    """A localhost port that nothing listens on (bound, then closed)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _client(mock: MockNIM, **kw) -> NIMClient:
    kw.setdefault("backoff_s", (0.01, 0.02))
    kw.setdefault("timeout_s", 20.0)
    return NIMClient(mock.url, "nvapi-test-key-123456", user_agent="kyora-test", **kw)


def _result(content: str = "", tool_calls: list[dict] | None = None) -> ChatResult:
    tcs = tool_calls or []
    return ChatResult(content=content, reasoning="", tool_calls=tcs, finish_reason="stop", usage={}, metrics=Metrics(),
                      message=build_assistant_message(content, "", tcs))


def _tc(name: str, args) -> dict:
    return {"id": "call_x", "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


class StreamingTests(unittest.TestCase):
    def test_content_and_reasoning_stream(self):
        reasoning = "First I consider the request. Then I weigh the options carefully before answering."
        content = "Hello from the mocked Kimi endpoint, streamed in many chunks."
        with MockNIM(lambda req: {"content": content, "reasoning": reasoning, "reasoning_chunks": 6, "chunks": 8}) as mock:
            deltas = []
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "hi"}], on_delta=deltas.append)
            req = mock.last_request
        self.assertEqual(res.content, content)
        self.assertEqual(res.reasoning, reasoning)
        self.assertEqual(res.finish_reason, "stop")
        self.assertTrue(req["stream"])
        self.assertEqual(req["stream_options"], {"include_usage": True})
        # MockNIM alternates delta.reasoning_content and delta.reasoning; both must be accumulated.
        reasoning_deltas = [d for d in deltas if d.kind == DELTA_REASONING]
        self.assertGreaterEqual(len(reasoning_deltas), 6)
        self.assertEqual("".join(d.text for d in reasoning_deltas), reasoning)
        self.assertEqual("".join(d.text for d in deltas if d.kind == DELTA_CONTENT), content)
        self.assertEqual(res.message, {"role": "assistant", "content": content, "reasoning_content": reasoning})

    def test_reasoning_sent_in_both_fields_is_taken_once(self):
        # vLLM >= 0.11 repeats delta.reasoning as the deprecated delta.reasoning_content in every chunk.
        reasoning = "Think A. Think B. Think C."
        with MockNIM(lambda req: {"content": "Answer", "reasoning": reasoning, "reasoning_chunks": 3,
                                  "reasoning_both": True}) as mock:
            deltas = []
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "hi"}], on_delta=deltas.append)
        self.assertEqual(res.reasoning, reasoning)
        self.assertEqual("".join(d.text for d in deltas if d.kind == DELTA_REASONING), reasoning)
        # Kimi echoes this message back as-is on the next turn, so it must hold the trace exactly once.
        self.assertEqual(res.message, {"role": "assistant", "content": "Answer", "reasoning_content": reasoning})

    def test_tool_call_accumulation_across_chunks(self):
        args = {"title": "Sky Runner", "files": ["index.html", "src/game.js"], "notes": "x" * 120}

        def handler(req):
            return {"content": "", "reasoning": "need two tools",
                    "tool_calls": [{"id": "call_a", "name": "emit_plan", "arguments": args},
                                   {"id": "call_b", "name": "lookup", "arguments": {"q": "phaser"}}],
                    "tool_chunks": 7}

        with MockNIM(handler) as mock:
            deltas = []
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "plan"}], on_delta=deltas.append)
        self.assertEqual(len(res.tool_calls), 2)
        first, second = res.tool_calls
        self.assertEqual(first["id"], "call_a")
        self.assertEqual(first["type"], "function")
        self.assertEqual(first["function"]["name"], "emit_plan")
        self.assertEqual(json.loads(first["function"]["arguments"]), args)
        self.assertEqual(tool_call_arguments(second), {"q": "phaser"})
        self.assertEqual(res.finish_reason, "tool_calls")
        tool_deltas = [d for d in deltas if d.kind == DELTA_TOOL_CALL]
        self.assertGreater(len(tool_deltas), 2, "arguments must arrive as several fragments")
        self.assertEqual(tool_deltas[0].tool_call["name"], "emit_plan")
        # the complete assistant message echoes reasoning and tool calls
        self.assertEqual(res.message["reasoning_content"], "need two tools")
        self.assertEqual([tc["id"] for tc in res.message["tool_calls"]], ["call_a", "call_b"])

    def test_usage_metrics_ttft_and_tps(self):
        usage = {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59}
        with MockNIM(lambda req: {"content": "a b c d e f g h", "chunks": 4, "delay_s": 0.01, "usage": usage}) as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "go"}])
        m = res.metrics
        self.assertEqual(m.tokens_source, "usage")
        self.assertEqual(m.prompt_tokens, 42)
        self.assertEqual(m.completion_tokens, 17)
        self.assertIsNotNone(m.ttft_ms)
        self.assertGreaterEqual(m.ttft_ms, 0.0)
        self.assertGreaterEqual(m.total_ms, m.ttft_ms)
        self.assertIsNotNone(m.tps)
        self.assertGreater(m.tps, 0)
        self.assertEqual(res.usage["total_tokens"], 59)

    def test_estimated_chunks_without_usage(self):
        with MockNIM(lambda req: {"content": "one two three four five six", "chunks": 5, "delay_s": 0.005,
                                  "usage": None}) as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "go"}])
        m = res.metrics
        self.assertEqual(m.tokens_source, "estimated_chunks")
        self.assertIsNone(m.prompt_tokens)
        self.assertEqual(m.completion_tokens, 5)
        self.assertEqual(res.usage, {})
        self.assertIsNotNone(m.ttft_ms)

    def test_non_stream_path(self):
        def handler(req):
            return tool_response("verdict", {"decision": "allow", "reason": "ok", "risk": "low"},
                                 reasoning="quick check", content="")

        with MockNIM(handler) as mock:
            deltas = []
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "judge"}], stream=False,
                                     on_delta=deltas.append)
            req = mock.last_request
        self.assertFalse(req["stream"])
        self.assertNotIn("stream_options", req)
        self.assertEqual(res.reasoning, "quick check")
        self.assertEqual(tool_call_arguments(res.tool_calls[0])["decision"], "allow")
        self.assertIsNone(res.metrics.ttft_ms)
        self.assertEqual(res.metrics.tokens_source, "usage")
        self.assertEqual({d.kind for d in deltas}, {DELTA_REASONING, DELTA_TOOL_CALL})

    def test_json_answer_to_stream_request(self):
        with MockNIM(lambda req: {"content": "plain json", "json_even_if_stream": True}) as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
        self.assertEqual(res.content, "plain json")

    def test_authorization_header_and_user_agent(self):
        with MockNIM(lambda req: {"content": "ok"}, require_key="nvapi-test-key-123456") as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
            headers = mock.calls[-1]["headers"]
        self.assertEqual(res.content, "ok")
        self.assertEqual(headers["authorization"], "Bearer nvapi-test-key-123456")
        self.assertEqual(headers["user-agent"], "kyora-test")
        with MockNIM(lambda req: {"content": "ok"}, require_key="other") as mock:
            with self.assertRaises(NIMError) as cm:
                _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.status, 401)


class ErrorTests(unittest.TestCase):
    def test_retry_on_429_then_success(self):
        state = {"n": 0}
        lock = threading.Lock()

        def handler(req):
            with lock:
                state["n"] += 1
                n = state["n"]
            if n == 1:
                return {"status": 429, "retry_after": 0,
                        "error_body": {"error": {"message": "rate limited", "code": 429}}}
            return {"content": "after retry"}

        with MockNIM(handler) as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
            n_requests = len(mock.requests)
        self.assertEqual(res.content, "after retry")
        self.assertEqual(n_requests, 2)

    def test_retry_on_5xx_with_scripted_once_rule(self):
        handler = scripted_handler([
            (lambda req: True, {"status": 503, "once": True}),
            (lambda req: True, {"content": "recovered"}),
        ])
        with MockNIM(handler) as mock:
            res = _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
            self.assertEqual(len(mock.requests), 2)
        self.assertEqual(res.content, "recovered")

    def test_retries_exhausted_raise_with_status(self):
        with MockNIM(lambda req: {"status": 500}) as mock:
            with self.assertRaises(NIMError) as cm:
                _client(mock, max_attempts=3).chat(MODEL, [{"role": "user", "content": "x"}])
            self.assertEqual(len(mock.requests), 3)
        self.assertEqual(cm.exception.status, 500)
        self.assertNotIsInstance(cm.exception, NIMUnreachable)

    def test_400_is_not_retried_and_carries_body(self):
        body = {"error": {"message": "tool_choice is not supported for this model", "code": 400}}
        with MockNIM(lambda req: {"status": 400, "error_body": body}) as mock:
            with self.assertRaises(NIMError) as cm:
                _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
            self.assertEqual(len(mock.requests), 1)
        exc = cm.exception
        self.assertEqual(exc.status, 400)
        self.assertIn("tool_choice is not supported", exc.body)
        self.assertIn("HTTP 400", str(exc))
        self.assertNotIn("nvapi-test-key", str(exc))
        self.assertTrue(is_tool_rejection(exc))
        self.assertFalse(is_tool_rejection(NIMError("HTTP 400", status=400, body="context length exceeded")))
        self.assertFalse(is_tool_rejection(NIMError("HTTP 500", status=500, body="tool crashed")))

    def test_unreachable_on_closed_port(self):
        client = NIMClient(f"http://127.0.0.1:{_free_closed_port()}/v1", "k", timeout_s=5.0, backoff_s=(0.01,))
        t0 = time.monotonic()
        with self.assertRaises(NIMUnreachable) as cm:
            client.chat(MODEL, [{"role": "user", "content": "x"}])
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertIsInstance(cm.exception, NIMError)
        self.assertIsNone(cm.exception.status)

    def test_cancel_mid_stream(self):
        content = "word " * 200
        with MockNIM(lambda req: {"content": content, "chunks": 100, "delay_s": 0.03}) as mock:
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            deltas = []
            timer.start()
            t0 = time.monotonic()
            try:
                with self.assertRaises(NIMCancelled):
                    _client(mock).chat(MODEL, [{"role": "user", "content": "x"}], cancel=cancel, on_delta=deltas.append)
            finally:
                timer.cancel()
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.5, "cancellation must stop the stream long before 100 x 30 ms")
        self.assertTrue(deltas, "some content arrived before the cancel")

    def test_cancel_before_request(self):
        cancel = threading.Event()
        cancel.set()
        with MockNIM() as mock:
            with self.assertRaises(NIMCancelled):
                _client(mock).chat(MODEL, [{"role": "user", "content": "x"}], cancel=cancel)
            self.assertEqual(mock.requests, [])

    def test_stream_error_chunk(self):
        # A gateway can answer 200 and then send an error object in the stream.
        from tests import mock_nim

        original = mock_nim.iter_stream_chunks

        def broken(req, spec):
            yield {"error": {"message": "upstream overloaded", "code": 503}}
            yield "[DONE]"

        mock_nim.iter_stream_chunks = broken
        try:
            with MockNIM(lambda req: {"content": "unused"}) as mock:
                with self.assertRaises(NIMError) as cm:
                    _client(mock).chat(MODEL, [{"role": "user", "content": "x"}])
        finally:
            mock_nim.iter_stream_chunks = original
        self.assertIn("upstream overloaded", str(cm.exception))
        self.assertEqual(cm.exception.status, 503)


class DiscoveryTests(unittest.TestCase):
    def test_list_models(self):
        with MockNIM(models=["a/one", "b/two"]) as mock:
            self.assertEqual(_client(mock).list_models(), ["a/one", "b/two"])

    def test_probe_reachable(self):
        with MockNIM(lambda req: {"content": "p", "reasoning": "r"}) as mock:
            out = _client(mock).probe(MODEL, timeout_s=10)
        self.assertTrue(out["reachable"])
        self.assertTrue(out["model_listed"])
        self.assertTrue(out["chat_ok"])
        self.assertTrue(out["reasoning_seen"])
        self.assertIsNotNone(out["latency_ms"])
        self.assertIsNone(out["error"])

    def test_probe_reachable_but_model_missing_and_chat_error(self):
        with MockNIM(lambda req: {"status": 404, "error_body": {"error": {"message": "model not found"}}},
                     models=["other/model"]) as mock:
            out = _client(mock).probe(MODEL, timeout_s=10)
        self.assertTrue(out["reachable"])
        self.assertFalse(out["model_listed"])
        self.assertFalse(out["chat_ok"])
        self.assertEqual(out["status"], 404)
        self.assertIn("model not found", out["error"])

    def test_probe_unreachable_never_raises(self):
        client = NIMClient(f"http://127.0.0.1:{_free_closed_port()}/v1", "k", timeout_s=3.0)
        out = client.probe(MODEL, timeout_s=3)
        self.assertFalse(out["reachable"])
        self.assertFalse(out["chat_ok"])
        self.assertTrue(out["error"])

    def test_key_hint_redacts(self):
        client = NIMClient("http://127.0.0.1:1/v1", "nvapi-abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", client.key_hint())
        self.assertNotIn("abcdefghijklmnop", repr(client))
        self.assertEqual(NIMClient("http://127.0.0.1:1/v1", "").key_hint(), "(no key)")
        with self.assertRaises(ValueError):
            NIMClient("", "k")


class BodyTests(unittest.TestCase):
    def test_build_body_merges_extra(self):
        client = NIMClient("http://127.0.0.1:1/v1/", "k")
        self.assertEqual(client.base_url, "http://127.0.0.1:1/v1")
        tool = function_tool("emit_x", "desc", {"type": "object", "properties": {}})
        body = client.build_body(MODEL, [{"role": "user", "content": "x"}], tools=[tool], tool_choice=force_tool("emit_x"),
                                 temperature=1, top_p=1, max_tokens=99,
                                 extra={"reasoning_effort": "max", "chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["temperature"], 1.0)
        self.assertIsInstance(body["temperature"], float)
        self.assertEqual(body["top_p"], 1.0)
        self.assertEqual(body["max_tokens"], 99)
        self.assertEqual(body["tools"], [tool])
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "emit_x"}})
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})
        self.assertTrue(body["stream"])
        # tool_choice without tools is dropped; sampling fields are omitted unless given
        plain = client.build_body(MODEL, [], stream=False, tool_choice="auto")
        self.assertNotIn("tool_choice", plain)
        self.assertNotIn("temperature", plain)
        self.assertFalse(plain["stream"])
        # extra wins over computed fields (explicit caller intent)
        over = client.build_body(MODEL, [], extra={"stream": False, "extra_body": {"reasoning_effort": "low"}})
        self.assertFalse(over["stream"])
        self.assertEqual(over["extra_body"], {"reasoning_effort": "low"})

    def test_extra_reaches_the_wire(self):
        with MockNIM(lambda req: {"content": "ok"}) as mock:
            _client(mock).chat(MODEL, [{"role": "user", "content": "x"}], extra={"reasoning_effort": "high"},
                               temperature=1.0, top_p=1.0)
            req = mock.last_request
        self.assertEqual(req["reasoning_effort"], "high")
        self.assertEqual(req["temperature"], 1.0)
        self.assertEqual(req["top_p"], 1.0)


class ChatWithEventsTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus(None)

    def tearDown(self):
        self.bus.close()

    def _types(self, run_id: str) -> list[str]:
        return [e.type for e in self.bus.replay(0, run_id=run_id)]

    def test_publishes_request_metrics_done_and_deltas(self):
        sub = self.bus.subscribe(run_id="r_1", types=[MODEL_DELTA])
        with MockNIM(lambda req: {"content": "streamed answer text", "reasoning": "thinking text", "chunks": 4}) as mock:
            res = chat_with_events(_client(mock), self.bus, role="kimi", model=MODEL, run_id="r_1", project_id="p_1",
                                   purpose="plan", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(res.content, "streamed answer text")
        types = self._types("r_1")
        self.assertEqual(types, [MODEL_REQUEST, MODEL_METRICS, MODEL_DONE])
        events = {e.type: e for e in self.bus.replay(0, run_id="r_1")}
        self.assertEqual(events[MODEL_REQUEST].data["role"], "kimi")
        self.assertEqual(events[MODEL_REQUEST].data["purpose"], "plan")
        self.assertEqual(events[MODEL_REQUEST].project_id, "p_1")
        metrics = events[MODEL_METRICS].data
        for key in ("ttft_ms", "total_ms", "prompt_tokens", "completion_tokens", "tps", "tokens_source"):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["model"], MODEL)
        self.assertEqual(metrics["tokens_source"], "usage")
        self.assertEqual(events[MODEL_DONE].data["content_chars"], len("streamed answer text"))
        got = []
        while True:
            try:
                ev = sub.queue.get_nowait()
            except queue.Empty:
                break
            if ev is not None:
                got.append(ev)
        sub.close()
        self.assertTrue(got, "model.delta events are fanned out live")
        self.assertEqual("".join(e.data.get("content", "") for e in got), "streamed answer text")
        self.assertEqual("".join(e.data.get("reasoning", "") for e in got), "thinking text")
        self.assertTrue(all(e.id == 0 for e in got), "deltas are not persisted")

    def test_publish_deltas_false(self):
        sub = self.bus.subscribe(run_id="r_2", types=[MODEL_DELTA])
        with MockNIM(lambda req: {"content": "abc def"}) as mock:
            chat_with_events(_client(mock), self.bus, role="kimi", model=MODEL, run_id="r_2", publish_deltas=False,
                             messages=[{"role": "user", "content": "x"}])
        self.assertTrue(sub.queue.empty())
        sub.close()

    def test_model_error_on_failure(self):
        with MockNIM(lambda req: {"status": 400, "error_body": {"error": {"message": "bad request body"}}}) as mock:
            with self.assertRaises(NIMError):
                chat_with_events(_client(mock), self.bus, role="nemotron", model="nvidia/x", run_id="r_3",
                                 messages=[{"role": "user", "content": "x"}])
        types = self._types("r_3")
        self.assertEqual(types, [MODEL_REQUEST, MODEL_ERROR])
        err = self.bus.replay(0, run_id="r_3", types=[MODEL_ERROR])[0].data
        self.assertEqual(err["status"], 400)
        self.assertEqual(err["role"], "nemotron")
        self.assertIn("bad request body", err["error"])

    def test_model_error_on_unreachable(self):
        client = NIMClient(f"http://127.0.0.1:{_free_closed_port()}/v1", "k", timeout_s=3.0)
        with self.assertRaises(NIMUnreachable):
            chat_with_events(client, self.bus, role="kimi", model=MODEL, run_id="r_4",
                             messages=[{"role": "user", "content": "x"}])
        err = self.bus.replay(0, run_id="r_4", types=[MODEL_ERROR])[0].data
        self.assertIsNone(err["status"])

    def test_bus_none_is_allowed(self):
        with MockNIM(lambda req: {"content": "ok"}) as mock:
            res = chat_with_events(_client(mock), None, role="kimi", model=MODEL,
                                   messages=[{"role": "user", "content": "x"}])
        self.assertEqual(res.content, "ok")


class StructuredExtractionTests(unittest.TestCase):
    def test_named_tool_call_preferred(self):
        res = _result(content='{"a": 0}', tool_calls=[_tc("other", {"b": 1}), _tc("emit_plan", {"title": "T"})])
        self.assertEqual(extract_structured(res, "emit_plan"), {"title": "T"})

    def test_any_tool_call_when_name_differs(self):
        res = _result(tool_calls=[_tc("emit_planx", {"title": "T"})])
        self.assertEqual(extract_structured(res, "emit_plan"), {"title": "T"})

    def test_json_in_text(self):
        res = _result(content='Here is the plan: {"title": "Sky", "n": {"x": "}"}} hope it helps')
        self.assertEqual(extract_structured(res, "emit_plan"), {"title": "Sky", "n": {"x": "}"}})

    def test_fenced_json(self):
        res = _result(content='Sure.\n```json\n{"decision": "allow", "reason": "fine", "risk": "low"}\n```\n')
        self.assertEqual(extract_structured(res, "verdict")["decision"], "allow")

    def test_envelope_unwrapped(self):
        inner = {"decision": "deny", "reason": "escapes workspace", "risk": "high"}
        res = _result(content=json.dumps({"name": "verdict", "arguments": json.dumps(inner)}))
        self.assertEqual(extract_structured(res, "verdict"), inner)
        res2 = _result(content=json.dumps({"type": "function", "function": {"name": "verdict", "arguments": inner}}))
        self.assertEqual(extract_structured(res2, "verdict"), inner)

    def test_think_block_and_garbage(self):
        res = _result(content='<think>maybe {"wrong": 1}</think>{"right": 2}')
        self.assertEqual(extract_structured(res, "x"), {"right": 2})
        self.assertIsNone(extract_structured(_result(content="no json here"), "x"))
        self.assertIsNone(extract_structured(_result(content=""), "x"))
        self.assertEqual(strip_think("<think>a</think> b"), "b")

    def test_unparsable_tool_arguments_fall_back_to_text(self):
        res = _result(content='{"ok": true}', tool_calls=[_tc("emit_x", "{not json")])
        self.assertEqual(extract_structured(res, "emit_x"), {"ok": True})
        self.assertEqual(tool_call_arguments({"function": {"name": "f", "arguments": ""}}), {})
        self.assertEqual(tool_call_arguments({"function": {"name": "f", "arguments": 'x {"a": 1} y'}}), {"a": 1})

    def test_extract_json_object_variants(self):
        self.assertEqual(extract_json_object('[1, 2] then {"k": "v"}'), {"k": "v"})
        self.assertIsNone(extract_json_object("[1, 2, 3]"))


class PromptHelperTests(unittest.TestCase):
    def test_render_prompt_fills_placeholders(self):
        text = render_prompt("kimi_judge.md", workspace="/tmp/ws", iteration=2, plan_title="Sky Runner")
        self.assertIn("/tmp/ws", text)
        self.assertIn("fix iteration 2", text)
        self.assertIn("Sky Runner", text)
        self.assertIn('{"decision": "allow" | "deny"', text)  # escaped braces survive

    def test_all_kimi_templates_render(self):
        render_prompt("kimi_intake.md", attachment_summary="(none)", imported_base_hint="absent")
        render_prompt("kimi_plan.md", intake_json="{}", allowed_engines='"vanilla"', min_tests=6)
        render_prompt("kimi_review.md", screenshot_count=0, run_summary_json="{}", gate_json="{}")

    def test_json_for_prompt_truncates_visibly(self):
        text = json_for_prompt({"k": "v" * 500}, limit=100)
        self.assertTrue(text.startswith("{"))
        self.assertIn("[truncated", text)
        self.assertEqual(json_for_prompt({"a": 1}), '{\n "a": 1\n}')


if __name__ == "__main__":
    unittest.main()
