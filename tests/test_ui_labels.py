"""Honest labels: UI, docs, deck and engine prompt describe what Kyora really does.

* The judge sees only Write/Edit/MultiEdit/NotebookEdit/Bash; local rules settle most of them and
  Kimi K3 decides the rest, so no surface may claim that Kimi K3 judges every tool call.
* Gate, effort and engine labels come from the run's recorded threshold, effort and backend.
* Claude Code's ``total_cost_usd`` is an API-equivalent list-price estimate, not a measured charge.
* The Presenter shows the Tokkio frame only for a 2xx/3xx answer of the Tokkio UI.

The JavaScript checks load the browser modules into Node with a stub ``window`` (they are pure
renderers); they are skipped when ``node`` is not installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import kyora.pipeline.runner as runner_module
from kyora.pipeline.runner import Runner
from tests.test_run_lifecycle import _RunnerHome  # shared runner harness (defines no tests of its own)

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "kyora" / "web"
NODE = shutil.which("node")

EVERY_CALL_CLAIMS = re.compile(
    r"every tool call|judges every|judged by Kimi K3|every action of the code engine|on every tool call|"
    r"judgement of every|every code-engine tool call|Every Write/Edit/Bash of the code engine is judged",
    re.IGNORECASE)
# Kimi K3 does not judge the finished game: the measured gate decides, Kimi K3 reviews failed gates and
# judges the writes/shell commands the local rules do not settle.
JUDGES_OUTPUT_CLAIMS = re.compile(r"Kimi\s+K3\s+judges\s+(?:it\b|the\s+result|the\s+game)", re.IGNORECASE)


def run_node(files: list[str], expr: str):
    """Load ``files`` (relative to kyora/web) into Node with a stub window and print ``expr`` as JSON."""
    script = (
        "const fs = require('fs'); const vm = require('vm');\n"
        "globalThis.window = globalThis;\n"
        "for (const f of JSON.parse(process.argv[2])) vm.runInThisContext(fs.readFileSync(f, 'utf8'), { filename: f });\n"
        "const K = window.Kyora;\n"
        f"const out = (function () {{ return {expr}; }})();\n"
        "process.stdout.write(JSON.stringify(out));\n")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "probe.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([NODE, str(path), json.dumps([str(WEB / f) for f in files])],
                              capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=td)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[-800:]}")
    return json.loads(proc.stdout)


class JudgeWordingTests(unittest.TestCase):
    SURFACES = [
        "kyora/web/index.html", "kyora/web/js/app.js", "README.md", "CHANGELOG.md", "docs/hub-mode.md",
        "docs/honesty.md", "docs/architecture.md", "docs/claude-code-engine.md",
        "kyora/pipeline/templates/claude_system.md", "kyora/projects/deck.py",
    ]

    def test_no_surface_claims_kimi_judges_every_tool_call(self):
        for rel in self.SURFACES:
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = [m.group(0) for m in EVERY_CALL_CLAIMS.finditer(text)]
            self.assertEqual(hits, [], f"{rel} still claims Kimi K3 judges every tool call")

    def test_no_surface_claims_kimi_judges_the_game(self):
        self.assertTrue(JUDGES_OUTPUT_CLAIMS.search("Claude Code implements it, Kimi K3 judges\nit)."))
        self.assertTrue(JUDGES_OUTPUT_CLAIMS.search("Kimi K3 judges the result"))
        self.assertIsNone(JUDGES_OUTPUT_CLAIMS.search("Kimi K3 judges the writes the local rules do not settle"))
        for rel in self.SURFACES + ["tokkio-kyora/README.md", "docs/tokkio-overlay.md",
                                    "tokkio-kyora/configs/config.kyora.yaml"]:
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = [m.group(0) for m in JUDGES_OUTPUT_CLAIMS.finditer(text)]
            self.assertEqual(hits, [], f"{rel} still says Kimi K3 judges the game")

    def test_surfaces_say_what_is_judged(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        note = re.search(r'id="judge-note">(.*?)</p>', html, re.S)
        self.assertIsNotNone(note, "the Judge card explains what passes through the judge")
        for word in ("Write", "Bash", "Local rules", "Kimi K3 decides the rest", "Read, Glob and Grep are not judged"):
            self.assertIn(word, note.group(1))
        prompt = (REPO / "kyora/pipeline/templates/claude_system.md").read_text(encoding="utf-8")
        self.assertIn("local rules", prompt)
        self.assertIn("Kimi K3 decides the rest", prompt)
        deck = (REPO / "kyora/projects/deck.py").read_text(encoding="utf-8")
        self.assertIn("local rules, then Kimi K3", deck)

    def test_judge_card_counts_verdict_sources(self):
        app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
        for key in ("'rule'", "'cached'", "'kimi'"):
            self.assertIn(key, app[app.index("function renderJudge"):app.index("function renderControls")])
        self.assertIn("if (d.source === 'rule') ctx.judge.rule += 1;", app)
        self.assertIn("else if (d.source === 'cache') ctx.judge.cached += 1;", app)
        self.assertIn("else if (d.source === 'kimi') ctx.judge.kimi += 1;", app)


@unittest.skipUnless(NODE, "node not installed")
class RunLabelRenderTests(unittest.TestCase):
    """Gate, engine and effort labels are rendered from the run, not from fixed strings."""

    def test_pipeline_chart_uses_the_run_threshold_and_backend(self):
        out = run_node(["js/pipeline-chart.js"], """{
            wide: K.renderPipelineChart({ gate: { status: 'done' } }, { width: 900, threshold: 0.8, backend: 'hub:claude_code' }),
            narrow: K.renderPipelineChart({}, { width: 320, threshold: 0.825 }),
            unknown: K.renderPipelineChart({}, { width: 900 }) }""")
        self.assertIn("&gt;= 80 %", out["wide"])
        self.assertIn(">below 80 %<", out["wide"])
        self.assertIn("Claude Code (Hub)", out["wide"])
        self.assertNotIn("94", out["wide"])
        self.assertNotIn("94", out["narrow"])
        # no recorded threshold or backend: no invented number, no engine name
        self.assertNotIn("94", out["unknown"])
        self.assertIn(">below threshold<", out["unknown"])
        self.assertIn("code engine", out["unknown"])
        self.assertNotIn("Claude Code", out["unknown"])

    def test_engine_tile_names_the_recorded_backend_and_effort(self):
        out = run_node(["js/pipeline-chart.js", "js/metrics.js"], """{
            api: K.metrics.renderTiles(K.metrics.emptyMetrics(), { backend: 'anthropic_api', effort: 'high' }),
            none: K.metrics.renderTiles(K.metrics.emptyMetrics(), null) }""")
        self.assertIn('<span class="tile-name">Anthropic API</span>', out["api"])
        self.assertIn('<span class="tile-role">code engine · effort high</span>', out["api"])
        self.assertNotIn("effort max", out["api"])
        self.assertIn('<span class="tile-role">code engine</span>', out["none"])
        self.assertNotIn("effort", out["none"])

    def test_claude_cost_is_labelled_as_a_list_price_estimate(self):
        out = run_node(["js/pipeline-chart.js", "js/metrics.js"], """(function () {
            const m = K.metrics.emptyMetrics();
            K.metrics.applyMetricsEvent(m, { type: 'engine.session', data: { backend: 'claude_code', model: 'claude-opus-5-5' } });
            K.metrics.applyMetricsEvent(m, { type: 'engine.result', data: { num_turns: 3, duration_ms: 9253, cost_usd: 1.00287625 } });
            return K.metrics.renderTiles(m, null); })()""")
        claude = out[out.index('data-sys="claude"'):]
        cost = re.search(r'<div data-stat="cost" title="([^"]*)">([^<]*)<b>([^<]*)</b>', claude)
        self.assertIsNotNone(cost, "the cost stat carries its explanation")
        note, label, value = cost.groups()
        self.assertEqual(label, "API-equiv. est.")
        self.assertEqual(value, "~$1.003")
        self.assertIn("list prices", note)
        self.assertIn("not a measured charge", note)
        self.assertNotIn(">cost<b>", claude)

    def test_slide_card_warns_when_an_engine_call_of_the_run_failed(self):
        out = run_node(["js/slides.js"], """(function () {
            K.util = { esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
                       enc: encodeURIComponent, fmtDate: (s) => String(s || ''), pctText: (x) => Math.round(x * 100) + ' %' };
            const S = K.modules.slides;
            const lay = { children: {} };
            const card = (engine) => S.cardHtml({ number: 2, tag: 'slide/v2', title: 'v2', parent: 1, engine,
              gate: { pass_rate: 1, ok: true, threshold: 0.94, passed: 21, total: 21 } }, lay, 'p1', 2);
            return {
              failed: card({ backend: 'claude_code', ok: false, engine_errors: [
                { kind: 'build', iteration: 0, backend: 'claude_code', error: 'error_max_turns <x>', tree_changed: true }] }),
              clean: card({ backend: 'claude_code', ok: true }),
              older: card({ backend: 'claude_code' }),
              imported: card({ backend: 'import' }) }; })()""")
        chip = re.search(r'<span class="chip sc-engine-error" data-tone="warn" title="([^"]*)">([^<]*)</span>', out["failed"])
        self.assertIsNotNone(chip, "a slide whose run had a failed engine call shows a warning chip")
        title, text = chip.groups()
        self.assertEqual(text, "engine reported an error during this build")
        self.assertIn("build (iteration 0), claude_code: error_max_turns &lt;x&gt; - its files were measured", title)
        self.assertIn('data-tone="ok"', out["failed"], "the measured gate badge is kept")
        for key in ("clean", "older", "imported"):
            self.assertNotIn("sc-engine-error", out[key], key)

    def test_presenter_shows_tokkio_only_for_a_successful_answer(self):
        out = run_node(["js/presenter.js"], """(function () {
            K.util = { esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) };
            const P = K.modules.presenter;
            const url = 'http://10.0.0.5:30111';
            const ui = (o) => ({ configured: true, ui: Object.assign({ configured: true, url }, o) });
            return {
              ok: P.mainState(ui({ reachable: true, ok: true, status: 200 }), url),
              bad: P.mainState(ui({ reachable: true, ok: false, status: 502, error: 'HTTP 502 Bad Gateway' }), url),
              notFound: P.mainState(ui({ reachable: true, ok: false, status: 404, error: 'HTTP 404 Not Found' }), url),
              down: P.mainState(ui({ reachable: false, ok: false, error: 'unreachable: timed out' }), url),
              off: P.mainState({ configured: false, ui: { configured: false } }, '') }; })()""")
        self.assertEqual(out["ok"], {"deployed": True, "reason": ""})
        self.assertFalse(out["bad"]["deployed"])
        self.assertIn("HTTP 502 Bad Gateway", out["bad"]["reason"])
        self.assertFalse(out["notFound"]["deployed"])
        self.assertIn("HTTP 404", out["notFound"]["reason"])
        self.assertFalse(out["down"]["deployed"])
        self.assertIn("did not answer", out["down"]["reason"])
        self.assertFalse(out["off"]["deployed"])
        js = (WEB / "js" / "presenter.js").read_text(encoding="utf-8")
        self.assertIn("const view = mainState(health, url);\n    if (view.deployed) {", js)

    def test_static_texts_do_not_hard_code_gate_or_effort(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for fixed in ("at effort max", "<code>--effort max</code>", "94 % is a gate", "at least 94 % of its counted"):
            self.assertNotIn(fixed, html)
        self.assertGreaterEqual(html.count('data-setting-label="effort"'), 2)
        self.assertIn('data-setting-label="gate"', html)
        app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("threshold: gateThreshold(ctx), backend:", app)
        self.assertIn("K.metrics.renderTiles(ctx ? ctx.metrics : K.metrics.emptyMetrics(), ctx ? ctx.engine : null)", app)

    def test_docs_do_not_fix_the_gate_or_the_fix_rounds(self):
        fixed = {
            "README.md": ("at least **94 % of that project", "GATE (>= 94 %)", "against 94 %", "up to 3 rounds"),
            "docs/honesty.md": ("below 94 % - not complete", "`pass_rate >= 0.94`"),
            "docs/ui-design.md": ('"below 94%"',),
        }
        for rel, phrases in fixed.items():
            text = " ".join((REPO / rel).read_text(encoding="utf-8").split())
            for phrase in phrases:
                self.assertNotIn(phrase, text, f"{rel} presents a configurable gate value as fixed")
            self.assertIn("94 % by default" if rel.startswith("docs/") else "quality.min_pass_rate", text)
        readme = " ".join((REPO / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("up to `quality.max_fix_iterations` rounds (default 3)", readme)

    def test_bench_warning_names_the_configured_effort_and_engine(self):
        out = run_node(["js/bench.js"], """{
            cc: K.modules.bench.warning('high', 'claude_code'),
            api: K.modules.bench.warning('medium', 'anthropic_api'),
            hub: K.modules.bench.warning('max', 'hub'),
            none: K.modules.bench.warning(null, null) }""")
        self.assertIn("Claude subscription usage (Claude Code CLI) at effort high;", out["cc"])
        self.assertIn("Anthropic API tokens on your Anthropic key at effort medium;", out["api"])
        self.assertIn("linked Kyora Hub at effort max;", out["hub"])
        self.assertIn("NVIDIA keys and code-engine usage; typically", out["none"])
        for text in out.values():
            self.assertNotIn("at max effort", text)
        js = (WEB / "js" / "bench.js").read_text(encoding="utf-8")
        self.assertIn("benchWarning(effort, estimate && estimate.engine)", js)
        self.assertIn("settingValue('claude_code.effort', null)", js)


BENCH_TESTS = REPO / "kyora" / "bench" / "tasks" / "b01-breakout.browser-tests.js"


class RunnerGateTextTests(_RunnerHome):
    """Narration and the Tokkio view name the gate the run applied (a bench task's 94 %), not Settings."""

    def test_bench_run_texts_and_recorded_threshold_use_the_run_gate(self):
        self.app.config.quality.min_pass_rate = 0.5  # Settings lowered; a bench run still gates at 0.94
        seen: dict = {}
        views: list[str] = []

        def build(ctx):
            seen["threshold_at_build"] = ctx.run.gate.get("threshold")
            (ctx.workspace / "index.html").write_text("game", encoding="utf-8")

        def gate(ctx, n, *_a):
            thr = runner_module.gate_threshold(ctx)
            return {"threshold": thr, "passed": 19, "total": 20, "pass_rate": 0.95, "ok": True}

        self._patch(intake_stage=lambda ctx: {}, plan_stage=lambda ctx: {"title": "t", "acceptance_tests": []},
                    design_stage=lambda ctx: {}, build_stage=build, select_engine=lambda ctx: SimpleNamespace(name="claude_code"),
                    test_stage=lambda ctx, n: ({}, {}, {}), gate_stage=gate,
                    finalize_stage=lambda ctx, g, ok: {"number": 1})
        runner_module.show_view = lambda ctx, title, **k: views.append(title)  # restored by _patch's cleanup
        p = self.store.create("Bench labels")
        run = self.store.create_run(p.id, "generate", "a game", options={
            "bench": {"task_id": "b01-breakout", "tests_file": str(BENCH_TESTS), "min_pass_rate": 0.94}})
        Runner(self.app, run, threading.Event()).execute()
        self.assertEqual(self.store.get_run(run.id).state, "complete")
        self.assertEqual(seen["threshold_at_build"], 0.94)
        gate_lines = [t for t in self.narrations if "the gate is" in t]
        self.assertEqual(len(gate_lines), 1)
        self.assertIn("95.0 percent; the gate is 94 percent and it passed", gate_lines[0])
        self.assertEqual(views[-1], "Slide v1 passed the 94% gate")

    def test_texts_never_read_the_settings_threshold(self):
        src = (REPO / "kyora" / "pipeline" / "runner.py").read_text(encoding="utf-8")
        self.assertNotIn("min_pass_rate * 100", src)
        self.assertNotIn("float(self.cfg.quality.min_pass_rate)", src)


if __name__ == "__main__":
    unittest.main()
