"""Kyora Bench runner: runs the real pipeline once per bench task and scores the run.

Each task is a full Kyora pipeline run (Kimi K3 intake/plan/judge/review,
Nemotron design, the selected code engine, static + Node + in-browser tests and
the >= 94 % gate). The TEST stage replaces the generated ``tests/browser-tests.js``
with the bench's own test file for the task (``options.bench.tests_file``), so the
gate measures the bench's acceptance tests. Nothing is simulated here: without a
code engine the bench refuses to start, and every number in ``results.json`` and
``report.html`` comes from a recorded run.

Task files are package resources (``kyora/bench/tasks``) read with
``importlib.resources`` so they also work inside ``kyora.pyz``; when an absolute
path is needed and the resources live inside a zip, the file is extracted to
``KYORA_HOME/bench/tasks-cache/`` first.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import queue
import re
import threading
import time
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from ..util.fs import atomic_write_bytes, read_json
from ..util.ids import now_iso
from . import report, scoring
from .scoring import SUITE_SIZE, TaskResult

log = logging.getLogger("kyora.bench")

WARNING = ("Each task runs the full pipeline: Nemotron + Kimi K3 tokens on your NVIDIA keys and Claude subscription "
           "usage at max effort; typically 10-30 min per task.")
NO_ENGINE = ("no code engine available (BLOCKED): install Claude Code and run claude login, link a Kyora Hub, "
             "or add an Anthropic API key")
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.blocked", "run.cancelled")
TERMINAL_STATES = frozenset({"complete", "failed_gate", "failed", "blocked", "cancelled"})
TASK_ID_RE = re.compile(r"^b\d{2}-[a-z0-9][a-z0-9-]*$")
FIXTURE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}

HARNESS_NOTES = """Kyora Bench harness rules (identical for every bench task; the bench's own browser tests check them):
- Plain HTML5 + JavaScript, no dependencies, no build step, no external URLs; index.html is the entry. The game is drawn on the first <canvas> of the page (DOM overlays for text and menus are fine).
- window.Game.step(n) advances exactly n fixed simulation ticks of 1/60 s synchronously and returns after the work is done; step(10000) must take well under a second (simulate only, do not render inside step). The first call to step() (n may be 0) switches the game to a manual clock for the rest of the page's life: the requestAnimationFrame loop keeps rendering but no longer advances the simulation, and start() does not leave manual mode. Normal real-time play is unchanged when step() is never called.
- Timers, cooldowns, spawns and animations that affect game state count ticks; nothing that changes getState() may depend on wall-clock time, except touch gestures that the brief explicitly times in milliseconds.
- The page loads in phase 'menu' and never starts by itself. start() works from any phase and resets the game (persisted data such as a saved high score stays). getState() returns a fresh, JSON-serialisable snapshot; changing it must not change the game.
- All randomness comes from a small PRNG seeded by setSeed(n): the same seed and the same calls give the same game. setSeed() takes effect at the next start().
- Coordinates in getState() are game-world units with x growing right and y growing down, unless the brief says otherwise. Helpers that return screen points (for example cellCenter) return {x, y} in CSS pixels relative to the top-left corner of the canvas for the current layout.
- Keyboard: listen on window for keydown/keyup and use KeyboardEvent.code ('ArrowLeft', 'Space', 'KeyP', 'Digit1', 'Escape', ...). Keydown takes effect immediately (a held movement key moves the game on every tick while held); a key held down repeats no discrete action.
- Pointer and touch: one click or tap is one action. Browsers fire pointer events and touch events for every touch, plus mouse and click events for an unprevented tap, so handle one family per gesture: either Pointer Events for mouse and touch, or touchstart/touchmove/touchend (calling preventDefault()) plus mouse events. Input handlers act immediately, without waiting for a tick. Give the canvas CSS touch-action: none so touches never scroll the page.
- Write .kyora/manifest.json and your own tests/browser-tests.js. When this task is measured, the bench replaces tests/browser-tests.js with its own test file for the contract above."""


class BenchRefused(Exception):
    """A bench run was refused; ``status`` is the HTTP status the route should use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ----------------------------------------------------------------------------- resources
def _tasks_root():
    return resources.files("kyora").joinpath("bench").joinpath("tasks")


def _resource(*parts: str):
    node = _tasks_root()
    for part in parts:
        node = node.joinpath(part)
    return node


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dataclass-like object or a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class BenchRunner:
    """Runs Kyora Bench tasks through the real pipeline and records measured results.

    Thread-safe: one bench run at a time (``run`` and ``start_async`` refuse while
    another run is active). ``app`` is the composition root (``kyora.app.App``)
    or any object exposing ``store``, ``run_manager``, ``bus``, ``engine()`` and
    ``home``.
    """

    def __init__(self, app, *, poll_s: float = 2.0, terminal_grace_s: float = 120.0):
        self.app = app
        self.poll_s = float(poll_s)
        self.terminal_grace_s = float(terminal_grace_s)
        self._lock = threading.Lock()
        self._running = False
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._current: dict | None = None
        self._progress: dict | None = None
        self._task_cache: dict[str, dict] = {}
        self._server = None

    # ------------------------------------------------------------------ paths
    @property
    def home(self) -> Path:
        """KYORA_HOME of the app."""
        return Path(getattr(self.app, "home"))

    @property
    def bench_dir(self) -> Path:
        """``KYORA_HOME/bench`` (results live in ``<timestamp>/`` sub-directories)."""
        return self.home / "bench"

    # ------------------------------------------------------------------ tasks
    def task_ids(self) -> list[str]:
        """Sorted ids of every task shipped in ``kyora/bench/tasks``."""
        ids = []
        for node in _tasks_root().iterdir():
            name = node.name
            if name.endswith(".json") and node.is_file() and TASK_ID_RE.match(name[:-5]):
                ids.append(name[:-5])
        return sorted(ids)

    def load_task(self, task_id: str) -> dict:
        """Return the full task definition; ``FileNotFoundError`` for an unknown id."""
        if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
            raise FileNotFoundError(f"unknown bench task: {task_id!r}")
        with self._lock:
            cached = self._task_cache.get(task_id)
        if cached is not None:
            return json.loads(json.dumps(cached))
        node = _resource(f"{task_id}.json")
        try:
            data = json.loads(node.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, KeyError) as exc:
            raise FileNotFoundError(f"unknown bench task: {task_id!r}") from exc
        if data.get("id") != task_id:
            raise ValueError(f"bench task file {task_id}.json declares id {data.get('id')!r}")
        with self._lock:
            self._task_cache[task_id] = data
        return json.loads(json.dumps(data))

    def list_tasks(self) -> list[dict]:
        """Summaries of every task, sorted by id."""
        out = []
        for tid in self.task_ids():
            t = self.load_task(tid)
            out.append({
                "id": t["id"], "title": t.get("title", ""), "category": t.get("category", ""),
                "kind": t.get("kind", "generate"), "engine": t.get("engine", "vanilla"),
                "timeout_min": t.get("timeout_min"), "fixture": t.get("fixture"),
                "min_browser_tests": (t.get("checks") or {}).get("min_browser_tests"),
                "min_pass_rate": scoring.task_rule((t.get("pass_rule") or {}).get("min_pass_rate")),
            })
        return out

    def browser_test_ids(self, task: dict) -> list[str]:
        """Test ids registered with ``KyoraTest.test('<id>', ...)`` in the task's browser-tests file."""
        name = (task.get("checks") or {}).get("browser_tests_file") or f"{task['id']}.browser-tests.js"
        text = _resource(name).read_text(encoding="utf-8")
        return re.findall(r"KyoraTest\.test\(\s*['\"]([^'\"]+)['\"]", text)

    def tests_file_path(self, task: dict) -> Path:
        """Absolute path of the task's browser-tests file (extracted to the cache when packaged in a zip)."""
        name = (task.get("checks") or {}).get("browser_tests_file") or f"{task['id']}.browser-tests.js"
        if "/" in name or "\\" in name or not name.endswith(".browser-tests.js"):
            raise ValueError(f"invalid browser_tests_file: {name!r}")
        node = _resource(name)
        if isinstance(node, Path) and node.is_file():
            return node.resolve()
        data = node.read_bytes()
        dest = self.bench_dir / "tasks-cache" / name
        try:
            same = dest.is_file() and dest.read_bytes() == data
        except OSError:
            same = False
        if not same:
            atomic_write_bytes(dest, data)
        return dest.resolve()

    def fixture_zip(self, name: str) -> bytes:
        """Build a deterministic ZIP (files at the archive root) of ``tasks/fixtures/<name>``."""
        if not FIXTURE_RE.match(name or ""):
            raise ValueError(f"invalid fixture name: {name!r}")
        root = _resource("fixtures", name)
        if not root.is_dir():
            raise FileNotFoundError(f"bench fixture not found: {name}")
        entries: list[tuple[str, bytes]] = []

        def walk(node, prefix: str) -> None:
            for child in node.iterdir():
                if child.name in _SKIP_NAMES or child.name.endswith(".pyc"):
                    continue
                rel = f"{prefix}{child.name}"
                if child.is_dir():
                    walk(child, rel + "/")
                elif child.is_file():
                    entries.append((rel, child.read_bytes()))

        walk(root, "")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, data in sorted(entries):
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
        return buf.getvalue()

    def compose_prompt(self, task: dict) -> str:
        """The exact prompt submitted to the pipeline: the task brief plus the shared harness rules."""
        return f"{task['prompt'].strip()}\n\n{HARNESS_NOTES}\n"

    # ------------------------------------------------------------------ engine / estimate
    def _engine(self):
        try:
            return self.app.engine()
        except Exception as exc:  # noqa: BLE001 - an engine probe failure means "no engine"
            log.warning("bench: engine selection failed: %s", exc)
            return None

    @staticmethod
    def _engine_name(eng) -> str | None:
        if eng is None:
            return None
        return getattr(eng, "name", None) or type(eng).__name__

    def _resolve_ids(self, task_ids: Iterable[str] | str | None) -> tuple[list[str], list[str]]:
        known = self.task_ids()
        if task_ids is None or task_ids == "" or task_ids == []:
            return known, []
        if isinstance(task_ids, str):
            task_ids = [x for x in task_ids.split(",")]
        wanted: list[str] = []
        unknown: list[str] = []
        for raw in task_ids:
            tid = str(raw).strip()
            if not tid:
                continue
            if tid in known:
                if tid not in wanted:
                    wanted.append(tid)
                continue
            # allow the short form "b01"
            match = [k for k in known if k.split("-", 1)[0] == tid]
            if len(match) == 1:
                if match[0] not in wanted:
                    wanted.append(match[0])
            else:
                unknown.append(tid)
        return wanted, unknown

    def estimate(self, task_ids: Iterable[str] | str | None = None) -> dict:
        """What a run of ``task_ids`` (default: the whole suite) would execute; spends nothing."""
        ids, unknown = self._resolve_ids(task_ids)
        eng = self._engine()
        timeouts = []
        for tid in ids:
            try:
                timeouts.append(int(self.load_task(tid).get("timeout_min") or 0))
            except (FileNotFoundError, ValueError) as exc:
                log.warning("bench: cannot read task %s: %s", tid, exc)
        out = {
            "tasks": ids,
            "count": len(ids),
            "pipeline_runs": len(ids),
            "warning": WARNING,
            "requires_engine": True,
            "engine_available": eng is not None,
            "engine": self._engine_name(eng),
            "suite_size": len(self.task_ids()),
            "max_minutes": sum(timeouts),
            "certifiable": len(ids) == SUITE_SIZE and not unknown,
        }
        if unknown:
            out["unknown"] = unknown
        return out

    # ------------------------------------------------------------------ state
    def is_running(self) -> bool:
        """True while a bench run is in progress."""
        with self._lock:
            return self._running

    def status(self) -> dict:
        """Current progress (``running``, ``current`` task/run, ``progress`` counters)."""
        with self._lock:
            return {"running": self._running, "current": dict(self._current) if self._current else None,
                    "progress": dict(self._progress) if self._progress else None}

    def cancel(self) -> bool:
        """Request cancellation of the active bench run; False when none is running."""
        with self._lock:
            if not self._running or self._cancel is None:
                return False
            self._cancel.set()
            return True

    def _result_dirs(self) -> list[Path]:
        if not self.bench_dir.is_dir():
            return []
        return sorted((p for p in self.bench_dir.iterdir() if p.is_dir() and (p / "results.json").is_file()),
                      key=lambda p: p.name)

    def last_results(self) -> dict | None:
        """The latest ``results.json`` payload, or None when the bench never ran here."""
        for d in reversed(self._result_dirs()):
            try:
                data = read_json(d / "results.json", default=None)
            except (OSError, ValueError) as exc:
                log.warning("bench: unreadable %s: %s", d / "results.json", exc)
                continue
            if isinstance(data, dict):
                data.setdefault("results_dir", str(d))
                return data
        return None

    def last_report_path(self) -> Path | None:
        """Path of the latest ``report.html``, or None."""
        for d in reversed(self._result_dirs()):
            if (d / "report.html").is_file():
                return d / "report.html"
        return None

    def badge(self) -> dict:
        """Certification badge computed from the latest measured run (never pre-filled)."""
        last = self.last_results()
        if not last:
            return {"certified": False, "reason": "no bench run yet", "pass_rate": None, "passed": 0, "run": 0,
                    "suite_complete": False, "engine": None, "model": None, "timestamp": None,
                    "total_suite": SUITE_SIZE, "threshold": scoring.THRESHOLD, "measured": False}
        stored = last.get("score") if isinstance(last.get("score"), dict) else {}
        sc = scoring.score(last.get("tasks") or [], total_suite=int(last.get("suite_size") or SUITE_SIZE),
                           engine=stored.get("engine"), model=stored.get("model"),
                           timestamp=stored.get("timestamp") or last.get("finished_at"))
        out = sc.to_dict()
        out.update({"measured": True, "bench_id": last.get("bench_id"), "results_dir": last.get("results_dir"),
                    "in_progress": bool(last.get("in_progress")), "cancelled": bool(last.get("cancelled"))})
        if last.get("in_progress") and out["certified"] is False and "in progress" not in out["reason"]:
            out["reason"] = ("bench run in progress; " + out["reason"]).rstrip("; ")
        return out

    # ------------------------------------------------------------------ run
    def _preflight(self, task_ids, confirm: bool) -> tuple[list[str], str | None, int]:
        ids, unknown = self._resolve_ids(task_ids)
        if confirm is not True:
            return ids, ("confirmation required: " + WARNING + " Pass confirm=true to start."), 400
        if unknown:
            return ids, "unknown bench task(s): " + ", ".join(unknown), 400
        if not ids:
            return ids, "no bench tasks selected", 400
        if self._engine() is None:
            return ids, NO_ENGINE, 400
        missing = [n for n in ("store", "run_manager", "bus") if getattr(self.app, n, None) is None]
        if missing:
            return ids, "Kyora components unavailable: " + ", ".join(missing), 400
        return ids, None, 0

    def _begin(self, cancel: threading.Event) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._cancel = cancel
            self._current = None
            self._progress = None
            return True

    def _end(self) -> None:
        with self._lock:
            self._running = False
            self._current = None

    def run(self, task_ids: Iterable[str] | str | None = None, *, confirm: bool, cancel: threading.Event) -> dict:
        """Run the selected tasks sequentially and return the ``results.json`` payload.

        Refuses with ``{"error": ...}`` when ``confirm`` is not True, when no code
        engine is available, or when another bench run is in progress.
        """
        ids, error, _status = self._preflight(task_ids, confirm)
        if error:
            return {"error": error, "estimate": self.estimate(task_ids)}
        if not self._begin(cancel):
            return {"error": "a bench run is already in progress"}
        try:
            return self._execute(ids, cancel)
        finally:
            self._end()

    def start_async(self, task_ids: Iterable[str] | str | None = None, confirm: bool = False) -> threading.Thread:
        """Validate and start a bench run in a background thread (used by ``POST /api/bench/run``).

        Raises :class:`BenchRefused` (status 400 or 409) instead of starting.
        """
        ids, error, status = self._preflight(task_ids, confirm)
        if error:
            raise BenchRefused(error, status)
        cancel = threading.Event()
        if not self._begin(cancel):
            raise BenchRefused("a bench run is already in progress", 409)

        def _target() -> None:
            try:
                self._execute(ids, cancel)
            except Exception:  # noqa: BLE001 - the thread must log, never die silently
                log.exception("bench run crashed")
            finally:
                self._end()

        t = threading.Thread(target=_target, name="kyora-bench", daemon=True)
        with self._lock:
            self._thread = t
        t.start()
        return t

    # ------------------------------------------------------------------ internals
    def _new_out_dir(self) -> tuple[str, Path]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        bench_id, n = stamp, 1
        while (self.bench_dir / bench_id).exists():
            n += 1
            bench_id = f"{stamp}-{n}"
        out = self.bench_dir / bench_id
        out.mkdir(parents=True, exist_ok=False)
        return bench_id, out

    def _ensure_server(self) -> None:
        """Start an embedded loopback server when the app was built without one (``kyora bench`` CLI).

        The pipeline needs it: the judge hook posts to ``/api/judge`` and the browser
        harness is served from ``/runner/...``.
        """
        started = getattr(self.app, "started", None)
        if started is None or not hasattr(started, "is_set") or started.is_set():
            return
        if not hasattr(self.app, "register_routes") or not hasattr(self.app, "start"):
            return
        from ..server import KyoraServer, Router
        router = Router()
        self.app.register_routes(router)
        port = int(getattr(getattr(getattr(self.app, "config", None), "server", None), "port", 0) or 0)
        try:
            server = KyoraServer(self.app, "127.0.0.1", port, router)
        except OSError as exc:
            log.info("bench: port %s busy (%s); using an ephemeral port", port, exc)
            # Both listeners ephemeral: an explicit server.preview_port is likely held by the same Kyora
            # (or program) that holds the main port.
            server = KyoraServer(self.app, "127.0.0.1", 0, router, preview_port=0)
        server.start()
        self._server = server
        self.app.start(server.url)
        log.info("bench: embedded Kyora server at %s (open it to watch progress and run browser tests)", server.url)

    def _publish(self, type_: str, data: dict) -> None:
        try:
            self.app.bus.publish(type_, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("bench: could not publish %s: %s", type_, exc)

    def _execute(self, ids: list[str], cancel: threading.Event) -> dict:
        self._ensure_server()
        bench_id, out_dir = self._new_out_dir()
        eng = self._engine()
        engine_name = self._engine_name(eng)
        started_at = now_iso()
        results: list[TaskResult] = []
        skipped: list[str] = []
        prompts: dict[str, str] = {}
        log.info("bench %s: %d task(s) on %s -> %s", bench_id, len(ids), engine_name, out_dir)

        def payload(in_progress: bool) -> dict:
            sc = scoring.score(results, total_suite=SUITE_SIZE, engine=engine_name, model=None)
            return {
                "bench_id": bench_id, "kyora_version": _kyora_version(), "suite_size": SUITE_SIZE,
                "task_ids": list(ids), "started_at": started_at,
                "finished_at": None if in_progress else now_iso(), "in_progress": in_progress,
                "cancelled": cancel.is_set(), "engine": engine_name, "model": sc.model,
                "platform": getattr(self.app, "platform", None), "harness_rules_sha256": _sha(HARNESS_NOTES),
                "prompt_sha256": dict(prompts), "skipped": list(skipped),
                "tasks": [r.to_dict() for r in results], "score": sc.to_dict(),
            }

        with self._lock:
            self._progress = {"bench_id": bench_id, "done": 0, "total": len(ids)}
        report.write_json(out_dir / "results.json", payload(True))
        for i, tid in enumerate(ids):
            if cancel.is_set():
                skipped.extend(ids[i:])
                break
            task = self.load_task(tid)
            prompts[tid] = _sha(self.compose_prompt(task))
            res = self._run_task(task, cancel, bench_id)
            results.append(res)
            with self._lock:
                self._progress = {"bench_id": bench_id, "done": i + 1, "total": len(ids)}
            log.info("bench %s: %s %s (%s, %.0f s)%s", bench_id, tid, "PASS" if res.passed else "FAIL",
                     res.state, res.duration_s, f" - {res.error}" if res.error else "")
            self._publish("bench.progress", {"done": i + 1, "total": len(ids), "task_id": tid, "passed": res.passed,
                                             "state": res.state, "bench_id": bench_id})
            report.write_json(out_dir / "results.json", payload(True))
        final = payload(False)
        report.write_json(out_dir / "results.json", final)
        report.write_html(out_dir / "report.html", final)
        final["results_dir"] = str(out_dir)
        self._publish("bench.done", {"score": final["score"], "bench_id": bench_id, "cancelled": final["cancelled"],
                                     "results_dir": str(out_dir)})
        return final

    def _run_task(self, task: dict, cancel: threading.Event, bench_id: str) -> TaskResult:
        app = self.app
        tid = task["id"]
        checks = task.get("checks") or {}
        rule = scoring.task_rule((task.get("pass_rule") or {}).get("min_pass_rate"))
        res = TaskResult(task_id=tid, title=task.get("title", ""), category=task.get("category", ""),
                         kind=task.get("kind", "generate"), min_browser_tests=checks.get("min_browser_tests"),
                         started_at=now_iso(), min_pass_rate=rule)
        t0 = time.monotonic()
        try:
            project = app.store.create(f"bench-{tid}-{bench_id}")
            pid = _field(project, "id")
            res.project_id = pid
            test_ids = self.browser_test_ids(task)
            options: dict[str, Any] = {
                "bench": {"task_id": tid, "tests_file": str(self.tests_file_path(task)), "test_ids": test_ids,
                          "min_browser_tests": checks.get("min_browser_tests"), "bench_id": bench_id,
                          "contract": task.get("contract"), "static_checks": checks.get("static") or [],
                          "min_pass_rate": rule},  # the gate uses this, never Settings > Quality gate
                "import_zip_attachment": None,
            }
            attachment_ids: list[str] = []
            if task.get("fixture"):
                data = self.fixture_zip(task["fixture"])
                att = self._add_fixture(pid, f"{task['fixture']}.zip", data)
                aid = _field(att, "id")
                attachment_ids.append(aid)
                options["import_zip_attachment"] = aid
            run = app.store.create_run(pid, task.get("kind", "generate"), self.compose_prompt(task), None,
                                       attachment_ids, options)
            rid = _field(run, "id")
            res.run_id = rid
            with self._lock:
                self._current = {"task_id": tid, "run_id": rid, "project_id": pid}
            res.error = self._wait_for_run(rid, pid, float(task.get("timeout_min") or 30) * 60.0, cancel)
            final = app.store.get_run(rid)
            res.state = str(_field(final, "state", "unknown"))
            gate = _field(final, "gate")
            res.gate = _jsonable(gate) if gate is not None else None
            final_gate = _field(gate, "final") if gate is not None else None
            res.threshold = _field(final_gate, "threshold") if final_gate is not None else None
            engine = _field(final, "engine") or {}
            res.engine = _field(engine, "backend") or None
            res.served = self._served(final, res.engine)
            models = list(dict.fromkeys(p.split("/", 1)[1] for p in res.served))
            res.active_model = " + ".join(models) if models else (_field(engine, "active_model") or None)
            if not res.error:
                run_error = _field(final, "error") or _field(final, "blocked_reason")
                if run_error and res.state != "complete":
                    res.error = str(run_error)
            res.bench_tests_seen = self._count_bench_tests(final, tid)
        except Exception as exc:  # noqa: BLE001 - one broken task must not stop the bench
            log.exception("bench task %s failed to run", tid)
            res.error = f"{type(exc).__name__}: {exc}"
            if res.state in ("not_run", "", None):
                res.state = "error"
        res.duration_s = round(time.monotonic() - t0, 1)
        res.finished_at = now_iso()
        res.evaluate()
        if res.state == "complete" and not res.passed and not res.error and res.bench_tests_seen is not None \
                and res.min_browser_tests is not None and res.bench_tests_seen < res.min_browser_tests:
            res.error = (f"only {res.bench_tests_seen} of the bench's {res.min_browser_tests} browser tests "
                         "appear in the gate results")
        return res

    def _add_fixture(self, pid: str, filename: str, data: bytes):
        try:
            return self.app.store.add_attachment(pid, filename, "application/zip", data, "import")
        except ValueError as exc:
            log.info("bench: attachment role 'import' refused (%s); using the default role", exc)
            return self.app.store.add_attachment(pid, filename, "application/zip", data, "reference")

    def _wait_for_run(self, rid: str, pid: str | None, timeout_s: float, cancel: threading.Event) -> str | None:
        """Submit ``rid`` and block until it reaches a terminal state; returns an error string or None."""
        app = self.app
        sub = app.bus.subscribe(run_id=rid, types=list(TERMINAL_EVENTS) + ["test.await_browser"])
        error: str | None = None
        try:
            app.run_manager.submit(rid)
            deadline = time.monotonic() + timeout_s
            cancel_sent = False
            grace_deadline: float | None = None
            while True:
                now = time.monotonic()
                if grace_deadline is not None and now >= grace_deadline:
                    error = (error or "") + " (run did not report a terminal state in time)"
                    break
                if not cancel_sent and (cancel.is_set() or now >= deadline):
                    if not cancel.is_set():
                        error = f"timed out after {timeout_s / 60:.0f} min"
                    else:
                        error = "bench cancelled"
                    self._cancel_run(rid)
                    cancel_sent = True
                    grace_deadline = time.monotonic() + self.terminal_grace_s
                wait = self.poll_s
                limit = grace_deadline if grace_deadline is not None else deadline
                wait = max(0.05, min(wait, limit - time.monotonic()))
                try:
                    ev = sub.queue.get(timeout=wait)
                except queue.Empty:
                    ev = None
                if ev is not None and ev.type == "test.await_browser":
                    url = (ev.data or {}).get("runner_url")
                    base = getattr(app, "server_url", "") or ""
                    log.info("bench: run %s waits for browser tests: %s%s", rid, base, url or "")
                    continue
                if ev is not None and ev.type in TERMINAL_EVENTS:
                    break
                # missed-event safety net: poll the stored state
                try:
                    state = _field(app.store.get_run(rid), "state")
                except Exception as exc:  # noqa: BLE001
                    log.debug("bench: get_run(%s) failed: %s", rid, exc)
                    state = None
                if state in TERMINAL_STATES:
                    break
        finally:
            sub.close()
        return error

    def _cancel_run(self, rid: str) -> None:
        try:
            self.app.run_manager.cancel(rid)
        except Exception as exc:  # noqa: BLE001
            log.warning("bench: cancel of run %s failed: %s", rid, exc)

    @staticmethod
    def _served(run, backend: str | None) -> list[str]:
        """``engine/model`` of every engine call of the run (``extra.engine_runs``), in order of first use."""
        extra = _field(run, "extra") or {}
        calls = extra.get("engine_runs") if isinstance(extra, dict) else None
        out = [f"{c.get('backend') or backend or '?'}/{c['active_model']}" for c in calls or []
               if isinstance(c, dict) and c.get("active_model")]
        return list(dict.fromkeys(out))

    def _count_bench_tests(self, run, task_id: str) -> int | None:
        """How many of the bench's browser tests appear in the run's final gate checks (None = unknown)."""
        prefix = task_id.split("-", 1)[0] + "-t"
        checks = None
        gate = _field(run, "gate") or {}
        final = _field(gate, "final") or {}
        if isinstance(final, dict) and isinstance(final.get("checks"), list):
            checks = final["checks"]
        if checks is None:
            checks = self._gate_checks_from_disk(run)
        if checks is None:
            return None
        ids = {c.get("id") for c in checks if isinstance(c, dict) and c.get("group", "browser") == "browser"
               and str(c.get("id", "")).startswith(prefix) and c.get("status") in ("pass", "fail")}
        return len(ids)

    def _gate_checks_from_disk(self, run) -> list | None:
        store = self.app.store
        run_dir_fn = getattr(store, "run_dir", None)
        if run_dir_fn is None:
            return None
        try:
            run_dir = Path(run_dir_fn(run))
        except Exception as exc:  # noqa: BLE001
            log.debug("bench: run_dir unavailable: %s", exc)
            return None
        iters = sorted((p for p in run_dir.glob("iter-*") if (p / "gate.json").is_file()),
                       key=lambda p: int(p.name.split("-", 1)[1]) if p.name.split("-", 1)[1].isdigit() else -1)
        if not iters:
            return None
        data = read_json(iters[-1] / "gate.json", default=None)
        if isinstance(data, dict) and isinstance(data.get("checks"), list):
            return data["checks"]
        return None


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return json.loads(json.dumps(obj, default=str))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _kyora_version() -> str:
    from .. import __version__
    return __version__
