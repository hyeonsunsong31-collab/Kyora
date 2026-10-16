"""Run manager and the pipeline state machine.

``RunManager`` owns a queue and a small pool of worker threads; a per-project
lock guarantees that two runs of one project never touch the same work tree at
once. ``Runner.execute`` drives one run::

    created -> intake -> plan -> [awaiting_approval] -> design -> build -> test -> gate
    gate ok                              -> finalize -> complete
    gate failed, iteration < max fixes   -> review -> fix -> test -> gate ...
    gate failed at the limit / 'stop'    -> finalize -> failed_gate (slide still committed)
    no engine / model unreachable / no browser -> blocked (resume continues at that stage)
    exception -> failed ; cancel -> cancelled (work tree rolled back)

Each stage persists ``run.json`` and publishes ``run.state``, ``stage.started``
and ``stage.finished``; per-stage model metrics are collected from the
``model.metrics`` events that the model wrappers publish.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Any, Callable

from ..projects.slides import LineageMoved
from ..util.fs import atomic_write_text
from ..util.ids import now_iso
from . import events as ev
from .stages import (
    RunCancelled, StageBlocked, StageContext, build_stage, design_stage, finalize_stage, fix_stage, gate_stage,
    intake_stage, load_gate, narrate, plan_stage, review_stage, show_view, test_stage,
)

log = logging.getLogger("kyora.runner")

ACTIVE_STATES = ("intake", "plan", "awaiting_approval", "design", "build", "test", "gate", "review", "fix", "finalize")


class RunManager:
    """Queue + workers for pipeline runs."""

    def __init__(self, app, workers: int = 2):
        self.app = app
        self.workers = max(1, int(workers or 1))
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._lock = threading.RLock()
        self._pending: list[str] = []
        self._running: dict[str, threading.Event] = {}
        self._approvals: dict[str, threading.Event] = {}
        self._project_locks: dict[str, threading.Lock] = {}
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._recover_interrupted()
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"kyora-run-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        with self._lock:
            for e in self._running.values():
                e.set()
        for _ in self._threads:
            self._queue.put(None)
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _recover_interrupted(self) -> None:
        """Runs left in an active state by a previous process become 'blocked' (resumable)."""
        store = getattr(self.app, "store", None)
        if store is None:
            return
        try:
            runs = store.list_runs(None, limit=500)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot scan runs for recovery: %s", exc)
            return
        for run in runs:
            if run.state in ACTIVE_STATES or run.state == "created":
                reason = "interrupted: Kyora stopped while this run was active; press Resume to continue"
                try:
                    store.set_state(run, "blocked", blocked_reason=reason)
                    self.app.bus.publish(ev.RUN_BLOCKED, {"reason": reason, "stage": run.state},
                                         run_id=run.id, project_id=run.project_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot mark run %s blocked: %s", run.id, exc)

    # ------------------------------------------------------------------ public API
    def submit(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._pending or run_id in self._running:
                return False
            self._pending.append(run_id)
            self._approvals.setdefault(run_id, threading.Event())
        self._queue.put(run_id)
        return True

    def cancel(self, run_id: str) -> bool:
        store = self.app.store
        with self._lock:
            if run_id in self._running:
                self._running[run_id].set()
                return True
            pending = run_id in self._pending
            if pending:
                self._pending.remove(run_id)
        try:
            run = store.get_run(run_id)
        except FileNotFoundError:
            return False
        if pending or run.state in ("blocked", "awaiting_approval", "created"):
            # All runs of a project share one work tree. While another run of the project holds the
            # project lock (it is executing), the rollback may only move this run's branch ref.
            plock = self._project_lock(run.project_id)
            idle = plock.acquire(blocking=False)
            try:
                if not store.slide_manager(run.project_id).rollback_working_tree(run, touch_tree=idle):
                    log.warning("cancelled run %s: its commits were left in place", run_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("rollback for cancelled run %s failed: %s", run_id, exc)
            finally:
                if idle:
                    plock.release()
            store.set_state(run, "cancelled")
            self.app.bus.publish(ev.RUN_CANCELLED, {}, run_id=run.id, project_id=run.project_id)
            self.app.bus.publish(ev.RUN_STATE, {"state": "cancelled", "iteration": run.iteration},
                                 run_id=run.id, project_id=run.project_id)
            return True
        return False

    def approve(self, run_id: str) -> bool:
        with self._lock:
            e = self._approvals.get(run_id)
            running = run_id in self._running
        if e is None or not running:
            return False
        e.set()
        return True

    def resume(self, run_id: str) -> bool:
        try:
            run = self.app.store.get_run(run_id)
        except FileNotFoundError:
            return False
        if run.state != "blocked":
            return False
        self.app.store.set_state(run, "created", blocked_reason=None)
        return self.submit(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._running or run_id in self._pending

    def status(self) -> dict:
        with self._lock:
            return {"running": list(self._running), "pending": list(self._pending), "workers": self.workers}

    # ------------------------------------------------------------------ workers
    def _project_lock(self, pid: str) -> threading.Lock:
        with self._lock:
            return self._project_locks.setdefault(pid, threading.Lock())

    def _worker(self) -> None:
        while not self._stopping.is_set():
            rid = self._queue.get()
            if rid is None:
                return
            with self._lock:
                if rid not in self._pending:
                    continue  # cancelled while queued
            try:
                run = self.app.store.get_run(rid)
            except Exception as exc:  # noqa: BLE001
                log.error("run %s vanished before start: %s", rid, exc)
                with self._lock:
                    if rid in self._pending:
                        self._pending.remove(rid)
                continue
            plock = self._project_lock(run.project_id)
            with plock:
                with self._lock:
                    if rid not in self._pending:
                        continue
                    self._pending.remove(rid)
                    cancel = threading.Event()
                    self._running[rid] = cancel
                    approval = self._approvals.setdefault(rid, threading.Event())
                try:
                    Runner(self.app, self.app.store.get_run(rid), cancel, approval).execute()
                except Exception as exc:  # noqa: BLE001 - execute() handles its own errors; this is a last resort
                    log.error("runner crashed for %s: %s\n%s", rid, exc, traceback.format_exc())
                finally:
                    with self._lock:
                        self._running.pop(rid, None)


class Runner:
    """Executes one run through the state machine (see module docstring)."""

    def __init__(self, app, run, cancel: threading.Event, approval: threading.Event | None = None):
        self.app = app
        self.run = run
        self.cancel = cancel
        self.approval = approval or threading.Event()
        self.store = app.store
        self.bus = app.bus
        self.cfg = app.config

    # ------------------------------------------------------------------ helpers
    def _publish(self, type_: str, data: dict | None = None) -> None:
        try:
            self.bus.publish(type_, data or {}, run_id=self.run.id, project_id=self.run.project_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("publish %s failed: %s", type_, exc)

    def _state(self, state: str, **fields: Any) -> None:
        self.store.set_state(self.run, state, **fields)
        self._publish(ev.RUN_STATE, {"state": state, "iteration": self.run.iteration})

    def _metrics_since(self, since_id: int) -> dict:
        """Aggregate the measured model.metrics events published during a stage, by role."""
        out: dict[str, dict] = {}
        try:
            events = self.bus.replay(since_id, run_id=self.run.id, types=[ev.MODEL_METRICS])
        except Exception as exc:  # noqa: BLE001
            log.warning("metrics replay failed: %s", exc)
            return out
        for e in events:
            d = e.data or {}
            role = str(d.get("role") or "model")
            agg = out.setdefault(role, {"model": d.get("model"), "calls": 0, "total_ms": 0.0, "prompt_tokens": 0,
                                        "completion_tokens": 0, "tps": [], "ttft_ms": [], "tokens_source": set()})
            agg["calls"] += 1
            agg["total_ms"] += float(d.get("total_ms") or 0)
            agg["prompt_tokens"] += int(d.get("prompt_tokens") or 0)
            agg["completion_tokens"] += int(d.get("completion_tokens") or 0)
            if isinstance(d.get("tps"), (int, float)):
                agg["tps"].append(float(d["tps"]))
            if isinstance(d.get("ttft_ms"), (int, float)):
                agg["ttft_ms"].append(float(d["ttft_ms"]))
            agg["tokens_source"].add(str(d.get("tokens_source") or "none"))
        for agg in out.values():
            tps = agg.pop("tps")
            ttft = agg.pop("ttft_ms")
            agg["tps_avg"] = round(sum(tps) / len(tps), 2) if tps else None
            agg["ttft_ms_avg"] = round(sum(ttft) / len(ttft), 1) if ttft else None
            agg["total_ms"] = round(agg["total_ms"], 1)
            agg["tokens_source"] = sorted(agg["tokens_source"])
        return out

    def _stage(self, name: str, fn: Callable[[], Any], *, state: str | None = None, rerun: bool = False) -> Any:
        """Run a stage with state/events/timing; skipped when already done (resume) unless ``rerun``."""
        info = self.run.stages.get(name) or {}
        if info.get("status") == "done" and not rerun:
            return fn() if name in ("intake", "plan", "design") else None  # these reload their artefacts
        self.cancel_check()
        self._state(state or name)
        started = time.monotonic()
        since = self.bus.last_id()
        self.run.stages[name] = {**info, "status": "running", "started": now_iso(), "finished": None}
        self.store.save_run(self.run)
        self._publish(ev.STAGE_STARTED, {"stage": name, "iteration": self.run.iteration})
        ok = False
        try:
            result = fn()
            ok = True
            return result
        finally:
            ms = round((time.monotonic() - started) * 1000, 1)
            status = "done" if ok else ("cancelled" if self.cancel.is_set() else "blocked_or_failed")
            entry = self.run.stages.get(name) or {}
            entry.update({"status": status, "finished": now_iso(), "ms": ms})
            metrics = self._metrics_since(since)
            if metrics:
                entry["metrics"] = metrics
            self.run.stages[name] = entry
            try:
                self.store.save_run(self.run)
            except Exception as exc:  # noqa: BLE001
                log.warning("save_run after stage %s failed: %s", name, exc)
            self._publish(ev.STAGE_FINISHED, {"stage": name, "ok": ok, "ms": ms, "iteration": self.run.iteration})

    def cancel_check(self) -> None:
        if self.cancel.is_set():
            raise RunCancelled()

    def _iter_record(self, n: int) -> dict:
        for rec in self.run.iterations:
            if isinstance(rec, dict) and rec.get("n") == n:
                return rec
        rec = {"n": n, "kind": "build" if n == 0 else "fix"}
        self.run.iterations.append(rec)
        return rec

    # ------------------------------------------------------------------ main
    def execute(self) -> None:
        run = self.run
        try:
            project = self.store.get(run.project_id)
            slides = self.store.slide_manager(project)
            ctx = StageContext(app=self.app, run=run, project=project, run_dir=self.store.run_dir(run),
                               workspace=slides.work_tree(), cancel=self.cancel, slides=slides, approval=self.approval)
            self._ctx = ctx
            try:  # before intake: a fresh generation clears the tree; a resume restores the run's last commit
                slides.prepare_run(run)
            except LineageMoved as exc:
                raise StageBlocked(str(exc), "prepare") from None
            run.gate["threshold"] = float(self.cfg.quality.min_pass_rate)
            self.store.save_run(run)
            self._publish(ev.RUN_STATE, {"state": run.state, "iteration": run.iteration, "started": True})

            self._stage("intake", lambda: intake_stage(ctx))
            plan = self._stage("plan", lambda: plan_stage(ctx))
            narrate(ctx, f"The plan for {plan.get('title') or 'your game'} is ready with "
                         f"{len(plan.get('acceptance_tests') or [])} acceptance tests.")
            show_view(ctx, f"Plan ready: {plan.get('title') or 'game'}")
            self._stage("design", lambda: design_stage(ctx))
            self._loop(ctx)
        except RunCancelled:
            self._on_cancel()
        except StageBlocked as b:
            stage = b.stage or self.run.state
            self._state("blocked", blocked_reason=b.reason, blocked_stage=stage)
            self._publish(ev.RUN_BLOCKED, {"reason": b.reason, "stage": stage})
            log.info("run %s blocked at %s: %s", run.id, stage, b.reason)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("run %s failed: %s\n%s", run.id, exc, tb)
            try:
                atomic_write_text(self.store.run_dir(run) / "error.txt", tb)
            except Exception:  # noqa: BLE001
                pass
            msg = f"{type(exc).__name__}: {exc}"
            try:
                self._state("failed", error=msg)
            except Exception as exc2:  # noqa: BLE001
                log.error("cannot record failure of %s: %s", run.id, exc2)
            self._publish(ev.RUN_FAILED, {"error": msg})

    def _loop(self, ctx: StageContext) -> None:
        run = self.run
        q = self.cfg.quality
        while True:
            self.cancel_check()
            n = int(run.iteration)
            rec = self._iter_record(n)
            if not rec.get("built"):
                if n == 0:
                    narrate(ctx, "Claude Code is now building your game at maximum effort.")
                    self._stage("build", lambda: build_stage(ctx), rerun=True)
                else:
                    prev_gate = load_gate(ctx, n - 1) or {}
                    from ..util.fs import read_json
                    review = read_json(ctx.run_dir / f"review-{n - 1}.json", default={}) or {}
                    self._stage("fix", lambda: fix_stage(ctx, n, prev_gate, review), rerun=True)
                rec["commit"] = ctx.slides.commit_iteration(run, n, None)
                rec["built"] = True
                rec["engine"] = {k: (run.engine or {}).get(k) for k in ("backend", "active_model", "ok", "num_turns")}
                self.store.save_run(run)
            gate = load_gate(ctx, n) if rec.get("gate") else None
            if gate is None:
                results: dict = {}
                self._stage("test", lambda: results.update(zip(("static", "node", "browser"), test_stage(ctx, n))),
                            rerun=True)
                gate = self._stage("gate", lambda: gate_stage(ctx, n, results["static"], results["node"],
                                                             results["browser"]), rerun=True)
                rec["gate"] = {k: gate.get(k) for k in ("passed", "total", "pass_rate", "ok")}
                self.store.save_run(run)
                pct = f"{(gate.get('pass_rate') or 0) * 100:.1f}"
                narrate(ctx, f"Iteration {n} passed {gate.get('passed')} of {gate.get('total')} checks, {pct} percent; "
                             f"the gate is {q.min_pass_rate * 100:g} percent and it {'passed' if gate.get('ok') else 'did not pass'}.")
            if gate.get("ok"):
                self._finish(ctx, gate, ok=True)
                return
            if n >= int(q.max_fix_iterations):
                self._finish(ctx, gate, ok=False, reason=f"gate not passed after {n} fix iteration(s)")
                return
            review = self._stage("review", lambda: review_stage(ctx, n, gate), rerun=True)
            rec["review"] = review.get("decision")
            self.store.save_run(run)
            if review.get("decision") == "stop":
                self._finish(ctx, gate, ok=False, reason="Kimi K3 review stopped the fix loop: "
                                                         + "; ".join(review.get("reasons") or [])[:300])
                return
            run.iteration = n + 1
            self.store.save_run(run)
            self._publish(ev.FIX_ITERATION, {"n": run.iteration})

    def _finish(self, ctx: StageContext, gate: dict, *, ok: bool, reason: str | None = None) -> None:
        slide = self._stage("finalize", lambda: finalize_stage(ctx, gate, ok), rerun=True)
        final_state = "complete" if ok else "failed_gate"
        fields: dict = {"result_slide": slide["number"]}
        if reason:
            fields["error"] = reason
        self._state(final_state, **fields)
        self._publish(ev.RUN_COMPLETED, {"slide": slide["number"], "state": final_state, "gate_ok": ok,
                                         "pass_rate": gate.get("pass_rate"), "threshold": gate.get("threshold")})
        show_view(ctx, f"Slide v{slide['number']} {'passed' if ok else 'is below'} the {self.cfg.quality.min_pass_rate * 100:g}% gate",
                  buttons=[("kyora:open_preview", "Open preview"), ("kyora:download", "Download ZIP")])
        narrate(ctx, f"Slide version {slide['number']} is saved; it {'passed' if ok else 'did not pass'} the quality gate.")

    def _on_cancel(self) -> None:
        run = self.run
        try:
            self.store.slide_manager(run.project_id).rollback_working_tree(run)
        except Exception as exc:  # noqa: BLE001
            log.warning("rollback for %s failed: %s", run.id, exc)
        try:
            self._state("cancelled")
        except Exception as exc:  # noqa: BLE001
            log.error("cannot record cancel of %s: %s", run.id, exc)
        self._publish(ev.RUN_CANCELLED, {})


__all__ = ["RunManager", "Runner", "ACTIVE_STATES"]
