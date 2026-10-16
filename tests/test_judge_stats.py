"""Regression tests for honesty-run-judge-stats-miss-unjudged-calls.

``run_engine`` must record the judge counters on the run so that:
* a local engine's judged calls land in ``run.judge``;
* a Hub engine's judge delta (reported in ``result.json``, carried on
  ``result.raw["hub"]["judge"]``) is folded in even though the local judge saw
  no verdict;
* a resumed run (fresh process, empty in-memory counters) restores the counts
  persisted with the run and keeps counting from there, instead of overwriting
  them with the smaller counts of the resumed leg.

No model or CLI is involved: a tiny fake engine stands in for the code engine.

Run: ``python3 -m unittest tests.test_judge_stats -v``
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from kyora.models.base import EngineResult
from kyora.pipeline import stages
from kyora.pipeline.events import EventBus
from kyora.pipeline.judge import JudgeService


def _cfg():
    cc = SimpleNamespace(resume_for_fixes=True, max_turns=12, model="claude-fable-5-1",
                         fallback_models=["claude-opus-5-5"], effort="max")
    quality = SimpleNamespace(min_pass_rate=0.94)
    return SimpleNamespace(claude_code=cc, quality=quality)


class LocalJudgingEngine:
    """Fake local engine: registers a judge context and judges a few calls, like Claude Code does."""

    name = "claude_code"

    def __init__(self, judge, decisions, result: EngineResult) -> None:
        self.judge = judge
        self.decisions = decisions
        self.result = result

    def run(self, job, sink, cancel):
        if self.judge is not None:
            self.judge.register_context(job.run_id, {"workspace": str(job.workspace),
                                                     "files": ["src/game.js"], "constraints": {}})
            for i, call in enumerate(self.decisions):
                self.judge.decide(job.run_id, {"tool_name": call[0], "tool_use_id": f"{job.kind}-{i}",
                                               "tool_input": call[1]})
            self.judge.forget(job.run_id)  # stats survive forget, like the real engine
        return self.result


class HubEngine:
    """Fake Hub engine: judges nothing locally; reports its judge delta on result.raw['hub']['judge']."""

    name = "hub"

    def __init__(self, delta: dict) -> None:
        self.result = EngineResult(ok=True, backend="hub:claude_code", requested_model="claude-fable-5-1",
                                   raw={"hub": {"judge": delta}})

    def run(self, job, sink, cancel):
        return self.result


class RunEngineJudgeStats(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-judgestats-")
        root = Path(self._tmp.name)
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.bus = EventBus(None)
        self.saved: list = []

    def tearDown(self) -> None:
        self.bus.close()
        self._tmp.cleanup()

    def make_ctx(self, engine, judge, run_judge=None):
        store = SimpleNamespace(save_run=lambda run: self.saved.append(run))
        app = SimpleNamespace(config=_cfg(), bus=self.bus, judge=judge, store=store)
        run = SimpleNamespace(id="r_1", project_id="p_1", options={}, iteration=1,
                              judge=run_judge if run_judge is not None else {"allowed": 0, "denied": 0, "unavailable": 0},
                              extra={}, engine=None, kind="build")
        ctx = stages.StageContext(app=app, run=run, project=None, run_dir=self.run_dir,
                                  workspace=self.workspace, cancel=threading.Event(), slides=None)
        ctx.engine = engine
        return ctx

    def test_local_engine_stats_recorded(self):
        judge = JudgeService(None, self.bus, None, Path(self._tmp.name))
        result = EngineResult(ok=True, backend="claude_code", requested_model="claude-fable-5-1")
        # one rule-allow (planned file), one rule-deny (git push)
        eng = LocalJudgingEngine(judge, [("Write", {"file_path": str(self.workspace / "src/game.js")}),
                                         ("Bash", {"command": "git push"})], result)
        ctx = self.make_ctx(eng, judge)
        stages.run_engine(ctx, "build", "prompt")
        self.assertEqual((ctx.run.judge["allowed"], ctx.run.judge["denied"], ctx.run.judge["rule"]), (1, 1, 2))

    def test_hub_delta_folded_into_run_judge(self):
        # The local judge never saw these verdicts; the Hub's reported delta must still reach run.judge.
        judge = JudgeService(None, self.bus, None, Path(self._tmp.name))
        eng = HubEngine({"allowed": 7, "denied": 1, "unavailable": 2, "cached": 0, "rule": 5, "kimi": 3})
        ctx = self.make_ctx(eng, judge)
        stages.run_engine(ctx, "build", "prompt")
        self.assertEqual(ctx.run.judge["allowed"], 7)
        self.assertEqual(ctx.run.judge["denied"], 1)
        self.assertEqual(ctx.run.judge["unavailable"], 2)
        self.assertEqual(ctx.run.judge["kimi"], 3)

    def test_resume_restores_persisted_counts(self):
        # A fresh process (empty judge) resumes a run whose build already judged 10 calls.
        judge = JudgeService(None, self.bus, None, Path(self._tmp.name))
        prior = {"allowed": 9, "denied": 1, "unavailable": 0, "cached": 0, "rule": 8, "kimi": 2}
        result = EngineResult(ok=True, backend="claude_code", requested_model="claude-fable-5-1")
        eng = LocalJudgingEngine(judge, [("Write", {"file_path": str(self.workspace / "src/game.js")})], result)
        ctx = self.make_ctx(eng, judge, run_judge=dict(prior))
        stages.run_engine(ctx, "fix", "prompt")
        # the resumed leg adds one rule-allow on top of the restored totals (not overwrites them)
        self.assertEqual(ctx.run.judge["allowed"], 10)
        self.assertEqual(ctx.run.judge["denied"], 1)
        self.assertEqual(ctx.run.judge["rule"], 9)
        self.assertEqual(ctx.run.judge["kimi"], 2)


if __name__ == "__main__":
    unittest.main()
