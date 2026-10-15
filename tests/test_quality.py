"""Tests for kyora.pipeline.quality: static checks, Node runner, browser results and the gate.

Every workspace is built inline in a temp dir so the tests do not depend on fixtures
written by other modules. Node-dependent tests run only when ``node`` is installed.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

from kyora.config import QualityCfg
from kyora.pipeline.quality import (BrowserResultsCollector, Check, GateResult, NodeRunner, QualityGate, StaticChecker,
                                    check_html, decode_screenshot, find_node, merge_checks, parse_tap, validate_browser_payload,
                                    validate_manifest)

NODE = find_node()
REPO = Path(__file__).resolve().parent.parent


def tiny_png(w: int = 2, h: int = 2) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


GOOD_INDEX = """<!doctype html>
<html><head><meta charset="utf-8"><title>T</title><link rel="stylesheet" href="style.css"></head>
<body>
<canvas id="c" width="10" height="10"></canvas>
<p>Hello<br>world
<ul><li>one<li>two</ul>
<script src="src/game.js"></script>
<script type="module" src="src/mod.mjs"></script>
</body></html>
"""

BROWSER_TESTS = """KyoraTest.test('at-01', 'boots', async (t) => { t.expect(1).toBe(1); });
KyoraTest.test("at-02", 'starts', async (t) => { t.expect(2).toBe(2); });
"""


def make_game(root: Path, *, files: dict | None = None, manifest: dict | None | str = "default") -> Path:
    """Write a small valid game into ``root`` (override/remove files with ``files``; value None deletes)."""
    base = {
        "index.html": GOOD_INDEX,
        "style.css": "body { margin: 0 }\n",
        "src/game.js": "window.Game = { start() { return 1; }, getState() { return { score: 0, lives: 3, phase: 'menu' }; } };\n",
        "src/mod.mjs": "export const answer = 42;\n",
        "tests/browser-tests.js": BROWSER_TESTS,
        "data/levels.json": '{"levels": [1, 2, 3]}\n',
    }
    if manifest == "default":
        manifest = {"entry": "index.html", "engine": "vanilla", "scripts": ["src/game.js"], "modules": ["src/mod.mjs"],
                    "tests": ["tests/browser-tests.js"], "version": 1}
    if manifest is not None:
        base[".kyora/manifest.json"] = manifest if isinstance(manifest, str) else json.dumps(manifest)
    base.update(files or {})
    for rel, content in base.items():
        p = root / rel
        if content is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


PLAN = {
    "acceptance_tests": [
        {"id": "at-01", "name": "boots", "kind": "browser", "required": True},
        {"id": "at-02", "name": "starts", "kind": "browser", "required": False},
    ],
    "constraints": {"no_external_deps": True, "max_total_mb": 25},
}


class TempHomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.tmp / "home")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self._tmp.cleanup()

    @staticmethod
    def by_id(checks: list[Check]) -> dict[str, Check]:
        return {c.id: c for c in checks}


# ============================================================================ HTML
class HtmlWellFormedTests(unittest.TestCase):
    def assertWellFormed(self, html: str) -> None:
        p = check_html(html)
        self.assertEqual(p.errors, [], html)

    def test_void_elements_need_no_close(self):
        self.assertWellFormed('<div><img src="a.png"><br><input type="text"><hr><meta charset="utf-8"></div>')
        self.assertWellFormed('<div><img src="a.png"/><br/></div>')

    def test_optional_close_tags_are_lenient(self):
        self.assertWellFormed("<ul><li>a<li>b</ul><p>one<p>two<div>x</div>")
        self.assertWellFormed("<table><thead><tr><th>h<th>i<tbody><tr><td>1<td>2<tr><td>3</table>")
        self.assertWellFormed("<dl><dt>a<dd>b<dt>c<dd>d</dl><select><option>a<option>b</select>")
        self.assertWellFormed("<html><head><title>x</title><body><p>no end tags anywhere")
        self.assertWellFormed("<div><p>para</div>")  # </div> closes the open <p>
        self.assertWellFormed("<p>stray close</p></p>")

    def test_svg_and_script_content(self):
        self.assertWellFormed('<svg viewBox="0 0 1 1"><path d="M0 0"/><circle r="1"></circle></svg>')
        self.assertWellFormed("<script>if (a < b && c > d) { document.write('<div>'); }</script>")

    def test_mismatch_reports_line(self):
        p = check_html("<div>\n<span>\n</div>\n")
        self.assertTrue(p.errors)
        line, msg = p.errors[0]
        self.assertEqual(line, 3)
        self.assertIn("mismatched </div>", msg)
        self.assertIn("<span> opened at line 2", msg)

    def test_unclosed_and_unexpected(self):
        p = check_html("<main>\n<section>\n</main>\n<div>")
        msgs = " | ".join(m for _, m in p.errors)
        self.assertIn("section", msgs)
        self.assertIn("unclosed <div>", msgs)
        p2 = check_html("<div></div></span>")
        self.assertIn("unexpected </span>", p2.errors[0][1])

    def test_links_and_tags_recorded(self):
        p = check_html('<canvas></canvas><script src="a.js"></script><link href="../x.css">')
        self.assertIn("canvas", p.tags_seen)
        self.assertIn(("script", "src", "a.js"), [(t, a, v) for _, t, a, v in p.links])


# ============================================================================ static
class StaticCheckerTests(TempHomeCase):
    def run_checker(self, plan=PLAN, node=None) -> dict[str, Check]:
        return self.by_id(StaticChecker(self.ws, plan, node).run())

    def test_valid_game_passes_without_node(self):
        make_game(self.ws)
        checks = self.run_checker()
        for cid in ("file_exists:index.html", "file_exists:.kyora/manifest.json", "file_exists:tests/browser-tests.js",
                    "manifest_valid", "html_wellformed:index.html", "json_valid:data/levels.json", "external_deps",
                    "size_limit", "tests_present:at-01", "tests_present:at-02", "no_path_escape"):
            self.assertEqual(checks[cid].status, "pass", f"{cid}: {checks[cid].detail}")
        for cid in ("file_exists:index.html", "manifest_valid", "html_wellformed:index.html"):
            self.assertTrue(checks[cid].required, cid)
        self.assertFalse(checks["tests_present:at-01"].required)
        # without node the JS checks are deferred (required) and name the browser fallback
        for rel in ("src/game.js", "src/mod.mjs"):
            c = checks[f"js_syntax:{rel}"]
            self.assertEqual(c.status, "deferred")
            self.assertTrue(c.required)
            self.assertEqual(c.group, "static")
        self.assertTrue(all(c.group == "static" for c in checks.values()))
        json.dumps([c.to_dict() for c in checks.values()])  # serialisable

    @unittest.skipUnless(NODE, "node not installed")
    def test_js_syntax_with_node_pass_and_fail(self):
        make_game(self.ws, files={"src/game.js": "let x = ;\n", "src/mod.mjs": "import { a } from './a.mjs';\nexport const b = a;\n",
                                  "src/a.mjs": "export const a = 1;\n"})
        checks = self.run_checker(node=NODE)
        bad = checks["js_syntax:src/game.js"]
        self.assertEqual(bad.status, "fail")
        self.assertIn("SyntaxError", bad.detail)
        self.assertIn("line 1", bad.detail)
        self.assertNotIn("kyora-check-", bad.detail)  # temp path replaced by the workspace path
        good = checks["js_syntax:src/mod.mjs"]
        self.assertEqual(good.status, "pass", good.detail)
        self.assertIn("module", good.runner)

    @unittest.skipUnless(NODE, "node not installed")
    def test_import_in_classic_script_fails(self):
        make_game(self.ws, files={"src/game.js": "import x from './y.js';\n"})
        checks = self.run_checker(node=NODE)
        self.assertEqual(checks["js_syntax:src/game.js"].status, "fail")
        self.assertIn("import", checks["js_syntax:src/game.js"].detail)

    def test_missing_files(self):
        make_game(self.ws, files={"index.html": None, "tests/browser-tests.js": None}, manifest=None)
        checks = self.run_checker()
        self.assertEqual(checks["file_exists:index.html"].status, "fail")
        self.assertEqual(checks["file_exists:.kyora/manifest.json"].status, "fail")
        self.assertEqual(checks["file_exists:tests/browser-tests.js"].status, "fail")
        self.assertEqual(checks["manifest_valid"].status, "fail")
        self.assertEqual(checks["html_wellformed:index.html"].status, "skip")
        self.assertEqual(checks["tests_present:at-01"].status, "fail")
        self.assertNotIn("js_syntax:src/game.js", checks)  # no manifest, no index.html -> nothing to check

    def test_missing_workspace(self):
        checks = StaticChecker(self.tmp / "nope", PLAN, None).run()
        self.assertEqual(checks[0].status, "fail")
        self.assertTrue(checks[0].required)

    def test_index_needs_canvas_or_script_and_other_html_not_required(self):
        make_game(self.ws, files={"index.html": "<!doctype html><html><body><div>hi</div></body></html>",
                                  "help.html": "<div><span></div>"})
        checks = self.run_checker()
        self.assertEqual(checks["html_wellformed:index.html"].status, "fail")
        self.assertIn("neither <canvas> nor <script>", checks["html_wellformed:index.html"].detail)
        self.assertEqual(checks["html_wellformed:help.html"].status, "fail")
        self.assertFalse(checks["html_wellformed:help.html"].required)
        self.assertIn("line 1", checks["html_wellformed:help.html"].detail)

    def test_manifest_validation(self):
        make_game(self.ws)
        self.assertEqual(validate_manifest(json.loads((self.ws / ".kyora/manifest.json").read_text()), self.ws), [])
        problems = validate_manifest({"entry": "", "engine": 3, "scripts": "a.js", "modules": [1], "tests": []}, self.ws)
        joined = " ".join(problems)
        for frag in ("'entry'", "'engine'", "'scripts'", "'modules'"):
            self.assertIn(frag, joined)
        problems = validate_manifest({"entry": "index.html", "engine": "vanilla", "scripts": ["src/missing.js", "..\\x.js"],
                                      "modules": ["../out.mjs"], "tests": ["/abs.js"]}, self.ws)
        joined = " ".join(problems)
        self.assertIn("does not exist", joined)
        self.assertIn("backslashes", joined)
        self.assertIn("escapes", joined)
        self.assertIn("absolute", joined)
        self.assertEqual(validate_manifest([], self.ws), ["manifest is not a JSON object"])

        make_game(self.ws, manifest="{not json")
        c = self.run_checker()["manifest_valid"]
        self.assertEqual(c.status, "fail")
        self.assertIn("invalid JSON", c.detail)

        make_game(self.ws, manifest={"entry": "index.html", "engine": "vanilla", "scripts": ["src/gone.js"], "modules": [],
                                     "tests": []})
        checks = self.run_checker()
        self.assertEqual(checks["manifest_valid"].status, "fail")
        self.assertIn("src/gone.js", checks["manifest_valid"].detail)
        self.assertEqual(checks["js_syntax:src/gone.js"].status, "fail")
        self.assertEqual(checks["js_syntax:src/gone.js"].detail, "file not found")

    def test_js_files_fall_back_to_index_scripts_without_manifest(self):
        make_game(self.ws, manifest=None)
        checks = self.run_checker()
        self.assertEqual(checks["js_syntax:src/game.js"].status, "deferred")
        self.assertIn("js_syntax:src/mod.mjs", checks)

    def test_json_valid(self):
        make_game(self.ws, files={"data/bad.json": '{\n  "a": 1,\n}\n'})
        checks = self.run_checker()
        self.assertEqual(checks["json_valid:data/levels.json"].status, "pass")
        bad = checks["json_valid:data/bad.json"]
        self.assertEqual(bad.status, "fail")
        self.assertRegex(bad.detail, r"^line [23] col \d+: ")

    def test_external_deps_with_and_without_constraint(self):
        make_game(self.ws, files={
            "index.html": GOOD_INDEX.replace("</body>", '<script src="https://cdn.example.com/lib.js"></script></body>'),
            "src/net.js": "fetch('https://api.example.com/x').then(r => r);\nimport('//cdn.example.com/m.js');\n",
            "style.css": "@import url('https://fonts.example.com/f.css');\n",
        })
        forbid = self.run_checker()["external_deps"]
        self.assertEqual(forbid.status, "fail")
        for url in ("cdn.example.com/lib.js", "api.example.com/x", "fonts.example.com/f.css", "//cdn.example.com/m.js"):
            self.assertIn(url, forbid.detail)
        allow = self.run_checker(plan={**PLAN, "constraints": {"no_external_deps": False}})["external_deps"]
        self.assertEqual(allow.status, "pass")
        self.assertIn("allowed by plan", allow.detail)

    def test_external_deps_ignores_plain_strings(self):
        make_game(self.ws, files={"src/game.js": "const ns = 'http://www.w3.org/2000/svg';\nconst a = '<a href=\"https://x.org\">';\n"})
        self.assertEqual(self.run_checker()["external_deps"].status, "pass")

    def test_size_limit_excludes_git(self):
        make_game(self.ws, files={".git/objects/big": b"\0" * (2 * 1024 * 1024)})
        plan = {**PLAN, "constraints": {"no_external_deps": True, "max_total_mb": 1}}
        self.assertEqual(self.run_checker(plan=plan)["size_limit"].status, "pass")
        make_game(self.ws, files={"assets/big.bin": b"\0" * (1024 * 1024 + 10)})
        c = self.run_checker(plan=plan)["size_limit"]
        self.assertEqual(c.status, "fail")
        self.assertIn("of 1 MB", c.detail)
        self.assertEqual(self.run_checker(plan={})["size_limit"].status, "pass")  # default 25 MB

    def test_tests_present(self):
        plan = {"acceptance_tests": [{"id": "at-01", "kind": "browser"}, {"id": "at-1", "kind": "browser"},
                                     {"id": "at-03", "kind": "browser"}, {"id": "at-04", "kind": "node"}]}
        make_game(self.ws)
        checks = self.run_checker(plan=plan)
        self.assertEqual(checks["tests_present:at-01"].status, "pass")
        self.assertEqual(checks["tests_present:at-1"].status, "fail")  # 'at-1' is not 'at-01'
        self.assertEqual(checks["tests_present:at-03"].status, "fail")
        self.assertNotIn("tests_present:at-04", checks)  # node tests are not looked up in browser-tests.js

    def test_no_path_escape(self):
        make_game(self.ws, files={"index.html": GOOD_INDEX.replace(
            "</body>", '<img src="../secret.png"><a href="https://ok.example">x</a><a href="#top">t</a>'
                       '<img src="data:image/png;base64,AAAA"><link rel="icon" href="/favicon.ico"></body>')})
        c = self.run_checker()["no_path_escape"]
        self.assertEqual(c.status, "fail")
        self.assertIn("../secret.png", c.detail)
        self.assertIn("/favicon.ico", c.detail)
        self.assertNotIn("ok.example", c.detail)

    @unittest.skipUnless(NODE, "node not installed")
    def test_harness_fixture_is_clean(self):
        plan = {"acceptance_tests": [{"id": f"at-0{i}", "kind": "browser"} for i in range(1, 9)]
                + [{"id": "at-09", "kind": "node"}, {"id": "at-10", "kind": "browser"}],
                "constraints": {"no_external_deps": True}}
        checks = StaticChecker(REPO / "tests" / "fixtures" / "harness-game", plan, NODE).run()
        bad = [(c.id, c.detail) for c in checks if c.status != "pass"]
        self.assertEqual(bad, [])


# ============================================================================ node
TAP_SAMPLE = """TAP version 13
# Subtest: at-01 adds \\# numbers
ok 1 - at-01 adds \\# numbers
  ---
  duration_ms: 1.5
  type: 'test'
  ...
# Subtest: fails
not ok 2 - fails
  ---
  duration_ms: 1.8
  type: 'test'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:

    2 !== 3
  code: 'ERR_ASSERTION'
  ...
ok 3 - skipped one # SKIP
  ---
  duration_ms: 0.1
  ...
not ok 4 - todo one # TODO
  ---
  duration_ms: 0.2
  error: 'x'
  ...
# Subtest: suite
    # Subtest: at-05 nested ok
    ok 1 - at-05 nested ok
      ---
      duration_ms: 0.3
      ...
    not ok 2 - nested fail
      ---
      duration_ms: 0.2
      failureType: 'testCodeFailure'
      error: 'boom'
      ...
    1..2
not ok 5 - suite
  ---
  duration_ms: 1.1
  type: 'suite'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  ...
1..5
# tests 6
"""


class NodeRunnerTests(TempHomeCase):
    def test_parse_tap_leaves(self):
        leaves = parse_tap(TAP_SAMPLE)
        names = [(e["name"], e["status"]) for e in leaves]
        self.assertEqual(names, [("at-01 adds # numbers", "pass"), ("fails", "fail"), ("skipped one", "skip"),
                                 ("todo one", "skip"), ("at-05 nested ok", "pass"), ("nested fail", "fail")])
        self.assertIn("2 !== 3", leaves[1]["error"])
        self.assertEqual(leaves[5]["error"], "boom")
        self.assertAlmostEqual(leaves[0]["duration_ms"], 1.5)

    def test_parse_tap_keeps_parent_with_own_failure(self):
        text = "# Subtest: s\n    ok 1 - child\nnot ok 1 - s\n  ---\n  failureType: 'hookFailed'\n  error: 'after hook'\n  ...\n"
        leaves = parse_tap(text)
        self.assertEqual([(e["name"], e["status"]) for e in leaves], [("child", "pass"), ("s", "fail")])

    def test_skip_without_node_or_file(self):
        c = NodeRunner(None, self.ws).logic_tests()
        self.assertEqual([(x.id, x.status) for x in c], [("node:logic-tests", "skip")])
        self.assertIn("node not available", c[0].detail)
        c = NodeRunner(NODE or "node", self.ws).logic_tests()
        self.assertEqual(c[0].status, "skip")
        self.assertIn("not present", c[0].detail)
        d = NodeRunner(None, self.ws).syntax_check(["a.js"])
        self.assertEqual(d[0].status, "fail")  # file does not exist
        (self.ws / "a.js").write_text("1;\n")
        d = NodeRunner(None, self.ws).syntax_check(["a.js"])
        self.assertEqual((d[0].status, d[0].group, d[0].required), ("deferred", "node", True))

    @unittest.skipUnless(NODE, "node not installed")
    def test_real_logic_tests(self):
        (self.ws / "tests").mkdir()
        (self.ws / "src").mkdir()
        (self.ws / "src" / "logic.mjs").write_text("export const add = (a, b) => a + b;\n", encoding="utf-8")
        (self.ws / "tests" / "logic-tests.mjs").write_text(
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "import { add } from '../src/logic.mjs';\n"
            "test('at-03 adds numbers', () => { assert.equal(add(1, 2), 3); });\n"
            "test('lt-01: subtracts wrongly', () => { assert.equal(add(1, -1), 5); });\n"
            "test('plain name', () => {});\n"
            "test('later', { skip: true }, () => {});\n", encoding="utf-8")
        checks = NodeRunner(NODE, self.ws).logic_tests()
        got = [(c.id, c.status) for c in checks]
        self.assertEqual(got, [("at-03", "pass"), ("lt-01", "fail"), ("lt-3", "pass"), ("lt-4", "skip")])
        self.assertTrue(all(c.group == "node" and c.runner == "node --test" for c in checks))
        self.assertIn("5", checks[1].detail)

    @unittest.skipUnless(NODE, "node not installed")
    def test_broken_test_file_is_a_failure(self):
        (self.ws / "tests").mkdir()
        (self.ws / "tests" / "logic-tests.mjs").write_text("import test from 'node:test';\ntest('x', () => {\n", encoding="utf-8")
        checks = NodeRunner(NODE, self.ws).logic_tests()
        self.assertTrue(any(c.status == "fail" for c in checks), [c.to_dict() for c in checks])


# ============================================================================ merge + gate
def ck(cid: str, status: str = "pass", group: str = "static", required: bool = False, detail: str = "") -> Check:
    return Check(cid, group, cid, status, required, detail, "test")


class MergeTests(unittest.TestCase):
    def test_deferred_replaced_by_browser_and_overrides(self):
        static = [ck("file_exists:index.html", required=True), ck("js_syntax:a.js", "deferred", required=True),
                  ck("js_syntax:b.js", "deferred", required=True), ck("at-01", "fail")]
        node = [ck("at-01", "pass", "node"), ck("lt-1", "pass", "node")]
        browser = [ck("js_syntax:a.js", "pass", "browser", True), ck("at-02", "pass", "browser")]
        merged = merge_checks(static, node, browser)
        m = {c.id: c for c in merged}
        self.assertEqual([c.id for c in merged], ["file_exists:index.html", "js_syntax:a.js", "js_syntax:b.js", "at-01",
                                                  "lt-1", "at-02"])
        self.assertEqual((m["js_syntax:a.js"].status, m["js_syntax:a.js"].group), ("pass", "browser"))
        self.assertEqual(m["js_syntax:b.js"].status, "deferred")
        self.assertEqual(m["at-01"].group, "node")

    def test_runner_results_cannot_mask_static_checks(self):
        static = [ck("manifest_valid", "fail", required=True), ck("file_exists:index.html", "fail", required=True)]
        browser = [ck("manifest_valid", "pass", "browser"), ck("file_exists:index.html", "pass", "browser")]
        m = {c.id: c for c in merge_checks(static, [], browser)}
        self.assertEqual(m["manifest_valid"].status, "fail")
        self.assertEqual(m["file_exists:index.html"].status, "fail")
        self.assertEqual(m["browser:manifest_valid"].status, "pass")
        self.assertFalse(QualityGate(0.5, {}).evaluate(list(m.values())).ok)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = QualityCfg(min_pass_rate=0.94, max_fix_iterations=3, browser_wait_s=10, require_plan_approval=False,
                              auto_run_browser_tests=True)

    def test_threshold_boundary_exactly_passes(self):
        checks = [ck(f"c{i}") for i in range(47)] + [ck(f"f{i}", "fail") for i in range(3)]
        g = QualityGate(self.cfg, {}).evaluate(checks)
        self.assertEqual((g.passed, g.total), (47, 50))
        self.assertEqual(g.pass_rate, 0.94)
        self.assertTrue(g.ok)
        g = QualityGate(self.cfg, {}).evaluate([ck(f"c{i}") for i in range(94)] + [ck(f"f{i}", "fail") for i in range(6)])
        self.assertTrue(g.ok)
        g = QualityGate(self.cfg, {}).evaluate([ck(f"c{i}") for i in range(93)] + [ck(f"f{i}", "fail") for i in range(7)])
        self.assertFalse(g.ok)
        self.assertEqual(g.to_dict()["pass_rate"], 0.93)

    def test_skip_and_optional_deferred_not_counted(self):
        g = QualityGate(self.cfg, {}).evaluate([ck("a"), ck("b", "skip"), ck("c", "deferred")])
        self.assertEqual((g.passed, g.total, g.ok), (1, 1, True))

    def test_required_failure_blocks(self):
        checks = [ck(f"c{i}") for i in range(99)] + [ck("manifest_valid", "fail", required=True)]
        g = QualityGate(self.cfg, {}).evaluate(checks)
        self.assertGreaterEqual(g.pass_rate, 0.94)
        self.assertFalse(g.ok)
        self.assertEqual(g.required_failures, ["manifest_valid"])

    def test_required_deferred_counts_as_failure(self):
        original = ck("js_syntax:a.js", "deferred", required=True, detail="node not available")
        g = QualityGate(self.cfg, {}).evaluate([ck(f"c{i}") for i in range(30)] + [original])
        self.assertFalse(g.ok)
        self.assertEqual(g.required_failures, ["js_syntax:a.js"])
        c = [x for x in g.checks if x.id == "js_syntax:a.js"][0]
        self.assertEqual(c.status, "fail")
        self.assertIn("no syntax verification available", c.detail)
        self.assertEqual(original.status, "deferred")  # input not mutated

    def test_missing_spec_ids_appended(self):
        plan = {"acceptance_tests": [{"id": "at-01", "kind": "browser"}, {"id": "at-02", "kind": "browser", "required": True},
                                     {"id": "at-03", "kind": "node"}, {"id": "at-04", "kind": "manual"}]}
        checks = [ck(f"c{i}") for i in range(40)] + [ck("at-01", group="browser"),
                                                     ck("at-03", "skip", group="node")]  # skipped still 'reported'
        g = QualityGate(self.cfg, plan).evaluate(checks)
        self.assertEqual(g.missing_spec_tests, ["at-02"])
        self.assertFalse(g.ok)
        missing = [c for c in g.checks if c.id == "missing:at-02"][0]
        self.assertEqual((missing.status, missing.group, missing.required), ("fail", "browser", True))
        self.assertIn("missing:at-02", g.required_failures)
        self.assertEqual(g.total, 42)  # 41 passes + the missing failure
        # a static check with the same id does not satisfy the spec
        g2 = QualityGate(self.cfg, {"acceptance_tests": [{"id": "x-1", "kind": "browser"}]}).evaluate([ck("x-1")])
        self.assertEqual(g2.missing_spec_tests, ["x-1"])

    def test_required_acceptance_test_failure_blocks(self):
        plan = {"acceptance_tests": [{"id": "at-01", "kind": "browser", "required": True}]}
        checks = [ck(f"c{i}") for i in range(60)] + [ck("at-01", "fail", "browser")]
        g = QualityGate(self.cfg, plan).evaluate(checks)
        self.assertGreater(g.pass_rate, 0.94)
        self.assertFalse(g.ok)
        self.assertEqual(g.required_failures, ["at-01"])

    def test_total_zero_is_not_ok(self):
        g = QualityGate(self.cfg, {}).evaluate([ck("a", "skip")])
        self.assertEqual((g.total, g.pass_rate, g.ok), (0, 0.0, False))
        self.assertFalse(QualityGate(0.5, {}).evaluate([]).ok)

    def test_float_threshold_and_to_dict(self):
        g = QualityGate(0.5, {}).evaluate([ck("a"), ck("b", "fail"), ck("c")])
        self.assertTrue(g.ok)
        d = g.to_dict()
        self.assertEqual(d["pass_rate"], 0.6666)  # floored, never rounded up
        self.assertEqual(len(d["checks"]), 3)
        self.assertNotIn("checks", g.to_dict(include_checks=False))
        self.assertIsInstance(g, GateResult)
        self.assertEqual(Check.from_dict(d["checks"][1]), g.checks[1])


# ============================================================================ browser results
def payload(run_id: str | None = "r_1", iteration: int | None = 1, *, results=None, shots=None, **extra) -> dict:
    results = results if results is not None else [
        {"id": "at-01", "name": "boots", "status": "pass", "ms": 18, "error": None},
        {"id": "at-02", "name": "starts", "status": "fail", "ms": 40, "error": "expected 1 to be 2"},
        {"id": "at-03", "name": "later", "status": "skip", "ms": 0, "error": None},
    ]
    p = {"type": "kyora:test-result", "run_id": run_id, "iteration": iteration, "ref": "run-r_1", "results": results,
         "summary": {"passed": 1, "failed": 1, "skipped": 1, "total": 3},
         "syntax_checks": [{"file": "src/game.js", "ok": True, "error": None, "method": "new Function"},
                           {"file": "src/mod.mjs", "ok": False, "error": "SyntaxError: Unexpected token", "method": "import()"}],
         "screenshots": shots if shots is not None else [{"name": "start", "data_url": data_url(tiny_png())},
                                                         {"name": "end", "data_url": data_url(tiny_png(3, 3))}],
         "env": {"ua": "test", "viewport": [390, 760], "touch": True, "started_at": "2026-10-15T00:00:00Z", "duration_ms": 120}}
    p.update(extra)
    return p


class BrowserResultsTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.run_dir = self.home / "projects" / "p_1" / "runs" / "r_1"
        self.run_dir.mkdir(parents=True)
        self.col = BrowserResultsCollector(self.home)

    def test_submit_then_wait_stores_files(self):
        self.col.expect("r_1", 1, self.run_dir)
        self.assertTrue(self.col.submit("r_1", payload()))
        got = self.col.wait("r_1", 1, timeout_s=1)
        self.assertIsNotNone(got)
        self.assertEqual(got["summary"]["total"], 3)
        shots = self.col.screenshots("r_1", 1)
        self.assertEqual([p.name for p in shots], ["start.png", "end.png"])
        self.assertTrue(shots[0].read_bytes().startswith(b"\x89PNG"))
        stored = json.loads((self.run_dir / "iter-1" / "browser.json").read_text(encoding="utf-8"))
        self.assertNotIn("data:image", json.dumps(stored))
        self.assertEqual(stored["screenshots"][0]["path"], "iter-1/screenshots/start.png")
        self.assertEqual(stored["run_id"], "r_1")
        self.assertIsNone(self.col.last_error("r_1"))
        self.assertEqual(self.col.latest("r_1")["iteration"], 1)

    def test_wait_wakes_on_submit_from_other_thread(self):
        self.col.expect("r_1", 2, self.run_dir)
        t = threading.Timer(0.2, lambda: self.col.submit("r_1", payload(iteration=2)))
        t.start()
        t0 = time.monotonic()
        got = self.col.wait("r_1", 2, timeout_s=10)
        t.join()
        self.assertIsNotNone(got)
        self.assertLess(time.monotonic() - t0, 5)

    def test_wait_timeout_and_cancel(self):
        self.col.expect("r_1", 1, self.run_dir)
        t0 = time.monotonic()
        self.assertIsNone(self.col.wait("r_1", 1, timeout_s=0.3))
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        t0 = time.monotonic()
        self.assertIsNone(self.col.wait("r_1", 1, timeout_s=30, cancel=cancel))
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIsNone(self.col.wait("r_1", 1, timeout_s=30, cancel=lambda: True))

    def test_late_submission_for_latest_iteration_and_stale_rejected(self):
        self.col.expect("r_1", 1, self.run_dir)
        self.assertIsNone(self.col.wait("r_1", 1, timeout_s=0.1))  # pipeline gave up (BLOCKED)
        self.assertTrue(self.col.submit("r_1", payload(iteration=1)))  # late, still accepted
        self.assertIsNotNone(self.col.wait("r_1", 1, timeout_s=0.1))  # a resumed wait sees it
        self.col.expect("r_1", 2, self.run_dir)
        with self.assertLogs("kyora.quality", "WARNING") as logs:
            self.assertFalse(self.col.submit("r_1", payload(iteration=1)))  # stale now
            self.assertIn("latest expected iteration", self.col.last_error("r_1"))
            self.assertFalse(self.col.submit("r_1", payload(iteration=3)))
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(self.col.submit("r_1", payload(iteration=None)))  # no iteration -> latest expected
        self.assertEqual(self.col.wait("r_1", 2, timeout_s=0.1)["iteration"], 2)
        self.assertEqual(self.col.latest("r_1")["iteration"], 2)

    def test_disk_fallback_and_run_dir_discovery(self):
        self.assertTrue(self.col.submit("r_1", payload(iteration=4)))  # never expected: run dir discovered
        self.assertTrue((self.run_dir / "iter-4" / "browser.json").is_file())
        fresh = BrowserResultsCollector(self.home)  # e.g. after a server restart
        self.assertEqual(fresh.wait("r_1", 4, timeout_s=0)["iteration"], 4)
        self.assertEqual(BrowserResultsCollector(self.home).latest("r_1")["iteration"], 4)
        self.assertEqual(len(BrowserResultsCollector(self.home).screenshots("r_1", 4)), 2)
        self.assertIsNone(fresh.latest("r_unknown"))

    def test_schema_rejection(self):
        self.col.expect("r_1", 1, self.run_dir)
        bad_cases = [
            ("not a dict", ["x"]),
            ("type", payload(type="other")),
            ("status", payload(results=[{"id": "a", "name": "a", "status": "ok", "ms": 1, "error": None}])),
            ("id", payload(results=[{"id": "", "name": "a", "status": "pass", "ms": 1, "error": None}])),
            ("ms", payload(results=[{"id": "a", "name": "a", "status": "pass", "ms": -1, "error": None}])),
            ("summary", payload(summary={"passed": 1})),
            ("syntax", payload(syntax_checks=[{"file": "a.js", "ok": "yes"}])),
            ("shots", payload(screenshots="nope")),
            ("iteration", payload(iteration=-1)),
        ]
        with self.assertLogs("kyora.quality", "WARNING"):
            for label, p in bad_cases:
                with self.subTest(label):
                    self.assertTrue(validate_browser_payload(p), label)
                    self.assertFalse(self.col.submit("r_1", p))
                    self.assertTrue(self.col.last_error("r_1"))
            self.assertFalse(self.col.submit("r_1", payload(run_id="r_other")))
            self.assertIn("does not match", self.col.last_error("r_1"))
            self.assertFalse(self.col.submit("../etc", payload(run_id=None)))
        self.assertIsNone(self.col.wait("r_1", 1, timeout_s=0))
        self.assertEqual(validate_browser_payload(payload()), [])

    def test_screenshot_decoding_limits(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\0" * 20
        big = b"\x89PNG\r\n\x1a\n" + b"\0" * (4 * 1024 * 1024 + 1)
        shots = [{"name": "start", "data_url": data_url(tiny_png())},
                 {"name": "fake", "data_url": data_url(b"GIF89a....")},
                 {"name": "photo", "data_url": data_url(jpeg, "image/jpeg")},
                 {"name": "huge", "data_url": data_url(big)},
                 {"name": "none", "data_url": None},
                 {"name": "gif", "data_url": "data:image/gif;base64,R0lGODlh"},
                 {"name": "../../evil name", "data_url": data_url(tiny_png())},
                 {"name": "start", "data_url": data_url(tiny_png())}]
        shots += [{"name": f"extra{i}", "data_url": data_url(tiny_png())} for i in range(3)]
        self.col.expect("r_1", 1, self.run_dir)
        self.assertTrue(self.col.submit("r_1", payload(shots=shots)))
        stored = self.col.wait("r_1", 1, timeout_s=1)["screenshots"]
        self.assertEqual(len(stored), 11)
        by = {i: s for i, s in enumerate(stored)}
        self.assertEqual(by[0]["path"], "iter-1/screenshots/start.png")
        self.assertIn("not a PNG", by[1]["error"])
        self.assertEqual(by[2]["path"], "iter-1/screenshots/photo.jpg")
        self.assertIn("larger than 4 MB", by[3]["error"])
        self.assertIn("no image", by[4]["error"])
        self.assertIn("not a base64 PNG/JPEG", by[5]["error"])
        self.assertEqual(by[6]["path"], "iter-1/screenshots/evil-name.png")
        self.assertEqual(by[7]["path"], "iter-1/screenshots/start-2.png")
        self.assertIn("dropped", by[8]["error"])
        files = sorted(p.name for p in (self.run_dir / "iter-1" / "screenshots").iterdir())
        self.assertEqual(files, ["evil-name.png", "photo.jpg", "start-2.png", "start.png"])
        self.assertEqual(len(self.col.screenshots("r_1", 1)), 4)
        with self.assertRaises(ValueError):
            decode_screenshot("data:image/png;base64,!!!", 1024)

    def test_to_checks(self):
        checks = BrowserResultsCollector.to_checks(payload(results=[
            {"id": "at-01", "name": "boots", "status": "pass", "ms": 18, "error": None},
            {"id": "at-01", "name": "dup", "status": "fail", "ms": 1, "error": "duplicate"},
            {"id": "at-02", "name": "x", "status": "skip", "ms": 0, "error": None}]))
        m = {c.id: c for c in checks}
        self.assertEqual(m["at-01"].status, "pass")
        self.assertEqual(m["at-01"].group, "browser")
        self.assertFalse(m["at-01"].required)
        self.assertEqual(m["at-01#2"].status, "fail")
        self.assertEqual(m["at-02"].status, "skip")
        self.assertEqual((m["js_syntax:src/game.js"].status, m["js_syntax:src/game.js"].required), ("pass", True))
        self.assertEqual(m["js_syntax:src/mod.mjs"].status, "fail")
        self.assertEqual(m["js_syntax:src/mod.mjs"].runner, "browser:import()")
        empty = BrowserResultsCollector.to_checks(payload(results=[], syntax_checks=[]))
        self.assertEqual([(c.id, c.status) for c in empty], [("browser:no-tests", "fail")])
        self.assertEqual(BrowserResultsCollector.to_checks(None), [])

    def test_end_to_end_gate_with_browser_results(self):
        make_game(self.ws)
        static = StaticChecker(self.ws, PLAN, None).run()
        self.col.expect("r_1", 1, self.run_dir)
        self.col.submit("r_1", payload(results=[{"id": "at-01", "name": "boots", "status": "pass", "ms": 5, "error": None},
                                                {"id": "at-02", "name": "starts", "status": "pass", "ms": 5, "error": None}],
                                       syntax_checks=[{"file": "src/game.js", "ok": True, "error": None, "method": "new Function"},
                                                      {"file": "src/mod.mjs", "ok": True, "error": None, "method": "import()"}]))
        browser = BrowserResultsCollector.to_checks(self.col.wait("r_1", 1, 1))
        merged = merge_checks(static, NodeRunner(None, self.ws).logic_tests(), browser)
        self.assertFalse(any(c.status == "deferred" for c in merged))
        g = QualityGate(0.94, PLAN).evaluate(merged)
        self.assertTrue(g.ok, [c.to_dict() for c in g.checks if c.status == "fail"])
        self.assertEqual(g.missing_spec_tests, [])
        # without browser results the deferred syntax checks and missing spec ids fail the gate
        g2 = QualityGate(0.94, PLAN).evaluate(merge_checks(static))
        self.assertFalse(g2.ok)
        self.assertEqual(sorted(g2.missing_spec_tests), ["at-01", "at-02"])
        self.assertIn("js_syntax:src/game.js", g2.required_failures)


if __name__ == "__main__":
    unittest.main()
