"""Regression tests: a run is only called finished for work that was really done and measured.

The real ``Runner``, ``run_engine``, ``gate_stage``, ``finalize_stage``, ``ProjectStore``
and git/snapshot slides run here; only the model stages (intake/plan/design/review)
and the device test round are stubbed, so each test pins one pipeline decision.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import kyora.pipeline.quality as Q
import kyora.pipeline.runner as R
import kyora.pipeline.stages as S
from kyora.models.base import Availability, CodeEngine, EngineResult
from kyora.pipeline.events import EventBus
from kyora.pipeline.quality import Check
from kyora.pipeline.stages import StageBlocked, StageContext, tree_fingerprint
from kyora.projects.store import ProjectStore

PLAN = {"title": "Sky Runner", "acceptance_tests": [{"id": "at-01", "name": "boots", "kind": "browser"}]}


class ScriptedEngine(CodeEngine):
    """Each call pops one step: ``(ok, error, files_to_write)``."""

    name = "claude_code"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def availability(self):
        return Availability(True, self.name)

    def run(self, job, sink, cancel):
        ok, error, files = self.steps.pop(0)
        self.calls.append(job.kind)
        for rel, text in (files or {}).items():
            p = Path(job.workspace) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return EngineResult(ok=ok, backend=self.name, requested_model=job.model, active_model="claude-opus-5-5",
                            error=error)


def passing_tests(ctx, n):
    return ([Check(f"s{i}", "static", "s", "pass") for i in range(20)], [], [Check("at-01", "browser", "boots", "pass")])


class RunnerHarness(unittest.TestCase):
    """Real store + Runner with the model stages stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = ProjectStore(base / "projects", base / "kyora.db")
        self.addCleanup(self.store.close)
        cc =SimpleNamespace(model="claude-opus-5-5", fallback_models=[], effort="max", max_turns=60, resume_for_fixes=True)
        cfg = SimpleNamespace(quality=SimpleNamespace(min_pass_rate=0.94, max_fix_iterations=3,
                                                      require_plan_approval=False), claude_code=cc)
        self.engine = None
        self.app = SimpleNamespace(store=self.store, bus=EventBus(None), config=cfg, judge=None, browser_results=None,
                                   tokkio=None, kimi=None, nemotron=None, engine=lambda: self.engine, home=base)
        self.plan = dict(PLAN)
        for name, fn in {"intake_stage": lambda ctx: {},
                         "plan_stage": lambda ctx: ctx.write_artifact("plan", self.plan) and self.plan,
                         "design_stage": lambda ctx: ({}, {}),
                         "narrate": lambda *a, **k: None, "show_view": lambda *a, **k: None,
                         "test_stage": passing_tests}.items():
            p = mock.patch.object(R, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def project_with_v1(self):
        p = self.store.create("demo")
        sm = self.store.slide_manager(p.id)
        (self.store.paths(p.id).repo / "index.html").write_text("<canvas></canvas><script>v1</script>")
        sm.commit_slide(None, "v1", {"ok": True}, {})
        return p

    def execute(self, run_id):
        R.Runner(self.app, self.store.get_run(run_id), threading.Event()).execute()
        return self.store.get_run(run_id)


class FailedEngineTests(RunnerHarness):
    def test_failed_engine_that_changed_nothing_blocks_a_modify_run(self):
        p = self.project_with_v1()
        self.engine = ScriptedEngine([(False, "success (HTTP 429): usage limit reached", None)])
        run = self.store.create_run(p.id, "modify", "add a double-jump power-up", parent_slide=1)
        run = self.execute(run.id)
        self.assertEqual(run.state, "blocked")
        self.assertEqual(run.extra.get("blocked_stage"), "build")
        self.assertIn("usage limit reached", run.blocked_reason)
        self.assertIsNone(run.result_slide)
        self.assertEqual([s["number"] for s in self.store.get(p.id).slides], [1])
        failures = run.extra.get("engine_failures")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "build")
        self.assertFalse(failures[0]["tree_changed"])
        self.assertIsNone(run.gate.get("final"), "the unchanged parent game must not be measured")

    def test_blocked_engine_failure_resumes_and_completes(self):
        p = self.project_with_v1()
        self.engine = ScriptedEngine([(False, "overloaded", None),
                                      (True, None, {"index.html": "<canvas></canvas><script>v2</script>"})])
        run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
        run = self.execute(run.id)
        self.assertEqual(run.state, "blocked")
        run = self.execute(run.id)
        self.assertEqual(run.state, "complete")
        self.assertEqual(run.result_slide, 2)

    def test_failed_engine_that_changed_files_is_measured_and_recorded(self):
        p = self.project_with_v1()
        self.engine = ScriptedEngine([(False, "error_max_turns", {"index.html": "<canvas></canvas><script>v2</script>"})])
        run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
        run = self.execute(run.id)
        self.assertEqual(run.state, "complete")
        self.assertEqual(run.extra["engine_failures"][0]["error"], "error_max_turns")
        self.assertTrue(run.extra["engine_failures"][0]["tree_changed"])
        self.assertTrue(run.extra["engine_runs"][0]["tree_changed"])

    def test_failed_fix_engine_that_changed_nothing_blocks(self):
        p = self.project_with_v1()
        calls = {"n": 0}

        def tests(ctx, n):
            calls["n"] += 1
            return ([Check("s0", "static", "s", "pass")], [], [Check("at-01", "browser", "boots", "fail")])
        review = mock.patch.object(R, "review_stage", lambda ctx, n, gate: {"decision": "fix", "fix_instructions": "x"})
        review.start()
        self.addCleanup(review.stop)
        with mock.patch.object(R, "test_stage", tests):
            self.engine = ScriptedEngine([(True, None, {"index.html": "<canvas></canvas><script>v2</script>"}),
                                          (False, "lost contact with the hub", None)])
            run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
            run = self.execute(run.id)
        self.assertEqual(self.engine.calls, ["build", "fix"])
        self.assertEqual(run.state, "blocked")
        self.assertEqual(run.extra.get("blocked_stage"), "fix")
        self.assertIn("lost contact with the hub", run.blocked_reason)
        self.assertEqual(calls["n"], 1, "the unchanged tree must not be measured again")
        self.assertEqual(run.extra["engine_failures"][0]["kind"], "fix")


class DeckRunTests(RunnerHarness):
    def test_finalize_deck_describes_the_finalizing_run_not_a_queued_one(self):
        p = self.store.create("demo")
        self.engine = ScriptedEngine([(True, None, {"index.html": "<canvas></canvas><script>alpha</script>"})])
        a = self.store.create_run(p.id, "generate", "ALPHA prompt: a space shooter")
        # the user queued another run meanwhile; it waits for the project lock in state 'created'
        self.store.create_run(p.id, "modify", "BRAVO prompt: make the ship red")
        run = self.execute(a.id)
        self.assertEqual(run.state, "complete")
        html = (self.store.paths(p.id).repo / "kyora" / "deck.html").read_text(encoding="utf-8")
        self.assertTrue("Sky Runner" in html and "100.0%" in html, "the deck does not describe the finalizing run")
        self.assertFalse("BRAVO" in html, "the deck describes the queued run")


class NodeAvailabilityTests(RunnerHarness):
    """Without Node a kind 'node' acceptance test can never report: Kimi is told, and such a plan blocks."""

    def ctx(self):
        p = self.store.create("demo")
        run = self.store.create_run(p.id, "generate", "a runner")
        sm = self.store.slide_manager(p.id)
        return StageContext(app=self.app, run=run, project=p, run_dir=self.store.run_dir(run),
                            workspace=sm.work_tree(), cancel=threading.Event(), slides=sm)

    def kimi_returning(self, plan):
        seen = {}

        def plan_fn(intake, run_id, project_id, cancel, **kw):
            seen.update(kw)
            return plan
        self.app.kimi = SimpleNamespace(plan=plan_fn)
        return seen

    def test_plan_stage_tells_kimi_and_blocks_node_tests_without_node(self):
        node_plan = {"title": "T", "acceptance_tests": [{"id": "at-01", "kind": "browser"},
                                                        {"id": "at-02", "kind": "node", "required": True}]}
        seen = self.kimi_returning(node_plan)
        with mock.patch.object(Q, "find_node", lambda: None):
            ctx = self.ctx()
            with self.assertRaises(StageBlocked) as cm:
                S.plan_stage(ctx)
        self.assertEqual(seen, {"node_available": False})
        self.assertEqual(cm.exception.stage, "plan")
        self.assertIn("at-02", cm.exception.reason)
        self.assertIn("Node.js is not installed", cm.exception.reason)
        with mock.patch.object(Q, "find_node", lambda: "/usr/bin/node"):
            seen = self.kimi_returning(node_plan)
            self.assertEqual(S.plan_stage(self.ctx())["acceptance_tests"][1]["kind"], "node")
        self.assertEqual(seen, {"node_available": True})

    def test_test_stage_blocks_instead_of_looping_fixes(self):
        ctx = self.ctx()
        ctx.write_artifact("plan", {"title": "T", "acceptance_tests": [{"id": "at-02", "kind": "node"}]})
        with mock.patch.object(Q, "find_node", lambda: None):
            with self.assertRaises(StageBlocked) as cm:
                S.test_stage(ctx, 0)
        self.assertEqual(cm.exception.stage, "test")
        self.assertIn("at-02", cm.exception.reason)


class TreeFingerprintTests(unittest.TestCase):
    def test_content_not_mtime_decides(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "index.html").write_text("a")
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("x")
            fp = tree_fingerprint(root)
            (root / "index.html").write_text("a")  # rewritten with the same bytes (hub unzip)
            (root / ".git" / "HEAD").write_text("y")  # git internals do not count
            self.assertEqual(tree_fingerprint(root), fp)
            (root / "src").mkdir()
            (root / "src" / "game.js").write_text("")
            self.assertNotEqual(tree_fingerprint(root), fp)


if __name__ == "__main__":
    unittest.main()
