"""The Kimi role on OpenRouter (Kimi K3.1 Pro) with its fallback models on NVIDIA's endpoint.

What the user specified (2026-11-03): ``moonshotai/kimi-k3.1-pro`` at ``https://openrouter.ai/api/v1`` with
``reasoning: {"enabled": true}`` and ``provider: {"only": ["moonshotai/mxfp4"], "allow_fallbacks": false}``,
the assistant message's ``reasoning_details`` passed back unmodified in the next turn. The key is the user's
and the repository is public, so it lives in ``config/keys.local.json`` (git-ignored) or OPENROUTER_API_KEY.

Two mock servers stand in for OpenRouter and NVIDIA (no network). ``openrouter=True`` makes a client on
127.0.0.1 speak OpenRouter's dialect, as one on openrouter.ai does by its host.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kyora.config import Config, KimiCfg, is_openrouter as config_is_openrouter
from kyora.models.kimi import ENDPOINT_RETRY_S, Kimi, KimiConversation
from kyora.models.nim_client import (
    BUSY_COOLDOWN_S, ModelChain, NIMClient, NIMEndpointDown, NIMError, _ReasoningDetails, adapt_messages,
    call_with_model_fallback, function_tool, is_openrouter,
)
from kyora.pipeline.events import EventBus
from tests.mock_nim import MockNIM

REPO = Path(__file__).resolve().parent.parent
K31, ULTRA, GLM = "moonshotai/kimi-k3.1-pro", "nvidia/nemotron-3-ultra-550b-a55b", "z-ai/glm-5.3"
OR_KEY, NV_KEY = "sk-or-v1-test-0123456789abcdef0123456789", "nvapi-test-working-0123456789abcdef"
PROVIDER = {"only": ["moonshotai/mxfp4"], "allow_fallbacks": False}
CREDITS = {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}
NO_TOOL_ENDPOINT = {"error": {"code": 404, "message": "No endpoints found that support tool use. To learn more about "
                                                      "provider routing, visit: https://openrouter.ai/docs/provider-routing"}}
NO_IMAGE_ENDPOINT = {"error": {"code": 404, "message": "No endpoints found that support image input"}}
INTAKE_ARGS = {"goal": "A paddle game", "genre": "arcade", "platform": "web", "must_haves": ["paddle"],
               "references": [], "risks": [], "imported_base": False, "notes": ""}
FRAGMENTS = [  # one reasoning block in three fragments: text, text, signature (OpenRouter streams it so)
    {"type": "reasoning.text", "text": "Count the ", "format": "unknown", "index": 0},
    {"type": "reasoning.text", "text": "letters.", "format": "unknown", "index": 0},
    {"type": "reasoning.text", "signature": "sig-abc", "format": "unknown", "index": 0},
]
MERGED = [{"type": "reasoning.text", "text": "Count the letters.", "format": "unknown", "index": 0,
           "signature": "sig-abc"}]


def _client(mock_: MockNIM, key: str, **kw) -> NIMClient:
    return NIMClient(mock_.url, key, backoff_s=(0.01,), max_attempts=2, timeout_s=20, **kw)


def _intake_answer() -> dict:
    return {"tool_calls": [{"id": "call_1", "name": "emit_intake", "arguments": INTAKE_ARGS}]}


class _TwoEndpoints(unittest.TestCase):
    """``self.or_`` plays OpenRouter (Kimi K3.1 Pro), ``self.nv`` NVIDIA (the fallback models)."""

    def setUp(self):
        self.or_handler = lambda req: _intake_answer()
        self.nv_handler = lambda req: _intake_answer()
        self.or_ = MockNIM(lambda req: self.or_handler(req), models=[K31]).start()
        self.nv = MockNIM(lambda req: self.nv_handler(req), models=[ULTRA, GLM]).start()
        self.addCleanup(self.or_.stop)
        self.addCleanup(self.nv.stop)
        self.bus = EventBus(None)
        self.addCleanup(self.bus.close)

    def kimi(self, or_key: str = OR_KEY, primary: NIMClient | None = None, effort: str = "max") -> Kimi:
        primary = primary or _client(self.or_, or_key, openrouter=True)
        return Kimi(primary, K31, KimiCfg(effort, effort, "top_level"), self.bus, fallback_models=[ULTRA, GLM],
                    fallback_client=_client(self.nv, NV_KEY, key_label="nemotron key"), provider=PROVIDER)

    def fallbacks(self) -> list[dict]:
        return [e.data for e in self.bus.replay(0) if e.type == "model.fallback"]


class OpenRouterRequestTests(_TwoEndpoints):
    def test_the_request_is_the_one_the_user_specified(self):
        out = self.kimi().intake("A paddle game", None)
        self.assertEqual(out["goal"], "A paddle game")
        self.assertEqual(len(self.nv.requests), 0, "NVIDIA is not asked while OpenRouter answers")
        req = self.or_.requests[0]
        self.assertEqual(req["model"], K31)
        self.assertEqual(req["reasoning"], {"enabled": True})
        self.assertEqual(req["provider"], PROVIDER)
        for absent in ("reasoning_effort", "chat_template_kwargs"):
            self.assertNotIn(absent, req)
        self.assertEqual((req["temperature"], req["top_p"], req["stream"]), (1.0, 1.0, True))
        auth = [c for c in self.or_.calls if c["method"] == "POST"][0]["headers"]["authorization"]
        self.assertEqual(auth, f"Bearer {OR_KEY}", "the OpenRouter key goes to OpenRouter")

    def test_lower_efforts_name_openrouters_effort_and_events_say_what_was_sent(self):
        self.kimi(effort="low").intake("A paddle game", None)
        self.assertEqual(self.or_.requests[0]["reasoning"], {"enabled": True, "effort": "low"})
        req_ev = [e.data for e in self.bus.replay(0) if e.type == "model.request"][0]
        self.assertEqual(req_ev["effort_sent"], 'reasoning={"enabled":true,"effort":"low"} (OpenRouter)')
        self.assertEqual(json.loads(req_ev["provider_sent"]), PROVIDER)

    def test_reasoning_details_go_back_unmodified_and_only_to_openrouter(self):
        lookup = function_tool("lookup", "Look a word up.", {"type": "object", "properties": {}})
        turns = iter([{"reasoning_details": FRAGMENTS, "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {}}]},
                      {"content": "three"}])
        self.or_handler = lambda req: next(turns)
        kimi = self.kimi()
        conv = KimiConversation("system")
        conv.add_user("How many r's are in strawberry?")
        res = kimi.think(conv, tools=[lookup], tool_impl={"lookup": lambda args: "strawberry"})
        self.assertEqual(res.content, "three")
        first_answer = conv.messages()[2]
        self.assertEqual(first_answer["reasoning_details"], MERGED, "fragments merged into one block")
        echoed = self.or_.requests[1]["messages"][2]
        self.assertEqual(echoed["reasoning_details"], MERGED, "sent back unmodified")
        self.assertNotIn("reasoning_content", echoed, "OpenRouter gets reasoning_details, not reasoning_content")
        self.assertEqual(echoed["tool_calls"][0]["function"]["name"], "lookup")

    def test_a_fallback_on_nvidia_gets_reasoning_content_and_no_reasoning_details(self):
        lookup = function_tool("lookup", "Look a word up.", {"type": "object", "properties": {}})
        turns = iter([{"reasoning_details": FRAGMENTS, "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {}}]},
                      {"status": 402, "error_body": CREDITS}])
        self.or_handler = lambda req: next(turns)
        self.nv_handler = lambda req: {"content": "three"}
        conv = KimiConversation("system")
        conv.add_user("How many r's are in strawberry?")
        res = self.kimi().think(conv, tools=[lookup], tool_impl={"lookup": lambda args: "strawberry"})
        self.assertEqual((res.content, res.model), ("three", ULTRA))
        sent = self.nv.requests[0]["messages"][2]
        self.assertNotIn("reasoning_details", sent, "an NVIDIA endpoint never gets OpenRouter's field")
        self.assertEqual(sent["reasoning_content"], "Count the letters.")
        self.assertEqual(self.nv.requests[0]["chat_template_kwargs"], {"enable_thinking": True})
        for absent in ("provider", "reasoning"):
            self.assertNotIn(absent, self.nv.requests[0], "routing and reasoning fields stay at OpenRouter")


class EndpointFallbackTests(_TwoEndpoints):
    def test_without_a_key_nothing_goes_to_openrouter_and_nvidia_answers(self):
        out = self.kimi(or_key="").intake("A paddle game", None)
        self.assertEqual(out["goal"], "A paddle game")
        self.assertEqual(self.or_.requests, [], "no request without a key")
        self.assertEqual(self.nv.requests[0]["model"], ULTRA)
        auth = [c for c in self.nv.calls if c["method"] == "POST"][0]["headers"]["authorization"]
        self.assertEqual(auth, f"Bearer {NV_KEY}", "the NVIDIA key goes to NVIDIA")
        [fb] = self.fallbacks()
        self.assertEqual((fb["from_model"], fb["to_model"], fb["temporary"]), (K31, ULTRA, True))
        self.assertIn("no API key for", fb["reason"])
        self.assertIn("nothing was sent there", fb["reason"])
        self.assertIn("keys.local.json", fb["reason"])
        self.assertIsNone(fb["status"], "no request, so no HTTP status (the UI would say 'answered HTTP ...')")
        self.assertEqual(fb["cooldown_s"], ENDPOINT_RETRY_S["key"])

    def test_no_credits_moves_to_nvidia_and_openrouter_waits(self):
        self.or_handler = lambda req: {"status": 402, "error_body": CREDITS}
        kimi = self.kimi()
        kimi.intake("A paddle game", None)
        self.assertEqual(len(self.or_.requests), 1, "402 is not retried")
        self.assertEqual(self.nv.requests[0]["model"], ULTRA)
        [fb] = self.fallbacks()
        self.assertIn("no credits", fb["reason"])
        self.assertGreater(kimi.chain.busy()[K31], BUSY_COOLDOWN_S)
        kimi.intake("A paddle game", None)  # the next call starts on NVIDIA while OpenRouter waits
        self.assertEqual(len(self.or_.requests), 1)
        self.assertEqual(len(self.nv.requests), 2)

    def test_an_unreachable_openrouter_moves_to_nvidia(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        dead = NIMClient(f"http://127.0.0.1:{port}/api/v1", OR_KEY, backoff_s=(0.01,), max_attempts=1, timeout_s=5,
                         openrouter=True)
        out = self.kimi(primary=dead).intake("A paddle game", None)
        self.assertEqual(out["goal"], "A paddle game")
        [fb] = self.fallbacks()
        self.assertIn("unreachable", fb["reason"])
        self.assertEqual(fb["cooldown_s"], ENDPOINT_RETRY_S["unreachable"])

    def test_no_provider_for_tools_degrades_the_tool_mode_and_stays_on_openrouter(self):
        def handler(req):
            if isinstance(req.get("tool_choice"), dict):  # a forced tool: no endpoint of the pinned provider
                return {"status": 404, "error_body": NO_TOOL_ENDPOINT}
            return _intake_answer()
        self.or_handler = handler
        kimi = self.kimi()
        out = kimi.intake("A paddle game", None)
        self.assertEqual(out["goal"], "A paddle game")
        self.assertEqual(kimi.tool_mode, "auto")
        self.assertEqual(self.nv.requests, [], "a tool refusal is not a reason to leave OpenRouter")
        self.assertEqual(kimi.chain.gone(), {}, "and the model is not gone")

    def test_no_provider_for_images_resends_without_them(self):
        def handler(req):
            has_image = any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
                            for m in req["messages"])
            return {"status": 404, "error_body": NO_IMAGE_ENDPOINT} if has_image else _intake_answer()
        self.or_handler = handler
        att = [{"name": "ref.png", "mime": "image/png", "image_data_url": "data:image/png;base64,iVBORw0KGgo="}]
        out = self.kimi().intake("A paddle game", att)
        self.assertEqual(out["goal"], "A paddle game")
        self.assertEqual(len(self.or_.requests), 2, "once with the image, once without")
        self.assertEqual(self.nv.requests, [])


class ReasoningDetailsMergeTests(unittest.TestCase):
    def merge(self, frags):
        acc = _ReasoningDetails()
        text = "".join(acc.add(f) for f in frags)
        return acc.blocks, text

    def test_text_pieces_and_a_signature_fragment_become_one_block(self):
        blocks, text = self.merge(FRAGMENTS)
        self.assertEqual(blocks, MERGED)
        self.assertEqual(text, "Count the letters.")

    def test_an_encrypted_block_on_the_same_index_stays_separate(self):
        blocks, _ = self.merge([{"type": "reasoning.text", "text": "a", "index": 0},
                                {"type": "reasoning.encrypted", "data": "QUJD", "index": 0},
                                {"type": "reasoning.text", "text": "b", "index": 0}])
        self.assertEqual(blocks, [{"type": "reasoning.text", "text": "ab", "index": 0},
                                  {"type": "reasoning.encrypted", "data": "QUJD", "index": 0}])

    def test_a_different_id_starts_a_new_block_and_a_missing_one_does_not(self):
        blocks, _ = self.merge([{"type": "reasoning.text", "text": "a", "index": 0, "id": "r1"},
                                {"type": "reasoning.text", "text": "b", "index": 0},
                                {"type": "reasoning.text", "text": "c", "index": 0, "id": "r2"}])
        self.assertEqual([b["text"] for b in blocks], ["ab", "c"])

    def test_summaries_concatenate_typeless_fragments_continue_and_unindexed_pass_through(self):
        blocks, text = self.merge([{"type": "reasoning.summary", "summary": "Plan: ", "index": 0},
                                   {"summary": "count.", "index": 0},
                                   {"type": "reasoning.encrypted", "data": "ZZ", "id": "x"}])
        self.assertEqual(blocks, [{"type": "reasoning.summary", "summary": "Plan: count.", "index": 0},
                                  {"type": "reasoning.encrypted", "data": "ZZ", "id": "x"}])
        self.assertEqual(text, "Plan: count.")

    def test_non_stream_answers_keep_them_as_sent(self):
        nim = MockNIM(lambda req: {"content": "3", "reasoning_details": MERGED}).start()
        self.addCleanup(nim.stop)
        res = _client(nim, OR_KEY, openrouter=True).chat(K31, [{"role": "user", "content": "x"}], stream=False)
        self.assertEqual(res.message["reasoning_details"], MERGED)
        self.assertEqual(res.reasoning, "Count the letters.")

    def test_streamed_text_is_counted_once(self):
        nim = MockNIM(lambda req: {"content": "3", "reasoning_details": FRAGMENTS}).start()
        self.addCleanup(nim.stop)
        res = _client(nim, OR_KEY, openrouter=True).chat(K31, [{"role": "user", "content": "x"}])
        self.assertEqual(res.reasoning, "Count the letters.", "OpenRouter repeats it in reasoning_details")
        self.assertEqual(res.reasoning_details, MERGED)


class DialectTests(unittest.TestCase):
    def test_openrouter_is_recognised_by_host_only(self):
        for url, want in (("https://openrouter.ai/api/v1", True), ("https://eu.openrouter.ai/api/v1", True),
                          ("https://openrouter.ai.evil.test/v1", False), ("https://integrate.api.nvidia.com/v1", False),
                          ("http://127.0.0.1:9/v1", False), ("", False)):
            self.assertEqual(is_openrouter(url), want, url)
            self.assertEqual(config_is_openrouter(url), want, url)
        self.assertTrue(NIMClient("http://127.0.0.1:9/v1", "", openrouter=True).openrouter)

    def test_messages_are_adapted_per_endpoint_without_touching_the_conversation(self):
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "reasoning_content": "r", "reasoning_details": MERGED}]
        to_or = adapt_messages(msgs, openrouter=True)
        to_nv = adapt_messages(msgs, openrouter=False)
        self.assertEqual(set(to_or[1]), {"role", "content", "reasoning_details"})
        self.assertEqual(set(to_nv[1]), {"role", "content", "reasoning_content"})
        self.assertIn("reasoning_details", msgs[1], "the caller's messages are unchanged")
        body = NIMClient("https://openrouter.ai/api/v1", OR_KEY).build_body(K31, msgs)
        self.assertNotIn("reasoning_content", body["messages"][1])


class ChainRuleTests(unittest.TestCase):
    def test_an_endpoint_down_moves_on_and_waits_its_own_cooldown(self):
        chain = ModelChain(K31, [ULTRA])
        bus = EventBus(None)
        self.addCleanup(bus.close)

        def call(model):
            if model == K31:
                raise NIMEndpointDown("openrouter.ai answered HTTP 402", status=402, cooldown_s=1800)
            return "answered"
        self.assertEqual(call_with_model_fallback(chain, bus, call, role="kimi"), "answered")
        self.assertGreater(chain.busy()[K31], 1700)
        [fb] = [e.data for e in bus.replay(0) if e.type == "model.fallback"]
        self.assertEqual((fb["status"], fb["temporary"], fb["cooldown_s"]), (402, True, 1800))
        with self.assertRaises(NIMEndpointDown):  # nothing left: the failure is raised
            call_with_model_fallback(ModelChain(K31, []), None, call, role="kimi")

    def test_no_provider_now_is_a_wait_and_a_tool_refusal_is_raised(self):
        chain = ModelChain(K31, [ULTRA])

        def no_provider(model):
            if model == K31:
                raise NIMError("HTTP 404", status=404, body=json.dumps(
                    {"error": {"message": "No allowed providers are available for the selected model."}}))
            return "answered"
        self.assertEqual(call_with_model_fallback(chain, None, no_provider, role="kimi"), "answered")
        self.assertIn(K31, chain.busy())
        self.assertEqual(chain.gone(), {}, "not gone: the pinned provider may return")

        def tools_refused(model):
            raise NIMError("HTTP 404", status=404, body=json.dumps(NO_TOOL_ENDPOINT))
        chain = ModelChain(K31, [ULTRA])
        with self.assertRaises(NIMError):
            call_with_model_fallback(chain, None, tools_refused, role="kimi")
        self.assertEqual((chain.gone(), chain.busy()), ({}, {}))


class KeyStorageTests(unittest.TestCase):
    """keys.json may be public: keys entered in Settings go to keys.local.json, never into keys.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.keys = json.loads((REPO / "config" / "keys.json").read_text(encoding="utf-8"))
        (self.dir / "keys.json").write_text(json.dumps(self.keys), encoding="utf-8")
        (self.dir / "settings.json").write_text("{}", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, name):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def test_the_committed_kimi_role_is_kimi_k31_pro_at_openrouter_without_a_key(self):
        nv = Config.load(self.dir).nvidia
        self.assertEqual((nv.kimi.model, nv.kimi.base_url, nv.kimi.api_key),
                         (K31, "https://openrouter.ai/api/v1", ""))
        self.assertEqual(nv.kimi.provider, PROVIDER)
        self.assertEqual(nv.kimi.fallback_base_url, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(nv.keys_for(nv.kimi.fallback_base_url), [("nemotron key", nv.nemotron.api_key)])
        self.assertTrue(nv.nemotron.api_key.startswith("nvapi-"))
        self.assertEqual(nv.alt_keys("kimi"), [], "the NVIDIA key never goes to OpenRouter")
        # a checkout or an unpacked private release may hold the user's keys.local.json: git must ignore it
        # (a release folder ships no .gitignore and is no git checkout)
        gitignore = REPO / ".gitignore"
        if gitignore.is_file():
            ignored = {ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()}
            self.assertTrue({"keys.local.json", "config/keys.local.json"} & ignored, ".gitignore keeps it out")

    def test_provider_routing_is_turned_off_with_null_and_kept_with_an_empty_object(self):
        # README: "provider": null lets OpenRouter choose; {} merges with the default and keeps the pin
        off = Config(Path(tempfile.gettempdir()), {"nvidia": {"kimi": {"provider": None}}}, {})
        self.assertEqual(off.nvidia.kimi.provider, {})
        kept = Config(Path(tempfile.gettempdir()), {"nvidia": {"kimi": {"provider": {}}}}, {})
        self.assertEqual(kept.nvidia.kimi.provider, PROVIDER)

    def test_a_local_file_supplies_the_key_and_settings_keep_it_there(self):
        (self.dir / "keys.local.json").write_text(json.dumps({"nvidia": {"kimi": {"api_key": OR_KEY}}}), encoding="utf-8")
        cfg = Config.load(self.dir)
        self.assertEqual(cfg.nvidia.kimi.api_key, OR_KEY)
        self.assertEqual(cfg.to_public()["local_keys"], ["nvidia.kimi.api_key"])
        self.assertNotIn(OR_KEY, json.dumps(cfg.to_public()), "masked on the way out")
        cfg.apply_patch({"keys": {"nvidia.kimi.fallback_models": [ULTRA]}})
        cfg.save()
        on_disk = self.read("keys.json")
        self.assertEqual(on_disk["nvidia"]["kimi"]["api_key"], "", "keys.json keeps its empty slot")
        self.assertEqual(on_disk["nvidia"]["kimi"]["fallback_models"], [ULTRA], "other settings go to keys.json")
        self.assertEqual(self.read("keys.local.json"), {"nvidia": {"kimi": {"api_key": OR_KEY}}})

    def test_a_key_entered_in_settings_goes_to_the_local_file(self):
        cfg = Config.load(self.dir)
        committed = self.keys["nvidia"]["nemotron"]["api_key"]
        cfg.apply_patch({"keys": {"nvidia.kimi.api_key": OR_KEY, "nvidia.nemotron.api_key": NV_KEY}})
        cfg.save()
        on_disk = self.read("keys.json")
        self.assertEqual(on_disk["nvidia"]["kimi"]["api_key"], "")
        self.assertEqual(on_disk["nvidia"]["nemotron"]["api_key"], committed, "keys.json keeps the committed key")
        self.assertEqual(self.read("keys.local.json")["nvidia"],
                         {"kimi": {"api_key": OR_KEY}, "nemotron": {"api_key": NV_KEY}})
        again = Config.load(self.dir).nvidia
        self.assertEqual((again.kimi.api_key, again.nemotron.api_key), (OR_KEY, NV_KEY), "the local file wins")

    def test_environment_keys_go_only_to_their_own_endpoint_and_are_never_saved(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": OR_KEY, "NVIDIA_API_KEY": NV_KEY}):
            self.keys["nvidia"]["nemotron"]["api_key"] = ""
            (self.dir / "keys.json").write_text(json.dumps(self.keys), encoding="utf-8")
            cfg = Config.load(self.dir)
            self.assertEqual((cfg.nvidia.kimi.api_key, cfg.nvidia.nemotron.api_key), (OR_KEY, NV_KEY))
            cfg.apply_patch({"settings": {"kimi.reasoning_effort": "high"}})
            cfg.save()
        self.assertNotIn(OR_KEY, (self.dir / "keys.json").read_text(encoding="utf-8"))
        self.assertNotIn(NV_KEY, (self.dir / "keys.json").read_text(encoding="utf-8"))
        self.assertFalse((self.dir / "keys.local.json").exists())


class AppDefaultShapeTests(unittest.TestCase):
    """The committed configuration in a running app: without the OpenRouter key the role answers on NVIDIA."""

    def test_fallback_client_uses_the_nvidia_key_and_settings_apply(self):
        from kyora.app import App
        nv_mock = MockNIM(lambda req: _intake_answer(), models=[ULTRA]).start()
        self.addCleanup(nv_mock.stop)
        tmp = Path(tempfile.mkdtemp(prefix="kyora-or-app-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        cfg_dir = tmp / "config"
        cfg_dir.mkdir()
        keys = json.loads((REPO / "config" / "keys.json").read_text(encoding="utf-8"))
        keys["nvidia"]["base_url"] = nv_mock.url
        keys["nvidia"]["nemotron"]["api_key"] = NV_KEY
        (cfg_dir / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
        (cfg_dir / "settings.json").write_text("{}", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            app = App(Config.load(cfg_dir), home=tmp / "home")
        self.addCleanup(app.stop)
        self.assertTrue(app.nim_kimi.openrouter)
        self.assertIs(app.kimi.fallback_client, app.nim_kimi_fallback)
        self.assertEqual((app.nim_kimi_fallback.base_url, app.nim_kimi_fallback.api_key),
                         (nv_mock.url.rstrip("/"), NV_KEY))
        out = app.kimi.intake("A paddle game", None)  # no OpenRouter key: offline, NVIDIA answers
        self.assertEqual(out["goal"], "A paddle game")
        self.assertEqual(nv_mock.requests[0]["model"], ULTRA)
        # Settings: the fallbacks moved onto the role's endpoint -> one client again
        app.config.apply_patch({"keys": {"nvidia.kimi.fallback_base_url": "https://openrouter.ai/api/v1"}})
        app.apply_model_settings()
        self.assertIsNone(app.kimi.fallback_client)


class PlanFileTests(unittest.TestCase):
    """KYORA_PLAN.md names the model that planned, never a fixed (retired) one."""

    def _render(self, role_models: dict | None) -> str:
        from types import SimpleNamespace
        from kyora.pipeline.stages import planner_label, render_template
        run = SimpleNamespace(extra={"role_models": role_models} if role_models is not None else {})
        return render_template("KYORA_PLAN.md.tmpl", title="T", engine="vanilla", planner=planner_label(run),
                               preview=True, summary="", files_list="", tasks_list="", constraints_list="")

    def test_the_plan_file_names_the_answering_model(self):
        text = self._render({"plan": {"model": K31, "requested_model": K31, "fallback": False}})
        self.assertIn(f"Planned by the Kimi role ({K31}).", text)
        text = self._render({"plan": {"model": GLM, "requested_model": K31, "fallback": True}})
        self.assertIn(f"Planned by the Kimi role ({GLM}, fallback for {K31}).", text)
        text = self._render(None)  # a plan that no model call of this run produced
        self.assertIn("Planned by the Kimi role.", text)
        for t in (text, self._render({"plan": {"model": GLM, "fallback": True}})):
            self.assertNotIn("moonshotai/kimi-k3)", t)
            self.assertNotIn("$planner", t)


class HermeticTestTests(unittest.TestCase):
    """The test suite never picks up the developer's own keys (a checkout or a private release may hold them)."""

    def test_key_variables_are_dropped_and_keys_local_json_is_never_copied(self):
        import tests
        for name in tests.KEY_ENV:
            self.assertNotIn(name, os.environ, name)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "config"
            src.mkdir()
            (src / "keys.json").write_text("{}", encoding="utf-8")
            (src / "keys.local.json").write_text(json.dumps({"nvidia": {"kimi": {"api_key": OR_KEY}}}),
                                                 encoding="utf-8")
            dst = Path(tests.copy_config(src, Path(d) / "copy"))
            self.assertTrue((dst / "keys.json").is_file())
            self.assertFalse((dst / "keys.local.json").exists())
        # every test that copies the repository's config folder goes through copy_config
        for f in sorted((REPO / "tests").rglob("*.py")):
            text = f.read_text(encoding="utf-8")
            if 'copytree(REPO / "config"' in text:
                self.assertIn('ignore_patterns("keys.local.json")', text, f.name)


class ReleaseKeyTests(unittest.TestCase):
    def test_local_keys_stay_out_of_the_public_zip_and_go_into_the_private_one(self):
        from tests.test_release_build import _load_build_release
        br = _load_build_release()
        self.assertIn("keys.local.json", br.EXCLUDE_FILE_NAMES)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pyz = d / "kyora.pyz"
            pyz.write_bytes(b"PK\x05\x06" + b"\0" * 18)
            local = d / "keys.local.json"
            local.write_text(json.dumps({"nvidia": {"kimi": {"api_key": OR_KEY}}}), encoding="utf-8")
            public, _, _ = br.build_zip(REPO, pyz, d / "dist", "0.0.0", [d])
            private, _, _ = br.build_zip(REPO, pyz, d / "dist", "0.0.0", [d], local_keys=local)
            self.assertEqual(private.parent.name, "private")
            with zipfile.ZipFile(public) as zf:
                self.assertFalse([n for n in zf.namelist() if n.endswith("keys.local.json")])
            with zipfile.ZipFile(private) as zf:
                info = zf.getinfo("config/keys.local.json")
                self.assertEqual((info.external_attr >> 16) & 0o777, 0o600)
                self.assertEqual(json.loads(zf.read(info))["nvidia"]["kimi"]["api_key"], OR_KEY)
            if os.name == "posix":
                self.assertEqual(private.stat().st_mode & 0o777, 0o600, "the private ZIP is readable by its owner only")


if __name__ == "__main__":
    unittest.main()
