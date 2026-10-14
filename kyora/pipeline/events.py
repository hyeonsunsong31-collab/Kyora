"""Event bus: every pipeline/model/engine signal flows through here.

Events are persisted to sqlite (except high-frequency ``model.delta`` tokens,
which are fan-out only) so the UI can replay with ``since`` and SSE
``Last-Event-ID`` after a reconnect. Thread-safe.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# Event type constants (documented in docs/architecture.md)
RUN_CREATED, RUN_STATE, RUN_BLOCKED, RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_AWAITING_APPROVAL = (
    "run.created", "run.state", "run.blocked", "run.completed", "run.failed", "run.cancelled", "run.awaiting_approval")
STAGE_STARTED, STAGE_PROGRESS, STAGE_FINISHED = "stage.started", "stage.progress", "stage.finished"
MODEL_REQUEST, MODEL_DELTA, MODEL_METRICS, MODEL_DONE, MODEL_ERROR = (
    "model.request", "model.delta", "model.metrics", "model.done", "model.error")
ENGINE_SESSION, ENGINE_MODEL_RESOLVED, ENGINE_TEXT, ENGINE_THINKING, ENGINE_TOOL_USE, ENGINE_TOOL_RESULT, ENGINE_RESULT, ENGINE_ERROR, ENGINE_COMPAT_RETRY, ENGINE_STDERR = (
    "engine.session", "engine.model_resolved", "engine.text", "engine.thinking", "engine.tool_use", "engine.tool_result",
    "engine.result", "engine.error", "engine.compat_retry", "engine.stderr")
JUDGE_REQUEST, JUDGE_VERDICT, JUDGE_UNAVAILABLE = "judge.request", "judge.verdict", "judge.unavailable"
TEST_STATIC, TEST_NODE, TEST_AWAIT_BROWSER, TEST_BROWSER_RESULT, GATE_RESULT, FIX_ITERATION, REVIEW_DECISION = (
    "test.static", "test.node", "test.await_browser", "test.browser_result", "gate.result", "fix.iteration", "review.decision")
SLIDE_CREATED, DECK_UPDATED = "slide.created", "deck.updated"
TOKKIO_NARRATION, TOKKIO_VIEW, TOKKIO_CLEAR = "tokkio.narration", "tokkio.view", "tokkio.clear"
SELFCHECK_UPDATE = "selfcheck.update"
BENCH_PROGRESS, BENCH_DONE = "bench.progress", "bench.done"
NON_PERSISTED = {MODEL_DELTA}


@dataclass
class Event:
    id: int
    ts: float
    type: str
    run_id: str | None
    project_id: str | None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "type": self.type, "run_id": self.run_id,
                "project_id": self.project_id, "data": self.data}


class Subscription:
    def __init__(self, bus: "EventBus", run_id: str | None, project_id: str | None, types: set[str] | None, prefixes: tuple[str, ...]):
        self.bus = bus
        self.run_id = run_id
        self.project_id = project_id
        self.types = types
        self.prefixes = prefixes
        self.queue: "queue.Queue[Event | None]" = queue.Queue(maxsize=10000)
        self.closed = False

    def matches(self, ev: Event) -> bool:
        if self.run_id and ev.run_id != self.run_id:
            return False
        if self.project_id and ev.project_id != self.project_id:
            return False
        if self.types and ev.type not in self.types and not any(ev.type.startswith(p) for p in self.prefixes):
            return False
        return True

    def close(self) -> None:
        self.closed = True
        self.bus.unsubscribe(self)
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class EventBus:
    def __init__(self, db_path: Path | str | None = None):
        self._db_path = str(db_path) if db_path else ":memory:"
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL") if self._db_path != ":memory:" else None
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, run_id TEXT, project_id TEXT, data TEXT)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id)")
        self._conn.commit()
        self._lock = threading.RLock()
        self._subs: list[Subscription] = []
        self._mem_counter = 0

    # ------------------------------------------------------------------ publish
    def publish(self, type: str, data: dict | None = None, *, run_id: str | None = None, project_id: str | None = None,
                persist: bool | None = None) -> Event:
        data = data or {}
        if persist is None:
            persist = type not in NON_PERSISTED
        ts = time.time()
        with self._lock:
            if persist:
                cur = self._conn.execute(
                    "INSERT INTO events (ts, type, run_id, project_id, data) VALUES (?, ?, ?, ?, ?)",
                    (ts, type, run_id, project_id, json.dumps(data, ensure_ascii=False, default=str)))
                self._conn.commit()
                ev_id = int(cur.lastrowid)
            else:
                ev_id = 0
            ev = Event(id=ev_id, ts=ts, type=type, run_id=run_id, project_id=project_id, data=data)
            subs = list(self._subs)
        for sub in subs:
            if sub.closed or not sub.matches(ev):
                continue
            try:
                sub.queue.put_nowait(ev)
            except queue.Full:
                # drop oldest to keep the live stream moving
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(ev)
                except Exception:
                    pass
        return ev

    # ------------------------------------------------------------------ subscribe
    def subscribe(self, *, run_id: str | None = None, project_id: str | None = None,
                  types: Iterable[str] | None = None) -> Subscription:
        tset = set(types) if types else None
        prefixes = tuple(t[:-1] for t in (tset or ()) if t.endswith("*"))
        sub = Subscription(self, run_id, project_id, tset, prefixes)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    # ------------------------------------------------------------------ query
    def replay(self, since_id: int = 0, *, run_id: str | None = None, project_id: str | None = None,
               types: Iterable[str] | None = None, limit: int = 5000) -> list[Event]:
        clauses = ["id > ?"]
        params: list = [int(since_id)]
        if run_id:
            clauses.append("run_id = ?"); params.append(run_id)
        if project_id:
            clauses.append("project_id = ?"); params.append(project_id)
        tlist = list(types) if types else []
        exact = [t for t in tlist if not t.endswith("*")]
        prefixes = [t[:-1] for t in tlist if t.endswith("*")]
        if exact or prefixes:
            ors = []
            if exact:
                ors.append("type IN (%s)" % ",".join("?" * len(exact))); params.extend(exact)
            for p in prefixes:
                ors.append("type LIKE ?"); params.append(p + "%")
            clauses.append("(" + " OR ".join(ors) + ")")
        sql = "SELECT id, ts, type, run_id, project_id, data FROM events WHERE %s ORDER BY id ASC LIMIT ?" % " AND ".join(clauses)
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Event(id=r[0], ts=r[1], type=r[2], run_id=r[3], project_id=r[4], data=json.loads(r[5] or "{}")) for r in rows]

    def last_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(row[0])

    def wait_for(self, type: str, pred: Callable[[Event], bool] | None = None, timeout: float = 60.0,
                 *, run_id: str | None = None) -> Event | None:
        sub = self.subscribe(run_id=run_id, types=[type])
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    ev = sub.queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if ev is None:
                    return None
                if pred is None or pred(ev):
                    return ev
        finally:
            sub.close()

    def close(self) -> None:
        with self._lock:
            for s in list(self._subs):
                s.close()
            self._conn.close()


class DeltaCoalescer:
    """Batches streaming token deltas into at most ~10 ``model.delta`` events per second."""

    def __init__(self, bus: EventBus, *, run_id: str | None, project_id: str | None, role: str, model: str, interval: float = 0.1):
        self.bus = bus
        self.run_id = run_id
        self.project_id = project_id
        self.role = role
        self.model = model
        self.interval = interval
        self._buf: dict[str, list[str]] = {"content": [], "reasoning": []}
        self._last = 0.0
        self._lock = threading.Lock()

    def add(self, kind: str, text: str) -> None:
        with self._lock:
            self._buf.setdefault(kind, []).append(text)
            now = time.monotonic()
            if now - self._last >= self.interval:
                self._flush_locked()
                self._last = now

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        payload = {k: "".join(v) for k, v in self._buf.items() if v}
        if not payload:
            return
        self._buf = {k: [] for k in self._buf}
        self.bus.publish(MODEL_DELTA, {"role": self.role, "model": self.model, **payload},
                         run_id=self.run_id, project_id=self.project_id, persist=False)
