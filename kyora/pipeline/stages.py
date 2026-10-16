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
import string
import subprocess
import threading
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..models.base import BusSink, CodeEngine, EngineJob, EngineResult
from ..util.fs import atomic_write_bytes, atomic_write_json, atomic_write_text, read_json
from . import events as ev

log = logging.getLogger("kyora.pipeline")

NO_ENGINE_REASON = ("no code engine: install Claude Code and run 'claude auth login', link a Kyora Hub "
                    "(Settings > Hub), or add an Anthropic API key")
NO_BROWSER_REASON = ("no browser attached: open the run in Kyora on any device (Pipeline > Run tests on this device), "
                     "then press Resume")
TEST_ID_RE = re.compile(r"""KyoraTest\.test\(\s*(['"`])([^'"`]+)\1""")
ENGINE_ATTRS = {"claude_code": "claude_engine", "hub": "hub_client", "anthropic_api": "anthropic_engine"}


# ----------------------------------------------------------------------------- control flow
class StageBlocked(Exception):
    """The run cannot continue until something outside Kyora changes (resumable)."""

    def __init__(self, reason: str, stage: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage


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


def threshold_pct(ctx: StageContext) -> str:
    return f"{ctx.cfg.quality.min_pass_rate * 100:g}"


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
    """Content hash of the work tree (``.git`` excluded): tells whether the engine changed any file."""
    h = hashlib.sha256()
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not (rel_dir == Path(".") and d == ".git"))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            h.update((rel_dir / name).as_posix().encode("utf-8", "surrogateescape") + b"\0")
            try:
                h.update(os.readlink(p).encode("utf-8", "surrogateescape") if p.is_symlink() else p.read_bytes())
            except OSError as exc:
                h.update(f"unreadable: {exc}".encode("utf-8", "replace"))
            h.update(b"\0")
    return h.hexdigest()


def run_engine(ctx: StageContext, kind: str, prompt: str) -> EngineResult:
    """Run the code engine once (build or fix) and record the result on the run.

    An engine result that is not ok blocks the run (resumable) unless the engine
    provably changed the work tree relative to the iteration base (the tree right
    before the call): a refused or rate-limited engine must never let TEST/GATE
    measure the unchanged parent game as if the requested change had been made.
    """
    eng = ctx.engine or select_engine(ctx)
    ctx.engine = eng
    cc = ctx.cfg.claude_code
    plan = ctx.read_artifact("plan") or {}
    resume = None
    if kind == "fix" and cc.resume_for_fixes and ctx.last_engine:
        if ctx.last_engine.get("session_id") and ctx.last_engine.get("backend_name") == eng.name:
            resume = ctx.last_engine["session_id"]
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
    ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 1, "message": f"{eng.name}: starting ({cc.model}, effort {cc.effort})"})
    base = tree_fingerprint(ctx.workspace)
    result = eng.run(job, BusSink(ctx.bus, ctx.run_id, ctx.project_id), ctx.cancel)
    changed = tree_fingerprint(ctx.workspace) != base
    info = result.to_dict()
    info["backend_name"] = eng.name
    info["tree_changed"] = changed
    ctx.last_engine = info
    ctx.run.engine = info
    ctx.run.extra.setdefault("engine_runs", []).append({"kind": kind, "iteration": ctx.run.iteration, **{
        k: info.get(k) for k in ("ok", "backend", "active_model", "fallback_used", "num_turns", "duration_ms", "error",
                                 "tree_changed")}})
    if not result.ok and not (ctx.cancel.is_set() or result.error == "cancelled"):
        ctx.run.extra.setdefault("engine_failures", []).append(
            {"kind": kind, "iteration": ctx.run.iteration, "backend": eng.name,
             "error": (result.error or "unknown error")[:500], "tree_changed": changed})
    if ctx.app.judge is not None:
        try:
            ctx.run.judge = {k: v for k, v in ctx.app.judge.stats(ctx.run_id).items()}
        except Exception as exc:  # noqa: BLE001
            log.warning("judge stats failed: %s", exc)
    ctx.save_run()
    if ctx.cancel.is_set() or result.error == "cancelled":
        raise RunCancelled()
    if not result.ok:
        if not (ctx.workspace / "index.html").is_file():
            raise StageBlocked(f"code engine ({eng.name}) failed without producing index.html: {result.error}", kind)
        if not changed:
            raise StageBlocked(f"code engine ({eng.name}) failed and changed no file in the work tree: "
                               f"{(result.error or 'unknown error')[:400]}", kind)
        ctx.publish(ev.STAGE_PROGRESS, {"stage": kind, "pct": 100, "message":
                    f"engine ended with an error ({(result.error or '')[:200]}) after changing the work tree; "
                    "measuring the files it produced"})
    return result


# ----------------------------------------------------------------------------- stages
def _require(obj: Any, what: str, stage: str):
    if obj is None:
        raise StageBlocked(f"{what} is not configured (check the NVIDIA API key and model in Settings)", stage)
    return obj


def _model_call(stage: str, who: str, fn, *args, **kwargs):
    """Call a model wrapper; network/API failures block the run (resumable) instead of failing it."""
    from ..models.nim_client import NIMCancelled, NIMError
    try:
        return fn(*args, **kwargs)
    except NIMCancelled:
        raise RunCancelled() from None
    except NIMError as exc:
        status = f" (HTTP {exc.status})" if exc.status else ""
        raise StageBlocked(f"{who} request failed{status}: {str(exc)[:400]}", stage) from None


def intake_stage(ctx: StageContext) -> dict:
    existing = ctx.read_artifact("intake")
    if isinstance(existing, dict):
        return existing
    app = ctx.app
    kimi = _require(app.kimi, "Kimi K3", "intake")
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
    intake = _model_call("intake", "Kimi K3", kimi.intake, prompt, views, ctx.run_id, ctx.project_id, ctx.cancel,
                         import_attachment_id=import_id)
    ctx.write_artifact("intake", intake)
    return intake


def plan_stage(ctx: StageContext) -> dict:
    plan = ctx.read_artifact("plan")
    if not isinstance(plan, dict):
        kimi = _require(ctx.app.kimi, "Kimi K3", "plan")
        intake = ctx.read_artifact("intake") or {}
        try:
            plan = _model_call("plan", "Kimi K3", kimi.plan, intake, ctx.run_id, ctx.project_id, ctx.cancel)
        except ValueError as exc:
            raise StageBlocked(f"Kimi K3 returned no valid plan: {exc}", "plan") from None
        plan = _apply_bench_contract(ctx, plan)
        ctx.write_artifact("plan", plan)
        ctx.save_run()
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
    nemo = _require(ctx.app.nemotron, "NVIDIA Nemotron-3-Ultra", "design")
    intake = ctx.read_artifact("intake") or {}
    plan = ctx.read_artifact("plan") or {}
    views = ctx.data.get("attachment_views")
    if views is None:
        ids = list(ctx.run.attachment_ids or [])
        views = ctx.app.store.attachment_views(ctx.project_id, ids) if ids else []
    if not isinstance(design, dict):
        try:
            design = _model_call("design", "Nemotron-3-Ultra", nemo.design_doc, intake, plan, views, ctx.run_id,
                                 ctx.project_id, ctx.cancel)
        except ValueError as exc:
            raise StageBlocked(f"Nemotron-3-Ultra returned no usable design: {exc}", "design") from None
        ctx.write_artifact("design", design)
        ctx.save_run()
    ctx.check_cancel()
    if not isinstance(ui, dict):
        try:
            ui = _model_call("design", "Nemotron-3-Ultra", nemo.ui_spec, intake, plan, ctx.run_id, ctx.project_id, ctx.cancel)
        except ValueError as exc:
            raise StageBlocked(f"Nemotron-3-Ultra returned no usable UI spec: {exc}", "design") from None
        ctx.write_artifact("ui_spec", ui)
        ctx.save_run()
    return design, ui


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
    static = StaticChecker(ctx.workspace, plan, node).run()
    atomic_write_json(ctx.iter_dir(n) / "static.json", [c.to_dict() for c in static])
    ctx.publish(ev.TEST_STATIC, {"iteration": n, "passed": sum(c.status == "pass" for c in static),
                                 "failed": sum(c.status == "fail" for c in static),
                                 "deferred": sum(c.status == "deferred" for c in static), "total": len(static)})
    ctx.check_cancel()
    node_checks = NodeRunner(node, ctx.workspace).logic_tests()
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
    if node and ctx.cfg.quality.auto_run_browser_tests and getattr(ctx.app, "server_url", ""):
        _headless_driver(ctx, ctx.app.server_url.rstrip("/") + runner_url, node)
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
    result = QualityGate(ctx.cfg.quality, plan).evaluate(merge_checks(static, node, browser))
    full = result.to_dict()
    full["iteration"] = n
    atomic_write_json(ctx.iter_dir(n) / "gate.json", full)
    summary = {k: full.get(k) for k in ("threshold", "passed", "total", "pass_rate", "ok", "required_failures",
                                        "missing_spec_tests")}
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
    kimi = _require(ctx.app.kimi, "Kimi K3", "review")
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
    review = _model_call("review", "Kimi K3", kimi.review, summary, compact_gate, shots, ctx.run_id, ctx.project_id,
                         ctx.cancel)
    atomic_write_json(ctx.run_dir / f"review-{n}.json", review)
    ctx.publish(ev.REVIEW_DECISION, {"iteration": n, "decision": review.get("decision"),
                                     "reasons": (review.get("reasons") or [])[:5]})
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
    try:
        DeckBuilder(ctx.project_id, ctx.app.store).write_into_workspace(ctx.workspace)
    except Exception as exc:  # noqa: BLE001 - the deck must not lose the slide
        log.warning("deck build failed: %s", exc)
    shots = []
    collector = ctx.app.browser_results
    if collector is not None:
        shots = [str(p) for p in collector.screenshots(ctx.run_id, int(gate.get("iteration", ctx.run.iteration)))]
    gate_summary = {k: gate.get(k) for k in ("threshold", "passed", "total", "pass_rate", "ok")}
    engine = {k: (ctx.run.engine or {}).get(k) for k in ("backend", "active_model", "fallback_used", "requested_model")}
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
    view = {"title": title, "preview_url": f"/preview/{ctx.project_id}/run-{ctx.run_id}/index.html", "stages": stages,
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
    "NO_ENGINE_REASON", "NO_BROWSER_REASON",
]
