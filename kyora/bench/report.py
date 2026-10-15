"""Kyora Bench report writers: ``results.json`` and a standalone ``report.html``.

The HTML report is a single self-contained file (inline CSS, no scripts, no
external URLs) so it can be attached, archived or opened offline. Every value
is HTML-escaped. It only renders what the payload contains; nothing is
estimated or filled in.
"""
from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any, Mapping

from ..util.fs import atomic_write_json, atomic_write_text


def write_json(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """Atomically write ``payload`` as UTF-8 JSON to ``path``; returns the path."""
    path = Path(path)
    atomic_write_json(path, dict(payload))
    return path


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _pct(rate: Any) -> str:
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return "-"
    return f"{math.floor(float(rate) * 1000) / 10:.1f}%"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "-"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _final_gate(task: Mapping[str, Any]) -> Mapping[str, Any] | None:
    gate = task.get("gate")
    if not isinstance(gate, Mapping):
        return None
    final = gate.get("final")
    if isinstance(final, Mapping):
        return final
    if "pass_rate" in gate and "ok" in gate:
        return gate
    return None


def _date_of(ts: Any) -> str:
    text = str(ts or "")
    return text[:10] if len(text) >= 10 else (text or "an unknown date")


_CSS = """
:root{--bg:#0b0f14;--panel:#121923;--text:#e6edf3;--muted:#8b98a7;--line:#223041;--ok:#2fbf71;--bad:#ef5b5b;--warn:#e0a63a;--accent:#76b900}
@media (prefers-color-scheme: light){:root{--bg:#f6f8fa;--panel:#fff;--text:#1f2328;--muted:#59636e;--line:#d0d7de;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--accent:#3d7a00}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:150px}
.card b{display:block;font-size:1.4rem}
.card span{color:var(--muted);font-size:.85rem}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:.85rem;border:1px solid}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.badge.bad{color:var(--bad);border-color:var(--bad)}
.reason{color:var(--warn);margin:0 0 16px}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
td.err{color:var(--bad);max-width:320px;overflow-wrap:anywhere;font-size:.85rem}
.pass{color:var(--ok);font-weight:600}.fail{color:var(--bad);font-weight:600}
footer{margin-top:24px;color:var(--muted);font-size:.9rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}
"""


def render_html(payload: Mapping[str, Any]) -> str:
    """Return the standalone HTML report for a bench ``results.json`` payload."""
    score = payload.get("score") if isinstance(payload.get("score"), Mapping) else {}
    tasks = [t for t in (payload.get("tasks") or []) if isinstance(t, Mapping)]
    engine = score.get("engine") or payload.get("engine") or "unknown engine"
    model = score.get("model") or payload.get("model") or "unknown model"
    timestamp = score.get("timestamp") or payload.get("finished_at") or payload.get("started_at")
    certified = score.get("certified") is True
    run = score.get("run", len(tasks))
    passed = score.get("passed", sum(1 for t in tasks if t.get("passed") is True))
    total_suite = score.get("total_suite", payload.get("suite_size", ""))

    rows = []
    for t in tasks:
        final = _final_gate(t) or {}
        gate_txt = _pct(final.get("pass_rate"))
        if isinstance(final.get("passed"), int) and isinstance(final.get("total"), int):
            gate_txt += f" ({final['passed']}/{final['total']})"
        result_cls = "pass" if t.get("passed") is True else "fail"
        result_txt = "PASS" if t.get("passed") is True else "FAIL"
        model_txt = t.get("active_model") or "-"
        if t.get("engine"):
            model_txt = f"{t.get('engine')} / {model_txt}"
        rows.append(
            "<tr>"
            f"<td><code>{_e(t.get('task_id'))}</code></td>"
            f"<td>{_e(t.get('title'))}<br><span class=\"sub\">{_e(t.get('category'))}</span></td>"
            f"<td class=\"{result_cls}\">{result_txt}</td>"
            f"<td>{_e(t.get('state'))}</td>"
            f"<td class=\"num\">{_e(gate_txt)}</td>"
            f"<td class=\"num\">{_e(_duration(t.get('duration_s')))}</td>"
            f"<td>{_e(model_txt)}</td>"
            f"<td class=\"err\">{_e(t.get('error') or '')}</td>"
            "</tr>")
    if not rows:
        rows.append('<tr><td colspan="8">No task ran in this bench run.</td></tr>')

    badge = ('<span class="badge ok">Kyora Bench certified</span>' if certified
             else '<span class="badge bad">not certified</span>')
    reason = "" if certified else f'<p class="reason">{_e(score.get("reason") or "")}</p>'
    skipped = payload.get("skipped") or []
    skipped_html = (f'<p class="sub">Not run (bench cancelled or stopped): {_e(", ".join(map(str, skipped)))}</p>'
                    if skipped else "")
    footer = (f"Measured by Kyora Bench on {_e(_date_of(timestamp))} with {_e(engine)}/{_e(model)} on this machine. "
              "No number here is estimated.")
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Kyora Bench report {_e(_date_of(timestamp))}</title><style>{_CSS}</style></head><body><main>"
        "<h1>Kyora Bench report</h1>"
        f"<p class=\"sub\">Bench run <code>{_e(payload.get('bench_id') or '')}</code> &middot; started "
        f"{_e(payload.get('started_at') or '-')} &middot; finished {_e(payload.get('finished_at') or '-')}</p>"
        "<div class=\"cards\">"
        f"<div class=\"card\"><b>{_e(_pct(score.get('pass_rate')) if run else '-')}</b><span>measured pass rate</span></div>"
        f"<div class=\"card\"><b>{_e(passed)}/{_e(run)}</b><span>tasks passed / run</span></div>"
        f"<div class=\"card\"><b>{_e(run)}/{_e(total_suite)}</b><span>suite coverage</span></div>"
        f"<div class=\"card\"><b>{_e(_pct(score.get('threshold', 0.94)))}</b><span>certification threshold</span></div>"
        "</div>"
        f"<p>{badge}</p>{reason}{skipped_html}"
        "<div class=\"wrap\"><table><thead><tr><th>Task</th><th>Title</th><th>Result</th><th>State</th>"
        "<th>Gate</th><th>Time</th><th>Engine / model</th><th>Error</th></tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table></div>"
        f"<footer>{footer}</footer>"
        "</main></body></html>\n")


def write_html(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """Atomically write the standalone HTML report for ``payload`` to ``path``."""
    path = Path(path)
    atomic_write_text(path, render_html(payload))
    return path
