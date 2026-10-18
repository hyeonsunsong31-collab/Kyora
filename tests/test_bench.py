"""Tests for Kyora Bench: task files, browser-test files, scoring/certification, reports, runner and routes.

The runner is exercised against an in-file fake app (fake store + fake run manager publishing real
EventBus events); no model endpoint is contacted. Browser-level checks (node --check, registration in
the real kyora-test-api.js, and a headless Chromium run of the bench tests against the shipped fixture
games) are skipped when Node or Playwright is unavailable.
"""
from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora.bench import report, runner as bench_runner, scoring
from kyora.bench.routes import register_routes
from kyora.bench.runner import HARNESS_NOTES, WARNING, BenchRefused, BenchRunner
from kyora.bench.scoring import TaskResult, score, task_passed, task_rule
from kyora.pipeline.events import EventBus
from kyora.server import KyoraServer, Router

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "kyora" / "bench" / "tasks"
API_JS = REPO / "kyora" / "web" / "runner" / "kyora-test-api.js"
EXPECTED_IDS = [
    "b01-breakout", "b02-snake", "b03-pong", "b04-space-shooter", "b05-flappy", "b06-asteroids",
    "b07-2048", "b08-match-3", "b09-sokoban", "b10-minesweeper", "b11-sliding-puzzle", "b12-memory-pairs",
    "b13-platformer", "b14-endless-runner", "b15-topdown-dodge",
    "b16-tower-defense-lite", "b17-idle-clicker", "b18-life-sim",
    "b19-text-adventure", "b20-visual-novel",
    "b21-tilemap-editor", "b22-particle-sandbox",
    "b23-snake-add-pause-highscore", "b24-breakout-touch-and-bugfix",
]
CATEGORIES = {"arcade", "puzzle", "action", "strategy/sim", "narrative", "systems", "modify"}
REQUIRED_KEYS = {"id", "title", "category", "kind", "prompt", "engine", "timeout_min", "constraints", "contract",
                 "checks", "pass_rule"}
TEST_CALL_RE = re.compile(r"KyoraTest\.test\(\s*'([^']+)'")
NODE = shutil.which("node")


def load_tasks() -> dict[str, dict]:
    out = {}
    for p in sorted(TASKS.glob("*.json")):
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    return out


# ============================================================================ task files
class TaskFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = load_tasks()

    def test_exactly_the_24_expected_tasks(self):
        self.assertEqual(sorted(self.tasks), EXPECTED_IDS)
        self.assertEqual(len({t["id"] for t in self.tasks.values()}), 24)
        for name, t in self.tasks.items():
            self.assertEqual(t["id"], name)

    def test_required_keys_and_values(self):
        for tid, t in self.tasks.items():
            with self.subTest(task=tid):
                self.assertTrue(REQUIRED_KEYS.issubset(t), REQUIRED_KEYS - set(t))
                self.assertIn(t["category"], CATEGORIES)
                self.assertIn(t["kind"], ("generate", "modify"))
                self.assertEqual(t["engine"], "vanilla")
                self.assertIsInstance(t["timeout_min"], int)
                self.assertGreater(t["timeout_min"], 0)
                self.assertTrue(t["title"])
                c = t["constraints"]
                self.assertIs(c["no_external_deps"], True)
                self.assertIs(c["touch_controls"], True)
                self.assertIsInstance(c["max_files"], int)
                self.assertEqual(t["pass_rule"], {"min_pass_rate": 0.94})
                self.assertIsInstance(t["checks"]["static"], list)
                self.assertIn("js_syntax", t["checks"]["static"])

    def test_contract_requires_the_common_game_api(self):
        for tid, t in self.tasks.items():
            with self.subTest(task=tid):
                contract = t["contract"]
                self.assertEqual(contract["globals"], ["window.Game"])
                api = contract["api"]
                for key in ("Game.start()", "Game.getState()", "Game.setSeed(n)", "Game.step(n)"):
                    self.assertIn(key, api)
                self.assertIn("phase:'menu'|'play'|'over'", api["Game.getState()"])
                self.assertIn("score", api["Game.getState()"])
                self.assertTrue(any(k.startswith("Game.input.") or k in ("Game.command(text)",) for k in api),
                                "the contract needs input helpers")

    def test_prompt_is_a_150_to_300_word_brief_with_the_contract_verbatim(self):
        for tid, t in self.tasks.items():
            with self.subTest(task=tid):
                words = len(t["prompt"].split())
                self.assertGreaterEqual(words, 150)
                self.assertLessEqual(words, 300)
                for key, desc in t["contract"]["api"].items():
                    self.assertIn(f"- {key}: {desc}", t["prompt"])

    def test_browser_test_files_register_enough_unique_prefixed_ids(self):
        for tid, t in self.tasks.items():
            with self.subTest(task=tid):
                js_name = t["checks"]["browser_tests_file"]
                self.assertEqual(js_name, f"{tid}.browser-tests.js")
                text = (TASKS / js_name).read_text(encoding="utf-8")
                calls = text.count("KyoraTest.test(")
                ids = TEST_CALL_RE.findall(text)
                self.assertEqual(len(ids), calls)
                self.assertGreaterEqual(calls, 8)
                self.assertLessEqual(calls, 14)
                self.assertLessEqual(t["checks"]["min_browser_tests"], calls)
                self.assertEqual(len(set(ids)), len(ids), "test ids must be unique")
                prefix = tid.split("-", 1)[0] + "-t"
                for i, test_id in enumerate(ids, 1):
                    self.assertEqual(test_id, f"{prefix}{i:02d}")
                self.assertIn("screenshot(", text)
                self.assertNotRegex(text, r"getImageData|readPixels", "bench tests must not depend on pixels")

    def test_modify_tasks_reference_existing_fixtures(self):
        for tid, t in self.tasks.items():
            with self.subTest(task=tid):
                if t["kind"] == "modify":
                    fixture = TASKS / "fixtures" / t["fixture"]
                    self.assertTrue((fixture / "index.html").is_file())
                    self.assertTrue((fixture / "src" / "game.js").is_file())
                else:
                    self.assertNotIn("fixture", t)
        self.assertEqual(self.tasks["b23-snake-add-pause-highscore"]["fixture"], "snake-basic")
        self.assertEqual(self.tasks["b24-breakout-touch-and-bugfix"]["fixture"], "breakout-keyboard")

    def test_fixtures_are_the_unmodified_starting_points(self):
        snake = (TASKS / "fixtures" / "snake-basic" / "src" / "game.js").read_text(encoding="utf-8")
        self.assertNotIn("togglePause", snake)
        self.assertNotIn("localStorage", snake)
        breakout = (TASKS / "fixtures" / "breakout-keyboard" / "src" / "game.js").read_text(encoding="utf-8")
        self.assertNotIn("touchstart", breakout)
        self.assertIn("var right = left + halfW;", breakout)  # the seeded collision bug described in b24

    @unittest.skipUnless(NODE, "node not installed")
    def test_node_check_every_javascript_file(self):
        files = sorted(TASKS.glob("*.js")) + sorted((TASKS / "fixtures").glob("*/src/*.js"))
        self.assertGreaterEqual(len(files), 26)
        for f in files:
            with self.subTest(file=f.name):
                proc = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(NODE, "node not installed")
    def test_files_register_their_ids_in_the_kyora_test_api(self):
        script = r"""
const fs = require('fs'), vm = require('vm');
const [api, ...files] = process.argv.slice(1);
const STUB = "window.KyoraTest = { __tests: [], test: function (id, name, fn) {" +
  " this.__tests.push({ id: id, name: name, problem: typeof fn === 'function' ? null : 'no fn' }); }," +
  " expect: function () {}, input: {} };";
const out = {};
for (const f of files) {
  const win = { addEventListener() {}, setTimeout, clearTimeout, Promise, performance: { now: () => Date.now() } };
  win.window = win;
  win.document = { querySelector: () => null, documentElement: {} };
  const ctx = vm.createContext(win);
  vm.runInContext(api && fs.existsSync(api) ? fs.readFileSync(api, 'utf8') : STUB, ctx, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(f, 'utf8'), ctx, { filename: f });
  out[f] = ctx.KyoraTest.__tests.map((t) => ({ id: t.id, problem: t.problem || null, fn: typeof t.fn }));
}
console.log(JSON.stringify(out));
"""
        files = [str(p) for p in sorted(TASKS.glob("*.browser-tests.js"))]
        proc = subprocess.run([NODE, "-e", script, str(API_JS), *files], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        registered = json.loads(proc.stdout)
        for f in files:
            with self.subTest(file=Path(f).name):
                expected = TEST_CALL_RE.findall(Path(f).read_text(encoding="utf-8"))
                got = registered[f]
                self.assertEqual([t["id"] for t in got], expected)
                self.assertEqual([t["problem"] for t in got], [None] * len(expected))


# ============================================================================ browser run (optional)
_BROWSER_SCRIPT = r"""
const http = require('http'), fs = require('fs'), path = require('path');
const [apiFile, jobsJson] = process.argv.slice(2);
let playwright;
try { playwright = require('playwright'); } catch (e) { console.log(JSON.stringify({ skip: 'playwright not installed' })); process.exit(0); }
const jobs = JSON.parse(jobsJson);
const TYPES = { '.html': 'text/html', '.js': 'text/javascript' };
(async () => {
  let job = null;
  const server = http.createServer((req, res) => {
    const url = decodeURIComponent(req.url.split('?')[0]);
    const file = url === '/__api.js' ? apiFile : url === '/__tests.js' ? job.tests : path.join(job.game, url);
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    res.end(fs.readFileSync(file));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try { browser = await playwright.chromium.launch(); } catch (e) {
    console.log(JSON.stringify({ skip: 'chromium unavailable: ' + String(e).slice(0, 200) })); server.close(); return;
  }
  const out = [];
  for (job of jobs) {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 760 }, hasTouch: true });
    const page = await ctx.newPage();
    await page.goto('http://127.0.0.1:' + server.address().port + '/index.html');
    await page.waitForTimeout(200);
    await page.addScriptTag({ url: '/__api.js' });
    await page.addScriptTag({ url: '/__tests.js' });
    const r = await page.evaluate(() => window.KyoraTest.__run({ timeoutMs: 10000 }));
    out.push(r.results.map((x) => ({ id: x.id, status: x.status, error: x.error })));
    await ctx.close();
  }
  await browser.close();
  server.close();
  console.log(JSON.stringify({ results: out }));
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
"""


def _npm_root() -> str | None:
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        return subprocess.run([npm, "root", "-g"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@unittest.skipUnless(NODE and API_JS.is_file(), "node or kyora-test-api.js missing")
@unittest.skipIf(os.environ.get("KYORA_SKIP_BROWSER") == "1", "KYORA_SKIP_BROWSER=1")
class FixtureBrowserTests(unittest.TestCase):
    """Runs bench test files in headless Chromium with the real KyoraTest API against the shipped fixtures."""

    def test_bench_tests_against_fixture_games(self):
        fx = TASKS / "fixtures"
        jobs = [
            {"game": str(fx / "snake-basic"), "tests": str(TASKS / "b02-snake.browser-tests.js")},
            {"game": str(fx / "breakout-keyboard"), "tests": str(TASKS / "b01-breakout.browser-tests.js")},
            {"game": str(fx / "breakout-keyboard"), "tests": str(TASKS / "b24-breakout-touch-and-bugfix.browser-tests.js")},
            {"game": str(fx / "snake-basic"), "tests": str(TASKS / "b23-snake-add-pause-highscore.browser-tests.js")},
        ]
        env = dict(os.environ)
        root = _npm_root()
        if root:
            env["NODE_PATH"] = os.pathsep.join(x for x in (env.get("NODE_PATH"), root) if x)
        if Path("/opt/pw-browsers").is_dir():
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "run.js"
            script.write_text(_BROWSER_SCRIPT, encoding="utf-8")
            proc = subprocess.run([NODE, str(script), str(API_JS), json.dumps(jobs)], capture_output=True, text=True, encoding="utf-8",
                                  timeout=120, env=env, cwd=td)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.fail(f"browser runner produced no JSON: {proc.stdout[-500:]} {proc.stderr[-500:]}")
        if "skip" in out:
            self.skipTest(out["skip"])
        self.assertNotIn("error", out)
        snake, breakout, b24, b23 = out["results"]

        def status(results):
            return {r["id"]: r["status"] for r in results}

        # the fixture snake implements the b02 contract exactly
        self.assertEqual(set(status(snake).values()), {"pass"}, snake)
        # the keyboard-only breakout implements b01 except touch
        self.assertEqual([i for i, s in status(breakout).items() if s == "fail"], ["b01-t13"], breakout)
        # b24: untouched fixture still fails the bug-fix and touch tests, passes the regression tests
        st = status(b24)
        for must_fail in ("b24-t04", "b24-t05", "b24-t08", "b24-t09", "b24-t10", "b24-t11"):
            self.assertEqual(st[must_fail], "fail", must_fail)
        for must_pass in ("b24-t01", "b24-t02", "b24-t03", "b24-t06", "b24-t07", "b24-t12", "b24-t13"):
            self.assertEqual(st[must_pass], "pass", (must_pass, b24))
        # b23: untouched fixture has no pause/high score yet
        st = status(b23)
        self.assertEqual(st["b23-t03"], "pass")
        self.assertEqual(st["b23-t04"], "fail")
        self.assertEqual(st["b23-t09"], "fail")


# ============================================================================ scoring
def _result(task_id: str, ok: bool = True, state: str = "complete", model: str | None = "claude-opus-5-5",
            engine: str = "claude_code", gate_threshold: float = 0.94, pass_rate: float | None = None,
            **kw) -> TaskResult:
    gate = {"threshold": gate_threshold, "final": {"pass_rate": pass_rate or (1.0 if ok else 0.8),
                                                   "passed": 20 if ok else 16, "total": 20, "ok": ok,
                                                   "required_failures": [], "threshold": gate_threshold}}
    r = TaskResult(task_id=task_id, state=state, gate=gate, engine=engine, active_model=model, **kw)
    r.evaluate()
    return r


class ScoringTests(unittest.TestCase):
    def test_task_pass_rule(self):
        ok_gate = {"final": {"ok": True, "pass_rate": 0.95, "threshold": 0.94}}
        self.assertTrue(task_passed("complete", ok_gate))
        self.assertFalse(task_passed("failed_gate", ok_gate))
        self.assertFalse(task_passed("blocked", ok_gate))
        self.assertFalse(task_passed("complete", {"final": {"ok": False, "pass_rate": 0.99}}))
        self.assertFalse(task_passed("complete", {"final": None}))
        self.assertFalse(task_passed("complete", None))
        self.assertFalse(task_passed("complete", {"final": {"ok": "true"}}), "ok must be the boolean True")
        self.assertFalse(task_passed("complete", ok_gate, bench_tests_seen=5, min_browser_tests=13))
        self.assertTrue(task_passed("complete", ok_gate, bench_tests_seen=13, min_browser_tests=13))
        self.assertTrue(task_passed("complete", ok_gate, bench_tests_seen=None, min_browser_tests=13))

    def test_task_pass_rule_ignores_a_lowered_settings_gate(self):
        # Settings > Quality gate at 0.5: the gate said ok at 60 %, the bench does not
        self.assertFalse(task_passed("complete", {"final": {"ok": True, "pass_rate": 0.6, "threshold": 0.5}}, 14, 14))
        self.assertFalse(task_passed("complete", {"final": {"ok": True, "pass_rate": 0.97, "threshold": 0.5}}))
        self.assertFalse(task_passed("complete", {"final": {"ok": True, "pass_rate": 0.9, "threshold": 0.94}}))
        self.assertFalse(task_passed("complete", {"final": {"ok": True, "pass_rate": 0.97}}), "threshold unrecorded")
        # the task's own rule applies when it is stricter, never a looser one
        gate = {"final": {"ok": True, "pass_rate": 0.95, "threshold": 0.94}}
        self.assertFalse(task_passed("complete", gate, min_pass_rate=0.97))
        self.assertTrue(task_passed("complete", gate, min_pass_rate=0.5))
        self.assertEqual((task_rule(0.5), task_rule(0.97), task_rule(None), task_rule("x")), (0.94, 0.97, 0.94, 0.94))
        sc = score([_result(t, gate_threshold=0.5, pass_rate=0.6) for t in EXPECTED_IDS])
        self.assertEqual(sc.passed, 0)
        self.assertFalse(sc.certified)

    def test_all_24_passing_on_one_model_is_certified(self):
        sc = score([_result(t) for t in EXPECTED_IDS])
        self.assertTrue(sc.certified)
        self.assertEqual((sc.passed, sc.run, sc.pass_rate), (24, 24, 1.0))
        self.assertTrue(sc.suite_complete)
        self.assertEqual((sc.engine, sc.model), ("claude_code", "claude-opus-5-5"))
        self.assertEqual(sc.reason, "")

    def test_23_of_24_is_certified(self):
        results = [_result(t, ok=(i != 5)) for i, t in enumerate(EXPECTED_IDS)]
        sc = score(results)
        self.assertEqual(sc.passed, 23)
        self.assertEqual(sc.pass_rate, 0.9583)
        self.assertGreaterEqual(sc.pass_rate, 0.94)
        self.assertTrue(sc.certified)

    def test_22_of_24_is_not_certified(self):
        results = [_result(t, ok=(i not in (3, 9))) for i, t in enumerate(EXPECTED_IDS)]
        sc = score(results)
        self.assertEqual(sc.passed, 22)
        self.assertEqual(sc.pass_rate, 0.9166)
        self.assertFalse(sc.certified)
        self.assertIn("below the 94% threshold", sc.reason)

    def test_incomplete_suite_is_not_certified_even_at_100_percent(self):
        sc = score([_result(t) for t in EXPECTED_IDS[:23]])
        self.assertEqual(sc.pass_rate, 1.0)
        self.assertFalse(sc.suite_complete)
        self.assertFalse(sc.certified)
        self.assertIn("suite incomplete: 23/24", sc.reason)

    def test_duplicates_do_not_complete_the_suite(self):
        results = [_result(t) for t in EXPECTED_IDS[:23]] + [_result(EXPECTED_IDS[0])]
        sc = score(results)
        self.assertEqual(sc.run, 23)
        self.assertFalse(sc.certified)
        # the last result for an id counts
        sc2 = score([_result("b01-breakout", ok=True), _result("b01-breakout", ok=False)])
        self.assertEqual(sc2.passed, 0)

    def test_suite_ids_must_match_when_given(self):
        results = [_result(t) for t in EXPECTED_IDS[:23]] + [_result("b99-other")]
        self.assertTrue(score(results).suite_complete)
        self.assertFalse(score(results, suite_ids=EXPECTED_IDS).suite_complete)

    def test_mixed_models_are_not_certified(self):
        results = [_result(t, model="claude-opus-5-5" if i else "claude-sonnet-5-5") for i, t in enumerate(EXPECTED_IDS)]
        sc = score(results)
        self.assertEqual(sc.passed, 24)
        self.assertFalse(sc.certified)
        self.assertIn("more than one engine/model", sc.reason)
        self.assertEqual(len(sc.models_seen), 2)

    def test_a_task_served_by_two_models_blocks_certification(self):
        # b07's BUILD fell back to opus, its FIX ran on fable: the last call alone would look fable-only
        served = {"b07-2048": ["claude_code/claude-opus-5-5", "claude_code/claude-fable-5-1"]}
        results = [_result(t, model="claude-fable-5-1", served=served.get(t, ["claude_code/claude-fable-5-1"]))
                   for t in EXPECTED_IDS]
        sc = score(results)
        self.assertEqual(sc.passed, 24)
        self.assertFalse(sc.certified)
        self.assertIn("mixed within b07-2048", sc.reason)
        self.assertEqual(sc.models_seen, ["claude_code/claude-fable-5-1", "claude_code/claude-opus-5-5"])
        same = score([_result(t, model="claude-fable-5-1", served=["claude_code/claude-fable-5-1"])
                      for t in EXPECTED_IDS])
        self.assertTrue(same.certified)
        self.assertEqual((same.engine, same.model), ("claude_code", "claude-fable-5-1"))

    def test_passing_task_without_a_model_blocks_certification(self):
        results = [_result(t) for t in EXPECTED_IDS[:23]] + [_result(EXPECTED_IDS[23], model=None)]
        sc = score(results)
        self.assertFalse(sc.certified)
        self.assertIn("without a recorded model", sc.reason)

    def test_empty_and_dict_inputs(self):
        sc = score([])
        self.assertFalse(sc.certified)
        self.assertEqual(sc.reason, "no tasks were run")
        self.assertEqual(sc.pass_rate, 0.0)
        sc2 = score([_result(t).to_dict() for t in EXPECTED_IDS], timestamp="2026-10-15T00:00:00Z")
        self.assertTrue(sc2.certified)
        d = sc2.to_dict()
        for key in ("pass_rate", "passed", "run", "suite_complete", "certified", "engine", "model", "timestamp", "reason"):
            self.assertIn(key, d)
        self.assertEqual(d["timestamp"], "2026-10-15T00:00:00Z")

    def test_task_result_round_trip(self):
        r = _result("b01-breakout", duration_s=12.5, run_id="r_1")
        again = TaskResult.from_dict({**r.to_dict(), "unknown": 1})
        self.assertEqual(again, r)
        self.assertTrue(again.passed)


# ============================================================================ report
class ReportTests(unittest.TestCase):
    def test_json_and_standalone_escaped_html(self):
        results = [_result("b01-breakout", duration_s=754.2, title="Breakout", category="arcade"),
                   _result("b02-snake", ok=False, state="failed_gate", error="<script>alert(1)</script> & co",
                           title="Snake", category="arcade")]
        sc = score(results, timestamp="2026-10-15T08:00:00Z")
        payload = {"bench_id": "20261015-080000", "started_at": "2026-10-15T07:00:00Z", "finished_at": "2026-10-15T08:00:00Z",
                   "suite_size": 24, "tasks": [r.to_dict() for r in results], "score": sc.to_dict(), "skipped": ["b03-pong"]}
        with tempfile.TemporaryDirectory() as td:
            jp = report.write_json(Path(td) / "out" / "results.json", payload)
            self.assertEqual(json.loads(jp.read_text(encoding="utf-8"))["bench_id"], "20261015-080000")
            hp = report.write_html(Path(td) / "out" / "report.html", payload)
            html = hp.read_text(encoding="utf-8")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; co", html)
        self.assertNotRegex(html, r"https?://")
        self.assertIn("Measured by Kyora Bench on 2026-10-15 with claude_code/claude-opus-5-5 on this machine. "
                      "No number here is estimated.", html)
        self.assertIn("100.0% (20/20)", html)
        self.assertIn("80.0% (16/20)", html)
        self.assertIn("12:34", html)
        self.assertIn("not certified", html)
        self.assertIn("b03-pong", html)
        self.assertIn("b02-snake", html)

    def test_certified_badge_only_when_certified(self):
        sc = score([_result(t) for t in EXPECTED_IDS], timestamp="2026-10-15T08:00:00Z")
        html = report.render_html({"tasks": [], "score": sc.to_dict()})
        self.assertIn("Kyora Bench certified", html)
        self.assertNotIn("not certified", html)


# ============================================================================ fake app for the runner
class FakeStore:
    """Minimal ProjectStore stand-in recording what the bench submits."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.projects: dict = {}
        self.runs: dict = {}
        self.attachments: dict = {}
        self.lock = threading.Lock()

    def create(self, name):
        with self.lock:
            pid = f"p_{len(self.projects) + 1:03d}"
            self.projects[pid] = SimpleNamespace(id=pid, name=name)
            return self.projects[pid]

    def add_attachment(self, pid, filename, mime, data, role="reference"):
        if role not in ("reference", "import"):
            raise ValueError(f"invalid attachment role {role!r}")
        with self.lock:
            aid = f"a_{len(self.attachments) + 1:03d}"
            self.attachments[aid] = SimpleNamespace(id=aid, name=filename, mime=mime, data=data, role=role, project_id=pid)
            return self.attachments[aid]

    def create_run(self, pid, kind, prompt, parent_slide=None, attachment_ids=None, options=None):
        with self.lock:
            rid = f"r_{len(self.runs) + 1:03d}"
            self.runs[rid] = SimpleNamespace(id=rid, project_id=pid, kind=kind, prompt=prompt, parent_slide=parent_slide,
                                             attachment_ids=list(attachment_ids or []), options=options or {},
                                             state="created", gate={"threshold": 0.94, "final": None}, engine={},
                                             error=None, blocked_reason=None)
            return self.runs[rid]

    def get_run(self, rid):
        return self.runs[rid]

    def run_dir(self, run):
        d = self.root / "runs" / (run if isinstance(run, str) else run.id)
        d.mkdir(parents=True, exist_ok=True)
        return d


class FakeRunManager:
    """Finishes submitted runs in a thread and publishes the terminal event on the real EventBus.

    ``outcome(task_id)`` returns ``(state, gate_ok, model)``; ``hang=True`` never finishes a run until cancel().
    """

    def __init__(self, app, outcome=None, hang=False, delay=0.01, checks_on_disk=False, drop_bench_tests=False,
                 engine_runs=None):
        self.app = app
        self.engine_runs = engine_runs
        self.outcome = outcome or (lambda tid: ("complete", True, "claude-opus-5-5"))
        self.hang = hang
        self.delay = delay
        self.checks_on_disk = checks_on_disk
        self.drop_bench_tests = drop_bench_tests
        self.submitted: list = []
        self.cancelled: list = []

    def submit(self, rid):
        self.submitted.append(rid)
        if not self.hang:
            threading.Thread(target=self._finish, args=(rid,), daemon=True).start()

    def _finish(self, rid):
        time.sleep(self.delay)
        run = self.app.store.get_run(rid)
        bench = run.options["bench"]
        state, ok, model = self.outcome(bench["task_id"])
        ids = bench["test_ids"][: 3 if self.drop_bench_tests else None]
        checks = [{"id": i, "group": "browser", "status": "pass" if ok else "fail", "required": False} for i in ids]
        checks.append({"id": "file_exists:index.html", "group": "static", "status": "pass", "required": True})
        passed = sum(1 for c in checks if c["status"] == "pass")
        final = {"threshold": 0.94, "passed": passed, "total": len(checks), "pass_rate": passed / len(checks), "ok": ok,
                 "required_failures": [], "missing_spec_tests": []}
        if self.checks_on_disk:
            d = self.app.store.run_dir(run) / "iter-1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "gate.json").write_text(json.dumps({**final, "checks": checks}), encoding="utf-8")
        else:
            final["checks"] = checks
        run.gate = {"threshold": 0.94, "final": final}
        run.engine = {"backend": "claude_code", "active_model": model, "fallback_used": False}
        if self.engine_runs is not None:
            run.extra = {"engine_runs": self.engine_runs(bench["task_id"])}
        run.state = state
        etype = {"complete": "run.completed", "failed_gate": "run.completed", "blocked": "run.blocked"}.get(state, "run.failed")
        self.app.bus.publish(etype, {"state": state}, run_id=rid, project_id=run.project_id)

    def cancel(self, rid):
        self.cancelled.append(rid)
        run = self.app.store.get_run(rid)
        run.state = "cancelled"
        self.app.bus.publish("run.cancelled", {}, run_id=rid, project_id=run.project_id)
        return True


_OPEN_BUSES: list = []


def make_app(home: Path, engine: bool = True, **rm_kwargs):
    app = SimpleNamespace(home=Path(home), bus=EventBus(None), platform={"kind": "test"}, missing={})
    _OPEN_BUSES.append(app.bus)
    app.store = FakeStore(Path(home) / "store")
    app.run_manager = FakeRunManager(app, **rm_kwargs)
    eng = SimpleNamespace(name="claude_code") if engine else None
    app.engine = lambda: eng
    return app


def wait_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class _HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KYORA_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        while _OPEN_BUSES:
            _OPEN_BUSES.pop().close()
        self._env.stop()
        self.tmp.cleanup()


# ============================================================================ runner
class RunnerTests(_HomeCase):
    def test_list_load_and_estimate(self):
        b = BenchRunner(make_app(self.home))
        tasks = b.list_tasks()
        self.assertEqual([t["id"] for t in tasks], EXPECTED_IDS)
        for t in tasks:
            self.assertTrue({"id", "title", "category", "kind", "timeout_min", "min_browser_tests"}.issubset(t))
        self.assertEqual(b.load_task("b07-2048")["title"], "2048")
        for bad in ("b99-nope", "../app", "b01-breakout/../../x", ""):
            with self.assertRaises(FileNotFoundError):
                b.load_task(bad)
        est = b.estimate()
        self.assertEqual(est["count"], 24)
        self.assertEqual(est["pipeline_runs"], 24)
        self.assertEqual(est["tasks"], EXPECTED_IDS)
        self.assertEqual(est["warning"], WARNING)
        self.assertIs(est["requires_engine"], True)
        self.assertIs(est["engine_available"], True)
        self.assertEqual(est["engine"], "claude_code")
        self.assertTrue(est["certifiable"])
        two = b.estimate("b01,b02-snake")
        self.assertEqual(two["tasks"], ["b01-breakout", "b02-snake"])
        self.assertFalse(two["certifiable"])
        bad = b.estimate(["b01-breakout", "zzz"])
        self.assertEqual(bad["unknown"], ["zzz"])
        self.assertIs(BenchRunner(make_app(self.home, engine=False)).estimate()["engine_available"], False)

    def test_tests_file_and_fixture_zip(self):
        b = BenchRunner(make_app(self.home))
        task = b.load_task("b23-snake-add-pause-highscore")
        path = b.tests_file_path(task)
        self.assertTrue(path.is_absolute() and path.is_file())
        self.assertEqual(path.name, "b23-snake-add-pause-highscore.browser-tests.js")
        data = b.fixture_zip("snake-basic")
        self.assertEqual(data, b.fixture_zip("snake-basic"), "fixture zips must be byte-for-byte reproducible")
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("index.html", names)
        self.assertIn("src/game.js", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.startswith("/")])
        with self.assertRaises(ValueError):
            b.fixture_zip("../tasks")
        with self.assertRaises(FileNotFoundError):
            b.fixture_zip("no-such-fixture")
        self.assertEqual(len(b.browser_test_ids(task)), 12)
        prompt = b.compose_prompt(task)
        self.assertTrue(prompt.startswith(task["prompt"]))
        self.assertIn(HARNESS_NOTES, prompt)

    def test_tests_file_is_extracted_when_resources_are_zipped(self):
        b = BenchRunner(make_app(self.home))
        task = b.load_task("b01-breakout")
        name = "b01-breakout.browser-tests.js"
        zpath = self.home / "kyora.pyz"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(f"kyora/bench/tasks/{name}", (TASKS / name).read_bytes())

        def fake_resource(*parts):
            return zipfile.Path(zpath, "kyora/bench/tasks/" + "/".join(parts))

        with mock.patch.object(bench_runner, "_resource", fake_resource):
            path = b.tests_file_path(task)
        self.assertEqual(path, (self.home / "bench" / "tasks-cache" / name).resolve())
        self.assertEqual(path.read_bytes(), (TASKS / name).read_bytes())

    def test_refusals(self):
        app = make_app(self.home)
        b = BenchRunner(app)
        res = b.run(["b01-breakout"], confirm=False, cancel=threading.Event())
        self.assertIn("confirmation required", res["error"])
        self.assertEqual(app.run_manager.submitted, [])
        res = BenchRunner(make_app(self.home, engine=False)).run(None, confirm=True, cancel=threading.Event())
        self.assertIn("no code engine", res["error"])
        res = b.run(["nope"], confirm=True, cancel=threading.Event())
        self.assertIn("unknown bench task", res["error"])
        with self.assertRaises(BenchRefused) as ctx:
            b.start_async(["b01-breakout"], confirm=False)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIsNone(b.last_results())
        self.assertEqual(b.badge()["reason"], "no bench run yet")
        self.assertFalse(b.badge()["certified"])
        self.assertIsNone(b.badge()["pass_rate"])

    def test_refuses_while_running(self):
        app = make_app(self.home, hang=True)
        b = BenchRunner(app, poll_s=0.05)
        t = b.start_async(["b01-breakout"], confirm=True)
        self.assertTrue(wait_until(lambda: app.run_manager.submitted))
        self.assertTrue(b.is_running())
        res = b.run(["b02-snake"], confirm=True, cancel=threading.Event())
        self.assertIn("already in progress", res["error"])
        with self.assertRaises(BenchRefused) as ctx:
            b.start_async(["b02-snake"], confirm=True)
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(b.cancel())
        t.join(10)
        self.assertFalse(b.is_running())
        self.assertFalse(b.cancel())

    def test_run_two_tasks_writes_results_and_report(self):
        app = make_app(self.home)
        sub = app.bus.subscribe(types=["bench.*"])
        b = BenchRunner(app, poll_s=0.05)
        res = b.run(["b01-breakout", "b23-snake-add-pause-highscore"], confirm=True, cancel=threading.Event())
        self.assertNotIn("error", res)
        out_dir = Path(res["results_dir"])
        self.assertEqual(out_dir.parent, self.home / "bench")
        self.assertTrue((out_dir / "results.json").is_file())
        self.assertTrue((out_dir / "report.html").is_file())
        saved = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["in_progress"])
        self.assertEqual([t["task_id"] for t in saved["tasks"]], ["b01-breakout", "b23-snake-add-pause-highscore"])
        for t in saved["tasks"]:
            self.assertTrue(t["passed"], t)
            self.assertEqual(t["state"], "complete")
            self.assertEqual(t["engine"], "claude_code")
            self.assertEqual(t["active_model"], "claude-opus-5-5")
            self.assertEqual(t["bench_tests_seen"], t["min_browser_tests"])
            self.assertTrue(t["run_id"] and t["project_id"])
            self.assertIsInstance(t["duration_s"], float)
        self.assertEqual(saved["score"]["passed"], 2)
        self.assertFalse(saved["score"]["certified"])
        self.assertIn("suite incomplete", saved["score"]["reason"])
        # what the pipeline received
        runs = list(app.store.runs.values())
        self.assertEqual(len(runs), 2)
        self.assertTrue(app.store.projects["p_001"].name.startswith("bench-b01-breakout-"))
        opts = runs[0].options
        self.assertEqual(opts["bench"]["task_id"], "b01-breakout")
        self.assertEqual(opts["bench"]["min_pass_rate"], 0.94)  # the gate threshold, whatever Settings say
        self.assertEqual([t["threshold"] for t in saved["tasks"]], [0.94, 0.94])
        self.assertEqual([t["min_pass_rate"] for t in saved["tasks"]], [0.94, 0.94])
        self.assertTrue(Path(opts["bench"]["tests_file"]).is_file())
        self.assertEqual(len(opts["bench"]["test_ids"]), 14)
        self.assertIsNone(opts["import_zip_attachment"])
        self.assertEqual(runs[0].kind, "generate")
        self.assertIn(HARNESS_NOTES, runs[0].prompt)
        modify = runs[1]
        self.assertEqual(modify.kind, "modify")
        att = app.store.attachments[modify.options["import_zip_attachment"]]
        self.assertEqual(att.role, "import")
        self.assertEqual(modify.attachment_ids, [att.id])
        self.assertIn("src/game.js", zipfile.ZipFile(io.BytesIO(att.data)).namelist())
        # events
        events = []
        while True:
            try:
                ev = sub.queue.get(timeout=0.2)
            except queue.Empty:
                break
            if ev is None:
                break
            events.append(ev)
        sub.close()
        progress = [e.data for e in events if e.type == "bench.progress"]
        self.assertEqual([(p["done"], p["total"], p["task_id"], p["passed"]) for p in progress],
                         [(1, 2, "b01-breakout", True), (2, 2, "b23-snake-add-pause-highscore", True)])
        done = [e.data for e in events if e.type == "bench.done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["score"]["passed"], 2)
        # latest results and badge
        self.assertEqual(b.last_results()["bench_id"], saved["bench_id"])
        badge = b.badge()
        self.assertFalse(badge["certified"])
        self.assertTrue(badge["measured"])
        self.assertEqual(badge["pass_rate"], 1.0)
        self.assertEqual(b.last_report_path(), out_dir / "report.html")

    def test_full_suite_certification_from_a_measured_run(self):
        failing = {"b05-flappy"}
        app = make_app(self.home, delay=0.0,
                       outcome=lambda tid: ("failed_gate" if tid in failing else "complete", tid not in failing,
                                            "claude-opus-5-5"))
        b = BenchRunner(app, poll_s=0.05)
        res = b.run(None, confirm=True, cancel=threading.Event())
        self.assertEqual(res["score"]["run"], 24)
        self.assertEqual(res["score"]["passed"], 23)
        self.assertTrue(res["score"]["certified"])
        self.assertTrue(b.badge()["certified"])
        failed = [t for t in res["tasks"] if not t["passed"]]
        self.assertEqual([t["task_id"] for t in failed], ["b05-flappy"])
        self.assertEqual(failed[0]["state"], "failed_gate")
        # a second, worse run replaces the badge
        failing.add("b06-asteroids")
        time.sleep(1.1)  # distinct results directory timestamp
        res2 = b.run(None, confirm=True, cancel=threading.Event())
        self.assertFalse(res2["score"]["certified"])
        self.assertEqual(res2["score"]["pass_rate"], 0.9166)
        badge = b.badge()
        self.assertFalse(badge["certified"])
        self.assertIn("below the 94% threshold", badge["reason"])

    def test_every_engine_call_decides_the_served_models(self):
        calls = {"b01-breakout": [{"kind": "build", "backend": "claude_code", "active_model": "claude-opus-5-5"},
                                  {"kind": "fix", "backend": "claude_code", "active_model": "claude-fable-5-1"}],
                 "b02-snake": [{"kind": "build", "backend": "claude_code", "active_model": "claude-fable-5-1"},
                               {"kind": "fix", "backend": "claude_code", "active_model": None, "ok": False}]}
        app = make_app(self.home, engine_runs=lambda tid: calls[tid],
                       outcome=lambda tid: ("complete", True, "claude-fable-5-1"))
        res = BenchRunner(app, poll_s=0.05).run(["b01-breakout", "b02-snake"], confirm=True, cancel=threading.Event())
        t1, t2 = res["tasks"]
        self.assertEqual(t1["served"], ["claude_code/claude-opus-5-5", "claude_code/claude-fable-5-1"])
        self.assertEqual(t1["active_model"], "claude-opus-5-5 + claude-fable-5-1")
        self.assertEqual(t2["served"], ["claude_code/claude-fable-5-1"])
        self.assertIn("mixed within b01-breakout", res["score"]["reason"])

    def test_missing_bench_tests_in_gate_fail_the_task(self):
        app = make_app(self.home, drop_bench_tests=True, checks_on_disk=True)
        res = BenchRunner(app, poll_s=0.05).run(["b02-snake"], confirm=True, cancel=threading.Event())
        t = res["tasks"][0]
        self.assertEqual(t["state"], "complete")
        self.assertEqual(t["bench_tests_seen"], 3)
        self.assertFalse(t["passed"])
        self.assertIn("appear in the gate results", t["error"])

    def test_cancel_stops_the_bench(self):
        app = make_app(self.home, hang=True)
        sub = app.bus.subscribe(types=["bench.done"])
        b = BenchRunner(app, poll_s=0.05)
        t = b.start_async(["b01-breakout", "b02-snake", "b03-pong"], confirm=True)
        self.assertTrue(wait_until(lambda: app.run_manager.submitted))
        self.assertTrue(b.status()["running"])
        self.assertEqual(b.status()["current"]["task_id"], "b01-breakout")
        self.assertTrue(b.cancel())
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(app.run_manager.cancelled, app.run_manager.submitted)
        last = b.last_results()
        self.assertTrue(last["cancelled"])
        self.assertEqual(last["skipped"], ["b02-snake", "b03-pong"])
        self.assertEqual(len(last["tasks"]), 1)
        self.assertEqual(last["tasks"][0]["state"], "cancelled")
        self.assertEqual(last["tasks"][0]["error"], "bench cancelled")
        self.assertFalse(last["tasks"][0]["passed"])
        ev = sub.queue.get(timeout=2)
        sub.close()
        self.assertTrue(ev.data["cancelled"])

    def test_task_timeout_cancels_the_run(self):
        app = make_app(self.home, hang=True)
        b = BenchRunner(app, poll_s=0.02)
        original = b.load_task

        def short_timeout(tid):
            task = original(tid)
            task["timeout_min"] = 0.005  # 0.3 s
            return task

        with mock.patch.object(b, "load_task", short_timeout):
            res = b.run(["b04-space-shooter"], confirm=True, cancel=threading.Event())
        t = res["tasks"][0]
        self.assertEqual(t["state"], "cancelled")
        self.assertTrue(t["error"].startswith("timed out after"))
        self.assertFalse(t["passed"])
        self.assertEqual(app.run_manager.cancelled, app.run_manager.submitted)

    def test_store_failure_is_recorded_not_raised(self):
        app = make_app(self.home)

        def boom(*a, **k):
            raise RuntimeError("disk full")

        app.store.create_run = boom
        with self.assertLogs("kyora.bench", level="ERROR"):
            res = BenchRunner(app, poll_s=0.05).run(["b01-breakout"], confirm=True, cancel=threading.Event())
        t = res["tasks"][0]
        self.assertEqual(t["state"], "error")
        self.assertIn("disk full", t["error"])
        self.assertFalse(t["passed"])


# ============================================================================ routes
class RouteTests(_HomeCase):
    def setUp(self):
        super().setUp()
        self.app = make_app(self.home, hang=True)
        self.app.bench = BenchRunner(self.app, poll_s=0.05)
        router = Router()
        register_routes(router, self.app)
        self.server = KyoraServer(self.app, "127.0.0.1", 0, router)
        self.server.start()

    def tearDown(self):
        if self.app.bench is not None and self.app.bench.is_running():
            self.app.bench.cancel()
            wait_until(lambda: not self.app.bench.is_running())
        self.server.shutdown()
        super().tearDown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.server.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def jcall(self, method, path, body=None):
        status, _, raw = self.call(method, path, body)
        return status, json.loads(raw.decode("utf-8"))

    def test_routes_end_to_end(self):
        status, body = self.jcall("GET", "/api/bench/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["tasks"]), 24)
        self.assertEqual(body["estimate"]["warning"], WARNING)
        self.assertFalse(body["running"])

        self.assertEqual(self.jcall("GET", "/api/bench/results"),
                         (200, {"results": None, "tasks": [], "running": False,
                                "status": {"running": False, "current": None, "progress": None}}))
        status, badge = self.jcall("GET", "/api/bench/badge")
        self.assertEqual((status, badge["certified"], badge["reason"]), (200, False, "no bench run yet"))
        self.assertEqual(self.call("GET", "/api/bench/report")[0], 404)

        status, body = self.jcall("POST", "/api/bench/run", {"task_ids": ["b01-breakout"]})
        self.assertEqual(status, 400)
        self.assertIn("confirmation required", body["error"])
        status, body = self.jcall("POST", "/api/bench/run", {"task_ids": "b01,b02", "confirm": "yes"})
        self.assertEqual(status, 400, "only the JSON boolean true confirms")
        status, body = self.jcall("POST", "/api/bench/run", {"task_ids": 5, "confirm": True})
        self.assertEqual(status, 400)

        status, body = self.jcall("POST", "/api/bench/run", {"task_ids": ["b01-breakout", "b02-snake"], "confirm": True})
        self.assertEqual(status, 202)
        self.assertTrue(body["started"])
        self.assertEqual(body["estimate"]["count"], 2)
        self.assertTrue(wait_until(lambda: self.app.run_manager.submitted))
        status, body = self.jcall("POST", "/api/bench/run", {"confirm": True})
        self.assertEqual(status, 409)
        status, body = self.jcall("POST", "/api/bench/cancel", {})
        self.assertEqual((status, body["cancelled"]), (200, True))
        self.assertTrue(wait_until(lambda: not self.app.bench.is_running()))

        status, body = self.jcall("GET", "/api/bench/results")
        self.assertEqual(status, 200)
        self.assertTrue(body["results"]["cancelled"])
        self.assertEqual(body["results"]["skipped"], ["b02-snake"])
        self.assertEqual([t["task_id"] for t in body["tasks"]], ["b01-breakout"])
        status, badge = self.jcall("GET", "/api/bench/badge")
        self.assertFalse(badge["certified"])
        self.assertTrue(badge["measured"])
        status, ctype, raw = self.call("GET", "/api/bench/report")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"No number here is estimated.", raw)

    def test_no_engine_is_refused(self):
        self.app.engine = lambda: None
        status, body = self.jcall("POST", "/api/bench/run", {"confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("no code engine", body["error"])
        self.assertFalse(body["estimate"]["engine_available"])

    def test_missing_bench_component_is_503(self):
        self.app.bench = None
        self.app.missing["bench"] = "ImportError: boom"
        status, body = self.jcall("GET", "/api/bench/tasks")
        self.assertEqual(status, 503)
        self.assertIn("boom", body["error"])


if __name__ == "__main__":
    unittest.main()
