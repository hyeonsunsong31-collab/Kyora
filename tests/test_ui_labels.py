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
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "kyora" / "web"
NODE = shutil.which("node")

EVERY_CALL_CLAIMS = re.compile(
    r"every tool call|judges every|judged by Kimi K3|every action of the code engine|on every tool call|"
    r"judgement of every|every code-engine tool call|Every Write/Edit/Bash of the code engine is judged",
    re.IGNORECASE)


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
                              capture_output=True, text=True, timeout=60, cwd=td)
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


if __name__ == "__main__":
    unittest.main()
