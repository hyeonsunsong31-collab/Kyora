"""Kyora Bench scoring: per-task pass/fail and the suite score / certification rule.

Rules (documented in ``docs/bench.md``):

* A task passes iff its pipeline run ended in state ``complete`` **and** the run's
  final quality gate says ``ok is True`` **and** that gate measured
  ``pass_rate >= rule`` with a ``threshold >= rule``, where ``rule`` is the task's
  ``pass_rule.min_pass_rate`` and never below 0.94 (so a lowered Settings gate can
  never pass a bench task). For bench runs the gate scores only the bench's own
  test ids (``passed bench ids / bench ids``) and still requires every required
  check. When the runner could verify how many of the bench's own browser tests
  appear in the gate results and that number is below the task's
  ``min_browser_tests``, the task fails as well (the bench tests demonstrably did
  not run, so the gate did not measure the bench).
* ``pass_rate = passed / run`` over the distinct task ids that ran in this bench
  run (floored to 4 decimals, never rounded up).
* ``certified`` iff the suite is complete (all ``total_suite`` distinct task ids ran
  in this one bench run), a single engine/model pair served every engine call of
  every task (``TaskResult.served``, from the run's ``extra.engine_runs``; a task
  served by two models is mixed), and ``pass_rate >= threshold`` (0.94).
  ``reason`` explains every ``False``.

Nothing here invents a number: a score is only ever computed from recorded
results.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, Mapping

from ..util.ids import now_iso

SUITE_SIZE = 24
THRESHOLD = 0.94


def _floor4(x: float) -> float:
    return math.floor(x * 10000) / 10000


@dataclass
class TaskResult:
    """Outcome of one bench task (one full pipeline run)."""

    task_id: str
    run_id: str | None = None
    project_id: str | None = None
    state: str = "not_run"
    gate: dict | None = None
    passed: bool = False
    duration_s: float = 0.0
    engine: str | None = None
    active_model: str | None = None
    error: str | None = None
    title: str = ""
    category: str = ""
    kind: str = ""
    min_browser_tests: int | None = None
    bench_tests_seen: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    min_pass_rate: float | None = None
    threshold: float | None = None
    served: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready dict of every field."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TaskResult":
        """Rebuild from :meth:`to_dict` output; unknown keys are ignored."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(d).items() if k in names})

    def final_gate(self) -> dict | None:
        """The ``gate.final`` dict of the run, or None when the run produced none."""
        gate = self.gate if isinstance(self.gate, dict) else None
        if not gate:
            return None
        final = gate.get("final")
        if isinstance(final, dict):
            return final
        # tolerate a bare gate dict ({pass_rate, ok, ...}) as the final gate
        if "ok" in gate and "pass_rate" in gate:
            return gate
        return None

    def evaluate(self) -> bool:
        """Recompute :attr:`passed` from the recorded state and gate."""
        self.passed = task_passed(self.state, self.gate, self.bench_tests_seen, self.min_browser_tests,
                                  self.min_pass_rate)
        return self.passed

    def pairs(self) -> list[str]:
        """Every ``engine/model`` pair that served this task (falls back to the last call's model)."""
        if self.served:
            return list(dict.fromkeys(str(p) for p in self.served))
        return [f"{self.engine or '?'}/{self.active_model}"] if self.active_model else []


def task_rule(min_pass_rate: Any = None) -> float:
    """A task's pass rule: its ``pass_rule.min_pass_rate``, never below :data:`THRESHOLD`."""
    try:
        rule = float(min_pass_rate) if min_pass_rate is not None else THRESHOLD
    except (TypeError, ValueError):
        rule = THRESHOLD
    return max(THRESHOLD, min(1.0, rule)) if math.isfinite(rule) else THRESHOLD


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def task_passed(state: str | None, gate: Mapping | None, bench_tests_seen: int | None = None,
                min_browser_tests: int | None = None, min_pass_rate: float | None = None) -> bool:
    """A task passes iff ``state == 'complete'``, ``gate.final.ok is True`` and the gate
    measured ``pass_rate >= rule`` against a ``threshold >= rule`` (:func:`task_rule`).

    When ``bench_tests_seen`` is known (not None) and below ``min_browser_tests``
    the task fails: the gate did not contain the bench's own tests.
    """
    if state != "complete":
        return False
    tr = TaskResult(task_id="_", state=state or "", gate=dict(gate) if isinstance(gate, Mapping) else None)
    final = tr.final_gate()
    if not final or final.get("ok") is not True:
        return False
    rule = task_rule(min_pass_rate)
    threshold = final.get("threshold", (gate or {}).get("threshold"))
    pass_rate = final.get("pass_rate")
    if not _num(threshold) or threshold + 1e-9 < rule or not _num(pass_rate) or pass_rate + 1e-9 < rule:
        return False
    if bench_tests_seen is not None and min_browser_tests is not None and bench_tests_seen < min_browser_tests:
        return False
    return True


@dataclass
class BenchScore:
    """Measured suite score. ``certified`` is only ever True from a measured full run."""

    pass_rate: float
    passed: int
    run: int
    suite_complete: bool
    certified: bool
    engine: str | None
    model: str | None
    timestamp: str
    reason: str
    total_suite: int = SUITE_SIZE
    threshold: float = THRESHOLD
    task_ids: list[str] = field(default_factory=list)
    models_seen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready dict of every field."""
        return asdict(self)


def _as_result(r: TaskResult | Mapping) -> TaskResult:
    if isinstance(r, TaskResult):
        return r
    if isinstance(r, Mapping):
        return TaskResult.from_dict(r)
    raise TypeError(f"expected TaskResult or dict, got {type(r).__name__}")


def score(results: Iterable[TaskResult | Mapping], total_suite: int = SUITE_SIZE, engine: str | None = None,
          model: str | None = None, *, threshold: float = THRESHOLD, suite_ids: Iterable[str] | None = None,
          timestamp: str | None = None) -> BenchScore:
    """Score one bench run.

    ``results`` are the tasks of a single bench run; when a task id appears more
    than once the last result counts. ``engine``/``model`` are used for display
    only when the results themselves do not identify a single engine/model pair;
    certification always looks at what actually served each task.
    """
    by_id: dict[str, TaskResult] = {}
    for raw in results:
        r = _as_result(raw)
        by_id[r.task_id] = r
    items = list(by_id.values())
    run = len(items)
    ok_ids = {r.task_id for r in items if task_passed(r.state, r.gate, r.bench_tests_seen, r.min_browser_tests,
                                                      r.min_pass_rate)}
    passed = len(ok_ids)
    pass_rate = _floor4(passed / run) if run else 0.0

    expected = set(suite_ids) if suite_ids is not None else None
    if expected is not None:
        suite_complete = expected.issubset(by_id.keys()) and len(expected) == total_suite
    else:
        suite_complete = run >= total_suite

    pairs = sorted({p for r in items for p in r.pairs()})
    mixed = sorted(r.task_id for r in items if len(r.pairs()) > 1)
    passing_without_model = [r.task_id for r in items if r.task_id in ok_ids and not r.pairs()]
    single_model = len(pairs) == 1 and not passing_without_model
    if len(pairs) == 1:
        engine_out, model_out = pairs[0].split("/", 1)
        engine_out = engine_out if engine_out != "?" else engine
    else:
        engine_out, model_out = engine, model

    reasons: list[str] = []
    if run == 0:
        reasons.append("no tasks were run")
    else:
        if not suite_complete:
            reasons.append(f"suite incomplete: {run}/{total_suite} distinct tasks ran in this bench run "
                           f"(all {total_suite} are required)")
        if len(pairs) > 1:
            reasons.append("more than one engine/model served the tasks: " + ", ".join(pairs)
                           + (f" (mixed within {', '.join(mixed)})" if mixed else ""))
        elif not pairs:
            reasons.append("no task recorded the model that served it")
        elif passing_without_model:
            reasons.append("passing tasks without a recorded model: " + ", ".join(sorted(passing_without_model)))
        if pass_rate < threshold:
            reasons.append(f"pass rate {pass_rate * 100:.1f}% ({passed}/{run}) is below the "
                           f"{threshold * 100:.0f}% threshold")
    certified = bool(run) and suite_complete and single_model and pass_rate >= threshold
    return BenchScore(pass_rate=pass_rate, passed=passed, run=run, suite_complete=suite_complete,
                      certified=certified, engine=engine_out, model=model_out,
                      timestamp=timestamp or now_iso(), reason="" if certified else "; ".join(reasons),
                      total_suite=total_suite, threshold=threshold, task_ids=sorted(by_id), models_seen=pairs)
