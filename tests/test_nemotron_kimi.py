"""Tests for the Nemotron and Kimi role wrappers against the in-process MockNIM (never a real endpoint)."""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from kyora.config import KimiCfg, NemotronCfg
from kyora.models.kimi import (
    DEFAULT_CONSTRAINTS, EXTRA_BODY_EFFORT_NOTE, GATE_NOT_PASSED_REASON, MAX_INTAKE_IMAGES, Kimi, KimiConversation,
    apply_gate_guard,
    build_intake_content, normalize_intake, normalize_plan, normalize_review, normalize_rel_path, normalize_verdict,
    truncate_strings, unwrap_payload,
)
from kyora.models.nemotron import NARRATION_MAX_CHARS, Nemotron, clean_narration, normalize_ui_spec
from kyora.models.nim_client import NIMClient, NIMError
from kyora.pipeline.events import MODEL_DELTA, MODEL_ERROR, EventBus
from tests.mock_nim import MockNIM, has_image, has_role, last_user_text, scripted_handler, tool_response

NEMOTRON_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
KIMI_MODEL = "moonshotai/kimi-k3"
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1x1).decode("ascii")

_QUIET = ("kyora.models.nim", "kyora.models.kimi", "kyora.models.nemotron", "kyora.judge")
_saved_levels: dict[str, int] = {}
_STATE: dict = {}


def setUpModule() -> None:
    _STATE["tmp"] = tempfile.TemporaryDirectory(prefix="kyora-test-models-")
    _STATE["home"] = os.environ.get("KYORA_HOME")
    os.environ["KYORA_HOME"] = _STATE["tmp"].name
    # fallbacks and repairs are logged as warnings by design; keep the test output readable
    for name in _QUIET:
        lg = logging.getLogger(name)
        _saved_levels[name] = lg.level
        lg.setLevel(logging.ERROR)


def tearDownModule() -> None:
    for name, level in _saved_levels.items():
        logging.getLogger(name).setLevel(level)
    if _STATE.get("home") is None:
        os.environ.pop("KYORA_HOME", None)
    else:
        os.environ["KYORA_HOME"] = _STATE["home"]
    _STATE["tmp"].cleanup()


# ----------------------------------------------------------------------------- fixtures
def valid_plan(n_tests: int = 6, **overrides) -> dict:
    plan = {
        "title": "Sky Runner",
        "engine": "vanilla",
        "summary": "Endless runner with touch controls.",
        "files": [{"path": "index.html", "purpose": "entry"}, {"path": "src/game.js", "purpose": "game loop"}],
        "contract": {"globals": ["window.Game"],
                     "api": {"Game.start()": "starts", "Game.getState()": "{score,lives,phase}"}},
        "tasks": [{"id": "t1", "title": "Shell", "depends_on": [], "files": ["index.html"], "done_when": "loads"},
                  {"id": "t2", "title": "Loop", "depends_on": ["t1"], "files": ["src/game.js"], "done_when": "runs"}],
        "acceptance_tests": [{"id": f"at-{i:02d}", "name": f"test {i}", "kind": "browser", "steps": "do it",
                              "required": i == 1} for i in range(1, n_tests + 1)],
        "constraints": {"no_external_deps": True},
        "design_brief_for_nemotron": "Bright sky palette, playful tone; menu, play and game-over screens.",
    }
    plan.update(overrides)
    return plan


def _messages(req: dict) -> list[dict]:
    return req.get("messages") or []


def _has_tool_message(req: dict) -> bool:
    return any(m.get("role") == "tool" for m in _messages(req))


def _system_text(req: dict) -> str:
    return "\n".join(str(m.get("content")) for m in _messages(req) if m.get("role") == "system")


class _MockCase(unittest.TestCase):
    """Starts a MockNIM per test with a replaceable handler and an in-memory bus."""

    handler = None

    def setUp(self):
        self.mock = MockNIM(lambda req: self.handler(req)).start()
        self.bus = EventBus(None)
        self.client = NIMClient(self.mock.url, "nvapi-test", user_agent="kyora-test", backoff_s=(0.01,), timeout_s=20.0)

    def tearDown(self):
        self.mock.stop()
        self.bus.close()

    def kimi(self, **cfg) -> Kimi:
        base = {"reasoning_effort": "max", "judge_reasoning_effort": "high", "effort_field": "top_level"}
        base.update(cfg)
        return Kimi(self.client, KIMI_MODEL, KimiCfg(**base), self.bus)

    def nemotron(self, thinking_for_design: bool = True) -> Nemotron:
        return Nemotron(self.client, NEMOTRON_MODEL, self.bus, NemotronCfg(thinking_for_design=thinking_for_design))

    def events(self, type_: str, run_id: str | None = None) -> list:
        return self.bus.replay(0, run_id=run_id, types=[type_])


# ----------------------------------------------------------------------------- Nemotron
class NemotronTests(_MockCase):
    def test_thinking_body(self):
        self.handler = lambda req: {"content": "ok"}
        nem = self.nemotron()
        nem.generate(system="sys", messages=[{"role": "user", "content": "hi"}], thinking=True, purpose="t")
        on = self.mock.last_request
        self.assertEqual(on["model"], NEMOTRON_MODEL)
        self.assertEqual(on["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(on["temperature"], 1.0)
        self.assertEqual(on["top_p"], 0.95)
        self.assertEqual(on["messages"][0], {"role": "system", "content": "sys"})
        nem.generate(system="sys", messages=[{"role": "user", "content": "hi"}], thinking=False, purpose="t")
        off = self.mock.last_request
        self.assertEqual(off["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(off["temperature"], 0.2)
        self.assertNotIn("top_p", off)
        self.assertIsNotNone(nem.last_metrics("t"))

    def test_design_doc_via_tool_call(self):
        design = {"gdd_markdown": "# Sky Runner\nRun.", "narrative": "A sky.",
                  "dialogue": [{"speaker": "Pip", "line": "Jump!"}, "Go go", {"who": "Ada", "text": ""}],
                  "presenter_intro": "**Welcome** to _Sky Runner_!"}
        self.handler = lambda req: tool_response("emit_design", design, reasoning="drafting")
        out = self.nemotron().design_doc({"goal": "runner"}, valid_plan(), run_id="r_d", project_id="p")
        req = self.mock.last_request
        self.assertEqual(req["tool_choice"], {"type": "function", "function": {"name": "emit_design"}})
        self.assertEqual(req["tools"][0]["function"]["name"], "emit_design")
        self.assertEqual(req["temperature"], 1.0)  # thinking_for_design default True
        self.assertIn("Bright sky palette", _system_text(req))
        self.assertEqual(out["gdd_markdown"], "# Sky Runner\nRun.")
        self.assertEqual(out["dialogue"], [{"speaker": "Pip", "line": "Jump!"}, {"speaker": "Narrator", "line": "Go go"}])
        self.assertEqual(out["presenter_intro"], "Welcome to Sky Runner!")

    def test_design_doc_json_in_text_fallback(self):
        design = {"gdd_markdown": "GDD body", "narrative": "N", "dialogue": [], "presenter_intro": "Hi."}
        self.handler = lambda req: {"content": "Here you go:\n```json\n" + json.dumps(design) + "\n```"}
        out = self.nemotron(thinking_for_design=False).design_doc({}, {})
        self.assertEqual(out["gdd_markdown"], "GDD body")
        self.assertEqual(self.mock.last_request["temperature"], 0.2)

    def test_design_doc_tool_rejection_falls_back_without_tools(self):
        design = {"gdd_markdown": "G", "narrative": "", "dialogue": [], "presenter_intro": ""}
        self.handler = scripted_handler([
            (lambda req: "tools" in req, {"status": 400, "error_body": {"error": {"message": "tools are not supported"}}}),
            (lambda req: "tools" not in req, {"content": json.dumps(design)}),
        ])
        out = self.nemotron().design_doc({}, {})
        self.assertEqual(out["gdd_markdown"], "G")
        self.assertEqual(len(self.mock.requests), 2)

    def test_design_doc_without_payload_raises(self):
        self.handler = lambda req: {"content": "I cannot do that."}
        with self.assertRaises(ValueError):
            self.nemotron().design_doc({}, {})

    def test_ui_spec_normalisation(self):
        spec = {"tokens": {"colors": {"bg": "#000"}, "type": {"body": "16px"}, "spacing": {"s": "4px"}},
                "screens": [{"name": "Main Menu", "purpose": "start", "components": [{"type": "button", "label": "Play"},
                                                                                       "Score label"]}],
                "copy": {"menu.play": "Play", "n": 3}}
        self.handler = lambda req: tool_response("emit_ui_spec", spec)
        out = self.nemotron().ui_spec({}, valid_plan())
        self.assertEqual(out["tokens"]["color"], {"bg": "#000"})
        self.assertEqual(out["tokens"]["radius"], {})
        self.assertEqual(out["screens"][0]["id"], "main-menu")
        self.assertEqual(out["screens"][0]["components"][1], {"type": "component", "label": "Score label", "notes": ""})
        self.assertEqual(out["copy"], {"menu.play": "Play", "n": "3"})

    def test_ui_spec_missing_tokens_raises(self):
        self.handler = lambda req: tool_response("emit_ui_spec", {"screens": [{"id": "a"}], "copy": {}})
        with self.assertRaises(ValueError):
            self.nemotron().ui_spec({}, {})
        self.assertEqual(len(self.mock.requests), 2)  # the invalid answer was asked for once more, then refused

    def test_truncated_ui_spec_is_asked_for_again_without_thinking(self):
        # live, 2026-10-18: thinking used all 12288 tokens and the payload arrived cut off without "tokens"
        good = {"tokens": {"color": {"bg": "#000"}, "type": {}, "spacing": {}, "radius": {}},
                "screens": [{"id": "menu", "name": "Menu", "purpose": "start", "components": []}], "copy": {}}

        def handler(req):
            if len(self.mock.requests) == 1:
                return tool_response("emit_ui_spec", {"screens": [{"id": "menu"}]}, finish_reason="length")
            return tool_response("emit_ui_spec", good)
        self.handler = handler
        out = self.nemotron(thinking_for_design=True).ui_spec({}, {})
        self.assertEqual(out["tokens"]["color"], {"bg": "#000"})
        first, second = self.mock.requests
        self.assertTrue(first["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(second["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(second["max_tokens"], 2 * first["max_tokens"])
        self.assertIn("cut off at the token limit", second["messages"][-1]["content"])
        with self.assertRaises(ValueError):
            normalize_ui_spec({"tokens": {"color": {}}, "screens": [{"id": "a"}]})

    def test_narration_is_clean(self):
        long_text = ("**Great news!** The `build` passed # all checks and the [game] is ready to play on your phone "
                     "right now, with touch controls, sound, a pause menu and three difficulty levels.")
        self.handler = lambda req: {"content": long_text}
        out = self.nemotron().narration("Summarise: build passed", run_id="r_n")
        req = self.mock.last_request
        self.assertLessEqual(len(out), NARRATION_MAX_CHARS)
        for ch in "*_`#[]":
            self.assertNotIn(ch, out)
        self.assertTrue(out.startswith("Great news! The build passed"))
        self.assertEqual(req["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(req["max_tokens"], 96)
        self.assertEqual(clean_narration("<think>x</think> Hello  \n world"), "Hello world")

    def test_narration_returns_empty_on_http_500(self):
        self.handler = lambda req: {"status": 500}
        out = self.nemotron().narration("say something", run_id="r_500", project_id="p_1")
        self.assertEqual(out, "")
        errors = self.events(MODEL_ERROR, "r_500")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].data["status"], 500)
        self.assertEqual(errors[0].data["role"], "nemotron")
        self.assertEqual(self.nemotron().narration("   "), "")


# ----------------------------------------------------------------------------- Kimi: core
class KimiCoreTests(_MockCase):
    def test_body_top_level_effort(self):
        self.handler = lambda req: {"content": "ok", "reasoning": "r"}
        kimi = self.kimi()
        conv = KimiConversation("system text")
        conv.add_user("hello")
        res = kimi.think(conv, purpose="probe")
        req = self.mock.last_request
        self.assertEqual(res.content, "ok")
        self.assertEqual(req["model"], KIMI_MODEL)
        self.assertEqual(req["temperature"], 1.0)
        self.assertEqual(req["top_p"], 1.0)
        self.assertEqual(req["reasoning_effort"], "max")
        self.assertNotIn("extra_body", req)
        self.assertNotIn("tools", req)
        self.assertEqual(kimi.last_metrics("probe")["tokens_source"], "usage")
        kimi.think(conv, effort="low")
        self.assertEqual(self.mock.last_request["reasoning_effort"], "low")

    def test_body_extra_body_effort(self):
        self.handler = lambda req: {"content": "ok"}
        conv = KimiConversation()
        conv.add_user("hi")
        self.kimi(effort_field="extra_body").think(conv)
        req = self.mock.last_request
        # The OpenAI SDK merges extra_body into the top level, so no server reads a literal "extra_body" key:
        # the effort travels as the top-level chat-template keyword instead.
        self.assertNotIn("extra_body", req)
        self.assertEqual(req["chat_template_kwargs"], {"reasoning_effort": "max"})
        self.assertNotIn("reasoning_effort", req)
        self.kimi(effort_field="extra_body", judge_reasoning_effort="low").think(conv, effort="low")
        self.assertEqual(self.mock.last_request["chat_template_kwargs"], {"reasoning_effort": "low"})
        self.assertEqual((req["temperature"], req["top_p"]), (1.0, 1.0))

    def test_effort_events_never_claim_extra_body_effort_applied(self):
        # Kimi K3's template is not documented to read chat_template_kwargs.reasoning_effort: model.request /
        # model.metrics name the field sent and mark the extra_body mode best-effort, never "applied".
        self.handler = lambda req: {"content": "ok"}
        conv = KimiConversation()
        conv.add_user("hi")
        self.kimi(effort_field="extra_body").think(conv, run_id="r_eb", purpose="probe")
        for type_ in ("model.request", "model.metrics"):
            data = self.events(type_, "r_eb")[0].data
            self.assertEqual(data["effort_sent"], "chat_template_kwargs.reasoning_effort=max", type_)
            self.assertIs(data["effort_best_effort"], True, type_)
            self.assertEqual(data["effort_note"], EXTRA_BODY_EFFORT_NOTE, type_)
            self.assertIn("may ignore", data["effort_note"])
            self.assertEqual((data["role"], data["model"], data["purpose"]), ("kimi", KIMI_MODEL, "probe"))
            self.assertNotIn("applied", json.dumps(data))
        self.kimi().think(conv, run_id="r_tl", effort="low")
        data = self.events("model.request", "r_tl")[0].data
        self.assertEqual(data["effort_sent"], "reasoning_effort=low")
        self.assertNotIn("effort_best_effort", data)
        self.assertNotIn("effort_note", data)

    def test_conversation_echoes_complete_assistant_message(self):
        def handler(req):
            if not _has_tool_message(req):
                return tool_response("lookup", {"q": "palette"}, reasoning="I should look this up first.",
                                     content="Checking.", call_id="call_lookup")
            return {"content": "Final answer.", "reasoning": "Now I know."}

        self.handler = handler
        conv = KimiConversation("sys")
        conv.add_user([{"type": "text", "text": "question"}])
        seen_args = []

        def lookup(args):
            seen_args.append(args)
            return "palette: sky blue"

        res = self.kimi().think(conv, tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
                                tool_impl={"lookup": lookup})
        self.assertEqual(res.content, "Final answer.")
        self.assertEqual(seen_args, [{"q": "palette"}])
        self.assertEqual(len(self.mock.requests), 2)
        second = self.mock.requests[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "tool"])
        echoed = second[2]
        self.assertEqual(echoed["content"], "Checking.")
        self.assertEqual(echoed["reasoning_content"], "I should look this up first.")
        self.assertEqual(echoed["tool_calls"][0]["id"], "call_lookup")
        self.assertEqual(echoed["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(json.loads(echoed["tool_calls"][0]["function"]["arguments"]), {"q": "palette"})
        self.assertEqual(second[3], {"role": "tool", "tool_call_id": "call_lookup", "content": "palette: sky blue"})
        # the conversation now ends with the final assistant message, also complete
        last = conv.last()
        self.assertEqual(last["reasoning_content"], "Now I know.")
        self.assertEqual(len(conv), 5)

    def test_conversation_is_a_safe_copy(self):
        conv = KimiConversation()
        msg = {"role": "assistant", "content": "x", "reasoning_content": "r",
               "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
        conv.add_assistant(msg)
        msg["tool_calls"][0]["id"] = "mutated"
        out = conv.messages()
        self.assertEqual(out[0]["tool_calls"][0]["id"], "c")
        out[0]["content"] = "changed"
        self.assertEqual(conv.messages()[0]["content"], "x")
        conv.add_assistant({"content": "no role"})
        self.assertEqual(conv.messages()[1]["role"], "assistant")
        with self.assertRaises(ValueError):
            conv.add_assistant({"role": "user", "content": "x"})
        with self.assertRaises(TypeError):
            conv.add_user(42)  # type: ignore[arg-type]

    def test_tool_loop_bounded_and_forced_choice_only_first(self):
        self.handler = lambda req: tool_response("ping", {"n": 1})
        calls = []
        conv = KimiConversation()
        conv.add_user("loop")
        res = self.kimi().think(conv, tools=[{"type": "function", "function": {"name": "ping", "parameters": {}}}],
                                tool_choice={"type": "function", "function": {"name": "ping"}},
                                tool_impl={"ping": lambda a: calls.append(a) or "pong"}, max_tool_rounds=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.mock.requests), 3)
        self.assertTrue(res.tool_calls, "the last (unexecuted) answer is returned")
        self.assertEqual(self.mock.requests[0]["tool_choice"]["function"]["name"], "ping")
        self.assertEqual(self.mock.requests[1]["tool_choice"], "auto")

    def test_tool_without_impl_returns_immediately(self):
        self.handler = lambda req: tool_response("emit_plan", {"title": "x"})
        conv = KimiConversation()
        conv.add_user("x")
        res = self.kimi().think(conv, tools=[{"type": "function", "function": {"name": "emit_plan", "parameters": {}}}],
                                tool_impl={"other": lambda a: "n/a"})
        self.assertEqual(res.tool_calls[0]["function"]["name"], "emit_plan")
        self.assertEqual(len(self.mock.requests), 1)

    def test_tool_impl_error_is_reported_to_model(self):
        def handler(req):
            if not _has_tool_message(req):
                return tool_response("boom", {})
            return {"content": "handled"}

        self.handler = handler
        conv = KimiConversation()
        conv.add_user("x")

        def boom(args):
            raise RuntimeError("disk full")

        res = self.kimi().think(conv, tools=[{"type": "function", "function": {"name": "boom", "parameters": {}}}],
                                tool_impl={"boom": boom})
        self.assertEqual(res.content, "handled")
        tool_msg = [m for m in self.mock.requests[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("RuntimeError: disk full", tool_msg["content"])

    def test_structured_falls_back_to_auto_then_remembers(self):
        verdict = {"decision": "allow", "reason": "fine", "risk": "low"}

        def handler(req):
            if isinstance(req.get("tool_choice"), dict):
                return {"status": 400, "error_body": {"error": {
                    "message": "tool_choice must be 'auto' or 'none' when thinking is enabled"}}}
            return tool_response("verdict", verdict)

        self.handler = handler
        kimi = self.kimi()
        out = kimi.judge({"workspace": "/ws"}, {"tool_name": "Bash", "tool_input": {"command": "make"}}, "r_1")
        self.assertEqual(out["decision"], "allow")
        self.assertEqual(kimi.tool_mode, "auto")
        self.assertEqual(len(self.mock.requests), 2)
        kimi.judge({"workspace": "/ws"}, {"tool_name": "Bash", "tool_input": {"command": "make all"}}, "r_1")
        self.assertEqual(len(self.mock.requests), 3, "the accepted request shape is reused")
        self.assertEqual(self.mock.last_request["tool_choice"], "auto")

    def test_structured_falls_back_to_tool_less_request(self):
        verdict = {"decision": "deny", "reason": "pushes to a remote", "risk": "high"}

        def handler(req):
            if "tools" in req:
                return {"status": 400, "error_body": {"error": {"message": "function calling is not supported"}}}
            return {"content": json.dumps(verdict)}

        self.handler = handler
        kimi = self.kimi()
        out = kimi.judge({}, {"tool_name": "Bash", "tool_input": {"command": "git push"}}, "r_2")
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(kimi.tool_mode, "none")
        self.assertEqual(len(self.mock.requests), 3)

    def test_non_tool_400_is_raised_not_degraded(self):
        self.handler = lambda req: {"status": 400, "error_body": {"error": {"message": "context length exceeded"}}}
        kimi = self.kimi()
        with self.assertRaises(NIMError) as cm:
            kimi.judge({}, {"tool_name": "Bash", "tool_input": {"command": "x"}}, "r_3")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(self.mock.requests), 1)
        self.assertEqual(kimi.tool_mode, "forced")

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            Kimi(None, KIMI_MODEL, KimiCfg("max", "max", "top_level"), self.bus)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Kimi(self.client, "", KimiCfg("max", "max", "top_level"), self.bus)
        kimi = Kimi(self.client, KIMI_MODEL, None, None)  # defaults when no cfg / no bus
        self.assertEqual(kimi.effort_extra(None), {"reasoning_effort": "max"})
        self.assertEqual(kimi.effort_extra(""), {})


# ----------------------------------------------------------------------------- Kimi: intake
class KimiIntakeTests(_MockCase):
    INTAKE = {"goal": "A sky runner for phones.", "genre": "arcade runner", "platform": "",
              "must_haves": ["touch jump", " ", "score"], "references": "ref.png: blue sky", "risks": [],
              "imported_base": True, "notes": None}

    def test_intake_with_image_attachment(self):
        self.handler = lambda req: tool_response("emit_intake", self.INTAKE)
        atts = [
            {"id": "a_1", "name": "ref.png", "mime": "image/png", "size": 48213, "role": "reference",
             "image_data_url": PNG_DATA_URL},
            {"id": "a_2", "name": "notes.md", "mime": "text/markdown", "size": 120, "role": "reference",
             "text_excerpt": "The hero is a paper plane."},
            {"id": "a_3", "name": "base.zip", "mime": "application/zip", "size": 5000, "role": "import",
             "zip_listing": ["index.html", "src/game.js"]},
        ]
        out = self.kimi().intake("Make a runner game", atts, run_id="r_i", project_id="p_i")
        req = self.mock.last_request
        self.assertTrue(has_image(req))
        self.assertTrue(has_role("system")(req))
        user = [m for m in req["messages"] if m["role"] == "user"][0]["content"]
        self.assertEqual(user[0]["type"], "text")
        self.assertIn("Make a runner game", user[0]["text"])
        image_parts = [p for p in user if p["type"] == "image_url"]
        self.assertEqual(image_parts, [{"type": "image_url", "image_url": {"url": PNG_DATA_URL}}])
        self.assertIn("Make a runner game", last_user_text(req))
        system = _system_text(req)
        self.assertIn("ref.png (image/png, 47.1 KB) [role: reference]", system)
        self.assertIn("shown to you as attached image 1", system)
        self.assertIn("The hero is a paper plane.", system)
        self.assertIn("index.html, src/game.js", system)
        self.assertIn("is present: base.zip", system)
        self.assertEqual(req["tool_choice"]["function"]["name"], "emit_intake")
        self.assertEqual(req["reasoning_effort"], "max")
        self.assertEqual(out, {"goal": "A sky runner for phones.", "genre": "arcade runner",
                               "platform": "web (touch + keyboard)", "must_haves": ["touch jump", "score"],
                               "references": ["ref.png: blue sky"], "risks": [], "imported_base": True, "notes": ""})

    def test_imported_base_follows_attachment_facts(self):
        self.handler = lambda req: tool_response("emit_intake", self.INTAKE)  # model claims imported_base: True
        out = self.kimi().intake("no zip here", [])
        self.assertFalse(out["imported_base"])
        self.assertIn("is absent", _system_text(self.mock.last_request))
        self.assertIn("(no attachments)", _system_text(self.mock.last_request))
        atts = [{"id": "a_9", "name": "game.zip", "mime": "application/zip", "listing": "index.html\nmain.js"}]
        out = self.kimi().intake("use my zip", atts, import_attachment_id="a_9")
        self.assertTrue(out["imported_base"])
        system = _system_text(self.mock.last_request)
        self.assertIn("is present: game.zip", system)
        self.assertIn("index.html, main.js", system)

    def test_image_cap_and_honest_summary(self):
        atts = [{"name": f"img{i}.png", "mime": "image/png", "image_data_url": PNG_DATA_URL} for i in range(8)]
        atts.append({"name": "photo.jpg", "mime": "image/jpeg"})  # no data URL supplied by the caller
        parts, summary, hint, has_import = build_intake_content("p", atts)
        self.assertEqual(sum(1 for p in parts if p["type"] == "image_url"), MAX_INTAKE_IMAGES)
        self.assertEqual(summary.count("NOT shown (limit of 6 images"), 2)
        self.assertIn("photo.jpg (image/jpeg): image NOT included", summary)
        self.assertEqual((hint, has_import), ("absent", False))

    def test_intake_repair_when_goal_missing(self):
        self.handler = scripted_handler([
            (lambda req: not _has_tool_message(req), tool_response("emit_intake", {"genre": "puzzle"})),
            (_has_tool_message, tool_response("emit_intake", {"goal": "A puzzle.", "genre": "puzzle"})),
        ])
        out = self.kimi().intake("puzzle please", [])
        self.assertEqual(out["goal"], "A puzzle.")
        self.assertEqual(len(self.mock.requests), 2)

    def test_normalize_intake_unit(self):
        with self.assertRaises(ValueError):
            normalize_intake({"goal": "  "}, has_import=False)
        out = normalize_intake({"goal": "g", "imported_base": "yes", "must_haves": "one"}, has_import=True)
        self.assertEqual(out["must_haves"], ["one"])
        self.assertTrue(out["imported_base"])


# ----------------------------------------------------------------------------- Kimi: plan
class KimiPlanTests(_MockCase):
    def test_plan_valid_first_time(self):
        self.handler = lambda req: tool_response("emit_plan", valid_plan(), reasoning="planning")
        out = self.kimi().plan({"goal": "runner"}, run_id="r_p", project_id="p")
        req = self.mock.last_request
        self.assertEqual(len(self.mock.requests), 1)
        self.assertEqual(req["tool_choice"]["function"]["name"], "emit_plan")
        self.assertIn('"vanilla", "phaser", "three", "zip-only"', _system_text(req))
        self.assertIn("at least 6 tests", _system_text(req))
        self.assertEqual(out["engine"], "vanilla")
        self.assertTrue(out["preview"])
        self.assertEqual(out["constraints"], DEFAULT_CONSTRAINTS)
        self.assertEqual([f["path"] for f in out["files"]], ["index.html", "src/game.js"])
        self.assertEqual(len(out["acceptance_tests"]), 6)
        self.assertTrue(out["acceptance_tests"][0]["required"])

    def test_plan_repair_path(self):
        bad = valid_plan(files=[{"path": "src/game.js", "purpose": "loop"}])
        good = valid_plan()

        def handler(req):
            if not _has_tool_message(req):
                return tool_response("emit_plan", bad, reasoning="first try", call_id="call_plan_1")
            return tool_response("emit_plan", good, call_id="call_plan_2")

        self.handler = handler
        kimi = self.kimi()
        out = kimi.plan({"goal": "runner"})
        self.assertEqual(out["files"][0]["path"], "index.html")
        self.assertEqual(len(self.mock.requests), 2)
        msgs = self.mock.requests[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(msgs[2]["reasoning_content"], "first try")
        self.assertEqual(msgs[2]["tool_calls"][0]["id"], "call_plan_1")
        self.assertEqual(msgs[3]["tool_call_id"], "call_plan_1")
        self.assertIn("index.html", msgs[3]["content"])
        self.assertIn("index.html", msgs[4]["content"])
        self.assertIn("JSON only", msgs[4]["content"])
        self.assertEqual(self.mock.requests[1]["tool_choice"]["function"]["name"], "emit_plan")
        self.assertIsNotNone(kimi.last_metrics("plan"))

    def test_plan_cut_off_at_the_token_limit_gets_room_and_less_thinking(self):
        def handler(req):
            if not _has_tool_message(req):
                return tool_response("emit_plan", {"title": "Runner"}, call_id="call_cut", finish_reason="length")
            return tool_response("emit_plan", valid_plan(), call_id="call_ok")

        self.handler = handler
        out = self.kimi().plan({"goal": "runner"})
        self.assertEqual(out["files"][0]["path"], "index.html")
        first, second = self.mock.requests
        self.assertEqual(second["max_tokens"], min(2 * first["max_tokens"], 65536))
        self.assertEqual(first["reasoning_effort"], "max")
        self.assertEqual(second["reasoning_effort"], "low")
        self.assertIn("cut off at the token limit", second["messages"][-1]["content"])

    def test_plan_without_node_forbids_node_tests(self):
        with_node = valid_plan()
        with_node["acceptance_tests"][1]["kind"] = "node"
        browser_only = valid_plan()

        def handler(req):
            return tool_response("emit_plan", browser_only if _has_tool_message(req) else with_node)
        self.handler = handler
        out = self.kimi().plan({"goal": "runner"}, node_available=False)
        self.assertEqual({t["kind"] for t in out["acceptance_tests"]}, {"browser"})
        self.assertIn("Node.js is NOT available", _system_text(self.mock.requests[0]))
        self.assertEqual(len(self.mock.requests), 2)
        self.assertIn("at-02", self.mock.requests[1]["messages"][3]["content"])
        # a model that insists on node tests is refused with the reason (the PLAN stage then blocks)
        self.mock.requests.clear()
        self.handler = lambda req: tool_response("emit_plan", with_node)
        with self.assertRaises(ValueError) as cm:
            self.kimi().plan({"goal": "runner"}, node_available=False)
        self.assertIn("Node.js is not available", str(cm.exception))
        # with Node the same plan is fine and Kimi is told that node tests can run
        self.mock.requests.clear()
        out = self.kimi().plan({"goal": "runner"}, node_available=True)
        self.assertEqual(out["acceptance_tests"][1]["kind"], "node")
        self.assertIn("Node.js is available", _system_text(self.mock.requests[0]))

    def test_plan_raises_after_failed_repair(self):
        self.handler = lambda req: tool_response("emit_plan", valid_plan(n_tests=3))
        with self.assertRaises(ValueError) as cm:
            self.kimi().plan({"goal": "x"}, min_tests=6)
        self.assertIn("after one repair attempt", str(cm.exception))
        self.assertIn("at least 6", str(cm.exception))
        self.assertEqual(len(self.mock.requests), 2)

    def test_plan_unparsable_answer_then_text_json(self):
        self.handler = scripted_handler([
            (lambda req: len(_messages(req)) == 2, {"content": "Sorry, thinking out loud only."}),
            (lambda req: len(_messages(req)) == 4, {"content": "```json\n" + json.dumps(valid_plan()) + "\n```"}),
        ])
        out = self.kimi().plan({"goal": "x"})
        self.assertEqual(out["title"], "Sky Runner")
        repair_msg = self.mock.requests[1]["messages"][-1]["content"]
        self.assertIn("no parsable emit_plan JSON object", repair_msg)

    def test_normalize_plan_paths_and_defaults(self):
        plan = valid_plan(
            engine="Three.js",
            files=[{"path": "./index.html", "purpose": ""}, "src\\game.js", {"path": "/assets/a.png", "purpose": "art"},
                   {"path": "index.html", "purpose": "entry point"}],
            tasks=[{"id": "t1", "title": "a", "files": ["./src/game.js"], "done_when": "x"}],
            acceptance_tests=[{"id": f"at-{i}", "steps": ["open", "click"], "required": "true" if i == 0 else None}
                              for i in range(6)],
            constraints={"max_total_mb": "10", "touch_controls": False, "notes": "no audio"},
        )
        out = normalize_plan(plan)
        self.assertEqual(out["engine"], "three")
        self.assertEqual(out["files"], [{"path": "index.html", "purpose": "entry point"},
                                        {"path": "src/game.js", "purpose": ""},
                                        {"path": "assets/a.png", "purpose": "art"}])
        self.assertEqual(out["tasks"][0]["files"], ["src/game.js"])
        self.assertEqual(out["tasks"][0]["depends_on"], [])
        test0 = out["acceptance_tests"][0]
        self.assertEqual((test0["kind"], test0["required"], test0["name"], test0["steps"]),
                         ("browser", True, "at-0", "open\nclick"))
        self.assertFalse(out["acceptance_tests"][1]["required"])
        self.assertEqual(out["constraints"], {"no_external_deps": True, "max_total_mb": 10, "touch_controls": False,
                                              "notes": "no audio"})
        self.assertTrue(out["preview"])
        zip_only = normalize_plan(valid_plan(engine="zip-only"))
        self.assertFalse(zip_only["preview"])
        self.assertTrue(normalize_plan(valid_plan(engine="zip-only", preview=True))["preview"])

    def test_normalize_plan_rejections(self):
        cases = {
            "escapes the project root": valid_plan(files=[{"path": "index.html"}, {"path": "../evil.js"}]),
            "not an earlier task id": valid_plan(tasks=[{"id": "t1", "depends_on": ["t2"]}, {"id": "t2"}]),
            "duplicate acceptance test id": valid_plan(acceptance_tests=[{"id": "a", "kind": "browser"}] * 7),
            "allowed: browser, node": valid_plan(acceptance_tests=[{"id": f"x{i}", "kind": "e2e"} for i in range(6)]),
            "'engine' is 'unity'": valid_plan(engine="unity"),
            "missing required key 'contract'": {k: v for k, v in valid_plan().items() if k != "contract"},
            "missing required key 'constraints'": {k: v for k, v in valid_plan().items() if k != "constraints"},
            "duplicate task id": valid_plan(tasks=[{"id": "t1"}, {"id": "t1"}]),
            "acceptance_tests[0] has an empty id": valid_plan(acceptance_tests=[{"id": " "}] + valid_plan()["acceptance_tests"]),
            "design_brief_for_nemotron": valid_plan(design_brief_for_nemotron=""),
            "absolute path not allowed": valid_plan(files=[{"path": "index.html"}, {"path": "C:\\x.js"}]),
            "max_total_mb must be a number": valid_plan(constraints={"max_total_mb": "big"}),
        }
        for needle, plan in cases.items():
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as cm:
                    normalize_plan(plan)
                self.assertIn(needle, str(cm.exception))
        # self-dependency is a forward reference too
        with self.assertRaises(ValueError):
            normalize_plan(valid_plan(tasks=[{"id": "t1", "depends_on": ["t1"]}]))
        # min_tests is honoured
        self.assertEqual(len(normalize_plan(valid_plan(n_tests=2), min_tests=2)["acceptance_tests"]), 2)

    def test_wrapped_plan_payload_is_unwrapped(self):
        self.handler = lambda req: tool_response("emit_plan", {"plan": valid_plan()})
        out = self.kimi().plan({"goal": "x"})
        self.assertEqual(out["title"], "Sky Runner")
        self.assertEqual(len(self.mock.requests), 1)
        self.assertEqual(unwrap_payload({"arguments": {"a": 1}}, "verdict"), {"a": 1})
        self.assertEqual(unwrap_payload({"review": {"a": 1}}, "emit_review"), {"a": 1})
        self.assertEqual(unwrap_payload({"other": {"a": 1}}, "emit_review"), {"other": {"a": 1}})
        self.assertEqual(unwrap_payload({"plan": {"a": 1}, "x": 2}, "emit_plan"), {"plan": {"a": 1}, "x": 2})
        self.assertIsNone(unwrap_payload(None, "emit_plan"))
        with self.assertRaises(ValueError):
            normalize_plan(valid_plan(constraints={"max_total_mb": float("nan")}))

    def test_normalize_rel_path(self):
        self.assertEqual(normalize_rel_path("././src//game.js"), "src/game.js")
        self.assertEqual(normalize_rel_path("\\assets\\x.png"), "assets/x.png")
        self.assertEqual(normalize_rel_path(".kyora/manifest.json"), ".kyora/manifest.json")
        for bad in ("", "..", "a/../../b", "C:/x", "~/x", "./"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_rel_path(bad)


# ----------------------------------------------------------------------------- Kimi: judge
class KimiJudgeTests(_MockCase):
    CTX = {"workspace": "/home/u/.kyora/projects/p_1/repo", "project_id": "p_1", "iteration": 2,
           "plan": valid_plan(), "recent_calls": [{"tool_name": "Write", "summary": "src/game.js"}]}

    def test_verdict_parse_and_request_shape(self):
        self.handler = lambda req: tool_response("verdict", {"decision": "DENY", "reason": "Edits tests to always pass.",
                                                             "risk": "High"}, reasoning="checking")
        sub = self.bus.subscribe(run_id="r_j", types=[MODEL_DELTA])
        call = {"tool_name": "Write", "tool_use_id": "tu_1", "cwd": "/home/u/.kyora/projects/p_1/repo",
                "tool_input": {"file_path": "tests/browser-tests.js", "content": "x" * 5000}}
        kimi = self.kimi()
        out = kimi.judge(self.CTX, call, "r_j")
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(out["risk"], "high")
        self.assertEqual(out["reason"], "Edits tests to always pass.")
        self.assertEqual(out["source"], "kimi")
        self.assertEqual(out["judge_model"], KIMI_MODEL)
        self.assertIsInstance(out["latency_ms"], int)
        self.assertTrue(sub.queue.empty(), "the judge does not publish token deltas")
        sub.close()
        req = self.mock.last_request
        self.assertEqual(req["tool_choice"], {"type": "function", "function": {"name": "verdict"}})
        schema = req["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["decision"]["enum"], ["allow", "deny"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(req["reasoning_effort"], "high")  # judge_reasoning_effort
        self.assertEqual(req["max_tokens"], Kimi.MAX_TOKENS["judge"])
        system = _system_text(req)
        self.assertIn("/home/u/.kyora/projects/p_1/repo", system)
        self.assertIn("fix iteration 2", system)
        self.assertIn("Sky Runner", system)
        user = last_user_text(req)
        payload = json.loads(user[user.index("{"):])
        self.assertEqual(payload["tool_name"], "Write")
        self.assertTrue(payload["tool_input_truncated"])
        self.assertIn("[truncated 1000 characters]", payload["tool_input"]["content"])
        self.assertLess(len(payload["tool_input"]["content"]), 4100)
        self.assertEqual(payload["plan_files"], ["index.html", "src/game.js"])
        self.assertEqual(payload["constraints"], {"no_external_deps": True})
        self.assertEqual(payload["acceptance_test_ids"], [f"at-{i:02d}" for i in range(1, 7)])
        self.assertEqual(payload["recent_calls"][0]["tool_name"], "Write")
        self.assertEqual(self.events("model.metrics", "r_j")[0].data["purpose"], "judge")

    def test_judge_raises_on_invalid_verdict(self):
        self.handler = lambda req: tool_response("verdict", {"decision": "maybe", "reason": "?", "risk": "low"})
        with self.assertRaises(ValueError):
            self.kimi().judge({}, {"tool_name": "Bash", "tool_input": {"command": "x"}}, "r")
        self.assertEqual(len(self.mock.requests), 2)  # one repair

    def test_judge_service_integration(self):
        from kyora.pipeline.judge import JudgeService, rule_verdict

        self.handler = lambda req: tool_response("verdict", {"decision": "allow", "reason": "Ordinary build step.",
                                                             "risk": "low"})
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as home:
            ctx = {"workspace": ws, "project_id": "p_1", "plan": valid_plan()}
            call = {"tool_name": "Bash", "tool_input": {"command": "node scripts/build.js --release"}, "tool_use_id": "t1"}
            if rule_verdict(ctx, call) is not None:
                self.skipTest("local judge rules decide this call; Kimi is not consulted")
            service = JudgeService(self.kimi(), self.bus, None, Path(home))
            service.register_context("r_js", ctx)
            verdict = service.decide("r_js", call)
        self.assertEqual(verdict["decision"], "allow")
        self.assertEqual(verdict["source"], "kimi")
        self.assertEqual(verdict["judge_model"], KIMI_MODEL)

    def test_normalize_verdict_and_truncation_units(self):
        self.assertEqual(normalize_verdict({"decision": "Allow", "reason": "", "risk": "LOW"}),
                         {"decision": "allow", "reason": "", "risk": "low"})
        for bad in ({"decision": "deny", "reason": "", "risk": "low"}, {"decision": "allow", "reason": "x"},
                    {"decision": "allow", "reason": "x", "risk": "extreme"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_verdict(bad)
        original = {"edits": [{"old_string": "a" * 10, "new_string": "b" * 20}], "n": 3}
        cut, truncated = truncate_strings(copy.deepcopy(original), limit=15)
        self.assertTrue(truncated)
        self.assertEqual(cut["edits"][0]["old_string"], "a" * 10)
        self.assertTrue(cut["edits"][0]["new_string"].startswith("b" * 15 + "… [truncated 5"))
        self.assertEqual(cut["n"], 3)
        self.assertEqual(truncate_strings(original, limit=100), (original, False))


# ----------------------------------------------------------------------------- Kimi: review
class KimiReviewTests(_MockCase):
    GATE_FAIL = {"threshold": 0.94, "passed": 41, "total": 46, "pass_rate": 0.891, "ok": False,
                 "required_failures": ["at-03"]}

    def test_complete_is_coerced_to_fix_when_gate_failed(self):
        self.handler = lambda req: tool_response("emit_review", {"decision": "complete", "reasons": ["Looks good."],
                                                                 "fix_instructions": "", "suspected_files": ["./src/game.js"]})
        shots = [PNG_1x1] * 5
        out = self.kimi().review({"iteration": 1}, self.GATE_FAIL, shots, run_id="r_rv", project_id="p")
        self.assertEqual(out["decision"], "fix")
        self.assertTrue(out["coerced"])
        self.assertEqual(out["reasons"], ["Looks good.", GATE_NOT_PASSED_REASON])
        self.assertEqual(out["suspected_files"], ["src/game.js"])
        req = self.mock.last_request
        user = [m for m in req["messages"] if m["role"] == "user"][0]["content"]
        images = [p for p in user if p["type"] == "image_url"]
        self.assertEqual(len(images), 4)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(images[0]["image_url"]["url"], PNG_DATA_URL)
        system = _system_text(req)
        self.assertIn("up to 4 screenshots", system)
        self.assertIn('"pass_rate": 0.891', system)
        self.assertEqual(req["tool_choice"]["function"]["name"], "emit_review")

    def test_complete_kept_when_gate_ok_and_fix_without_instructions_repaired(self):
        self.handler = lambda req: tool_response("emit_review", {"decision": "complete", "reasons": ["Gate passed."],
                                                                 "fix_instructions": "", "suspected_files": []})
        out = self.kimi().review({}, {"ok": True, "pass_rate": 0.97}, [])
        self.assertEqual(out["decision"], "complete")
        self.assertFalse(out["coerced"])
        self.mock.reset()
        self.handler = scripted_handler([
            (lambda req: not _has_tool_message(req), tool_response("emit_review", {"decision": "fix", "reasons": ["x"]})),
            (_has_tool_message, tool_response("emit_review", {
                "decision": "fix", "reasons": ["Score never increments (at-04)."],
                "fix_instructions": "1. Increment score on coin pickup.", "suspected_files": ["src/game.js", "../x"]})),
        ])
        out = self.kimi().review({}, self.GATE_FAIL, [b"", "not-an-image"])
        self.assertEqual(out["decision"], "fix")
        self.assertEqual(out["suspected_files"], ["src/game.js"])
        self.assertEqual(len(self.mock.requests), 2)
        self.assertFalse(has_image(self.mock.requests[0]))

    def test_stop_and_guard_units(self):
        out = apply_gate_guard(normalize_review({"decision": "stop", "reasons": "contradictory plan"}), {"ok": False})
        self.assertEqual((out["decision"], out["reasons"], out["coerced"]), ("stop", ["contradictory plan"], False))
        for gate in (None, {}, {"ok": "true"}, {"ok": 1}):
            with self.subTest(gate=gate):
                self.assertEqual(apply_gate_guard({"decision": "complete", "reasons": []}, gate)["decision"], "fix")
        with self.assertRaises(ValueError):
            normalize_review({"decision": "ship it"})


class ConcurrencyTests(_MockCase):
    def test_parallel_judges_share_one_kimi(self):
        self.handler = lambda req: tool_response("verdict", {"decision": "allow", "reason": "ok", "risk": "low"})
        kimi = self.kimi()
        results: "queue.Queue" = queue.Queue()

        def worker(i: int) -> None:
            try:
                results.put(kimi.judge({"workspace": "/ws"}, {"tool_name": "Bash", "tool_input": {"command": f"c{i}"}},
                                       f"r{i}")["decision"])
            except Exception as exc:  # noqa: BLE001
                results.put(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        got = [results.get_nowait() for _ in range(6)]
        self.assertEqual(got, ["allow"] * 6)
        self.assertIsNotNone(kimi.last_metrics("judge"))


if __name__ == "__main__":
    unittest.main()
