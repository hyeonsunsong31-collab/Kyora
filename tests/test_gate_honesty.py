"""Regression tests: a run is only called finished for work that was really done and measured.

The real ``Runner``, ``run_engine``, ``gate_stage``, ``finalize_stage``, ``ProjectStore``
and git/snapshot slides run here; only the model stages (intake/plan/design/review)
and the device test round are stubbed, so each test pins one pipeline decision.
"""
from __future__ import annotations

import json
import os
import shutil
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
from kyora.pipeline.quality import BrowserResultsCollector, Check
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
        ok, error, files, *model = self.steps.pop(0)
        self.calls.append(job.kind)
        for rel, text in (files or {}).items():
            p = Path(job.workspace) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return EngineResult(ok=ok, backend=self.name, requested_model=job.model,
                            active_model=model[0] if model else "claude-opus-5-5",
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
        cfg = SimpleNamespace(quality=SimpleNamespace(min_pass_rate=0.94, max_fix_iterations=3, browser_wait_s=1,
                                                      require_plan_approval=False, auto_run_browser_tests=False),
                              claude_code=cc)
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
        # the failure that blocked the run stays on the slide, marked as one that changed nothing
        engine = self.store.get(p.id).slide(2)["engine"]
        self.assertIs(engine["ok"], False)
        self.assertEqual([(e["error"], e["tree_changed"]) for e in engine["engine_errors"]], [("overloaded", False)])

    def test_failed_engine_that_changed_files_is_measured_and_recorded(self):
        p = self.project_with_v1()
        self.engine = ScriptedEngine([(False, "error_max_turns", {"index.html": "<canvas></canvas><script>v2</script>"})])
        run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
        run = self.execute(run.id)
        self.assertEqual(run.state, "complete")
        self.assertEqual(run.extra["engine_failures"][0]["error"], "error_max_turns")
        self.assertTrue(run.extra["engine_failures"][0]["tree_changed"])
        self.assertTrue(run.extra["engine_runs"][0]["tree_changed"])
        # the gate passed (measured) but the slide never hides that the engine failed
        slide = self.store.get(p.id).slide(run.result_slide)
        self.assertTrue(slide["gate"]["ok"])
        self.assertIs(slide["engine"]["ok"], False)
        self.assertEqual(slide["engine"]["engine_errors"], [{"kind": "build", "iteration": 0, "backend": "claude_code",
                                                             "error": "error_max_turns", "tree_changed": True}])
        tag = next(t for t in self.store.slide_manager(p.id).backend.list_tags() if t["name"] == slide["tag"])
        self.assertEqual(tag["meta"]["engine"], slide["engine"])

    def test_a_clean_engine_run_marks_the_slide_engine_ok(self):
        p = self.project_with_v1()
        self.engine = ScriptedEngine([(True, None, {"index.html": "<canvas></canvas><script>v2</script>"})])
        run = self.execute(self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1).id)
        self.assertEqual(run.state, "complete")
        engine = self.store.get(p.id).slide(run.result_slide)["engine"]
        self.assertIs(engine["ok"], True)
        self.assertNotIn("engine_errors", engine)

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


class InvalidKimiAnswerTests(RunnerHarness):
    """A twice-invalid Kimi answer (ValueError) blocks the run (resumable) instead of failing it."""

    INVALID = "Kimi review: invalid emit_review payload after one repair attempt: 'decision' missing"

    def test_invalid_review_blocks_and_resumes(self):
        p = self.project_with_v1()
        answers = [ValueError(self.INVALID), {"decision": "stop", "reasons": ["cannot be fixed"]}]

        def review(summary, gate, shots, run_id, project_id, cancel):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        self.app.kimi = SimpleNamespace(review=review)
        failing = lambda ctx, n: ([Check("s0", "static", "s", "pass")], [], [Check("at-01", "browser", "boots", "fail")])
        with mock.patch.object(R, "test_stage", failing):
            self.engine = ScriptedEngine([(True, None, {"index.html": "<canvas></canvas><script>v2</script>"})])
            run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
            run = self.execute(run.id)
            self.assertEqual(run.state, "blocked")
            self.assertEqual(run.extra.get("blocked_stage"), "review")
            self.assertIn("no valid review", run.blocked_reason)
            self.assertIsNone(run.error)
            run = self.execute(run.id)  # resume: the measured gate is reused, the review is asked again
        self.assertEqual(run.state, "failed_gate")
        self.assertEqual(run.result_slide, 2)
        self.assertEqual(self.engine.calls, ["build"])

    def test_invalid_intake_blocks(self):
        def intake(*a, **k):
            raise ValueError("Kimi intake: invalid emit_intake payload after one repair attempt: 'goal' missing")
        self.app.kimi = SimpleNamespace(intake=intake)
        p = self.store.create("demo")
        run = self.store.create_run(p.id, "generate", "a runner")
        sm = self.store.slide_manager(p.id)
        ctx = StageContext(app=self.app, run=run, project=p, run_dir=self.store.run_dir(run),
                           workspace=sm.work_tree(), cancel=threading.Event(), slides=sm)
        with self.assertRaises(StageBlocked) as cm:
            S.intake_stage(ctx)
        self.assertEqual(cm.exception.stage, "intake")
        self.assertIn("no valid intake", cm.exception.reason)


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


class ServedModelsTests(RunnerHarness):
    def test_slide_names_every_model_that_served_the_run(self):
        p = self.project_with_v1()

        def tests(ctx, n):
            status = "fail" if n == 0 else "pass"
            return ([Check("s0", "static", "s", "pass")], [], [Check("at-01", "browser", "b", status)])
        review = mock.patch.object(R, "review_stage", lambda ctx, n, gate: {"decision": "fix", "fix_instructions": "x"})
        review.start()
        self.addCleanup(review.stop)
        with mock.patch.object(R, "test_stage", tests):
            self.engine = ScriptedEngine([(True, None, {"index.html": "<canvas></canvas><b>2</b>"}, "claude-opus-5-5"),
                                          (True, None, {"index.html": "<canvas></canvas><b>3</b>"}, "claude-fable-5-1")])
            run = self.store.create_run(p.id, "modify", "add a pause menu", parent_slide=1)
            run = self.execute(run.id)
        self.assertEqual(run.state, "complete")
        self.assertEqual(S.served_models(run), ["claude-opus-5-5", "claude-fable-5-1"])
        slide = self.store.get(p.id).slides[-1]
        self.assertEqual(slide["engine"]["active_model"], "claude-opus-5-5 + claude-fable-5-1")
        html = (self.store.paths(p.id).repo / "kyora" / "deck.html").read_text(encoding="utf-8")
        self.assertTrue("claude-opus-5-5" in html and "claude-fable-5-1" in html, "the deck must list both models")


BENCH_TESTS = Path(S.__file__).resolve().parent.parent / "bench" / "tasks" / "b01-breakout.browser-tests.js"


class BenchGateTests(RunnerHarness):
    """A bench run is scored on the bench's own test ids at the task's rule, whatever Settings say."""

    def bench_ctx(self, settings_threshold=0.5):
        self.app.config.quality.min_pass_rate = settings_threshold  # Settings > Quality gate lowered
        p = self.store.create("bench")
        run = self.store.create_run(p.id, "generate", "breakout", options={
            "bench": {"task_id": "b01-breakout", "tests_file": str(BENCH_TESTS), "min_pass_rate": 0.94}})
        sm = self.store.slide_manager(p.id)
        ctx = StageContext(app=self.app, run=run, project=p, run_dir=self.store.run_dir(run),
                           workspace=sm.work_tree(), cancel=threading.Event(), slides=sm)
        plan = S._apply_bench_contract(ctx, {"title": "Breakout", "acceptance_tests": []})
        ctx.write_artifact("plan", plan)
        ws = ctx.workspace
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / ".kyora").mkdir(exist_ok=True)
        (ws / "tests").mkdir(exist_ok=True)
        (ws / "index.html").write_text('<!doctype html><html><head><title>x</title></head><body><canvas></canvas>'
                                       '<script src="src/game.js"></script></body></html>')
        (ws / "src" / "game.js").write_text("window.Game = {};")
        (ws / ".kyora" / "manifest.json").write_text(json.dumps(
            {"entry": "index.html", "engine": "vanilla", "scripts": ["src/game.js"], "modules": [],
             "tests": ["tests/browser-tests.js"], "version": 1}))
        # the measured engine wrote 60 trivial Node tests of its own
        (ws / "tests" / "logic-tests.mjs").write_text(
            "import test from 'node:test';\n" + "".join(f"test('trivial {i}', () => {{}});\n" for i in range(60)))
        return ctx, [t["id"] for t in plan["acceptance_tests"]]

    def measure(self, ctx, ids, failing):
        results = [{"id": i, "name": i, "status": "fail" if k < failing else "pass", "ms": 1,
                    "error": "x" if k < failing else None} for k, i in enumerate(ids)]
        payload = {"results": results, "syntax_checks": [{"file": "src/game.js", "ok": True, "method": "new Function"}]}
        self.app.browser_results = SimpleNamespace(expect=lambda *a: None, wait=lambda *a: payload,
                                                   to_checks=BrowserResultsCollector.to_checks,
                                                   screenshots=lambda *a: [])
        static, node, browser = S.test_stage(ctx, 0)
        return S.gate_stage(ctx, 0, static, node, browser)

    def test_two_failing_bench_tests_fail_the_gate(self):
        ctx, ids = self.bench_ctx()
        self.assertEqual(len(ids), 14)
        gate = self.measure(ctx, ids, failing=2)
        self.assertEqual((gate["passed"], gate["total"], gate["scope"]), (12, 14, "bench_tests"))
        self.assertEqual(gate["threshold"], 0.94)
        self.assertFalse(gate["ok"])
        self.assertFalse(any(c["id"].startswith("tests_present:") for c in gate["checks"]))
        self.assertEqual(ctx.run.gate["final"]["threshold"], 0.94)
        self.assertEqual(S.threshold_pct(ctx), "94")

    def test_all_bench_tests_passing_passes_with_the_task_threshold(self):
        ctx, ids = self.bench_ctx()
        gate = self.measure(ctx, ids, failing=0)
        self.assertEqual((gate["passed"], gate["total"], gate["ok"], gate["threshold"]), (14, 14, True, 0.94))

    def test_a_failing_required_check_still_fails_a_bench_gate(self):
        ctx, ids = self.bench_ctx()
        (ctx.workspace / ".kyora" / "manifest.json").unlink()
        gate = self.measure(ctx, ids, failing=0)
        self.assertFalse(gate["ok"])
        self.assertIn("file_exists:.kyora/manifest.json", gate["required_failures"])

    def test_normal_runs_keep_the_settings_threshold(self):
        ctx = self.bench_ctx(settings_threshold=0.8)[0]
        ctx.run.options.pop("bench")
        self.assertEqual(S.gate_threshold(ctx), 0.8)


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

    def test_skipped_dirs_do_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "index.html").write_text("a")
            fp = tree_fingerprint(root)
            for rel in ("node_modules/pkg/index.js", "src/node_modules/dep/x.js", "tools/__pycache__/t.pyc",
                        "vendor/.git/HEAD"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x")
            self.assertEqual(tree_fingerprint(root), fp)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_a_fifo_never_blocks_and_counts_by_type(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "index.html").write_text("a")
            fp = tree_fingerprint(root)
            os.mkfifo(root / "pipe")  # no writer: reading it would block forever
            out: list = []
            t = threading.Thread(target=lambda: out.append(tree_fingerprint(root)), daemon=True)
            t.start()
            t.join(10)
            self.assertFalse(t.is_alive(), "tree_fingerprint blocked on a FIFO")
            self.assertNotEqual(out[0], fp)
            self.assertEqual(tree_fingerprint(root), out[0])


if __name__ == "__main__":
    unittest.main()
