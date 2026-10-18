"""What the live NVIDIA API answered on 2026-10-18, reproduced against the mock:

* the key in the Nemotron slot: 403 "Authorization failed" for every model
* the key in the Kimi slot: 200 for nvidia/nemotron-3-ultra-550b-a55b
* moonshotai/kimi-k3: 410 "reached its end of life on 2026-10-15"

Kyora must keep every role working through the other configured key and the configured
fallback model, and say so in events and the self-check - never silently.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from kyora.config import Config, KimiCfg, NemotronCfg
from kyora.models.kimi import Kimi, KimiConversation
from kyora.models.nemotron import Nemotron
from kyora.models.nim_client import ModelChain, NIMClient, NIMError, explain_http_failure
from kyora.pipeline.events import EventBus
from tests.mock_nim import MockNIM

REPO = Path(__file__).resolve().parent.parent
GOOD, BAD = "nvapi-good-key", "nvapi-refused-key"
ULTRA, K3 = "nvidia/nemotron-3-ultra-550b-a55b", "moonshotai/kimi-k3"
GONE = {"type": "about:blank", "title": "Gone", "status": 410,
        "detail": "The model 'moonshotai/kimi-k3' has reached its end of life on 2026-10-15T08:00:00Z and is no longer available."}
FORBIDDEN = {"status": 403, "title": "Forbidden", "detail": "Authorization failed"}


class NvidiaLike:
    """Answers like integrate.api.nvidia.com did, by key and model."""

    def __init__(self):
        self.mock = MockNIM(self.handle, models=[ULTRA, "moonshotai/kimi-k2.6"]).start()

    def handle(self, req: dict) -> dict:
        auth = self.mock.calls[-1]["headers"].get("authorization", "")
        if req.get("model") == K3:
            return {"status": 410, "error_body": GONE}
        if auth != f"Bearer {GOOD}":
            return {"status": 403, "error_body": FORBIDDEN}
        return {"content": "ok", "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    def keys_sent(self) -> list[str]:
        return [c["headers"].get("authorization", "").replace("Bearer ", "") for c in self.mock.calls
                if c["method"] == "POST"]

    def models_sent(self) -> list[str]:
        return [r.get("model") for r in self.mock.requests]


class _Base(unittest.TestCase):
    def setUp(self):
        self.api = NvidiaLike()
        self.addCleanup(self.api.mock.stop)
        self.bus = EventBus(None)
        self.addCleanup(self.bus.close)

    def events(self, type_: str) -> list[dict]:
        return [e.data for e in self.bus.replay(0) if e.type == type_]


class ExplainTests(unittest.TestCase):
    def test_wording_for_what_nvidia_answered(self):
        self.assertIn("Public API Endpoints", explain_http_failure(403, json.dumps(FORBIDDEN), ULTRA))
        self.assertIn("Authorization failed", explain_http_failure(403, json.dumps(FORBIDDEN), ULTRA))
        retired = explain_http_failure(410, json.dumps(GONE), K3)
        self.assertIn("retired by the provider on 2026-10-15", retired)
        self.assertIn("fallback models", retired)
        self.assertIn("API key", explain_http_failure(401, "", ULTRA))
        self.assertIn("not served at this endpoint", explain_http_failure(404, "", "x/y"))
        self.assertEqual(explain_http_failure(None, "", "x"), "")


class KeyFallbackTests(_Base):
    def client(self, key: str, alt=(("kimi key", GOOD),)) -> NIMClient:
        return NIMClient(self.api.mock.url, key, key_label="nemotron key", alt_keys=alt, backoff_s=(0.01,))

    def test_refused_key_switches_to_the_other_slot_and_stays_switched(self):
        c = self.client(BAD)
        res = c.chat(ULTRA, [{"role": "user", "content": "hi"}], stream=False, max_tokens=1)
        self.assertEqual(res.content, "ok")
        self.assertEqual(self.api.keys_sent(), [BAD, GOOD])
        self.assertEqual(c.key_label, "kimi key")
        notes = c.pop_key_notes()
        self.assertEqual(notes, [{"kind": "key", "from_key": "nemotron key", "to_key": "kimi key", "status": 403,
                                  "model": ULTRA}])
        self.assertEqual(c.pop_key_notes(), [])
        c.chat(ULTRA, [{"role": "user", "content": "again"}], stream=False, max_tokens=1)
        self.assertEqual(self.api.keys_sent(), [BAD, GOOD, GOOD])  # sticky: the refused key is not retried

    def test_every_key_refused_raises_once_each(self):
        c = self.client(BAD, alt=(("kimi key", "nvapi-also-refused"),))
        with self.assertRaises(NIMError) as cm:
            c.chat(ULTRA, [{"role": "user", "content": "hi"}], stream=False, max_tokens=1)
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.api.keys_sent(), [BAD, "nvapi-also-refused"])

    def test_no_alternates_means_no_retry(self):
        c = self.client(BAD, alt=())
        with self.assertRaises(NIMError):
            c.chat(ULTRA, [{"role": "user", "content": "hi"}], stream=False, max_tokens=1)
        self.assertEqual(self.api.keys_sent(), [BAD])

    def test_key_switch_is_published_by_the_role(self):
        n = Nemotron(self.client(BAD), ULTRA, self.bus, NemotronCfg(thinking_for_design=False))
        n.generate(system="s", messages=[{"role": "user", "content": "x"}], thinking=False, purpose="design")
        fb = self.events("model.fallback")
        self.assertEqual(len(fb), 1)
        self.assertEqual((fb[0]["kind"], fb[0]["from_key"], fb[0]["to_key"], fb[0]["status"]),
                         ("key", "nemotron key", "kimi key", 403))
        self.assertIn("Public API Endpoints", fb[0]["reason"])
        self.assertNotIn(GOOD, json.dumps(fb))  # labels only, never key material
        self.assertNotIn(BAD, json.dumps(fb))


class ModelFallbackTests(_Base):
    def kimi(self, fallbacks=(ULTRA,)) -> Kimi:
        client = NIMClient(self.api.mock.url, GOOD, key_label="kimi key", backoff_s=(0.01,))
        return Kimi(client, K3, KimiCfg(reasoning_effort="max", judge_reasoning_effort="max", effort_field="top_level"),
                    self.bus, fallback_models=fallbacks)

    def test_retired_kimi_k3_falls_back_visibly_and_sends_the_fallback_models_own_fields(self):
        k = self.kimi()
        res = k.think(KimiConversation("sys"), purpose="intake", publish_deltas=False)
        self.assertEqual(res.content, "ok")
        self.assertEqual(self.api.models_sent(), [K3, ULTRA])
        k3_body, ultra_body = self.api.mock.requests
        self.assertEqual(k3_body.get("reasoning_effort"), "max")
        self.assertNotIn("reasoning_effort", ultra_body)  # Kimi's field is not sent to Nemotron
        self.assertEqual(ultra_body.get("chat_template_kwargs"), {"enable_thinking": True})
        fb = self.events("model.fallback")
        self.assertEqual(len(fb), 1)
        self.assertEqual((fb[0]["kind"], fb[0]["role"], fb[0]["from_model"], fb[0]["to_model"], fb[0]["status"]),
                         ("model", "kimi", K3, ULTRA, 410))
        self.assertIn("2026-10-15", fb[0]["reason"])
        metrics = self.events("model.metrics")
        self.assertEqual(metrics[-1]["model"], ULTRA)
        self.assertEqual(metrics[-1]["requested_model"], K3)
        self.assertTrue(metrics[-1]["fallback"])
        self.assertEqual(k.model, ULTRA)
        k.think(KimiConversation("sys"), purpose="plan", publish_deltas=False)
        self.assertEqual(self.api.models_sent(), [K3, ULTRA, ULTRA])  # the retired model is not asked again

    def test_without_fallbacks_the_410_is_raised_with_its_explanation(self):
        k = self.kimi(fallbacks=())
        with self.assertRaises(NIMError) as cm:
            k.think(KimiConversation("sys"), purpose="intake", publish_deltas=False)
        self.assertEqual(cm.exception.status, 410)
        err = self.events("model.error")[-1]
        self.assertIn("retired by the provider on 2026-10-15", err["hint"])

    def test_a_refusal_is_not_a_reason_to_change_model(self):
        client = NIMClient(self.api.mock.url, BAD, key_label="kimi key", backoff_s=(0.01,))
        k = Kimi(client, ULTRA, KimiCfg(reasoning_effort="max", judge_reasoning_effort="max", effort_field="top_level"),
                 self.bus, fallback_models=["some/other-model"])
        with self.assertRaises(NIMError):
            k.think(KimiConversation("sys"), purpose="intake", publish_deltas=False)
        self.assertEqual(self.api.models_sent(), [ULTRA])  # 403 is about the key, not the model

    def test_chain_bookkeeping(self):
        chain = ModelChain(K3, [ULTRA, K3, "", ULTRA])
        self.assertEqual(chain.candidates(), [K3, ULTRA])
        chain.mark_gone(K3, "retired")
        self.assertEqual((chain.active, chain.fallback_active), (ULTRA, True))
        chain.mark_gone(ULTRA, "gone too")
        self.assertEqual(chain.candidates(), [K3])  # never empty: the error of the requested model is shown


class AppWiringTests(unittest.TestCase):
    def setUp(self):
        self.api = NvidiaLike()
        self.addCleanup(self.api.mock.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-fallback-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = self.tmp / "config"
        shutil.copytree(REPO / "config", cfg)
        keys = json.loads((cfg / "keys.json").read_text(encoding="utf-8"))
        keys["nvidia"]["base_url"] = self.api.mock.url
        keys["nvidia"]["nemotron"]["api_key"] = BAD   # refused, like the Nemotron slot's key
        keys["nvidia"]["kimi"]["api_key"] = GOOD      # works for Nemotron-3-Ultra, like the Kimi slot's key
        (cfg / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
        from kyora.app import App
        self.app = App(Config.load(cfg), home=self.tmp / "home")
        self.addCleanup(self.app.stop)

    def test_shipped_config_keeps_both_roles_answering(self):
        nv = self.app.config.nvidia
        self.assertEqual(nv.kimi.fallback_models[0], ULTRA)
        self.assertTrue(nv.share_keys)
        self.assertEqual(self.app.nemotron.narration("Build passed."), "ok")
        self.assertEqual(self.app.nim_nemotron.key_label, "kimi key")
        self.app.kimi.think(KimiConversation("sys"), purpose="intake", publish_deltas=False)
        self.assertEqual(self.app.kimi.model, ULTRA)

    def test_self_check_names_the_substitutions(self):
        from kyora.selfcheck import SelfCheck
        items = {it["id"]: it for it in SelfCheck(self.app).run(probe_models=True)["items"]}
        nem, kimi = items["nemotron"], items["kimi"]
        self.assertEqual(nem["status"], "warn", nem)
        self.assertIn("the nemotron key was refused (HTTP 403); the kimi key answered", nem["detail"])
        self.assertTrue(nem["measured"]["key_fallback"])
        self.assertEqual(kimi["status"], "warn", kimi)
        self.assertIn(f"{K3}: HTTP 410", kimi["detail"])
        self.assertIn(f"answered by the fallback model {ULTRA}", kimi["detail"])
        self.assertEqual(kimi["measured"]["fallback_model"], ULTRA)

    def test_share_keys_off_and_settings_apply_without_restart(self):
        changed = self.app.config.apply_patch({"keys": {"nvidia": {"share_keys": False}}})
        self.assertEqual(changed, ["keys.nvidia.share_keys"])
        self.app.apply_model_settings()
        with self.assertRaises(NIMError):  # sharing off: the refused key is the only one
            self.app.nim_nemotron.chat(ULTRA, [{"role": "user", "content": "x"}], stream=False, max_tokens=1)
        self.app.config.apply_patch({"keys": {"nvidia": {"nemotron": {"api_key": GOOD}, "kimi": {"fallback_models": []}}}})
        self.app.apply_model_settings()
        self.assertEqual(self.app.nim_nemotron.chat(ULTRA, [{"role": "user", "content": "x"}], stream=False,
                                                    max_tokens=1).content, "ok")
        self.assertEqual(self.app.kimi.chain.fallbacks, [])

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            self.app.config.apply_patch({"keys": {"nvidia": {"kimi": {"base_url": "ftp://x"}}}})
        with self.assertRaises(ValueError):
            self.app.config.apply_patch({"keys": {"nvidia": {"kimi": {"fallback_models": "not-a-list"}}}})
        with self.assertRaises(ValueError):
            self.app.config.apply_patch({"keys": {"nvidia": {"share_keys": "yes"}}})
        self.app.config.apply_patch({"keys": {"nvidia": {"kimi": {"base_url": "https://api.moonshot.ai/v1"}}}})
        self.assertEqual(self.app.config.nvidia.kimi.base_url, "https://api.moonshot.ai/v1")
        self.assertEqual(self.app.config.nvidia.alt_keys("kimi"), [])  # another endpoint: never send it the NVIDIA key


if __name__ == "__main__":
    unittest.main()
