"""Pipeline stages: intake -> plan -> design -> build -> test -> gate -> review/fix -> finalize.

Each stage is a plain function over a :class:`StageContext`. Stages never
fabricate output: a model or engine that cannot be reached raises
:class:`StageBlocked` (the run becomes ``blocked`` and can be resumed), a
cancellation raises :class:`RunCancelled`. Every stage persists its artefact
in the run directory (``intake.json``, ``plan.json`` ...) and re-reads it when
present, so a resumed run does not repeat model calls that already succeeded.

The orchestration (state machine, persistence, events) lives in
``kyora/pipeline/runner.py``; the HTTP routes in ``kyora/pipeline/routes.py``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import string
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from ..models.base import (FAILURE_KINDS, BusSink, CodeEngine, EngineJob, EngineResult, classify_failure,
                           engine_candidates, reset_time)
from ..util.fs import atomic_write_bytes, atomic_write_json, atomic_write_text, read_json
from ..util.ids import now_iso
from . import events as ev
from .quality import SKIP_DIRS

log = logging.getLogger("kyora.pipeline")

NO_ENGINE_REASON = ("no code engine: install Claude Code and run 'claude auth login', link a Kyora Hub "
                    "(Settings > Hub), add an Anthropic API key, or add an NVIDIA API key for Kyora's own code engine")
# Engine failover (settings.engines): an engine that fails for account reasons hands the same BUILD/FIX to the
# next available engine; these texts tell the next engine (or the resumed Claude Code session) where it starts.
FAILURE_LABELS = {"quota": "usage limit, credits or subscription", "auth": "login or API key refused",
                  "unavailable": "service unreachable or overloaded", "model": "model not available",
                  "interrupted": "Kyora stopped while it was running"}
CONTINUE_PREFIX = "Continue the task from where you stopped. "
PARTIAL_WORK_PREFIX = ("A previous code engine ({name}) stopped part-way ({reason}). The work tree already contains "
                       "its partial work: inspect it and complete the task below without starting over.\n\n")
CHANGED_SINCE_TEXT = ("Since this session stopped, {name} changed files in the work tree: inspect them before you "
                      "continue. ")
KEPT_WORK_TEXT = "The work tree contains the partial work of the stopped session: inspect it before you continue. "
# attempts whose session a resumed run continues (--resume): stopped by a usage limit, or by Kyora stopping
CONTINUED_FAILURES = ("quota", "interrupted")
# a usage limit whose reset already passed (refused again right after it, a stale value, clock skew): the n-th
# automatic resume waits this long, plus the run manager's minute of grace (1, 5, 15, then 30 min)
PAST_RESET_RETRY_S = (0.0, 240.0, 840.0, 1740.0)
# an engine stopped by a rate limit (HTTP 429, no reset time known) or an outage of its service is tried again after
# these waits, indexed by the run's automatic resumes so far (at most AUTO_RESUME_MAX of them)
TRANSIENT_RETRY_S = (120.0, 300.0, 900.0, 1800.0)
_RATE_LIMIT_RE = re.compile(r"\bHTTP 429\b|too many requests|rate[- ]?limit", re.IGNORECASE)
_CREDIT_RE = re.compile(r"\bHTTP 402\b|payment required|insufficient[ _](?:credit|balance|funds|quota)|billing|"
                        r"credit balance is too low", re.IGNORECASE)


def passes_by_itself(failure_kind: str | None, error: str | None) -> bool:
    """An engine failure that ends without the user: a service outage, or a rate limit (not credits or billing)."""
    if failure_kind == "unavailable":
        return True
    text = str(error or "")
    return failure_kind == "quota" and bool(_RATE_LIMIT_RE.search(text)) and not _CREDIT_RE.search(text)
SESSION_GONE_RE = re.compile(r"no conversation found|session\b.{0,60}(?:not found|does not exist|expired)|"
                             r"(?:could not|cannot|unable to) resume", re.IGNORECASE)
NO_BROWSER_REASON = ("no browser attached: open the run in Kyora on any device (Pipeline > Run tests on this device), "
                     "then press Resume")
TEST_ID_RE = re.compile(r"""KyoraTest\.test\(\s*(['"`])([^'"`]+)\1""")
ENGINE_ATTRS = {"claude_code": "claude_engine", "hub": "hub_client", "anthropic_api": "anthropic_engine",
                "openai_agent": "agent_engine"}


# ----------------------------------------------------------------------------- control flow
class StageBlocked(Exception):
    """The run cannot continue until something outside Kyora changes (resumable).

    ``resume_at`` (epoch seconds) is set when the cause ends by itself at a known time
    (a usage limit's reset): the run manager then resumes the run at that time.
    """

    def __init__(self, reason: str, stage: str = "", resume_at: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.resume_at = resume_at


class RunCancelled(Exception):
    """The user cancelled the run."""


# ----------------------------------------------------------------------------- context
@dataclass
class StageContext:
    app: Any
    run: Any
    project: Any
    run_dir: Path
    workspace: Path
    cancel: threading.Event
    slides: Any
    approval: threading.Event = field(default_factory=threading.Event)
    data: dict = field(default_factory=dict)
    engine: CodeEngine | None = None
    last_engine: dict | None = None
    shutdown: threading.Event | None = None  # set with ``cancel`` when Kyora stops: an interruption, not a cancel
    failed_over: bool = False  # the last BUILD/FIX failed over: the next one chooses its engine again

    @property
    def cfg(self):
        return self.app.config

    @property
    def bus(self):
        return self.app.bus

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def project_id(self) -> str:
        return self.run.project_id

    def publish(self, type_: str, data: dict | None = None) -> None:
        try:
            self.bus.publish(type_, data or {}, run_id=self.run.id, project_id=self.run.project_id)
        except Exception as exc:  # noqa: BLE001 - events must never break the pipeline
            log.warning("publish %s failed: %s", type_, exc)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise RunCancelled()

    def save_run(self) -> None:
        self.app.store.save_run(self.run)

    # artefacts ------------------------------------------------------------------
    def write_artifact(self, key: str, obj: Any) -> Path:
        path = self.run_dir / f"{key}.json"
        atomic_write_json(path, obj)
        self.run.artifacts[key] = path.name
        self.data[key] = obj
        return path

    def read_artifact(self, key: str) -> Any:
        if key in self.data:
            return self.data[key]
        obj = read_json(self.run_dir / f"{key}.json", default=None)
        if obj is not None:
            self.data[key] = obj
        return obj

    def iter_dir(self, n: int) -> Path:
        d = self.run_dir / f"iter-{n}"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ----------------------------------------------------------------------------- templates
_TEMPLATE_CACHE: dict[str, str] = {}


def render_template(name: str, **values: Any) -> str:
    """Render ``kyora/pipeline/templates/<name>`` with ``$placeholders`` (safe_substitute)."""
    text = _TEMPLATE_CACHE.get(name)
    if text is None:
        text = resources.files("kyora").joinpath("pipeline").joinpath("templates").joinpath(name).read_text(encoding="utf-8")
        _TEMPLATE_CACHE[name] = text
    return string.Template(text).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def _bullets(items: list[str], empty: str = "- (none)") -> str:
    items = [str(i).strip() for i in items if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else empty


def plan_tests(plan: dict) -> list[dict]:
    out = []
    for t in (plan or {}).get("acceptance_tests") or []:
        if isinstance(t, dict) and t.get("id"):
            out.append({"id": str(t["id"]), "name": str(t.get("name") or t["id"]), "kind": str(t.get("kind") or "browser"),
                        "steps": str(t.get("steps") or ""), "required": bool(t.get("required"))})
    return out


def plan_files(plan: dict) -> list[str]:
    out = []
    for f in (plan or {}).get("files") or []:
        path = f.get("path") if isinstance(f, dict) else f
        if isinstance(path, str) and path:
            out.append(path)
    return out


def bench_options(ctx: StageContext) -> dict:
    """``options.bench`` of a Kyora Bench run (the bench injected its own test file), else ``{}``."""
    bench = (ctx.run.options or {}).get("bench") or {}
    return bench if isinstance(bench, dict) and bench.get("tests_file") else {}


def gate_threshold(ctx: StageContext) -> float:
    """The gate threshold: Settings for normal runs; for bench runs the task's rule, never below 0.94."""
    bench = bench_options(ctx)
    if not bench:
        return float(ctx.cfg.quality.min_pass_rate)
    from ..bench.scoring import THRESHOLD
    try:
        rule = float(bench.get("min_pass_rate") or THRESHOLD)
    except (TypeError, ValueError):
        rule = THRESHOLD
    return max(THRESHOLD, min(1.0, rule))  # Settings > Quality gate cannot lower a bench task's bar


def threshold_pct(ctx: StageContext) -> str:
    return f"{gate_threshold(ctx) * 100:g}"


def served_engines(run: Any) -> list[dict]:
    """``{"backend", "model"}`` of every code-engine call of ``run`` a model served (``extra.engine_runs``), in order
    of first use and without repeats; the deck and the slide name each backend and model that worked on the run."""
    out: list[dict] = []
    for e in (run.extra or {}).get("engine_runs") or []:
        if isinstance(e, dict) and e.get("active_model"):
            item = {"backend": e.get("backend"), "model": str(e["active_model"])}
            if item not in out:
                out.append(item)
    if not out and (run.engine or {}).get("active_model"):
        out = [{"backend": run.engine.get("backend"), "model": str(run.engine["active_model"])}]
    return out


def served_models(run: Any) -> list[str]:
    """Every model that served a code-engine call of ``run`` (``extra.engine_runs``), in order of first use."""
    models = [e.get("active_model") for e in (run.extra or {}).get("engine_runs") or []
              if isinstance(e, dict) and e.get("active_model")]
    if not models and (run.engine or {}).get("active_model"):
        models = [run.engine["active_model"]]
    return list(dict.fromkeys(str(m) for m in models))


# ----------------------------------------------------------------------------- engine selection
def select_engine(ctx: StageContext) -> CodeEngine:
    """The preferred engine when available, else the first available one; blocked when none."""
    app = ctx.app
    pref = str((ctx.run.options or {}).get("engine_pref") or "auto")
    if pref in ENGINE_ATTRS:
        eng = getattr(app, ENGINE_ATTRS[pref], None)
        if eng is not None:
            try:
                avail = eng.availability()
            except Exception as exc:  # noqa: BLE001
                avail = None
                log.warning("engine %s availability failed: %s", pref, exc)
            if avail is not None and avail.available:
                return eng
            ctx.publish(ev.STAGE_PROGRESS, {"stage": "build", "pct": 0, "message":
                        f"preferred engine {pref} unavailable ({getattr(avail, 'reason', 'error')}); choosing automatically"})
    eng = app.engine()
    if eng is None:
        raise StageBlocked(NO_ENGINE_REASON, "build")
    return eng


def tree_fingerprint(root: Path) -> str:
    """Content hash of the work tree: tells whether the engine changed any file.

    The directories the gate never measures (``quality.SKIP_DIRS``: ``.git``, ``node_modules``, ...) are
    skipped at any depth. Every entry is ``lstat``-ed and only regular files are read; a symlink counts by
    its target, anything else (FIFO, socket, device) by its type and size, so it can never block the run.
    """
    h = hashlib.sha256()
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update((rel_dir / name).as_posix().encode("utf-8", "surrogateescape") + b"\0")
            try:
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode):
                    # O_NONBLOCK: a file swapped for a FIFO after the lstat still cannot block the read
                    fd = os.open(p, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as fh:
                        while chunk := fh.read(1 << 20):  # None (no data on a non-blocking fd) ends it too
                            h.update(chunk)
                elif stat.S_ISLNK(st.st_mode):
                    h.update(b"link:" + os.readlink(p).encode("utf-8", "surrogateescape"))
                else:
                    h.update(f"special:{stat.S_IFMT(st.st_mode)}:{st.st_size}".encode("ascii"))
            except OSError as exc:
                h.update(f"unreadable: {exc}".encode("utf-8", "replace"))
            h.update(b"\0")
    return h.hexdigest()


def _engines_setting(ctx: StageContext, name: str, default: Any) -> Any:
    """``settings.engines.<name>`` (``failover``, ``auto_resume_on_limit``); ``default`` for configs without it."""
    return getattr(getattr(ctx.cfg, "engines", None), name, default)


def utc_iso(epoch: float) -> str:
    """``2026-10-18T13:20:00Z`` for epoch seconds."""
    return datetime.fromtimestamp(float(epoch), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def failure_reason(kind: str | None, error: str | None, limit: int = 160) -> str:
    """Short reason for events and prompts: the kind of failure plus the start of the engine's own error."""
    label = FAILURE_LABELS.get(kind or "", "error")
    detail = " ".join(str(error or "").split())[:limit]
    return f"{label}: {detail}" if detail else label


def _engine_start_text(ctx: StageContext, eng: CodeEngine) -> tuple[str, str | None]:
    """``(what the progress line names, effort)``: Kyora's agent engine runs its own model and has no effort;
    the Hub engine names what the Hub really runs (never Claude's model and effort for the Hub's agent engine)."""
    cc = ctx.cfg.claude_code
    name = getattr(eng, "name", "")
    if name == "openai_agent":
        model = getattr(getattr(ctx.cfg, "agent_engine", None), "model", "") or "its configured model"
        return str(model), None
    if name == "hub":  # the same lookup as Runner._build's narration (the status is cached by the hub client)
        try:
            details = eng.availability().details or {}
        except Exception:  # noqa: BLE001 - a label only
            details = {}
        backend = str(details.get("remote_backend") or "")
        remote = details.get("remote_engine") if isinstance(details.get("remote_engine"), dict) else {}
        rdetails = remote.get("details") if isinstance(remote.get("details"), dict) else {}
        if backend == "openai_agent":
            return f"{rdetails.get('model') or 'its configured model'} on the Kyora Hub", None
        if backend in ("claude_code", "anthropic_api"):  # the job carries this device's model and effort
            return f"{cc.model}, effort {cc.effort} on the Kyora Hub", cc.effort
        return "the Kyora Hub engine", None
    return f"{cc.model}, effort {cc.effort}", cc.effort


def _quota_session(ctx: StageContext, kind: str, eng: CodeEngine) -> dict | None:
    """The last Claude Code attempt of this kind and iteration when it stopped at a usage limit, or was
    interrupted by Kyora stopping, with a session (also Claude Code on the Kyora Hub: backend ``hub:claude_code``).

    Its session is continued (``--resume``) when the run comes back after the limit, so the
    work it had planned and started is not redone from scratch.
    """
    name = getattr(eng, "name", "")
    if name not in ("claude_code", "hub"):
        return None
    for e in reversed(ctx.run.extra.get("engine_runs") or []):
        if isinstance(e, dict) and e.get("kind") == kind and e.get("iteration") == ctx.run.iteration \
                and e.get("engine") == name:
            if name == "hub" and not str(e.get("backend") or "").endswith("claude_code"):
                return None  # the Hub ran another engine: there is no Claude Code session to continue
            if name == "hub" and e.get("failure_kind") != "quota":
                # a cancelled (interrupted) Hub job's work never reached this tree, and the Hub replaces the
                # job's workspace with the next upload: its session would continue on edits it no longer has
                return None
            return e if e.get("failure_kind") in CONTINUED_FAILURES and e.get("session_id") else None
    return None


def _still_failing(ctx: StageContext, name: str, now: float) -> bool:
    """Whether engine ``name``'s last attempt of this run failed for an account reason that cannot have cleared yet:
    a refused login or key, a model or service failure, or a usage limit with no recorded reset or one still ahead."""
    for e in reversed(ctx.run.extra.get("engine_runs") or []):
        if isinstance(e, dict) and e.get("engine") == name:
            if e.get("failure_kind") not in FAILURE_KINDS:
                return False  # it worked, or a task failure / interruption: nothing says it fails again
            at = e.get("resets_at")
            retry = e.get("retry_at")
            if not _is_epoch(at) and _is_epoch(retry):  # a rate limit or an outage: usable again after the back-off
                return now < retry
            if e.get("failure_kind") != "quota" or not _is_epoch(at):
                return True
            failed_at = e.get("failed_at")
            if isinstance(failed_at, (int, float)) and at <= failed_at:
                # the reset had already passed when it refused (stale or skewed time): wait a back-off from the refusal
                return now < failed_at + PAST_RESET_RETRY_S[1]
            return at > now
    return False


def _is_epoch(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _choose_after_failover(ctx: StageContext) -> CodeEngine:
    """The engine for a BUILD/FIX after a failover: the preferred (or first) engine again once its failure can have
    cleared (a usage limit whose recorded reset passed), else the highest-ranked engine of ``settings.engines.order``
    that is not still failing (usually the fallback that ran last, whose session continues). ``ctx.failed_over`` stays set until the run is back on the preferred
    engine, so every later BUILD/FIX checks again."""
    preferred = select_engine(ctx)
    now = time.time()
    eng = preferred
    if _still_failing(ctx, preferred.name, now):
        usable = [c for c in engine_candidates(ctx.app, exclude=[preferred.name])
                  if not _still_failing(ctx, c.name, now)]
        # in settings order: the running fallback (and its session) stays unless an engine ranked above it cleared
        eng = usable[0] if usable else eng
    ctx.failed_over = eng.name != preferred.name
    return eng


def _engine_attempt(ctx: StageContext, eng: CodeEngine, kind: str, prompt: str,
                    resume: str | None) -> tuple[EngineResult, dict]:
    """Run ``eng`` once and record the attempt (``run.engine``, ``extra.engine_runs`` / ``engine_failures``)."""
    cc = ctx.cfg.claude_code
    plan = ctx.read_artifact("plan") or {}
    if ctx.app.judge is not None:
        try:  # resume: restore the counts saved with the run so the restart does not lose them
            ctx.app.judge.prime_stats(ctx.run_id, ctx.run.judge)
        except Exception as exc:  # noqa: BLE001
            log.warning("judge prime_stats failed: %s", exc)
    system_prompt = render_template("claude_system.md", threshold_pct=threshold_pct(ctx))
    job = EngineJob(
        run_id=ctx.run_id, workspace=ctx.workspace, prompt=prompt, system_prompt=system_prompt,
        max_turns=cc.max_turns, model=cc.model, fallback_models=list(cc.fallback_models), effort=cc.effort,
        resume_session_id=resume, kind=kind, project_id=ctx.project_id,
        judge_context={"workspace": str(ctx.workspace), "plan": {"title": plan.get("title"), "files": plan.get("files"),
                                                                  "constraints": plan.get("constraints")},
                       "files": plan.get("files") or [], "constraints": plan.get("constraints") or {},
                       "iteration": ctx.run.iteration, "run_dir": str(ctx.run_dir), "project_id": ctx.project_id,
                       "test_spec": [t["id"] for t in plan_tests(plan)], "plan_title": plan.get("title")},
    )
    what, effort = _engine_start_text(ctx, eng)
    ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 1, "message": f"{eng.name}: starting ({what})",
                                    "backend": eng.name, "effort": effort})
    before = tree_fingerprint(ctx.workspace)
    # kept with the run while the engine works: after a hard kill (or a stop the engine outlived) the next start
    # keeps and announces partial work only when this attempt really changed the tree (runner._keep_interrupted_work)
    ctx.run.extra["engine_attempt"] = {"kind": kind, "iteration": ctx.run.iteration, "engine": eng.name, "base": before}
    ctx.save_run()
    try:
        result = eng.run(job, BusSink(ctx.bus, ctx.run_id, ctx.project_id), ctx.cancel)
    finally:
        ctx.run.extra.pop("engine_attempt", None)
    changed = tree_fingerprint(ctx.workspace) != before
    cancelled = ctx.cancel.is_set() or result.error == "cancelled"
    failure = None if cancelled else classify_failure(result)
    if cancelled and ctx.shutdown is not None and ctx.shutdown.is_set():
        failure = "interrupted"  # Kyora stopped, not the user: the resumed run continues this session
    info = result.to_dict()
    info["backend_name"] = eng.name
    info["effort"] = effort  # recorded so the UI labels the run's real effort, not today's Settings
    info["tree_changed"] = changed
    info["failure_kind"] = failure
    if resume:
        info["resumed_session_id"] = resume
    if failure == "quota":
        info["resets_at"] = reset_time(result)  # a later BUILD/FIX skips this engine until then
        info["failed_at"] = time.time()
    if info.get("resets_at") is None and passes_by_itself(failure, result.error):
        info.setdefault("failed_at", time.time())
        done = len(ctx.run.extra.get("auto_resumes") or [])
        info["retry_at"] = info["failed_at"] + TRANSIENT_RETRY_S[min(done, len(TRANSIENT_RETRY_S) - 1)]
    ctx.last_engine = info
    ctx.run.engine = info
    entry = {"kind": kind, "iteration": ctx.run.iteration, "engine": eng.name, **{
        k: info.get(k) for k in ("ok", "backend", "active_model", "fallback_used", "num_turns", "duration_ms", "error",
                                 "tree_changed", "failure_kind", "session_id")}}
    if info.get("resets_at") is not None:
        entry["resets_at"] = info["resets_at"]
    if info.get("failed_at") is not None:
        entry["failed_at"] = info["failed_at"]
    if info.get("retry_at") is not None:
        entry["retry_at"] = info["retry_at"]
    ctx.run.extra.setdefault("engine_runs", []).append(entry)
    if not result.ok and not cancelled:
        ctx.run.extra.setdefault("engine_failures", []).append(
            {"kind": kind, "iteration": ctx.run.iteration, "backend": eng.name,
             "error": (result.error or "unknown error")[:500], "tree_changed": changed, "failure_kind": failure})
    if ctx.app.judge is not None:
        try:
            stats = {k: v for k, v in ctx.app.judge.stats(ctx.run_id).items()}
            # Hub engines judge on the remote phone, so the local counters stay zero; fold in the judge
            # delta the Hub reported in result.json (hub.py) so run.judge reflects the calls it judged.
            hub_judge = (result.raw or {}).get("hub", {}).get("judge") if isinstance(result.raw, dict) else None
            if isinstance(hub_judge, dict):
                for k, v in hub_judge.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        stats[k] = stats.get(k, 0) + int(v)
            ctx.run.judge = stats
        except Exception as exc:  # noqa: BLE001
            log.warning("judge stats failed: %s", exc)
    ctx.save_run()
    return result, info


def run_engine(ctx: StageContext, kind: str, prompt: str) -> EngineResult:
    """Run the code engine for one BUILD/FIX, failing over to the next engine on account failures; record it all.

    An attempt that fails for account reasons (``models.base.classify_failure``: usage limit,
    credits, login, unreachable service, retired model) hands the same job to the next available
    engine of ``settings.engines.order`` (``engines.failover``), told about any partial work it left
    (``engine.failover``). A task failure (max turns, timeout, refusal) never fails over. Every
    attempt is recorded (``extra.engine_runs``, ``extra.engine_failures``, ``extra.engine_failovers``)
    and ``ctx.engine`` is the engine that ran last; a failover is never a success by itself, TEST
    and GATE measure the result as always.

    When the last attempt is not ok the run blocks (resumable) unless the engines provably changed
    the work tree relative to the iteration base (the tree right before this call): a refused or
    rate-limited engine must never let TEST/GATE measure the unchanged parent game as if the
    requested change had been made. A usage limit with a known reset time and no engine left blocks
    with ``resume_at`` (``engines.auto_resume_on_limit``): the run manager resumes the run then, and
    Claude Code continues its limited (or interrupted) session (``--resume``); an engine that starts on
    kept partial work (``extra.partial_work_kept``) is told so. After a failover every later BUILD/FIX
    chooses its engine again until the run is back on the preferred engine, which it takes once its
    failure can have cleared (a usage limit whose recorded reset passed). When Kyora stops during the
    call, what its engines wrote is committed (``extra.partial_work_kept``) for the resumed run.
    """
    if ctx.failed_over:  # the fallback of the last call must not keep every later FIX of the run
        eng = _choose_after_failover(ctx)
    else:
        eng = ctx.engine or select_engine(ctx)
    cc = ctx.cfg.claude_code
    pref = str((ctx.run.options or {}).get("engine_pref") or "auto")
    base = tree_fingerprint(ctx.workspace)
    tried: list[str] = []
    failed: list[tuple[CodeEngine, EngineResult, dict]] = []  # account failures of this call, in order
    attempts: list[tuple[CodeEngine, EngineResult, dict]] = []  # every attempt of this call, in order
    task_prompt = prompt
    kept = ctx.run.extra.get("partial_work_kept")
    if not (isinstance(kept, dict) and kept.get("kind") == kind and kept.get("iteration") == ctx.run.iteration):
        kept = None
    if kept:  # a resumed run: the tree holds the partial work an engine left when it stopped (kept, committed)
        task_prompt = PARTIAL_WORK_PREFIX.format(name=kept.get("engine") or "name not recorded",
                                                 reason=kept.get("reason") or "stopped") + prompt
    fresh_session = False  # the limited session could not be resumed: start a new one
    while True:
        if tried and ctx.cancel.is_set():
            break  # cancelled while the next engine was chosen: do not start it
        ctx.engine = eng
        resume = None
        attempt_prompt = task_prompt
        limited = None if fresh_session else _quota_session(ctx, kind, eng)
        if limited:
            resume = str(limited["session_id"])
            changed_by = (kept.get("engine") or "an interrupted attempt") if kept else None
            note = ""
            if kept and changed_by != eng.name:  # the session knows its own work, not what another engine wrote since
                note = CHANGED_SINCE_TEXT.format(name=changed_by)
            elif kept and eng.name == "hub":
                # the Hub starts a new session when it no longer has this one (its jobs expire after a day) or its
                # engine cannot resume, and passes the prompt on: that session must still learn of the kept work
                note = KEPT_WORK_TEXT
            attempt_prompt = CONTINUE_PREFIX + note + prompt
            why = "was interrupted when Kyora stopped" if limited.get("failure_kind") == "interrupted" \
                else "stopped at a usage limit"
            ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 1, "message":
                        f"{eng.name}: continuing session {resume} that {why}"})
        elif not fresh_session and kind == "fix" and cc.resume_for_fixes and ctx.last_engine:
            if ctx.last_engine.get("session_id") and ctx.last_engine.get("backend_name") == eng.name:
                resume = ctx.last_engine["session_id"]
        result, info = _engine_attempt(ctx, eng, kind, attempt_prompt, resume)
        attempts.append((eng, result, info))
        tried.append(eng.name)
        if ctx.cancel.is_set() or result.error == "cancelled" or result.ok:
            break
        failure = info["failure_kind"]
        if limited and failure is None and not info["tree_changed"] and SESSION_GONE_RE.search(result.error or ""):
            # the session cut off by the limit is gone (expired): a fresh session continues the kept work
            fresh_session = True
            tried.pop()
            task_prompt = PARTIAL_WORK_PREFIX.format(
                name=eng.name, reason=failure_reason(limited.get("failure_kind"), limited.get("error"))) + prompt
            continue
        if failure is None:
            break  # a task failure: another engine would not do better
        failed.append((eng, result, info))
        if not _engines_setting(ctx, "failover", True):
            break
        nxt = (engine_candidates(ctx.app, exclude=tried) or [None])[0]
        if nxt is None:
            break
        reason = failure_reason(failure, result.error)
        pinned = pref == eng.name
        data = {"from": eng.name, "to": nxt.name, "reason": reason, "failure_kind": failure, "kind": kind,
                "iteration": ctx.run.iteration, "pinned": pinned, "tree_changed": info["tree_changed"]}
        ctx.run.extra.setdefault("engine_failovers", []).append({**data, "at": now_iso()})
        failures = ctx.run.extra.get("engine_failures") or []
        if failures and isinstance(failures[-1], dict):
            failures[-1]["failover_to"] = nxt.name
        ctx.save_run()
        ctx.publish(ev.ENGINE_FAILOVER, data)
        ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 1, "message":
                    f"{'the pinned engine ' if pinned else ''}{eng.name} stopped ({reason}); "
                    f"continuing the {kind} with {nxt.name}"})
        log.info("run %s: %s %s failed over from %s to %s (%s)", ctx.run_id, kind, ctx.run.iteration, eng.name,
                 nxt.name, reason)
        if info["tree_changed"]:
            task_prompt = PARTIAL_WORK_PREFIX.format(name=eng.name, reason=reason) + prompt
        eng = nxt
        ctx.failed_over = True
    changed = tree_fingerprint(ctx.workspace) != base
    if ctx.cancel.is_set() or result.error == "cancelled":
        if changed and ctx.shutdown is not None and ctx.shutdown.is_set():
            # Kyora is stopping: a resume restores the run's last commit, so keep what this call's engines wrote (and
            # only then: an engine told of partial work must find it in the tree)
            _keep_partial_work(ctx, kind, *_partial_writer(attempts, eng, result))
        raise RunCancelled()
    if not result.ok:
        _after_failed_attempts(ctx, kind, eng, result, failed, changed)
    return result


def _keep_partial_work(ctx: StageContext, kind: str, engine: str | None = None, reason: str | None = None) -> None:
    """Commit what an engine stopped by a usage limit (or by Kyora stopping) wrote: resuming restores the run's last
    commit, so the continued session (or the next engine) finds its partial work instead of the iteration's base.

    ``extra.partial_work_kept`` names the engine and why it stopped: the resumed BUILD/FIX tells its engine.
    """
    try:
        ctx.slides.commit_iteration(ctx.run, ctx.run.iteration, None)
        ctx.run.extra["partial_work_kept"] = {"kind": kind, "iteration": ctx.run.iteration, "at": now_iso(),
                                              "engine": engine, "reason": reason}
        ctx.save_run()
    except Exception as exc:  # noqa: BLE001 - the block itself must still happen
        log.warning("run %s: could not keep the partial work of the stopped engine: %s", ctx.run_id, exc)


def _partial_writer(failed: list[tuple[CodeEngine, EngineResult, dict]], eng: CodeEngine,
                    result: EngineResult) -> tuple[str, str]:
    """``(engine, reason)`` of the last failed attempt that changed the work tree (else the last attempt)."""
    for e, r, i in reversed(failed):
        if i["tree_changed"]:
            return e.name, failure_reason(i["failure_kind"], None if i["failure_kind"] == "interrupted" else r.error)
    return eng.name, failure_reason(failed[-1][2]["failure_kind"] if failed else None, result.error)


def _after_failed_attempts(ctx: StageContext, kind: str, eng: CodeEngine, result: EngineResult,
                           failed: list[tuple[CodeEngine, EngineResult, dict]], changed: bool) -> None:
    """The last attempt failed: block (resumable, maybe resuming by itself) or measure what the engines changed."""
    error = result.error or "unknown error"
    tried = ""
    if len(failed) > 1:
        tried = "; ".join(f"{e.name}: {failure_reason(i['failure_kind'], r.error, 120)}" for e, r, i in failed)
    # a usage limit is why no engine is left when the last attempt failed for account reasons and one of them
    # (usually Claude Code) reported a limit; the soonest known reset is when an engine is back
    limit_block = bool(failed) and failed[-1][1] is result and any(
        i["failure_kind"] == "quota" or _is_epoch(i.get("retry_at")) for _, _, i in failed)
    if limit_block and _engines_setting(ctx, "auto_resume_on_limit", True):
        now = time.time()
        # the soonest time an engine is back: a usage limit's known reset, or the back-off of a rate limit / outage
        resets = [(reset_time(r, now), e.name, r.error, "limit") for e, r, i in failed if i["failure_kind"] == "quota"]
        resets += [(i["retry_at"], e.name, r.error, "transient") for e, r, i in failed
                   if _is_epoch(i.get("retry_at")) and reset_time(r, now) is None]
        resets = sorted((t for t in resets if t[0] is not None), key=lambda t: t[0])
        if resets:
            at, limited, limited_error, why = resets[0]
            if why == "limit" and at < now:
                # a reset already in the past (refused again right after it, a stale value or clock skew): the limit
                # should be over, so resume instead of grading the half-finished work as the result; each further
                # automatic resume waits longer (PAST_RESET_RETRY_S, at most AUTO_RESUME_MAX times)
                done = len(ctx.run.extra.get("auto_resumes") or [])
                at = now + PAST_RESET_RETRY_S[min(done, len(PAST_RESET_RETRY_S) - 1)]
            if changed:
                _keep_partial_work(ctx, kind, *_partial_writer(failed, eng, result))
            others = (f"the engines tried: {tried}; no other code engine is available" if tried
                      else "failover is off (settings engines.failover)" if not _engines_setting(ctx, "failover", True)
                      else "no other code engine is available")
            detail = ' '.join(str(limited_error or '').split())[:300]
            if why == "transient":
                limit = (f"code engine ({limited}) is rate limited or its service is temporarily unavailable "
                         f"({detail}); {others}. It is tried again at {utc_iso(at)}")
            else:
                limit = f"code engine ({limited}) hit a usage limit ({detail}); {others}. The limit resets at {utc_iso(at)}"
            if bench_options(ctx):
                # Kyora Bench records the blocked task and moves on: resuming it by itself after the reset would spend
                # the new quota (and the device) on a task the bench already reported, next to its later tasks
                raise StageBlocked(limit + "; a Kyora Bench run does not resume by itself"
                                   + ("; the partial work is kept" if changed else ""), kind)
            reason = (f"{limit}: Kyora resumes this run by itself a minute "
                      f"later (settings engines.auto_resume_on_limit), or press Resume to retry sooner"
                      + ("; the partial work is kept and continued" if changed else ""))
            ctx.run.extra["auto_resume_at"] = at
            ctx.save_run()
            raise StageBlocked(reason, kind, resume_at=at)
    also = f" (engines tried: {tried})" if tried else ""
    if not (ctx.workspace / "index.html").is_file():
        if limit_block and changed:
            _keep_partial_work(ctx, kind, *_partial_writer(failed, eng, result))
        raise StageBlocked(f"code engine ({eng.name}) failed without producing index.html: {error}{also}", kind)
    if not changed:
        raise StageBlocked(f"code engine ({eng.name}) failed and changed no file in the work tree: "
                           f"{error[:400]}{also}", kind)
    ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 100, "message":
                f"engine ended with an error ({error[:200]}) after changing the work tree; "
                "measuring the files it produced"})


# ----------------------------------------------------------------------------- stages
def _require(obj: Any, what: str, stage: str):
    if obj is None:
        raise StageBlocked(f"{what} is not configured (check the NVIDIA API key and model in Settings)", stage)
    return obj


TEMPORARY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _model_call(stage: str, who: str, fn, *args, **kwargs):
    """Call a model wrapper; network/API failures block the run (resumable) instead of failing it.

    A failure that usually passes by itself (endpoint unreachable, 429, a 5xx, after the client's own
    retries and the role's fallback models) blocks with ``resume_at`` = now + the busy cooldown, so the
    run manager resumes the run by itself (at most ``AUTO_RESUME_MAX`` times); any other failure waits
    for the user's Resume.
    """
    from ..models.nim_client import BUSY_COOLDOWN_S, NIMCancelled, NIMError, NIMUnreachable
    try:
        return fn(*args, **kwargs)
    except NIMCancelled:
        raise RunCancelled() from None
    except NIMError as exc:
        status = f" (HTTP {exc.status})" if exc.status else ""
        temporary = isinstance(exc, NIMUnreachable) or exc.status in TEMPORARY_STATUSES
        resume_at = time.time() + BUSY_COOLDOWN_S if temporary else None
        then = f"; Kyora tries again by itself in {BUSY_COOLDOWN_S:g} s" if temporary else ""
        raise StageBlocked(f"{who} request failed{status}: {str(exc)[:400]}{then}", stage,
                           resume_at=resume_at) from None


def intake_stage(ctx: StageContext) -> dict:
    existing = ctx.read_artifact("intake")
    if isinstance(existing, dict):
        return existing
    app = ctx.app
    kimi = _require(app.kimi, "The Kimi role (planner)", "intake")
    opts = ctx.run.options or {}
    import_id = opts.get("import_zip_attachment")
    if import_id and not opts.get("imported"):
        from ..projects.zipper import import_game_zip
        path = app.store.attachment_path(ctx.project_id, import_id)
        files = import_game_zip(path, ctx.workspace)
        opts["imported"] = True
        ctx.slides.commit_iteration(ctx.run, 0, None)
        ctx.publish(ev.STAGE_PROGRESS, {"stage": "intake", "pct": 30, "message": f"imported {len(files)} files from the base ZIP"})
        ctx.save_run()
    ids = list(ctx.run.attachment_ids or [])
    if import_id and import_id not in ids:
        ids.append(import_id)
    views = app.store.attachment_views(ctx.project_id, ids) if ids else []
    ctx.data["attachment_views"] = views
    prompt = ctx.run.prompt or ("Continue from the imported game base." if import_id else "")
    meta: dict = {}
    try:
        intake = _model_call("intake", "The Kimi role", kimi.intake, prompt, views, ctx.run_id, ctx.project_id,
                             ctx.cancel, import_attachment_id=import_id, meta=meta)
    except ValueError as exc:
        raise StageBlocked(f"the Kimi role returned no valid intake: {exc}", "intake") from None
    _record_role_model(ctx, "intake", meta)
    ctx.write_artifact("intake", intake)
    return intake


def _record_role_model(ctx: StageContext, stage: str, meta: dict) -> None:
    """Keep which model answered a role's stage (``run.extra.role_models``): the deck names it, not the role's default."""
    if meta.get("model"):
        ctx.run.extra.setdefault("role_models", {})[stage] = {k: meta.get(k) for k in
                                                              ("model", "requested_model", "fallback", "images_sent")}


RULE_REVIEW_CHECKS = 20  # failing checks a rule-based fix decision lists


def rule_review(gate: dict, why: str) -> dict:
    """A ``fix`` decision built from the measured gate alone, for when the review model could not answer.

    Lists the failing checks (required ones first, at most :data:`RULE_REVIEW_CHECKS`) and the acceptance
    tests without a result. ``source: "rules"`` and ``model: None``: no model reviewed this iteration.
    """
    failing = [c for c in gate.get("checks") or [] if isinstance(c, dict) and c.get("status") == "fail"]
    failing.sort(key=lambda c: not c.get("required"))  # stable: the gate's own order within each group
    lines = []
    for c in failing[:RULE_REVIEW_CHECKS]:
        detail = " ".join(str(c.get("detail") or "").split())
        lines.append(f"- {c.get('id')}{' (required)' if c.get('required') else ''}: {c.get('name') or c.get('id')}"
                     + (f" - {detail[:300]}" if detail else ""))
    if len(failing) > RULE_REVIEW_CHECKS:
        lines.append(f"- ... and {len(failing) - RULE_REVIEW_CHECKS} more failing checks (gate.json)")
    missing = [str(t) for t in gate.get("missing_spec_tests") or []]
    if missing:
        lines.append("- acceptance tests that reported no result (write or repair them): " + ", ".join(missing[:20]))
    text = ("Make these failing checks of the measured gate pass, most important first:\n" + "\n".join(lines)
            if lines else "The gate did not pass: run the tests, find the failing checks in gate.json and fix them.")
    return {"decision": "fix", "source": "rules", "reasons": [why[:400]], "fix_instructions": text,
            "suspected_files": [], "coerced": False, "model": None, "images_sent": 0}


def unrunnable_node_tests(plan: dict, node: str | None) -> list[str]:
    """Plan acceptance ids of kind ``node`` when this device has no Node (they could never report a result)."""
    return [] if node else [t["id"] for t in plan_tests(plan) if t["kind"] == "node"]


def _block_unrunnable_node_tests(plan: dict, node: str | None, stage: str) -> None:
    ids = unrunnable_node_tests(plan, node)
    if ids:
        raise StageBlocked(f"the plan has Node acceptance tests ({', '.join(ids)}) but Node.js is not installed on this "
                           "device, so they can never run: install Node.js and press Resume, or start a new run "
                           "(the Kimi role then plans browser tests only)", stage)


def plan_stage(ctx: StageContext) -> dict:
    from .quality import find_node
    node = find_node()
    plan = ctx.read_artifact("plan")
    if not isinstance(plan, dict):
        kimi = _require(ctx.app.kimi, "The Kimi role (planner)", "plan")
        intake = ctx.read_artifact("intake") or {}
        meta: dict = {}
        try:
            plan = _model_call("plan", "The Kimi role", kimi.plan, intake, ctx.run_id, ctx.project_id, ctx.cancel,
                               node_available=bool(node), meta=meta)
        except ValueError as exc:
            raise StageBlocked(f"the Kimi role returned no valid plan: {exc}", "plan") from None
        _record_role_model(ctx, "plan", meta)
        plan = _apply_bench_contract(ctx, plan)
        ctx.write_artifact("plan", plan)
        ctx.save_run()
    _block_unrunnable_node_tests(plan, node, "plan")
    if ctx.cfg.quality.require_plan_approval and not (ctx.run.options or {}).get("plan_approved"):
        ctx.app.store.set_state(ctx.run, "awaiting_approval")
        ctx.publish(ev.RUN_AWAITING_APPROVAL, {"plan_title": plan.get("title"), "tests": len(plan_tests(plan)),
                                               "files": len(plan_files(plan))})
        ctx.publish(ev.RUN_STATE, {"state": "awaiting_approval", "iteration": ctx.run.iteration})
        while not ctx.approval.wait(0.5):
            ctx.check_cancel()
        ctx.check_cancel()
        ctx.run.options["plan_approved"] = True
        ctx.app.store.set_state(ctx.run, "plan")
    return plan


def _apply_bench_contract(ctx: StageContext, plan: dict) -> dict:
    """For Kyora Bench runs the bench's own test ids are the acceptance tests the gate measures."""
    bench = (ctx.run.options or {}).get("bench") or {}
    tests_file = bench.get("tests_file")
    if not tests_file:
        return plan
    try:
        text = Path(tests_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise StageBlocked(f"bench test file unreadable: {exc}", "plan") from None
    ids = list(dict.fromkeys(m.group(2) for m in TEST_ID_RE.finditer(text)))
    plan = dict(plan)
    plan["kimi_acceptance_tests"] = plan.get("acceptance_tests")
    plan["acceptance_tests"] = [{"id": i, "name": f"Kyora Bench {bench.get('task_id', '')} {i}", "kind": "browser",
                                 "steps": "defined by the Kyora Bench task", "required": False} for i in ids]
    return plan


def design_stage(ctx: StageContext) -> tuple[dict, dict]:
    design = ctx.read_artifact("design")
    ui = ctx.read_artifact("ui_spec")
    if isinstance(design, dict) and isinstance(ui, dict):
        return design, ui
    nemo = _require(ctx.app.nemotron, "The Nemotron role (design)", "design")
    intake = ctx.read_artifact("intake") or {}
    plan = ctx.read_artifact("plan") or {}
    views = ctx.data.get("attachment_views")
    if views is None:
        ids = list(ctx.run.attachment_ids or [])
        views = ctx.app.store.attachment_views(ctx.project_id, ids) if ids else []
    if not isinstance(design, dict):
        try:
            design = _model_call("design", "The Nemotron role", nemo.design_doc, intake, plan, views, ctx.run_id,
                                 ctx.project_id, ctx.cancel)
        except ValueError as exc:
            raise StageBlocked(f"the Nemotron role returned no usable design: {exc}", "design") from None
        _record_nemotron(ctx, nemo, "design")
        ctx.write_artifact("design", design)
        ctx.save_run()
    ctx.check_cancel()
    if not isinstance(ui, dict):
        try:
            ui = _model_call("design", "The Nemotron role", nemo.ui_spec, intake, plan, ctx.run_id, ctx.project_id, ctx.cancel)
        except ValueError as exc:
            raise StageBlocked(f"the Nemotron role returned no usable UI spec: {exc}", "design") from None
        _record_nemotron(ctx, nemo, "ui_spec")
        ctx.write_artifact("ui_spec", ui)
        ctx.save_run()
    return design, ui


def _record_nemotron(ctx: StageContext, nemo: Any, purpose: str) -> None:
    answered = getattr(nemo, "answered_by", None)
    chain = getattr(nemo, "chain", None)
    if callable(answered):
        model = answered(purpose)
        _record_role_model(ctx, purpose, {"model": model, "requested_model": getattr(chain, "requested", None),
                                          "fallback": bool(chain) and model != chain.requested})


def write_build_files(ctx: StageContext) -> None:
    """Write CLAUDE.md, KYORA_PLAN.md, KYORA_TESTSPEC.md, DESIGN.md and the manifest skeleton into the work tree."""
    plan = ctx.read_artifact("plan") or {}
    design = ctx.read_artifact("design") or {}
    ui = ctx.read_artifact("ui_spec") or {}
    engine = str(plan.get("engine") or "vanilla")
    files = [f"`{f.get('path')}` - {f.get('purpose', '')}" if isinstance(f, dict) else f"`{f}`"
             for f in plan.get("files") or []]
    api = (plan.get("contract") or {}).get("api") or {}
    contract = [f"`{k}` - {v}" for k, v in api.items()] if isinstance(api, dict) else []
    cons = plan.get("constraints") or {}
    constraints = [f"`{k}`: {json.dumps(v)}" for k, v in cons.items()] if isinstance(cons, dict) else []
    tasks = []
    for t in plan.get("tasks") or []:
        if isinstance(t, dict):
            dep = ", ".join(t.get("depends_on") or []) or "none"
            tasks.append(f"**{t.get('id')}** {t.get('title', '')} (depends on: {dep}; files: "
                         f"{', '.join(t.get('files') or [])}) - done when: {t.get('done_when', '')}")
    tests = []
    for t in plan_tests(plan):
        tests.append(f"### `{t['id']}` - {t['name']}\n- kind: {t['kind']}{' - REQUIRED' if t['required'] else ''}\n"
                     f"- steps: {t['steps'] or '(see name)'}\n")
    title = str(plan.get("title") or "Untitled game")
    ws = ctx.workspace
    atomic_write_text(ws / "CLAUDE.md", render_template("CLAUDE.md.tmpl", title=title, engine=engine,
                                                         files_list=_bullets(files), contract_list=_bullets(contract),
                                                         constraints_list=_bullets(constraints)))
    atomic_write_text(ws / "KYORA_PLAN.md", render_template("KYORA_PLAN.md.tmpl", title=title, engine=engine,
                                                             preview=plan.get("preview", True),
                                                             summary=plan.get("summary") or "", files_list=_bullets(files),
                                                             tasks_list=_bullets(tasks), constraints_list=_bullets(constraints)))
    atomic_write_text(ws / "KYORA_TESTSPEC.md", render_template("KYORA_TESTSPEC.md.tmpl", threshold_pct=threshold_pct(ctx),
                                                                 tests_list="\n".join(tests) or "(no tests planned)"))
    design_md = ["# Design (NVIDIA Nemotron-3-Ultra)", "", design.get("gdd_markdown") or "", "",
                 "## Narrative", "", design.get("narrative") or "", "", "## Dialogue", ""]
    design_md += [f"- **{d.get('speaker')}**: {d.get('line')}" for d in design.get("dialogue") or [] if isinstance(d, dict)]
    design_md += ["", "## UI specification (tokens, screens, copy)", "", "```json",
                  json.dumps(ui, ensure_ascii=False, indent=2), "```", ""]
    atomic_write_text(ws / "DESIGN.md", "\n".join(design_md))
    manifest = ws / ".kyora" / "manifest.json"
    if not manifest.exists():
        atomic_write_json(manifest, {"entry": "index.html", "engine": engine, "scripts": [], "modules": [],
                                     "tests": ["tests/browser-tests.js"], "version": 1})


def build_stage(ctx: StageContext) -> EngineResult:
    write_build_files(ctx)
    plan = ctx.read_artifact("plan") or {}
    tests = plan_tests(plan)
    browser_ids = ", ".join(t["id"] for t in tests if t["kind"] == "browser") or "none"
    node_ids = [t["id"] for t in tests if t["kind"] == "node"]
    node_hint = (f"Also write tests/logic-tests.mjs (node:test) with one test per node acceptance id "
                 f"({', '.join(node_ids)}), each test name starting with its id.") if node_ids else ""
    bench = (ctx.run.options or {}).get("bench") or {}
    user_prompt = ctx.run.prompt or "(no prompt: continue from the imported game base)"
    if bench.get("harness_notes"):
        user_prompt += "\n\n" + str(bench["harness_notes"])
    prompt = render_template("build_prompt.md", title=plan.get("title") or "the game", browser_ids=browser_ids,
                             node_hint=node_hint, user_prompt=user_prompt)
    return run_engine(ctx, "build", prompt)


def fix_stage(ctx: StageContext, n: int, prev_gate: dict, review: dict) -> EngineResult:
    plan = ctx.read_artifact("plan") or {}
    failing = []
    for c in prev_gate.get("checks") or []:
        if c.get("status") == "fail":
            failing.append(f"`{c.get('id')}` ({c.get('group')}){' REQUIRED' if c.get('required') else ''}: "
                           f"{(c.get('detail') or c.get('name') or '')[:300]}")
    prompt = render_template(
        "fix_prompt.md", iteration=n - 1, title=plan.get("title") or "the game",
        pass_pct=f"{(prev_gate.get('pass_rate') or 0) * 100:.1f}", passed=prev_gate.get("passed", 0),
        total=prev_gate.get("total", 0), threshold_pct=threshold_pct(ctx), failing_list=_bullets(failing[:60]),
        fix_instructions=review.get("fix_instructions") or "(no instructions)",
        reviewer=(f"The review ({review['model']}) looked at the result and" if review.get("model") else
                  "No review model could answer, so Kyora's rules list the failing checks; the gate"),
        suspected_files=", ".join(review.get("suspected_files") or []) or "(none named)")
    write_build_files(ctx)  # restores the contract files if the engine changed them
    return run_engine(ctx, "fix", prompt)


# ----------------------------------------------------------------------------- tests
def _inject_bench_tests(ctx: StageContext) -> None:
    bench = (ctx.run.options or {}).get("bench") or {}
    tests_file = bench.get("tests_file")
    if not tests_file:
        return
    target = ctx.workspace / "tests" / "browser-tests.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            same = target.read_bytes() == Path(tests_file).read_bytes()
        except OSError:
            same = False
        if not same:
            shutil.copyfile(target, ctx.workspace / "tests" / "generated-browser-tests.js")
    shutil.copyfile(tests_file, target)


def _headless_driver(ctx: StageContext, url: str, node: str) -> None:
    """Drive the harness with Node + Playwright when available (optional; phones and PCs can attach instead)."""
    if os.environ.get("KYORA_HEADLESS", "1") == "0":
        return
    try:
        src = resources.files("kyora").joinpath("runners").joinpath("headless_browser.mjs").read_bytes()
        dst = ctx.app.home / "bin" / "headless_browser.mjs"
        if not dst.exists() or dst.read_bytes() != src:
            atomic_write_bytes(dst, src)
    except OSError as exc:
        log.warning("headless driver unavailable: %s", exc)
        return
    timeout_ms = max(30_000, int(ctx.cfg.quality.browser_wait_s * 1000))

    def _run() -> None:
        try:
            proc = subprocess.run([node, str(dst), url, "--timeout", str(timeout_ms)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout_ms / 1000 + 30, cwd=str(ctx.app.home))
            if proc.returncode != 0:
                log.info("headless driver did not deliver a result (%s): %s", proc.returncode,
                         (proc.stderr or proc.stdout or "")[-300:])
        except Exception as exc:  # noqa: BLE001
            log.info("headless driver failed: %s", exc)

    threading.Thread(target=_run, name=f"headless-{ctx.run_id}", daemon=True).start()


def test_stage(ctx: StageContext, n: int) -> tuple[list, list, list]:
    from .quality import NodeRunner, StaticChecker, find_node
    _inject_bench_tests(ctx)
    plan = ctx.read_artifact("plan") or {}
    node = find_node()
    _block_unrunnable_node_tests(plan, node, "test")  # e.g. Node removed since PLAN: never loop fixes on them
    static = StaticChecker(ctx.workspace, plan, node).run()
    if bench_options(ctx):  # the bench injected the test file: 'its ids are in its file' measures nothing
        static = [c for c in static if not c.id.startswith("tests_present:")]
    atomic_write_json(ctx.iter_dir(n) / "static.json", [c.to_dict() for c in static])
    ctx.publish(ev.TEST_STATIC, {"iteration": n, "passed": sum(c.status == "pass" for c in static),
                                 "failed": sum(c.status == "fail" for c in static),
                                 "deferred": sum(c.status == "deferred" for c in static), "total": len(static)})
    ctx.check_cancel()
    node_checks = NodeRunner(node, ctx.workspace, cancel=ctx.cancel).logic_tests()
    atomic_write_json(ctx.iter_dir(n) / "node.json", [c.to_dict() for c in node_checks])
    ctx.publish(ev.TEST_NODE, {"iteration": n, "available": bool(node), "checks": len(node_checks),
                               "failed": sum(c.status == "fail" for c in node_checks)})
    collector = ctx.app.browser_results
    if collector is None:
        raise StageBlocked("browser results collector unavailable", "test")
    collector.expect(ctx.run_id, n, ctx.run_dir)
    runner_url = f"/runner/{ctx.project_id}/run-{ctx.run_id}?run={ctx.run_id}&iter={n}&auto=1&post=1"
    ctx.run.extra["runner_url"] = runner_url
    ctx.save_run()
    ctx.publish(ev.TEST_AWAIT_BROWSER, {"runner_url": runner_url, "iteration": n})
    show_view(ctx, f"Testing iteration {n}", buttons=[("kyora:run_tests", "Run tests")])
    if node and ctx.cfg.quality.auto_run_browser_tests and getattr(ctx.app, "preview_url", ""):
        _headless_driver(ctx, ctx.app.preview_url.rstrip("/") + runner_url, node)  # the harness's own origin
    payload = collector.wait(ctx.run_id, n, float(ctx.cfg.quality.browser_wait_s), ctx.cancel)
    ctx.check_cancel()
    if payload is None:
        raise StageBlocked(NO_BROWSER_REASON, "test")
    browser = collector.to_checks(payload)
    ctx.publish(ev.TEST_BROWSER_RESULT, {"iteration": n, "summary": payload.get("summary")})
    return static, node_checks, browser


def gate_stage(ctx: StageContext, n: int, static: list, node: list, browser: list) -> dict:
    from .quality import QualityGate, merge_checks
    plan = ctx.read_artifact("plan") or {}
    # a bench run is scored on the bench's own test ids only (engine-written tests cannot dilute it)
    scored = [t["id"] for t in plan_tests(plan)] if bench_options(ctx) else None
    result = QualityGate(gate_threshold(ctx), plan, scored_ids=scored).evaluate(merge_checks(static, node, browser))
    full = result.to_dict()
    full["iteration"] = n
    atomic_write_json(ctx.iter_dir(n) / "gate.json", full)
    summary = {k: full.get(k) for k in ("threshold", "passed", "total", "pass_rate", "ok", "required_failures",
                                        "missing_spec_tests", "scope")}
    summary["iteration"] = n
    ctx.run.gate["final"] = summary
    ctx.run.gate["threshold"] = summary["threshold"]
    ctx.save_run()
    ctx.publish(ev.GATE_RESULT, summary)
    return full


def load_gate(ctx: StageContext, n: int) -> dict | None:
    return read_json(ctx.run_dir / f"iter-{n}" / "gate.json", default=None)


def review_stage(ctx: StageContext, n: int, gate: dict) -> dict:
    existing = read_json(ctx.run_dir / f"review-{n}.json", default=None)
    if isinstance(existing, dict):
        return existing
    kimi = _require(ctx.app.kimi, "The Kimi role (reviewer)", "review")
    shots: list[bytes] = []
    collector = ctx.app.browser_results
    if collector is not None:
        for p in collector.screenshots(ctx.run_id, n)[:4]:
            try:
                shots.append(Path(p).read_bytes())
            except OSError:
                pass
    plan = ctx.read_artifact("plan") or {}
    summary = {"title": plan.get("title"), "iteration": n, "engine": {k: (ctx.run.engine or {}).get(k) for k in
                                                                     ("backend", "active_model", "num_turns", "error")},
               "files": plan_files(plan), "acceptance_tests": [t["id"] for t in plan_tests(plan)],
               "previous_reviews": [read_json(ctx.run_dir / f"review-{i}.json", default={}).get("decision")
                                    for i in range(n)]}
    compact_gate = {k: gate.get(k) for k in ("threshold", "passed", "total", "pass_rate", "ok", "required_failures",
                                             "missing_spec_tests")}
    compact_gate["failing_checks"] = [{k: c.get(k) for k in ("id", "group", "name", "required", "detail")}
                                      for c in gate.get("checks") or [] if c.get("status") == "fail"][:80]
    # a review model that cannot answer does not stop a run whose gate was measured: the failing checks become
    # the fix instructions (source "rules"); the runner still stops at quality.max_fix_iterations
    try:
        review = _model_call("review", "The Kimi role", kimi.review, summary, compact_gate, shots, ctx.run_id,
                             ctx.project_id, ctx.cancel)
    except ValueError as exc:
        review = rule_review(gate, f"the review model returned no valid review ({str(exc)[:200]}); "
                                   "the failing checks of the measured gate are the fix instructions")
    except StageBlocked as exc:
        review = rule_review(gate, f"the review model was unavailable ({exc.reason[:200]}); "
                                   "the failing checks of the measured gate are the fix instructions")
    review.setdefault("source", "model")
    if review.get("model"):
        _record_role_model(ctx, f"review-{n}", {"model": review.get("model"), "requested_model": getattr(
            getattr(kimi, "chain", None), "requested", None), "images_sent": review.get("images_sent", 0)})
    atomic_write_json(ctx.run_dir / f"review-{n}.json", review)
    ctx.publish(ev.REVIEW_DECISION, {"iteration": n, "decision": review.get("decision"),
                                     "reasons": (review.get("reasons") or [])[:5], "source": review.get("source"),
                                     "model": review.get("model"), "images_sent": review.get("images_sent"),
                                     "screenshots": review.get("screenshots")})
    return review


# ----------------------------------------------------------------------------- finalize
def finalize_stage(ctx: StageContext, gate: dict, ok: bool) -> dict:
    from ..projects.deck import DeckBuilder
    plan = ctx.read_artifact("plan") or {}
    if ctx.run.kind == "modify":
        words = " ".join((ctx.run.prompt or "Modification").split())
        title = words if len(words) <= 80 else words[:77].rsplit(" ", 1)[0] + "..."
    else:
        title = str(plan.get("title") or "Generated game")
    served = served_engines(ctx.run)
    if not isinstance(ctx.run.engine, dict):
        ctx.run.engine = {}
    ctx.run.engine["served"] = served  # every backend/model that served the run; active_model stays the last call's
    try:
        DeckBuilder(ctx.project_id, ctx.app.store).write_into_workspace(ctx.workspace, run=ctx.run)
    except Exception as exc:  # noqa: BLE001 - the deck must not lose the slide
        log.warning("deck build failed: %s", exc)
    shots = []
    collector = ctx.app.browser_results
    if collector is not None:
        shots = [str(p) for p in collector.screenshots(ctx.run_id, int(gate.get("iteration", ctx.run.iteration)))]
    gate_summary = {k: gate.get(k) for k in ("threshold", "passed", "total", "pass_rate", "ok")}
    engine = {k: (ctx.run.engine or {}).get(k) for k in ("backend", "active_model", "fallback_used", "requested_model")}
    engine["served"] = served
    models = served_models(ctx.run)
    if len(models) > 1:  # the slide names every model that served this run, not only the last call's
        engine["active_model"] = " + ".join(models)
        engine["fallback_used"] = any(e.get("fallback_used") for e in ctx.run.extra.get("engine_runs") or []
                                      if isinstance(e, dict))
    # a failed engine call is never hidden: the slide keeps the measured gate (and its state) but says so
    errors = [{k: f.get(k) for k in ("kind", "iteration", "backend", "error", "tree_changed")}
              for f in ctx.run.extra.get("engine_failures") or [] if isinstance(f, dict)]
    engine["ok"] = not errors
    if errors:
        engine["engine_errors"] = errors
    slide = ctx.slides.commit_slide(ctx.run, title, gate_summary, engine, shots)
    ctx.run.result_slide = slide["number"]
    ctx.save_run()
    ctx.publish(ev.SLIDE_CREATED, {"number": slide["number"], "tag": slide["tag"], "sha": slide["sha"],
                                   "title": slide["title"], "gate_ok": bool(ok)})
    ctx.publish(ev.DECK_UPDATED, {"url": f"/projects/{ctx.project_id}/deck"})
    return slide


# ----------------------------------------------------------------------------- presenter (Tokkio)
def show_view(ctx: StageContext, title: str, *, buttons: list[tuple[str, str]] | None = None,
              screenshot_b64: str | None = None) -> None:
    """Publish what the Tokkio presenter shows (also rendered as cards in the Kyora UI). No model call."""
    tk = getattr(ctx.app, "tokkio", None)
    if tk is None:
        return
    stages = [{"name": name, "status": (ctx.run.stages.get(name) or {}).get("status", "idle")}
              for name in ("intake", "plan", "design", "build", "test", "gate", "review", "fix", "finalize")]
    preview_base = str(getattr(ctx.app, "preview_url", "") or "").rstrip("/")  # game code: the preview origin
    view = {"title": title, "preview_url": f"{preview_base}/preview/{ctx.project_id}/run-{ctx.run_id}/index.html", "stages": stages,
            "buttons": [{"id": i, "text": t} for i, t in (buttons or [])]}
    if screenshot_b64:
        view["screenshot_b64"] = screenshot_b64
    try:
        tk.show_view(ctx.run_id, ctx.project_id, view)
    except Exception as exc:  # noqa: BLE001
        log.warning("tokkio view failed: %s", exc)


def narrate(ctx: StageContext, status_text: str) -> None:
    """Nemotron narration for a deployed Tokkio avatar (background, best effort, never blocks the run)."""
    tk = getattr(ctx.app, "tokkio", None)
    nemo = getattr(ctx.app, "nemotron", None)
    if tk is None or nemo is None:
        return
    try:
        if not tk.configured():
            return
    except Exception:  # noqa: BLE001
        return

    def _go() -> None:
        try:
            text = nemo.narration(status_text, ctx.run_id, ctx.project_id)
            if text:
                tk.narrate(ctx.run_id, ctx.project_id, text)
        except Exception as exc:  # noqa: BLE001
            log.info("narration failed: %s", exc)

    t = threading.Thread(target=_go, name=f"narrate-{ctx.run_id}", daemon=True)
    t.start()


__all__ = [
    "StageBlocked", "RunCancelled", "StageContext", "render_template", "select_engine", "run_engine",
    "intake_stage", "plan_stage", "design_stage", "write_build_files", "build_stage", "fix_stage", "test_stage",
    "gate_stage", "load_gate", "review_stage", "finalize_stage", "show_view", "narrate", "plan_tests", "plan_files",
    "NO_ENGINE_REASON", "NO_BROWSER_REASON", "gate_threshold", "rule_review", "RULE_REVIEW_CHECKS",
    "passes_by_itself", "TRANSIENT_RETRY_S", "served_models", "served_engines", "tree_fingerprint",
    "CONTINUE_PREFIX", "PARTIAL_WORK_PREFIX", "CHANGED_SINCE_TEXT", "KEPT_WORK_TEXT", "failure_reason", "utc_iso",
]
