"""Standalone HTML presentation deck for a project (print-to-PDF friendly).

The deck is one self-contained HTML file: inline CSS and JS, images as data
URLs, and a Content-Security-Policy that forbids any network request. Every
number shown comes from files this project's runs wrote (``gate.json``,
``run.json``, slide records); missing data is labelled as missing, never
invented. All text is HTML-escaped.

Sections (element ids): ``cover, concept, design, architecture, screenshots,
tests, changelog, honesty``.
"""
from __future__ import annotations

import base64
import html
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from ..util.fs import atomic_write_text, read_json, safe_join

if TYPE_CHECKING:  # pragma: no cover
    from .store import Project, ProjectStore, Run

log = logging.getLogger("kyora.projects.deck")

HONESTY_TEXT = ("Kyora orchestrates NVIDIA Nemotron-3-Ultra, Kimi K3, Claude Code and NVIDIA Tokkio over their APIs. "
                "Every number in this deck was measured by this project's own runs.")
SECTION_IDS = ("cover", "concept", "design", "architecture", "screenshots", "tests", "changelog", "honesty")
MAX_SCREENSHOTS = 8
MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024
MAX_CHECK_ROWS = 400
MAX_GDD_CHARS = 24000
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
_IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|(rgb|rgba|hsl|hsla)\([0-9\s.,%a-z/+-]{1,60}\)|[a-zA-Z]{3,20})$")
_ITER_RE = re.compile(r"^iter-(\d+)$")
_NOT_PENDING = frozenset({"complete", "failed_gate", "failed", "cancelled", "blocked"})
_BACKEND_LABELS = {"claude_code": "Claude Code", "anthropic_api": "Anthropic API (Claude)", "hub": "Kyora Hub (Claude Code)",
                   "import": "imported ZIP - no code engine ran"}


def esc(value: Any) -> str:
    """Escape text for an HTML text node."""
    return html.escape("" if value is None else str(value), quote=False)


def attr(value: Any) -> str:
    """Escape text for a double-quoted HTML attribute."""
    return html.escape("" if value is None else str(value), quote=True)


def _served_models(run: "Run | None") -> list[str]:
    """Every model that served a code-engine call of ``run`` (``extra.engine_runs``), in order of first use."""
    if run is None:
        return []
    calls = (getattr(run, "extra", None) or {}).get("engine_runs") or []
    models = [c.get("active_model") for c in calls if isinstance(c, dict) and c.get("active_model")]
    if not models and isinstance(run.engine, dict) and run.engine.get("active_model"):
        models = [run.engine["active_model"]]
    return list(dict.fromkeys(str(m) for m in models))


def _run_engine(run: "Run | None") -> dict:
    """The run's engine record with ``active_model`` naming every model that served it."""
    eng = dict(run.engine) if run is not None and isinstance(run.engine, dict) else {}
    models = _served_models(run)
    if len(models) > 1:
        eng["active_model"] = " + ".join(models)
    return eng


def _pct(rate: Any) -> str | None:
    try:
        return f"{float(rate) * 100:.1f}%"
    except (TypeError, ValueError):
        return None


def _threshold_pct(th: Any) -> str:
    try:
        return f"{float(th) * 100:g}%"
    except (TypeError, ValueError):
        return "94%"


def _safe_json(path: Path) -> Any:
    try:
        return read_json(path, default=None)
    except (ValueError, OSError) as exc:
        log.warning("deck: unreadable %s: %s", path, exc)
        return None


def _inline_md(text: str) -> str:
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def render_markdown(text: str) -> str:
    """Tiny, safe Markdown subset (headings, lists, paragraphs, bold/italic/code); input is escaped first."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    in_list: str | None = None
    in_code = False
    code: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + _inline_md(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                flush_para(); close_list()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(); close_list()
            level = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped)
        if m:
            flush_para()
            kind = "ol" if m.group(1)[0].isdigit() else "ul"
            if in_list != kind:
                close_list()
                out.append(f"<{kind}>")
                in_list = kind
            out.append("<li>" + _inline_md(m.group(2)) + "</li>")
            continue
        if not stripped:
            flush_para(); close_list()
            continue
        close_list()
        para.append(stripped)
    if in_code:
        out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush_para(); close_list()
    return "\n".join(out)


def _kv_table(d: dict, limit: int = 60) -> str:
    rows = []
    for i, (k, v) in enumerate(d.items()):
        if i >= limit:
            rows.append(f"<tr><td colspan=\"2\" class=\"muted\">… {len(d) - limit} more</td></tr>")
            break
        val = v if isinstance(v, (str, int, float, bool)) or v is None else _short_json(v)
        rows.append(f"<tr><th>{esc(k)}</th><td>{esc(val)}</td></tr>")
    return "<table class=\"kv\">" + "".join(rows) + "</table>" if rows else ""


def _short_json(v: Any, limit: int = 300) -> str:
    import json
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _ul(items: Iterable[Any], limit: int = 40) -> str:
    items = [i for i in items if i not in (None, "")]
    if not items:
        return ""
    lis = "".join(f"<li>{esc(i if isinstance(i, str) else _short_json(i))}</li>" for i in items[:limit])
    more = f"<li class=\"muted\">… {len(items) - limit} more</li>" if len(items) > limit else ""
    return f"<ul>{lis}{more}</ul>"


def _missing(text: str) -> str:
    return f"<p class=\"missing\">{esc(text)}</p>"


class DeckBuilder:
    """Builds the project deck from the project's slides and its runs' artefacts."""

    def __init__(self, project: "Project | str", store: "ProjectStore"):
        self.store = store
        self.project = store.get(project) if isinstance(project, str) else project

    # ------------------------------------------------------------------ data
    def _refresh(self) -> "Project":
        if self.store is not None:
            try:
                self.project = self.store.get(self.project.id)
            except FileNotFoundError:
                pass
        return self.project

    def _run(self, rid: str | None) -> "Run | None":
        if not rid or self.store is None:
            return None
        try:
            return self.store.get_run(rid)
        except FileNotFoundError:
            return None

    def _latest_run(self) -> "Run | None":
        if self.store is None:
            return None
        runs = self.store.list_runs(self.project.id, limit=1)
        return runs[0] if runs else None

    @staticmethod
    def _iterations(run_dir: Path) -> list[tuple[int, Path]]:
        out = []
        if run_dir.is_dir():
            for d in run_dir.iterdir():
                m = _ITER_RE.match(d.name)
                if m and d.is_dir():
                    out.append((int(m.group(1)), d))
        return sorted(out)

    def _collect(self, slide_n: int | None, pending_run: "Run | None" = None) -> dict:
        project = self._refresh()
        slides = sorted((dict(s) for s in project.slides), key=lambda s: int(s.get("number") or 0))
        data: dict[str, Any] = {"project": project, "slide": None, "run": None, "pending": None}
        if slide_n is not None:
            slide = next((s for s in slides if int(s.get("number") or 0) == int(slide_n)), None)
            if slide is None:
                raise FileNotFoundError(f"slide v{slide_n} not found")
            data["slide"] = slide
            data["run"] = self._run(slide.get("run_id"))
            data["slides"] = [s for s in slides if int(s.get("number") or 0) <= int(slide_n)]
        else:
            # FINALIZE passes its own run: the newest-created run may be another one still queued
            run = pending_run if pending_run is not None else self._latest_run()
            data["slides"] = slides
            if run is not None and run.result_slide is None and run.state not in _NOT_PENDING:
                # FINALIZE writes the deck into the work tree right before the slide is committed
                data["run"] = run
                data["pending"] = {"number": max((int(s.get("number") or 0) for s in slides), default=0) + 1,
                                   "title": None, "run": run}
            elif slides:
                cur = next((s for s in slides if s.get("number") == project.current_slide), slides[-1])
                data["slide"] = cur
                data["run"] = self._run(cur.get("run_id"))
            else:
                data["run"] = run
        run = data["run"]
        rd = self.store.run_dir(run) if (run is not None and self.store is not None) else None
        data["run_dir"] = rd
        for name in ("intake", "plan", "design", "ui_spec"):
            val = _safe_json(rd / f"{name}.json") if rd else None
            data[name] = val if isinstance(val, dict) else {}
        gate = None
        shots: list[tuple[str, str]] = []
        if rd:
            iters = self._iterations(rd)
            for _, d in reversed(iters):
                g = _safe_json(d / "gate.json")
                if isinstance(g, dict):
                    gate = g
                    break
            for _, d in reversed(iters):
                shots = self._screenshots_in(d / "screenshots")
                if shots:
                    break
        if gate is None and run is not None and isinstance(run.gate, dict) and isinstance(run.gate.get("final"), dict):
            gate = dict(run.gate["final"])
        if gate is None and data["slide"] and isinstance(data["slide"].get("gate"), dict):
            gate = dict(data["slide"]["gate"])
        if not shots and data["slide"]:
            shots = self._screenshots_from_paths(data["slide"].get("screenshots") or [])
        data["gate"] = gate
        data["screenshots"] = shots
        if data["pending"] is not None:
            plan_title = data["plan"].get("title") if isinstance(data["plan"], dict) else None
            data["pending"]["title"] = plan_title or (run.prompt[:80] if run and run.prompt else "untitled")
            data["pending"]["label"] = "this build" if run.state == "finalize" else f"in progress: {run.state}"
        return data

    def _encode_image(self, path: Path) -> str | None:
        mime = _IMG_TYPES.get(path.suffix.lower())
        if not mime:
            return None
        try:
            if path.stat().st_size > MAX_SCREENSHOT_BYTES:
                log.info("deck: screenshot too large, skipped: %s", path)
                return None
            return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            log.warning("deck: cannot read screenshot %s: %s", path, exc)
            return None

    def _screenshots_in(self, d: Path) -> list[tuple[str, str]]:
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if len(out) >= MAX_SCREENSHOTS:
                break
            if p.is_file() and not p.is_symlink():
                url = self._encode_image(p)
                if url:
                    out.append((p.stem, url))
        return out

    def _screenshots_from_paths(self, rels: list) -> list[tuple[str, str]]:
        if self.store is None:
            return []
        root = self.store.paths(self.project.id).root
        out = []
        for rel in rels[:MAX_SCREENSHOTS]:
            try:
                p = safe_join(root, *str(rel).split("/"))
            except PermissionError:
                continue
            url = self._encode_image(p) if p.is_file() else None
            if url:
                out.append((p.stem, url))
        return out

    # ------------------------------------------------------------------ sections
    def _cover(self, d: dict) -> str:
        p = d["project"]
        slide, pending, run, gate = d["slide"], d["pending"], d["run"], d["gate"]
        if pending:
            version = f"v{pending['number']} ({pending['label']})"
            title = pending["title"]
            date = run.created_at if run else ""
        elif slide:
            version, title, date = f"v{slide.get('number')}", slide.get("title"), slide.get("created_at")
        else:
            version, title, date = "no slide yet", (d["plan"].get("title") or "Not built yet"), p.created_at
        if gate and gate.get("total"):
            ok = bool(gate.get("ok"))
            badge = (f"<span class=\"badge {'ok' if ok else 'warn'}\">Gate {esc(_pct(gate.get('pass_rate')))} "
                     f"({esc(gate.get('passed'))}/{esc(gate.get('total'))}) · "
                     f"{'passed' if ok else 'below threshold'} · threshold {esc(_threshold_pct(gate.get('threshold')))}</span>")
        else:
            badge = "<span class=\"badge none\">Gate: not measured</span>"
        engine = _run_engine(run) or \
                 ((slide or {}).get("engine") if isinstance((slide or {}).get("engine"), dict) else None) or {}
        eng_line = ""
        if engine.get("backend"):
            model = engine.get("active_model")
            eng_line = (f"<p class=\"muted\">Code engine: {esc(_BACKEND_LABELS.get(engine.get('backend'), engine.get('backend')))}"
                        + (f" · {'models' if ' + ' in str(model) else 'model'} {esc(model)}" if model else "")
                        + (" (fallback)" if engine.get("fallback_used") else "") + "</p>")
        return (f"<section id=\"cover\" class=\"slide cover\"><p class=\"eyebrow\">Kyora project deck · {esc(version)}</p>"
                f"<h1>{esc(p.name)}</h1><p class=\"subtitle\">{esc(title)}</p>{badge}"
                f"<p class=\"muted\">{esc(date)}</p>{eng_line}</section>")

    def _concept(self, d: dict) -> str:
        plan, intake, run = d["plan"], d["intake"], d["run"]
        parts = []
        if plan.get("title") or plan.get("summary"):
            parts.append(f"<h3>{esc(plan.get('title') or 'Plan')}</h3>")
            if plan.get("summary"):
                parts.append(f"<p>{esc(plan.get('summary'))}</p>")
            meta = [f"engine: {plan.get('engine')}" if plan.get("engine") else None,
                    f"{len(plan.get('acceptance_tests') or [])} acceptance tests" if plan.get("acceptance_tests") else None]
            meta = [m for m in meta if m]
            if meta:
                parts.append(f"<p class=\"muted\">{esc(' · '.join(meta))}</p>")
        if intake:
            if intake.get("goal"):
                parts.append(f"<h3>Goal</h3><p>{esc(intake.get('goal'))}</p>")
            facts = {k: intake.get(k) for k in ("genre", "platform") if intake.get(k)}
            if facts:
                parts.append(_kv_table(facts))
            for key, label in (("must_haves", "Must-haves"), ("references", "References"), ("risks", "Risks")):
                if isinstance(intake.get(key), list) and intake.get(key):
                    parts.append(f"<h4>{esc(label)}</h4>{_ul(intake.get(key))}")
        if run is not None and run.prompt:
            prompt = run.prompt if len(run.prompt) <= 2000 else run.prompt[:2000] + "…"
            parts.append(f"<h4>Prompt ({esc(run.kind)})</h4><blockquote>{esc(prompt)}</blockquote>")
        body = "".join(parts) or _missing("No intake or plan recorded for this version (imported or not yet planned).")
        return f"<section id=\"concept\" class=\"slide\"><h2>Concept</h2>{body}</section>"

    def _design(self, d: dict) -> str:
        design, ui = d["design"], d["ui_spec"]
        parts = []
        gdd = design.get("gdd_markdown")
        if isinstance(gdd, str) and gdd.strip():
            text = gdd if len(gdd) <= MAX_GDD_CHARS else gdd[:MAX_GDD_CHARS] + "\n\n…"
            parts.append(f"<div class=\"gdd\">{render_markdown(text)}</div>")
        if design.get("narrative"):
            parts.append(f"<h3>Narrative</h3><p>{esc(design.get('narrative'))}</p>")
        dialogue = design.get("dialogue")
        if isinstance(dialogue, list) and dialogue:
            lines = "".join(f"<li><strong>{esc(x.get('speaker'))}:</strong> {esc(x.get('line'))}</li>"
                            for x in dialogue[:30] if isinstance(x, dict))
            parts.append(f"<h3>Dialogue</h3><ul class=\"dialogue\">{lines}</ul>")
        tokens = ui.get("tokens") if isinstance(ui.get("tokens"), dict) else {}
        colors = tokens.get("color") if isinstance(tokens.get("color"), dict) else {}
        if colors:
            sw = []
            for name, val in list(_flatten(colors).items())[:40]:
                sval = str(val).strip()
                chip = (f"<span class=\"chip\" style=\"background:{attr(sval)}\"></span>"
                        if _COLOR_RE.match(sval) else "<span class=\"chip none\"></span>")
                sw.append(f"<li>{chip}<span>{esc(name)}</span><code>{esc(sval)}</code></li>")
            parts.append(f"<h3>Design tokens: colour</h3><ul class=\"swatches\">{''.join(sw)}</ul>")
        for key, label in (("type", "Type"), ("spacing", "Spacing"), ("radius", "Radius")):
            if isinstance(tokens.get(key), dict) and tokens.get(key):
                parts.append(f"<h4>{esc(label)}</h4>{_kv_table(_flatten(tokens[key]))}")
        screens = ui.get("screens")
        if isinstance(screens, list) and screens:
            items = "".join(f"<li><strong>{esc(s.get('name') or s.get('id'))}</strong> — {esc(s.get('purpose'))}</li>"
                            for s in screens[:30] if isinstance(s, dict))
            parts.append(f"<h3>Screens</h3><ul>{items}</ul>")
        if isinstance(ui.get("copy"), dict) and ui.get("copy"):
            parts.append(f"<h3>Copy</h3>{_kv_table(_flatten(ui['copy']), limit=40)}")
        body = "".join(parts) or _missing("No design document or UI spec recorded for this version.")
        return f"<section id=\"design\" class=\"slide\"><h2>Design</h2>{body}</section>"

    def _architecture(self, d: dict) -> str:
        run, slide, gate = d["run"], d["slide"], d["gate"]
        engine = _run_engine(run) or \
                 ((slide or {}).get("engine") if isinstance((slide or {}).get("engine"), dict) else {}) or {}
        backend = engine.get("backend")
        models = [m for m in str(engine.get("active_model") or "").split(" + ") if m]
        if backend == "import":  # a slide imported from a ZIP: no code engine built it
            engine_label, model = "imported ZIP", "no code engine ran"
        elif not backend:
            engine_label, model = "not built yet", "no code engine ran"
        else:
            engine_label = _BACKEND_LABELS.get(backend, str(backend))
            engine_label = engine_label if len(engine_label) <= 22 else "Claude Code"
            model = (models[0] if len(models) == 1 else f"{len(models)} models (below)" if models
                     else engine.get("requested_model") or "model not resolved")
        threshold = (gate or {}).get("threshold")
        if threshold is None and run is not None and isinstance(run.gate, dict):
            threshold = run.gate.get("threshold")
        nodes = [
            ("Intake", "Kimi K3", "vision intake"),
            ("Plan", "Kimi K3", "plan + tests"),
            ("Design", "Nemotron-3-Ultra", "GDD + UI spec"),
            ("Build", engine_label, str(model)[:24]),
            ("Test", "browser + node", "tests"),
            ("Gate", f"gate {_threshold_pct(threshold)}", "measured"),
            ("Finalize", "git slide", "+ deck"),
        ]
        w, gap, x0, y0, h = 140, 22, 12, 40, 96
        svg = [f"<svg class=\"arch\" viewBox=\"0 0 {x0 * 2 + 7 * w + 6 * gap} 220\" role=\"img\" "
               f"aria-label=\"Kyora pipeline: Intake, Plan, Design, Build, Test, Gate, Finalize\" "
               f"xmlns=\"http://www.w3.org/2000/svg\">",
               "<defs><marker id=\"arrow\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" markerWidth=\"7\" markerHeight=\"7\" "
               "orient=\"auto-start-reverse\"><path d=\"M0,0 L10,5 L0,10 z\" fill=\"currentColor\"/></marker></defs>"]
        for i, (stage, sys1, sys2) in enumerate(nodes):
            x = x0 + i * (w + gap)
            svg.append(f"<g class=\"node\"><rect x=\"{x}\" y=\"{y0}\" width=\"{w}\" height=\"{h}\" rx=\"12\"/>"
                       f"<text x=\"{x + w / 2:g}\" y=\"{y0 + 30}\" class=\"stage\">{esc(stage)}</text>"
                       f"<text x=\"{x + w / 2:g}\" y=\"{y0 + 56}\" class=\"sys\">{esc(sys1)}</text>"
                       f"<text x=\"{x + w / 2:g}\" y=\"{y0 + 76}\" class=\"sys2\">{esc(sys2)}</text></g>")
            if i < len(nodes) - 1:
                svg.append(f"<line x1=\"{x + w + 2}\" y1=\"{y0 + h / 2:g}\" x2=\"{x + w + gap - 2}\" y2=\"{y0 + h / 2:g}\" "
                           f"class=\"edge\" marker-end=\"url(#arrow)\"/>")
        bx = x0 + 3 * (w + gap)
        gx = x0 + 5 * (w + gap)
        svg.append(f"<path d=\"M{gx + w / 2:g},{y0 + h + 4} v26 H{x0 + 3 * (w + gap) + w / 2:g} v-22\" class=\"edge dashed\" "
                   f"marker-end=\"url(#arrow)\"/>"
                   f"<text x=\"{(gx + bx + w) / 2:g}\" y=\"{y0 + h + 46}\" class=\"sys2\">below threshold: Kimi K3 review → fix</text>")
        svg.append(f"<text x=\"{bx + w / 2:g}\" y=\"{y0 - 14}\" class=\"sys2\">every tool call judged by Kimi K3</text>")
        svg.append("</svg>")
        served = (f"<p class=\"muted\">Models that served the code engine for this version: {esc(', '.join(models))}.</p>"
                  if len(models) > 1 else "")
        return ("<section id=\"architecture\" class=\"slide\"><h2>Architecture</h2>" + "".join(svg) +
                "<p class=\"muted\">Fixed roles over their own APIs: Kimi K3 plans, judges and reviews; "
                "NVIDIA Nemotron-3-Ultra writes the design; the code engine implements; tests run in a real "
                "browser and Node; the gate decides from measured results.</p>" + served + "</section>")

    def _screenshots(self, d: dict) -> str:
        shots = d["screenshots"]
        if not shots:
            body = _missing("No screenshots were captured for this version.")
        else:
            body = "<div class=\"shots\">" + "".join(
                f"<figure><img src=\"{attr(url)}\" alt=\"{attr(name)}\"><figcaption>{esc(name)}</figcaption></figure>"
                for name, url in shots) + "</div>"
        return f"<section id=\"screenshots\" class=\"slide\"><h2>Screenshots</h2>{body}</section>"

    def _tests(self, d: dict) -> str:
        gate = d["gate"]
        if not gate:
            return ("<section id=\"tests\" class=\"slide\"><h2>Tests and quality gate</h2>"
                    + _missing("No test results were measured for this version.") + "</section>")
        ok = bool(gate.get("ok"))
        summary = (f"<p class=\"big {'ok' if ok else 'warn'}\">{esc(_pct(gate.get('pass_rate')) or 'n/a')}</p>"
                   f"<p>{esc(gate.get('passed'))} of {esc(gate.get('total'))} checks passed · threshold "
                   f"{esc(_threshold_pct(gate.get('threshold')))} · {'gate passed' if ok else 'gate not passed'}</p>")
        extra = ""
        if gate.get("required_failures"):
            extra += f"<h4>Required checks failing</h4>{_ul(gate.get('required_failures'))}"
        if gate.get("missing_spec_tests"):
            extra += f"<h4>Acceptance tests missing from the results</h4>{_ul(gate.get('missing_spec_tests'))}"
        run = d["run"]
        failures = (getattr(run, "extra", None) or {}).get("engine_failures") if run is not None else None
        if failures:
            extra += "<h4>Code engine errors in this run</h4>" + _ul(
                f"{f.get('kind')} (iteration {f.get('iteration')}, {f.get('backend') or 'engine'}): {f.get('error')} - "
                + ("it had changed files, which were measured" if f.get("tree_changed") else "no file changed")
                for f in failures if isinstance(f, dict))
        checks = gate.get("checks") if isinstance(gate.get("checks"), list) else []
        table = ""
        if checks:
            rows = []
            for c in checks[:MAX_CHECK_ROWS]:
                if not isinstance(c, dict):
                    continue
                status = str(c.get("status") or "")
                rows.append(f"<tr class=\"st-{attr(status)}\"><td><code>{esc(c.get('id'))}</code></td><td>{esc(c.get('group'))}</td>"
                            f"<td>{esc(c.get('name'))}</td><td>{esc(status)}</td><td>{'yes' if c.get('required') else ''}</td></tr>")
            more = (f"<tr><td colspan=\"5\" class=\"muted\">… {len(checks) - MAX_CHECK_ROWS} more</td></tr>"
                    if len(checks) > MAX_CHECK_ROWS else "")
            table = ("<table class=\"checks\"><thead><tr><th>id</th><th>group</th><th>check</th><th>status</th>"
                     "<th>required</th></tr></thead><tbody>" + "".join(rows) + more + "</tbody></table>")
        return f"<section id=\"tests\" class=\"slide\"><h2>Tests and quality gate</h2>{summary}{extra}{table}</section>"

    def _changelog(self, d: dict) -> str:
        items = []
        for s in d["slides"]:
            items.append(self._change_item(s.get("number"), s.get("title"), s.get("created_at"), s.get("gate"),
                                           s.get("engine"), s.get("parent")))
        pending = d["pending"]
        if pending:
            run = pending["run"]
            final = run.gate.get("final") if isinstance(run.gate, dict) else None
            items.append(self._change_item(pending["number"], f"{pending['title']} ({pending['label']})", run.created_at,
                                           final, _run_engine(run), run.parent_slide))
        body = ("<ol class=\"changelog\">" + "".join(items) + "</ol>") if items else _missing("No slides yet.")
        return f"<section id=\"changelog\" class=\"slide\"><h2>Changelog</h2>{body}</section>"

    @staticmethod
    def _change_item(number, title, date, gate, engine, parent) -> str:
        if isinstance(gate, dict) and gate.get("total"):
            g = f"gate {_pct(gate.get('pass_rate'))} ({gate.get('passed')}/{gate.get('total')}){' ✓' if gate.get('ok') else ''}"
        else:
            g = "no gate"
        engine = engine if isinstance(engine, dict) else {}
        eng = _BACKEND_LABELS.get(engine.get("backend"), engine.get("backend") or "engine n/a")
        if engine.get("active_model"):
            eng += f" · {engine.get('active_model')}"
        par = f"v{parent}" if parent is not None else "—"
        return (f"<li><span class=\"ver\">v{esc(number)}</span> <strong>{esc(title)}</strong>"
                f"<span class=\"muted\"> · {esc(date)} · {esc(g)} · {esc(eng)} · parent {esc(par)}</span></li>")

    @staticmethod
    def _honesty() -> str:
        return f"<section id=\"honesty\" class=\"slide end\"><footer class=\"honesty\"><p>{esc(HONESTY_TEXT)}</p></footer></section>"

    # ------------------------------------------------------------------ public
    def build(self, slide_n: int | None = None, run: "Run | None" = None) -> str:
        """Return the deck as one standalone HTML document (no external requests).

        ``run`` is the run being finalized (its slide is not committed yet); without it
        and without ``slide_n`` the project's newest run or current slide is shown.
        """
        d = self._collect(slide_n, run)
        p = d["project"]
        sections = [self._cover(d), self._concept(d), self._design(d), self._architecture(d), self._screenshots(d),
                    self._tests(d), self._changelog(d), self._honesty()]
        nav = "".join(f"<a href=\"#{s}\">{esc(s.capitalize())}</a>" for s in SECTION_IDS)
        return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                f"<meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\">"
                "<meta name=\"generator\" content=\"Kyora\">"
                f"<title>{esc(p.name)} · Kyora deck</title><style>{_CSS}</style></head><body>"
                f"<nav class=\"deck-nav\" aria-label=\"Sections\">{nav}</nav><main>"
                + "\n".join(sections) +
                f"</main><p class=\"hint\">← → switch sections · print for PDF</p><script>{_JS}</script></body></html>\n")

    def write_into_workspace(self, workspace: Path | str, slide_n: int | None = None, run: "Run | None" = None) -> Path:
        """Write the deck to ``<workspace>/kyora/deck.html`` and return the path (``run``: see :meth:`build`)."""
        out = Path(workspace) / "kyora" / "deck.html"
        atomic_write_text(out, self.build(slide_n, run))
        return out


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


_CSS = """
:root{--bg:#0b0d12;--panel:#141821;--ink:#e9edf5;--muted:#97a1b5;--line:#2a3142;--accent:#76b900;--warn:#f0a020;--bad:#e5484d}
*{box-sizing:border-box}html{scroll-snap-type:y proximity;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:0 20px}
.deck-nav{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:4px;padding:8px 12px;background:rgba(11,13,18,.92);border-bottom:1px solid var(--line)}
.deck-nav a{color:var(--muted);text-decoration:none;font-size:13px;padding:4px 8px;border-radius:6px}
.deck-nav a:hover,.deck-nav a:focus{color:var(--ink);background:var(--panel)}
section.slide{min-height:100vh;padding:56px 0 40px;scroll-snap-align:start;border-bottom:1px solid var(--line)}
section.cover{display:flex;flex-direction:column;justify-content:center}
h1{font-size:clamp(32px,6vw,64px);margin:.1em 0}h2{font-size:28px;margin:0 0 16px}h3{margin:20px 0 8px}h4{margin:16px 0 6px;color:var(--muted)}
.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.12em;font-size:13px}
.subtitle{font-size:22px;color:var(--ink);margin:0 0 16px}.muted{color:var(--muted)}.missing{color:var(--muted);font-style:italic}
.badge{display:inline-block;padding:6px 12px;border-radius:999px;font-weight:600;font-size:14px;width:fit-content}
.badge.ok{background:rgba(118,185,0,.18);color:#a6e22e}.badge.warn{background:rgba(240,160,32,.18);color:var(--warn)}.badge.none{background:var(--panel);color:var(--muted)}
.big{font-size:56px;font-weight:700;margin:0}.big.ok{color:var(--accent)}.big.warn{color:var(--warn)}
blockquote{margin:8px 0;padding:10px 14px;border-left:3px solid var(--accent);background:var(--panel);white-space:pre-wrap}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
table.kv th{width:30%;color:var(--muted);font-weight:500}
tr.st-fail td{color:var(--bad)}tr.st-pass td:nth-child(4){color:var(--accent)}tr.st-skip td,tr.st-deferred td{color:var(--muted)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}pre{background:var(--panel);padding:12px;overflow:auto;border-radius:8px}
.swatches{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.swatches li{display:flex;align-items:center;gap:8px}.chip{width:28px;height:28px;border-radius:6px;border:1px solid var(--line);flex:none}.chip.none{background:repeating-linear-gradient(45deg,var(--panel),var(--panel) 4px,var(--line) 4px,var(--line) 8px)}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
figure{margin:0;background:var(--panel);border-radius:10px;padding:8px}figure img{width:100%;height:auto;border-radius:6px;display:block}figcaption{color:var(--muted);font-size:13px;padding-top:6px}
svg.arch{width:100%;height:auto;color:var(--muted);margin:12px 0}
svg.arch rect{fill:var(--panel);stroke:var(--accent);stroke-width:1.5}
svg.arch .stage{fill:var(--ink);font-weight:700;font-size:16px;text-anchor:middle}
svg.arch .sys{fill:var(--accent);font-size:13px;text-anchor:middle}svg.arch .sys2{fill:var(--muted);font-size:12px;text-anchor:middle}
svg.arch .edge{stroke:currentColor;stroke-width:1.6;fill:none}svg.arch .dashed{stroke-dasharray:5 4}
ol.changelog{padding-left:0;list-style:none}ol.changelog li{padding:10px 0;border-bottom:1px solid var(--line)}
.ver{display:inline-block;min-width:44px;font-weight:700;color:var(--accent)}
.honesty{font-size:18px;max-width:780px}.end{display:flex;align-items:center}
.hint{position:fixed;right:12px;bottom:8px;color:var(--muted);font-size:12px;margin:0}
@media (max-width:640px){section.slide{padding-top:40px}.big{font-size:40px}}
@media print{
  html{scroll-snap-type:none}body{background:#fff;color:#000}
  .deck-nav,.hint{display:none}
  section.slide{min-height:auto;page-break-after:always;break-after:page;border:0;padding:24px 0}
  section.slide:last-child{page-break-after:auto;break-after:auto}
  .muted,h4{color:#444}.badge,.big{color:#000!important;background:none!important}
  svg.arch rect{fill:#fff;stroke:#000}svg.arch .stage,svg.arch .sys,svg.arch .sys2{fill:#000}svg.arch{color:#000}
  blockquote,pre,figure{background:#f4f4f4}
}
"""

_JS = """
(function(){
  'use strict';
  var ids = %s;
  function current(){
    var best = 0, bestDist = Infinity;
    for (var i = 0; i < ids.length; i++){
      var el = document.getElementById(ids[i]);
      if (!el) continue;
      var d = Math.abs(el.getBoundingClientRect().top - 40);
      if (d < bestDist){ bestDist = d; best = i; }
    }
    return best;
  }
  function go(i){
    i = Math.max(0, Math.min(ids.length - 1, i));
    var el = document.getElementById(ids[i]);
    if (el){ el.scrollIntoView({behavior: 'smooth', block: 'start'}); try { history.replaceState(null, '', '#' + ids[i]); } catch (e) {} }
  }
  document.addEventListener('keydown', function(e){
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    var t = e.target && e.target.tagName;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t)) return;
    var k = e.key;
    if (k === 'ArrowRight' || k === 'ArrowDown' || k === 'PageDown' || k === ' '){ e.preventDefault(); go(current() + 1); }
    else if (k === 'ArrowLeft' || k === 'ArrowUp' || k === 'PageUp'){ e.preventDefault(); go(current() - 1); }
    else if (k === 'Home'){ e.preventDefault(); go(0); }
    else if (k === 'End'){ e.preventDefault(); go(ids.length - 1); }
  });
})();
""" % ("[" + ",".join(f"'{s}'" for s in SECTION_IDS) + "]")
