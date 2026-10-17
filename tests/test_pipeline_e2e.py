"""End-to-end pipeline tests: real App, server, store, git slides, quality gate and routes.

Only the external systems are replaced: the NVIDIA endpoint by ``tests/mock_nim.MockNIM``
(scripted Kimi K3 / Nemotron answers), the code engine by ``FakeCodeEngine`` and the
device browser by ``FakeBrowser``. Nothing here ships in ``kyora/``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

from kyora.app import App
from kyora.config import Config
from kyora.server import KyoraServer, Router
from kyora.util.fs import atomic_write_json

from tests.fake_engine import FakeBrowser, FakeCodeEngine, TEST_IDS
from tests.mock_nim import MockNIM, scripted_handler, tool_response

REPO = Path(__file__).resolve().parent.parent
TERMINAL = {"complete", "failed_gate", "failed", "cancelled", "blocked"}

INTAKE = {"goal": "A block catcher", "genre": "arcade", "platform": "web (touch + keyboard)",
          "must_haves": ["catch blocks"], "references": [], "risks": [], "imported_base": False, "notes": ""}
PLAN = {
    "title": "Block Catcher", "engine": "vanilla", "preview": True, "summary": "Catch falling blocks.",
    "files": [{"path": "index.html", "purpose": "entry"}, {"path": "src/game.js", "purpose": "game"}],
    "contract": {"globals": ["window.Game"], "api": {"Game.start()": "start", "Game.getState()": "state"}},
    "tasks": [{"id": "t1", "title": "Game", "depends_on": [], "files": ["src/game.js"], "done_when": "tests pass"}],
    "acceptance_tests": [{"id": i, "name": f"test {i}", "kind": "browser", "steps": "see fixture",
                          "required": i == "at-01"} for i in TEST_IDS],
    "constraints": {"no_external_deps": True, "max_total_mb": 25, "touch_controls": True},
    "design_brief_for_nemotron": "Bright arcade palette.",
}
DESIGN = {"gdd_markdown": "# Block Catcher\nCatch blocks.", "narrative": "None.",
          "dialogue": [{"speaker": "HUD", "line": "Catch!"}], "presenter_intro": "Catch the blocks."}
UI = {"tokens": {"color": {"bg": "#000000", "text": "#ffffff"}, "type": {"family": "system-ui"},
                 "spacing": {"md": "16px"}, "radius": {"control": "6px"}},
      "screens": [{"id": "menu", "name": "Menu", "purpose": "start", "components": [{"type": "button", "label": "Start"}]}],
      "copy": {"menu.start": "Start"}}
REVIEW_FIX = {"decision": "fix", "reasons": ["syntax error"], "fix_instructions": "fix src/game.js",
              "suspected_files": ["src/game.js"]}


def _system_has(phrase: str):
    def pred(req: dict) -> bool:
        for m in req.get("messages") or []:
            if m.get("role") == "system" and phrase in str(m.get("content")):
                return True
        return False
    return pred


def make_handler():
    return scripted_handler([
        (_system_has("INTAKE ANALYST"), tool_response("emit_intake", INTAKE)),
        (_system_has("PLANNER"), tool_response("emit_plan", PLAN)),
        (_system_has("UI/UX DIRECTOR"), tool_response("emit_ui_spec", UI)),
        (_system_has("DESIGN DIRECTOR"), tool_response("emit_design", DESIGN)),
        (_system_has("FINAL REVIEWER"), tool_response("emit_review", REVIEW_FIX)),
        (_system_has("INSTANT JUDGE"), tool_response("verdict", {"decision": "allow", "reason": "ok", "risk": "low"})),
        (lambda req: True, {"content": "Status update."}),
    ])


class PipelineE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls._env = {k: os.environ.get(k) for k in ("KYORA_HOME", "KYORA_HEADLESS")}
        os.environ["KYORA_HOME"] = str(root / "home")
        os.environ["KYORA_HEADLESS"] = "0"
        cls.mock = MockNIM(make_handler()).start()
        cfg_dir = root / "config"
        cfg_dir.mkdir()
        keys = json.loads((REPO / "config" / "keys.json").read_text())
        keys["nvidia"]["base_url"] = cls.mock.url
        keys["nvidia"]["nemotron"]["api_key"] = "test-key"
        keys["nvidia"]["kimi"]["api_key"] = "test-key"
        keys["tokkio"] = {"ui_url": "", "configurator_url": ""}
        atomic_write_json(cfg_dir / "keys.json", keys)
        settings = json.loads((REPO / "config" / "settings.json").read_text())
        settings["server"]["workers"] = 1
        settings["quality"]["browser_wait_s"] = 20
        settings["quality"]["max_fix_iterations"] = 3
        atomic_write_json(cfg_dir / "settings.json", settings)
        cls.app = App(Config.load(cfg_dir), home=root / "home")
        cls.engine = FakeCodeEngine()
        cls.app.claude_engine = cls.engine
        cls.app.anthropic_engine = None
        cls.app.hub_client = None
        router = Router()
        cls.app.register_routes(router)
        cls.server = KyoraServer(cls.app, "127.0.0.1", 0, router)
        cls.server.start()
        cls.app.start(cls.server.url)
        cls.browser_mode: dict = {}
        cls.browser = FakeBrowser(cls.app, cls.server.url, lambda rid, n: cls.browser_mode.get((rid, n), "pass"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.server.shutdown()
        cls.app.stop()
        cls.mock.stop()
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def setUp(self):
        q = self.app.config.raw_settings["quality"]
        q["require_plan_approval"] = False
        q["max_fix_iterations"] = 3
        self.engine.script = ["ok"]
        self.engine.sleep_s = 0.0
        self.engine.calls.clear()
        self.app.claude_engine = self.engine

    # ------------------------------------------------------------------ helpers
    def api(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def new_project(self, name="Game") -> str:
        status, body = self.api("POST", "/api/projects", {"name": name})
        self.assertEqual(status, 201, body)
        return body["project"]["id"]

    def start_run(self, pid: str, **body) -> str:
        body.setdefault("prompt", "Build a block catcher with touch controls.")
        status, resp = self.api("POST", f"/api/projects/{pid}/runs", body)
        self.assertEqual(status, 201, resp)
        return resp["run"]["id"]

    def wait_state(self, rid: str, states=TERMINAL, timeout: float = 90.0):
        deadline = time.time() + timeout
        run = None
        while time.time() < deadline:
            run = self.app.store.get_run(rid)
            if run.state in states:
                return run
            time.sleep(0.1)
        self.fail(f"run {rid} stuck in {run.state if run else '?'}; stages={run.stages if run else None}")

    def state_history(self, rid: str) -> list[str]:
        return [e.data.get("state") for e in self.app.bus.replay(0, run_id=rid, types=["run.state"])]

    # ------------------------------------------------------------------ scenarios
    def test_1_generate_completes_with_slide_and_deck(self):
        pid = self.new_project("Generate")
        rid = self.start_run(pid)
        run = self.wait_state(rid)
        self.assertEqual(run.state, "complete", run.error or run.blocked_reason)
        self.assertEqual(run.result_slide, 1)
        self.assertTrue(run.gate["final"]["ok"])
        self.assertGreaterEqual(run.gate["final"]["pass_rate"], 0.94)
        hist = self.state_history(rid)
        for s in ("intake", "plan", "design", "build", "test", "gate", "finalize", "complete"):
            self.assertIn(s, hist)
        self.assertLess(hist.index("intake"), hist.index("plan"))
        self.assertLess(hist.index("build"), hist.index("gate"))
        self.assertEqual(run.engine["backend"], "fake")
        # the effort the engine ran with is recorded (UI labels read it, not today's Settings)
        self.assertEqual(run.engine["effort"], self.app.config.claude_code.effort)
        starts = [e.data for e in self.app.bus.replay(0, run_id=rid, types=["stage.progress"])
                  if e.data.get("stage") == "build" and e.data.get("effort")]
        self.assertTrue(starts, "BUILD announces its backend and effort")
        self.assertEqual((starts[0]["backend"], starts[0]["effort"]), ("fake", self.app.config.claude_code.effort))
        self.assertIn("kimi", run.stages["intake"].get("metrics", {}))
        self.assertIn("nemotron", run.stages["design"].get("metrics", {}))
        status, raw = self.api("GET", f"/api/projects/{pid}/slides/1/download")
        self.assertEqual(status, 200)
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        self.assertTrue(any(n.endswith("kyora/deck.html") for n in names), names)
        self.assertTrue(any(n.endswith("src/game.js") for n in names))
        self.assertTrue(any(n.endswith("CLAUDE.md") for n in names))
        tags = [t["name"] for t in self.app.store.slide_manager(pid).backend.list_tags()]
        self.assertIn("slide/v1", tags)
        status, body = self.api("GET", f"/api/runs/{rid}")
        self.assertEqual(body["run"]["result_slide"], 1)

    def test_2_broken_build_is_fixed_after_review(self):
        pid = self.new_project("Fix loop")
        self.engine.script = ["broken", "ok"]
        rid = self.start_run(pid)
        type(self).browser_mode[(rid, 0)] = "fail"
        run = self.wait_state(rid)
        self.assertEqual(run.state, "complete", run.error or run.blocked_reason)
        self.assertEqual(len(run.iterations), 2)
        self.assertFalse(run.iterations[0]["gate"]["ok"])
        self.assertTrue(run.iterations[1]["gate"]["ok"])
        self.assertTrue((self.app.store.run_dir(run) / "review-0.json").is_file())
        self.assertEqual([j.kind for j in self.engine.calls], ["build", "fix"])
        self.assertIn("fix src/game.js", self.engine.calls[1].prompt)

    def test_3_no_engine_blocks_then_resumes(self):
        pid = self.new_project("Blocked")
        self.app.claude_engine = None
        rid = self.start_run(pid)
        run = self.wait_state(rid)
        self.assertEqual(run.state, "blocked")
        self.assertIn("no code engine", run.blocked_reason)
        intake_calls = sum(1 for r in self.mock.requests if "INTAKE ANALYST" in json.dumps(r.get("body", r)))
        self.app.claude_engine = self.engine
        status, body = self.api("POST", f"/api/runs/{rid}/resume")
        self.assertEqual(status, 200, body)
        run = self.wait_state(rid, states=TERMINAL - {"blocked"})
        self.assertEqual(run.state, "complete", run.error or run.blocked_reason)
        intake_calls_after = sum(1 for r in self.mock.requests if "INTAKE ANALYST" in json.dumps(r.get("body", r)))
        self.assertEqual(intake_calls, intake_calls_after, "resume must not repeat finished model stages")

    def test_4_cancel_during_build_rolls_back(self):
        pid = self.new_project("Cancel")
        self.engine.sleep_s = 30
        self.engine.started.clear()
        rid = self.start_run(pid)
        self.assertTrue(self.engine.started.wait(30))
        status, body = self.api("POST", f"/api/runs/{rid}/cancel")
        self.assertEqual(status, 200, body)
        run = self.wait_state(rid)
        self.assertEqual(run.state, "cancelled")
        work = self.app.store.slide_manager(pid).work_tree()
        self.assertFalse((work / "src" / "game.js").exists())
        self.assertFalse((work / "CLAUDE.md").exists())

    def test_5_modify_stacks_a_second_slide(self):
        pid = self.new_project("Stack")
        rid1 = self.start_run(pid)
        self.assertEqual(self.wait_state(rid1).state, "complete")
        rid2 = self.start_run(pid, kind="modify", parent_slide=1, prompt="Add a pause button.")
        run2 = self.wait_state(rid2)
        self.assertEqual(run2.state, "complete", run2.error or run2.blocked_reason)
        self.assertEqual(run2.result_slide, 2)
        slides = self.app.store.get(pid).slides
        self.assertEqual(slides[1]["parent"], 1)
        self.assertTrue(slides[1]["title"].startswith("Add a pause button"))

    def test_6_plan_approval_waits_then_continues(self):
        self.app.config.raw_settings["quality"]["require_plan_approval"] = True
        pid = self.new_project("Approve")
        rid = self.start_run(pid)
        run = self.wait_state(rid, states={"awaiting_approval"} | TERMINAL)
        self.assertEqual(run.state, "awaiting_approval")
        self.assertEqual(self.engine.calls, [])
        status, body = self.api("POST", f"/api/runs/{rid}/approve")
        self.assertEqual(status, 200, body)
        run = self.wait_state(rid)
        self.assertEqual(run.state, "complete", run.error or run.blocked_reason)

    def test_7_gate_failure_at_limit_still_commits_slide(self):
        self.app.config.raw_settings["quality"]["max_fix_iterations"] = 1
        pid = self.new_project("Below gate")
        self.engine.script = ["broken"]
        rid = self.start_run(pid)
        type(self).browser_mode[(rid, 0)] = "fail"
        type(self).browser_mode[(rid, 1)] = "fail"
        run = self.wait_state(rid)
        self.assertEqual(run.state, "failed_gate")
        self.assertEqual(run.result_slide, 1)
        self.assertFalse(run.gate["final"]["ok"])
        slide = self.app.store.get(pid).slides[0]
        self.assertFalse(slide["gate"]["ok"])

    def test_8_routes_validation(self):
        pid = self.new_project("Routes")
        status, body = self.api("POST", f"/api/projects/{pid}/runs", {"prompt": "  "})
        self.assertEqual(status, 400)
        status, body = self.api("POST", f"/api/projects/{pid}/runs", {"prompt": "x", "options": {"engine_pref": "gpt"}})
        self.assertEqual(status, 400)
        status, body = self.api("POST", "/api/runs/r_nope/cancel")
        self.assertEqual(status, 404)
        status, body = self.api("POST", "/api/judge", {"run_id": "r_x", "hook_token": "bad", "tool_name": "Write"})
        self.assertEqual(status, 403)
        status, body = self.api("POST", "/api/actions", {"action": "download", "project_id": pid})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
