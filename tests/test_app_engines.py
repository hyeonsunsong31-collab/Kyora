"""The Kyora agent engine in the composition root: wiring, ``/api/status``, Settings and self-check.

A real :class:`kyora.app.App` on a temporary copy of ``config/`` whose NVIDIA endpoint is a
:class:`tests.mock_nim.MockNIM`; no model endpoint is contacted. The engine itself is tested in
``tests/test_openai_agent.py``; here only what ``app.py`` and the Settings round trip make of it.
Run: ``python3 -m unittest tests.test_app_engines -v``
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kyora.config import Config
from kyora.server import KyoraServer, Router
from tests.mock_nim import MockNIM, tool_response

REPO = Path(__file__).resolve().parents[1]
GOOD = "nvapi-agent-good-key-0123456789"
OTHER = "nvapi-agent-other-key-9876543210"


def probe_handler(req: dict) -> dict:
    """Answer the agent probe (it offers the kyora_probe function) with a tool call, anything else with text."""
    names = [((t.get("function") or {}).get("name")) for t in (req.get("tools") or [])]
    if "kyora_probe" in names:
        return tool_response("kyora_probe", {"ok": True}, call_id="probe_1")
    return {"content": "ok"}


class _AppBase(unittest.TestCase):
    def setUp(self):
        self.mock = MockNIM(probe_handler, models=["z-ai/glm-5.3", "nvidia/nemotron-3-ultra-550b-a55b"]).start()
        self.addCleanup(self.mock.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-app-engines-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = {k: os.environ.get(k) for k in ("KYORA_HOME", "KYORA_CONFIG_DIR", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY")}

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in ("NVIDIA_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)
        os.environ["KYORA_HOME"] = str(self.tmp / "home")
        self.cfg_dir = self.tmp / "config"
        shutil.copytree(REPO / "config", self.cfg_dir)
        os.environ["KYORA_CONFIG_DIR"] = str(self.cfg_dir)
        keys = json.loads((self.cfg_dir / "keys.json").read_text(encoding="utf-8"))
        keys["nvidia"]["base_url"] = self.mock.url
        keys["nvidia"]["nemotron"]["api_key"] = OTHER
        keys["nvidia"]["kimi"]["api_key"] = GOOD
        keys["anthropic"] = {"api_key": ""}
        keys["agent"] = {"base_url": "", "api_key": ""}
        keys["hub"] = {"url": "", "token": ""}
        keys["tokkio"] = {"ui_url": "", "configurator_url": ""}
        (self.cfg_dir / "keys.json").write_text(json.dumps(keys), encoding="utf-8")

    def make_app(self, *, without_claude: bool = True):
        from kyora.app import App
        app = App(Config.load(self.cfg_dir), home=self.tmp / "home")
        self.addCleanup(app.stop)
        app.run_manager = None  # these tests never start runs
        if without_claude:  # this device may have a logged-in Claude Code; the tests must not depend on it
            app.claude_engine = None
            app.anthropic_engine = None
        return app


class AppWiringTests(_AppBase):
    def test_agent_engine_is_built_and_listed(self):
        from kyora.models.openai_agent import OpenAIAgentEngine
        app = self.make_app()
        self.assertIsInstance(app.agent_engine, OpenAIAgentEngine, app.missing)
        self.assertNotIn("openai_agent", app.missing)
        self.assertIs(app.agent_engine.judge, app.judge)
        st = app.status()
        self.assertTrue(st["components"]["openai_agent"])
        self.assertEqual(list(st["engines"]), ["hub", "claude_code", "anthropic_api", "openai_agent"])
        agent = st["engines"]["openai_agent"]
        self.assertTrue(agent["available"], agent)
        self.assertEqual(agent["backend"], "openai_agent")
        self.assertEqual(agent["details"]["model"], "z-ai/glm-5.3")
        self.assertEqual(agent["details"]["endpoint"], f"127.0.0.1:{self.mock.port}")
        self.assertEqual(agent["details"]["key_label"], "kimi key")
        self.assertNotIn(GOOD, json.dumps(st), "no key material in /api/status")
        # without Claude Code, the Messages API and a Hub the agent engine is the selected one
        self.assertIs(app.engine(), app.agent_engine)
        self.assertEqual(st["selected_engine"]["backend"], "openai_agent")

    def test_settings_apply_without_a_restart(self):
        app = self.make_app()
        app.config.apply_patch({"settings": {"agent_engine.enabled": False}})
        st = app.status()
        self.assertFalse(st["engines"]["openai_agent"]["available"])
        self.assertIn("disabled", st["engines"]["openai_agent"]["reason"])
        self.assertIsNone(st["selected_engine"])
        app.config.apply_patch({"settings": {"agent_engine.enabled": True, "agent_engine.model": "poolside/laguna-xs-2.1",
                                             "engines.order": ["claude_code", "anthropic_api"]}})
        self.assertIsNone(app.engine(), "an engine left out of engines.order is never selected")
        self.assertEqual(app.status()["engines"]["openai_agent"]["details"]["model"], "poolside/laguna-xs-2.1")
        app.config.apply_patch({"settings": {"engines.order": ["openai_agent", "claude_code"]}})
        self.assertIs(app.engine(), app.agent_engine)

    def test_a_broken_engine_module_is_reported_missing(self):
        with mock.patch.dict(sys.modules, {"kyora.models.openai_agent": None}):
            app = self.make_app()
        self.assertIsNone(app.agent_engine)
        self.assertRegex(app.missing["openai_agent"], r"(ModuleNotFound|Import)Error")  # 3.10 vs 3.13 wording
        st = app.status()
        self.assertFalse(st["components"]["openai_agent"])
        self.assertFalse(st["engines"]["openai_agent"]["available"])
        self.assertRegex(st["engines"]["openai_agent"]["reason"], r"(ModuleNotFound|Import)Error")
        from kyora.selfcheck import SelfCheck
        it = {i["id"]: i for i in SelfCheck(app).run(probe_models=False)["items"]}["agent_engine"]
        self.assertEqual(it["status"], "fail")
        self.assertRegex(it["detail"], r"(ModuleNotFound|Import)Error")

    def test_self_check_probes_the_agent_engine(self):
        from kyora.selfcheck import SelfCheck
        app = self.make_app()
        report = SelfCheck(app, agent_probe_timeout_s=20).run(probe_models=True)
        items = {i["id"]: i for i in report["items"]}
        agent = items["agent_engine"]
        self.assertEqual(agent["status"], "ok", agent)
        self.assertIn("tool calls work", agent["detail"])
        self.assertTrue(agent["measured"]["tool_calls_ok"])
        probes = [r for r in self.mock.requests if any(((t.get("function") or {}).get("name")) == "kyora_probe"
                                                       for t in (r.get("tools") or []))]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["model"], "z-ai/glm-5.3")
        sent = [c for c in self.mock.calls if c.get("json") is probes[0] or c.get("json") == probes[0]]
        self.assertTrue(sent, "the probe request was recorded with its headers")
        auth = {k.lower(): v for k, v in sent[0]["headers"].items()}.get("authorization")
        self.assertEqual(auth, f"Bearer {GOOD}", "the probe uses the kimi key (the primary agent key)")
        sel = items["engine_selected"]
        self.assertEqual(sel["status"], "ok")
        self.assertIn("Kyora agent engine", sel["detail"])
        # Claude Code is not loaded in this app: its line is a caveat, not a failure, while the agent runs BUILD/FIX
        self.assertEqual(items["claude_cli"]["status"], "warn", items["claude_cli"])
        self.assertIn("Kyora agent engine", items["claude_cli"]["detail"])

    def test_without_probe_models_nothing_is_sent_to_the_agent_endpoint(self):
        from kyora.selfcheck import SelfCheck
        app = self.make_app()
        before = len(self.mock.requests)
        it = {i["id"]: i for i in SelfCheck(app).run(probe_models=False)["items"]}["agent_engine"]
        self.assertEqual(it["status"], "ok", it)
        self.assertIn("not contacted", it["detail"])
        self.assertEqual(len(self.mock.requests), before)


class SettingsRoundTripTests(_AppBase):
    """The fields the Settings view sends for the agent engine, the engine order and the NVIDIA roles."""

    PATCH = {
        "keys": {"agent.base_url": "https://llm.example.test/v1", "agent.api_key": "sk-agent-secret-0123456789",
                 "nvidia.nemotron.base_url": "", "nvidia.kimi.base_url": "https://api.moonshot.ai/v1",
                 "nvidia.nemotron.fallback_models": ["nvidia/nemotron-3-super-120b-a12b"],
                 "nvidia.kimi.fallback_models": ["z-ai/glm-5.3"], "nvidia.share_keys": False},
        "settings": {"agent_engine.enabled": True, "agent_engine.model": "openai/gpt-oss-20b",
                     "agent_engine.fallback_models": ["z-ai/glm-5.3", "poolside/laguna-xs-2.1"],
                     "agent_engine.max_turns": 40, "agent_engine.max_tokens": 8192, "agent_engine.timeout_s": 1800,
                     "agent_engine.context_chars": 200000, "agent_engine.temperature": 0.4, "agent_engine.top_p": 0.9,
                     "agent_engine.thinking": False, "agent_engine.self_check_tool": False,
                     "engines.order": ["openai_agent", "claude_code"], "engines.failover": False,
                     "engines.auto_resume_on_limit": False},
    }

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        router = Router()
        self.app.register_routes(router)
        self.server = KyoraServer(self.app, "127.0.0.1", 0, router)
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()

    def test_every_new_field_is_patchable_and_round_trips(self):
        status, pub = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        for section in ("keys", "settings"):
            for path in self.PATCH[section]:
                self.assertIn(path, pub["patchable"], f"{section}.{path} is not editable in Settings")
        status, res = self.call("PUT", "/api/settings", self.PATCH)
        self.assertEqual(status, 200, res)
        self.assertEqual(len(res["changed"]), len(self.PATCH["keys"]) + len(self.PATCH["settings"]) - 2, res)
        self.assertFalse(res["restart_required"])
        on_disk = json.loads((self.cfg_dir / "keys.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["agent"], {"base_url": "https://llm.example.test/v1", "api_key": "sk-agent-secret-0123456789"})
        settings = json.loads((self.cfg_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["agent_engine"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(settings["engines"]["order"], ["openai_agent", "claude_code"])

        # the engine reads the new values through its config callable: no restart
        c = self.app.agent_engine.cfg
        self.assertEqual((c.model, c.base_url, c.api_key, c.key_label, c.alt_keys),
                         ("openai/gpt-oss-20b", "https://llm.example.test/v1", "sk-agent-secret-0123456789", "agent key", []))
        self.assertEqual((c.max_turns, c.max_tokens, c.timeout_s, c.context_chars, c.temperature, c.top_p),
                         (40, 8192, 1800, 200000, 0.4, 0.9))
        self.assertFalse(c.thinking or c.self_check_tool)
        eng = self.app.config.engines
        self.assertEqual((eng.order, eng.failover, eng.auto_resume_on_limit), (["openai_agent", "claude_code"], False, False))
        nv = self.app.config.nvidia
        self.assertEqual(nv.kimi.base_url, "https://api.moonshot.ai/v1")
        self.assertEqual(nv.nemotron.base_url, self.mock.url.rstrip("/"))
        self.assertFalse(nv.share_keys)
        # NVIDIA endpoint and fallbacks changed in Settings reach the running clients and roles
        self.assertEqual(self.app.nim_kimi.base_url, "https://api.moonshot.ai/v1")
        self.assertEqual(self.app.kimi.chain.fallbacks, ["z-ai/glm-5.3"])
        st = self.app.status()["engines"]["openai_agent"]
        self.assertEqual(st["details"]["endpoint"], "llm.example.test")
        self.assertEqual(st["details"]["key_label"], "agent key")

        # the secret is masked on the way out; sending the masked value back changes nothing
        status, pub = self.call("GET", "/api/settings")
        masked = pub["keys"]["agent"]["api_key"]
        self.assertNotEqual(masked, "sk-agent-secret-0123456789")
        self.assertIn("…", masked)
        self.assertNotIn("sk-agent-secret-0123456789", json.dumps(pub))
        status, res = self.call("PUT", "/api/settings", {"keys": {"agent": {"api_key": masked}}})
        self.assertEqual((status, res["changed"]), (200, []))
        self.assertEqual(self.app.config.agent_engine.api_key, "sk-agent-secret-0123456789")
        status, pub = self.call("GET", "/api/settings?reveal=1")
        self.assertEqual(pub["keys"]["agent"]["api_key"], "sk-agent-secret-0123456789")

    def test_invalid_values_are_refused_naming_the_field(self):
        bad = [
            ({"settings": {"agent_engine.max_turns": 0}}, "agent_engine.max_turns"),
            ({"settings": {"agent_engine.temperature": 2.5}}, "agent_engine.temperature"),
            ({"settings": {"agent_engine.top_p": -0.1}}, "agent_engine.top_p"),
            ({"settings": {"agent_engine.model": "  "}}, "agent_engine.model"),
            ({"settings": {"agent_engine.enabled": "yes"}}, "agent_engine.enabled"),
            ({"settings": {"engines.order": ["openai_agent", "gpt"]}}, "engines.order"),
            ({"settings": {"engines.order": []}}, "engines.order"),
            ({"settings": {"engines.failover": 1}}, "engines.failover"),
            ({"keys": {"agent.base_url": "ftp://x"}}, "agent.base_url"),
            ({"keys": {"nvidia.kimi.base_url": "not a url"}}, "nvidia.kimi.base_url"),
            ({"keys": {"nvidia.kimi.fallback_models": "z-ai/glm-5.3"}}, "nvidia.kimi.fallback_models"),
            ({"keys": {"nvidia.share_keys": "true"}}, "nvidia.share_keys"),
        ]
        for patch, field in bad:
            status, res = self.call("PUT", "/api/settings", patch)
            self.assertEqual(status, 400, (patch, res))
            self.assertIn(field, res["error"], patch)  # settings.js maps the error to the field by its path
        self.assertEqual(self.app.config.agent_engine.max_turns, 80, "a refused patch changes nothing")

    def test_empty_endpoint_means_the_nvidia_endpoint_and_keys(self):
        status, res = self.call("PUT", "/api/settings", {"keys": {"agent.base_url": "", "agent.api_key": ""}})
        self.assertEqual(status, 200, res)
        c = self.app.agent_engine.cfg
        self.assertEqual(c.base_url, self.mock.url.rstrip("/"))
        self.assertEqual((c.api_key, c.key_label), (GOOD, "kimi key"))
        self.assertEqual(c.alt_keys, [("nemotron key", OTHER)])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SettingsViewTests(unittest.TestCase):
    """settings.js lists the new fields with types the API validates (csv -> list, bool, int, float)."""

    def test_groups_and_types(self):
        import subprocess
        script = ("const fs=require('fs'),vm=require('vm');globalThis.window=globalThis;"
                  "vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));"
                  "const S=window.Kyora.modules.settings;"
                  "const el=(t,v,p,min,max)=>({type:t==='bool'?'checkbox':'text',checked:v,value:v,min:min,max:max,"
                  "getAttribute:(k)=>k==='data-type'?t:k==='data-p'?p:null});"
                  "process.stdout.write(JSON.stringify({groups:S.GROUPS.map(g=>({title:g.title,hint:g.hint,"
                  "fields:g.fields.map(f=>[f.s,f.p,f.type])})),"
                  "csv:S.parseInput(el('csv',' z-ai/glm-5.3 , poolside/laguna-xs-2.1,','agent_engine.fallback_models')),"
                  "order:S.parseInput(el('csv','openai_agent, claude_code','engines.order')),"
                  "temp:S.parseInput(el('float','0.4','agent_engine.temperature',0,2)),"
                  "hot:S.parseInput(el('float','2.5','agent_engine.temperature',0,2)),"
                  "turns0:S.parseInput(el('int','0','agent_engine.max_turns',1)),"
                  "turns:S.parseInput(el('int','40','agent_engine.max_turns',1)),"
                  "rate:S.parseInput(el('float','1.5','quality.min_pass_rate',0,1))}));")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "probe.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run([shutil.which("node"), str(path), str(REPO / "kyora" / "web" / "js" / "settings.js")],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=td)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        out = json.loads(proc.stdout)
        groups = {g["title"]: g for g in out["groups"]}
        agent = groups["Kyora agent engine"]
        self.assertEqual(agent["hint"], "no Claude needed")
        fields = {p: (s, t) for s, p, t in agent["fields"]}
        self.assertEqual(fields["agent_engine.enabled"], ("settings", "bool"))
        self.assertEqual(fields["agent_engine.fallback_models"], ("settings", "csv"))
        for p in ("agent_engine.max_turns", "agent_engine.max_tokens", "agent_engine.timeout_s", "agent_engine.context_chars"):
            self.assertEqual(fields[p], ("settings", "int"), p)
        for p in ("agent_engine.temperature", "agent_engine.top_p"):
            self.assertEqual(fields[p], ("settings", "float"), p)
        self.assertEqual(fields["agent.base_url"], ("keys", "url"))
        self.assertEqual(fields["agent.api_key"], ("keys", "secret"))
        engines = {p: (s, t) for s, p, t in groups["Code engines"]["fields"]}
        self.assertEqual(engines, {"engines.order": ("settings", "csv"), "engines.failover": ("settings", "bool"),
                                   "engines.auto_resume_on_limit": ("settings", "bool")})
        nv = {p: (s, t) for s, p, t in groups["NVIDIA API Catalog"]["fields"]}
        for p, t in (("nvidia.nemotron.base_url", "url"), ("nvidia.kimi.base_url", "url"),
                     ("nvidia.nemotron.fallback_models", "csv"), ("nvidia.kimi.fallback_models", "csv"),
                     ("nvidia.share_keys", "bool")):
            self.assertEqual(nv[p], ("keys", t), p)
        # every field the view offers is one the server accepts
        from kyora.config import PATCHABLE_KEYS, PATCHABLE_SETTINGS, FILE_ONLY_SETTINGS
        for g in out["groups"]:
            for s, p, _t in g["fields"]:
                allowed = PATCHABLE_KEYS if s == "keys" else PATCHABLE_SETTINGS | FILE_ONLY_SETTINGS
                if p in ("claude_code.judge_tools", "server.preview_port", "server.allowed_hosts", "server.workers"):
                    continue  # shown read-only (not patchable over HTTP) on purpose
                self.assertIn(p, allowed, f"{s}.{p}")
        self.assertEqual(out["csv"], {"value": ["z-ai/glm-5.3", "poolside/laguna-xs-2.1"]})
        self.assertEqual(out["order"], {"value": ["openai_agent", "claude_code"]})
        self.assertEqual(out["temp"], {"value": 0.4})
        self.assertIn("between 0 and 2", out["hot"]["error"])
        self.assertIn("at least 1", out["turns0"]["error"])
        self.assertEqual(out["turns"], {"value": 40})
        self.assertIn("0.94 = 94 %", out["rate"]["error"])


if __name__ == "__main__":
    unittest.main()
