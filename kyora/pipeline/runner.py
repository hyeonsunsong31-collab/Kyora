"""Run manager and the pipeline state machine.

``RunManager`` owns a queue and a small pool of worker threads; a per-project
lock guarantees that two runs of one project never touch the same work tree at
once. ``Runner.execute`` drives one run::

    created -> intake -> plan -> [awaiting_approval] -> design -> build -> test -> gate
    gate ok                              -> finalize -> complete
    gate failed, iteration < max fixes   -> review -> fix -> test -> gate ...
    gate failed at the limit / 'stop'    -> finalize -> failed_gate (slide still committed)
    no engine / model unreachable / no browser -> blocked (resume continues at that stage)
    engine usage limit, no other engine, known reset -> blocked + auto resume at the reset time (+60 s)
    exception -> failed ; cancel -> cancelled (work tree rolled back)
    Kyora stopping (RunManager.stop) -> blocked 'interrupted: Kyora stopped ...' (work kept, resumable)
    (an interrupted BUILD/FIX is committed, also after a hard kill, and its Claude Code session is continued)

Each stage persists ``run.json`` and publishes ``run.state``, ``stage.started``
and ``stage.finished``; per-stage model metrics are collected from the
``model.metrics`` events that the model wrappers publish.

A run blocked by a usage limit whose reset time is known (``extra.auto_resume_at``,
see ``stages.run_engine``) is resumed by ``RunManager`` a minute after the reset
(``run.auto_resume``); the timer is re-armed when Kyora starts again and dropped
when the run is resumed by hand, cancelled or deleted.
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
    RunCancelled, StageBlocked, StageContext, build_stage, design_stage, failure_reason, finalize_stage, fix_stage,
    gate_stage, gate_threshold, intake_stage, load_gate, narrate, plan_stage, review_stage, select_engine, show_view,
    test_stage, tree_fingerprint, utc_iso,
)

log = logging.getLogger("kyora.runner")

ACTIVE_STATES = ("intake", "plan", "awaiting_approval", "design", "build", "test", "gate", "review", "fix", "finalize")
INTERRUPTED_REASON = "interrupted: Kyora stopped while this run was active; press Resume to continue"
_ENGINE_LABELS = {"claude_code": "Claude Code", "anthropic_api": "The Anthropic API engine",
                  "hub": "The code engine on the Kyora Hub", "openai_agent": "Kyora's own code engine"}
AUTO_RESUME_GRACE_S = 60.0  # resume a minute after the reported reset (clock skew, the provider's own rounding)
AUTO_RESUME_STEP_S = 600.0  # a timer wakes at least every 10 min and re-checks the wall clock (sleep/suspend)
AUTO_RESUME_MAX = 24  # automatic resumes per run; after that the run waits for the user's Resume


def auto_resume_epoch(run) -> float | None:
    """``extra.auto_resume_at`` of a run (epoch seconds of the limit's reset), or None."""
    at = (getattr(run, "extra", None) or {}).get("auto_resume_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
        return float(at)
    return None


def _gate_pct(ctx: StageContext, gate: dict) -> str:
    """The gate percentage a run really applied: the measured gate's threshold, else the run's (never Settings)."""
    thr = gate.get("threshold") if isinstance(gate, dict) else None
    if not isinstance(thr, (int, float)) or isinstance(thr, bool):
        thr = gate_threshold(ctx)
    return f"{float(thr) * 100:g}"


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
        # run id -> lock ordering the automatic resume, Resume and Cancel: each reads and writes the same run.json
        self._run_locks: dict[str, threading.RLock] = {}
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._resume_timers: dict[str, tuple[threading.Timer, float, object]] = {}  # run id -> (timer, at, token)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._recover_interrupted()
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"kyora-run-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        self._rearm_auto_resumes()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the workers; running runs end 'blocked' (resumable) with their work kept, not cancelled."""
        self._stopping.set()  # set first: a Runner that sees its cancel event checks it to tell stop from cancel
        with self._lock:
            for e in self._running.values():
                e.set()
            timers, self._resume_timers = list(self._resume_timers.values()), {}
        for timer, _at, _token in timers:  # the next start re-arms them from run.json
            timer.cancel()
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
        for listed in runs:
            if not (listed.state in ACTIVE_STATES or listed.state == "created"):
                continue
            with self._run_lock(listed.id):  # a Cancel from the web UI meanwhile must not be overwritten
                try:
                    run = store.get_run(listed.id)
                except Exception as exc:  # noqa: BLE001 - deleted with its project meanwhile
                    log.info("recovery of run %s skipped: %s", listed.id, exc)
                    continue
                if not (run.state in ACTIVE_STATES or run.state == "created"):
                    continue
                if run.state in ("build", "fix"):
                    self._keep_interrupted_work(store, run)
                reason = INTERRUPTED_REASON
                try:
                    store.set_state(run, "blocked", blocked_reason=reason)
                    self.app.bus.publish(ev.RUN_BLOCKED, {"reason": reason, "stage": run.state},
                                         run_id=run.id, project_id=run.project_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot mark run %s blocked: %s", run.id, exc)

    @staticmethod
    def _keep_interrupted_work(store, run) -> None:
        """Commit what the engine of a BUILD/FIX cut off by a hard kill (iOS ending iSH, Termux, out of memory)
        wrote: a resume restores the run's last commit, so the work would be lost (saved before the run blocks)."""
        branch = (run.options or {}).get("branch")
        attempt = run.extra.pop("engine_attempt", None)  # what the engine started from (stages._engine_attempt)
        if not branch:
            return
        try:
            sm = store.slide_manager(run.project_id)
            if sm.backend.current_branch() != branch:
                return  # the work tree is not on this run's branch: nothing in it is this run's work
            engine = None
            if isinstance(attempt, dict) and attempt.get("kind") == run.state \
                    and attempt.get("iteration") == run.iteration:
                if tree_fingerprint(sm.work_tree()) == attempt.get("base"):
                    return  # the stopped engine changed nothing: no commit, no partial-work claim
                engine = attempt.get("engine")
            sm.commit_iteration(run, run.iteration, None)
            run.extra["partial_work_kept"] = {"kind": run.state, "iteration": run.iteration, "at": now_iso(),
                                              "engine": engine, "reason": failure_reason("interrupted", None)}
        except Exception as exc:  # noqa: BLE001 - the run must still become resumable
            log.warning("run %s: could not keep the work of its interrupted %s: %s", run.id, run.state, exc)

    # ------------------------------------------------------------------ automatic resume after a usage limit
    def _rearm_auto_resumes(self) -> None:
        """On start: arm the timers of runs blocked by a usage limit; resume those whose reset has passed."""
        store = getattr(self.app, "store", None)
        if store is None:
            return
        try:
            runs = store.list_runs(None, limit=500)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot scan runs for automatic resume: %s", exc)
            return
        for run in runs:
            if run.state == "blocked" and auto_resume_epoch(run) is not None:
                self._arm_auto_resume(run)

    def _arm_auto_resume(self, run) -> bool:
        """Resume ``run`` (blocked, ``extra.auto_resume_at`` set) a minute after the reset; publish run.auto_resume."""
        at = auto_resume_epoch(run)
        if at is None or run.state != "blocked" or self._stopping.is_set():
            return False
        if (run.options or {}).get("bench"):
            return False  # Kyora Bench recorded the blocked task and moved on: it must not resume later by itself
        with self._run_lock(run.id):  # a Cancel or Resume meanwhile is never overwritten or re-armed by a stale copy
            try:
                run = self.app.store.get_run(run.id)
            except Exception as exc:  # noqa: BLE001 - deleted with its project, or unreadable
                log.info("automatic resume of %s not armed: %s", run.id, exc)
                return False
            if run.state != "blocked" or auto_resume_epoch(run) != at:
                return False  # cancelled, resumed or blocked again meanwhile
            done = len((run.extra or {}).get("auto_resumes") or [])
            if done >= AUTO_RESUME_MAX:
                log.warning("run %s: not resuming it automatically again after %d automatic resumes; press Resume",
                            run.id, done)
                run.extra.pop("auto_resume_at", None)  # the reason must not promise a resume that will not happen
                try:
                    self.app.store.set_state(run, "blocked", blocked_reason=(
                        f"{run.blocked_reason or 'usage limit'} [automatic resume stopped after {done} automatic "
                        "resumes: press Resume]"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot record the end of automatic resumes for %s: %s", run.id, exc)
                return False
            due = at + AUTO_RESUME_GRACE_S
            delay = max(0.0, due - time.time())
            self._schedule_resume(run.id, at, delay)
            # announced under the lock too: no Cancel can come between arming and the "resumes at" event
            self._publish_for(run, ev.RUN_AUTO_RESUME, {"at": utc_iso(max(due, time.time())),
                                                        "in_s": int(round(delay)), "reset_at": utc_iso(at),
                                                        "reason": str(run.blocked_reason or "usage limit")[:300]})
        log.info("run %s resumes by itself in %d s (limit reset %s)", run.id, int(delay), utc_iso(at))
        return True

    def _schedule_resume(self, run_id: str, at: float, delay: float) -> None:
        token = object()
        timer = threading.Timer(min(max(0.0, delay), AUTO_RESUME_STEP_S), self._auto_resume_due, args=(run_id, at, token))
        timer.daemon = True
        timer.name = f"kyora-auto-resume-{run_id}"
        with self._lock:
            old = self._resume_timers.pop(run_id, None)
            stopping = self._stopping.is_set()
            if not stopping:
                self._resume_timers[run_id] = (timer, at, token)
        if old is not None:
            old[0].cancel()
        if not stopping:
            timer.start()

    def _auto_resume_due(self, run_id: str, at: float, token: object) -> None:
        """Timer callback: re-arm until the wall clock reached the reset + grace, then resume the run."""
        with self._lock:
            cur = self._resume_timers.get(run_id)
            if cur is None or cur[2] is not token:
                return  # cancelled or replaced meanwhile
            self._resume_timers.pop(run_id, None)
        if self._stopping.is_set():
            return
        remaining = at + AUTO_RESUME_GRACE_S - time.time()
        if remaining > 0.5:
            self._schedule_resume(run_id, at, remaining)
            return
        with self._run_lock(run_id):  # a Cancel or Resume meanwhile must not be overwritten by a stale copy
            try:
                run = self.app.store.get_run(run_id)
            except Exception as exc:  # noqa: BLE001 - deleted with its project, or unreadable
                log.info("automatic resume of %s skipped: %s", run_id, exc)
                return
            if run.state != "blocked" or auto_resume_epoch(run) != at:
                return  # resumed by hand, cancelled, or blocked again for another reason
            run.extra.setdefault("auto_resumes", []).append(now_iso())
            log.info("run %s: usage limit reset at %s; resuming it", run_id, utc_iso(at))
            try:
                self._requeue(run)
            except Exception as exc:  # noqa: BLE001 - a timer thread has nobody to report to
                log.warning("cannot resume %s automatically: %s", run_id, exc)

    def cancel_auto_resume(self, run_id: str) -> bool:
        """Drop the automatic resume of ``run_id`` (True when one was armed)."""
        with self._lock:
            cur = self._resume_timers.pop(run_id, None)
        if cur is None:
            return False
        cur[0].cancel()
        return True

    def _publish_for(self, run, type_: str, data: dict) -> None:
        try:
            self.app.bus.publish(type_, data, run_id=run.id, project_id=run.project_id)
        except Exception as exc:  # noqa: BLE001 - events must never break the run manager
            log.warning("publish %s failed: %s", type_, exc)

    # ------------------------------------------------------------------ public API
    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._lock:
            return self._run_locks.setdefault(run_id, threading.RLock())

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
        with self._run_lock(run_id):  # an automatic resume due right now waits, then sees the cancelled state
            self.cancel_auto_resume(run_id)  # inside the lock: a worker arming the timer meanwhile cannot slip in
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
        with self._run_lock(run_id):
            try:
                run = self.app.store.get_run(run_id)
            except FileNotFoundError:
                return False
            if run.state != "blocked":
                return False
            return self._requeue(run)

    def _requeue(self, run) -> bool:
        """A blocked ``run`` becomes 'created' and is queued (the caller holds the run's lock)."""
        self.cancel_auto_resume(run.id)
        run.extra.pop("auto_resume_at", None)  # a new block sets it again when a limit is hit again
        self.app.store.set_state(run, "created", blocked_reason=None)
        return self.submit(run.id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._running or run_id in self._pending

    def status(self) -> dict:
        with self._lock:
            return {"running": list(self._running), "pending": list(self._pending), "workers": self.workers,
                    "auto_resume": {rid: utc_iso(at + AUTO_RESUME_GRACE_S)
                                    for rid, (_t, at, _tok) in self._resume_timers.items()}}

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
                    if self._stopping.is_set():
                        return  # Kyora is stopping: the run stays queued; the next start marks it blocked
                    self._pending.remove(rid)
                    cancel = threading.Event()
                    self._running[rid] = cancel
                    approval = self._approvals.setdefault(rid, threading.Event())
                runner = None
                try:
                    runner = Runner(self.app, self.app.store.get_run(rid), cancel, approval, shutdown=self._stopping)
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - execute() handles its own errors; this is a last resort
                    log.error("runner crashed for %s: %s\n%s", rid, exc, traceback.format_exc())
                finally:
                    with self._lock:
                        self._running.pop(rid, None)
                if runner is not None and runner.run.state == "blocked" and auto_resume_epoch(runner.run) is not None:
                    self._arm_auto_resume(runner.run)


class Runner:
    """Executes one run through the state machine (see module docstring)."""

    def __init__(self, app, run, cancel: threading.Event, approval: threading.Event | None = None,
                 shutdown: threading.Event | None = None):
        self.app = app
        self.run = run
        self.cancel = cancel
        self.approval = approval or threading.Event()
        self.shutdown = shutdown or threading.Event()  # set with ``cancel`` when Kyora stops: interrupt, not cancel
        self.store = app.store
        self.bus = app.bus
        self.cfg = app.config
        self._ctx: StageContext | None = None

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
            cancelled = self.cancel.is_set() and not self.shutdown.is_set()
            status = "done" if ok else ("cancelled" if cancelled else "blocked_or_failed")
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
                               workspace=slides.work_tree(), cancel=self.cancel, slides=slides, approval=self.approval,
                               shutdown=self.shutdown)
            self._ctx = ctx
            try:  # before intake: a fresh generation clears the tree; a resume restores the run's last commit
                slides.prepare_run(run)
            except LineageMoved as exc:
                raise StageBlocked(str(exc), "prepare") from None
            run.gate["threshold"] = gate_threshold(ctx)  # the threshold this run's gate applies (bench: the task's)
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
            if self.shutdown.is_set():
                self._on_shutdown()
            else:
                self._on_cancel()
        except StageBlocked as b:
            stage = b.stage or self.run.state
            resume_at = getattr(b, "resume_at", None)
            fields: dict = {"blocked_reason": b.reason, "blocked_stage": stage}
            if resume_at:
                fields["auto_resume_at"] = float(resume_at)  # RunManager resumes the run then
            else:
                run.extra.pop("auto_resume_at", None)
            self._state("blocked", **fields)
            data = {"reason": b.reason, "stage": stage}
            if resume_at:
                data["auto_resume_at"] = utc_iso(resume_at)
            self._publish(ev.RUN_BLOCKED, data)
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
                    self._stage("build", lambda: self._build(ctx), rerun=True)
                else:
                    prev_gate = load_gate(ctx, n - 1) or {}
                    from ..util.fs import read_json
                    review = read_json(ctx.run_dir / f"review-{n - 1}.json", default={}) or {}
                    self._stage("fix", lambda: fix_stage(ctx, n, prev_gate, review), rerun=True)
                rec["commit"] = ctx.slides.commit_iteration(run, n, None)
                rec["built"] = True
                run.extra.pop("partial_work_kept", None)  # the iteration is committed: no partial work any more
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
                             f"the gate is {_gate_pct(ctx, gate)} percent and it {'passed' if gate.get('ok') else 'did not pass'}.")
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

    def _build(self, ctx: StageContext) -> Any:
        """Select the engine first (no engine -> blocked, nothing announced), then narrate what really builds."""
        ctx.engine = ctx.engine or select_engine(ctx)
        name = str(getattr(ctx.engine, "name", "") or "")
        label = _ENGINE_LABELS.get(name) or (f"The {name} code engine" if name else "The code engine")
        backend, model = name, ""
        if name == "hub":  # name what the Hub really runs (its status was read when the engine was selected)
            try:
                details = ctx.engine.availability().details or {}
            except Exception:  # noqa: BLE001 - narration only; the generic label stays
                details = {}
            backend = str(details.get("remote_backend") or "")
            if backend in _ENGINE_LABELS and backend != "hub":
                label = f"{_ENGINE_LABELS[backend]} on the Kyora Hub"
            remote = details.get("remote_engine") if isinstance(details.get("remote_engine"), dict) else {}
            model = str(((remote.get("details") or {}) if isinstance(remote.get("details"), dict) else {})
                        .get("model") or "")
        elif name == "openai_agent":
            model = str(getattr(getattr(self.cfg, "agent_engine", None), "model", "") or "")
        if backend == "openai_agent":  # its own model; Claude Code's effort setting does not apply to it
            how = f" with {model}" if model else ""
        else:
            effort = str(getattr(getattr(self.cfg, "claude_code", None), "effort", "") or "")
            how = " at maximum effort" if effort == "max" else (f" at {effort} effort" if effort else "")
        narrate(ctx, f"{label} is now building your game{how}.")
        return build_stage(ctx)

    def _finish(self, ctx: StageContext, gate: dict, *, ok: bool, reason: str | None = None) -> None:
        slide = self._stage("finalize", lambda: finalize_stage(ctx, gate, ok), rerun=True)
        final_state = "complete" if ok else "failed_gate"
        fields: dict = {"result_slide": slide["number"]}
        if reason:
            fields["error"] = reason
        self._state(final_state, **fields)
        self._publish(ev.RUN_COMPLETED, {"slide": slide["number"], "state": final_state, "gate_ok": ok,
                                         "pass_rate": gate.get("pass_rate"), "threshold": gate.get("threshold")})
        show_view(ctx, f"Slide v{slide['number']} {'passed' if ok else 'is below'} the {_gate_pct(ctx, gate)}% gate",
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

    def _on_shutdown(self) -> None:
        """Kyora is stopping: keep the run's work (no rollback; what the engines of an interrupted BUILD/FIX wrote was
        committed by ``run_engine``) and leave it resumable."""
        stage = self.run.state
        self.run.extra.pop("auto_resume_at", None)  # an interrupted run waits for the user's Resume
        try:
            self._state("blocked", blocked_reason=INTERRUPTED_REASON, blocked_stage=stage)
        except Exception as exc:  # noqa: BLE001
            log.error("cannot record interruption of %s: %s", self.run.id, exc)
        self._publish(ev.RUN_BLOCKED, {"reason": INTERRUPTED_REASON, "stage": stage})
        log.info("run %s interrupted at %s by shutdown", self.run.id, stage)


__all__ = ["RunManager", "Runner", "ACTIVE_STATES", "INTERRUPTED_REASON", "AUTO_RESUME_GRACE_S", "AUTO_RESUME_MAX",
           "auto_resume_epoch"]
