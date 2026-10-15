"""Tests for kyora.projects.zipper (zip-slip safety, import) and kyora.projects.deck."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from kyora.projects.deck import HONESTY_TEXT, SECTION_IDS, DeckBuilder, render_markdown
from kyora.projects.store import ProjectStore
from kyora.projects.zipper import UnsafeZipError, import_game_zip, list_zip, unzip_safe, zip_dir
from kyora.util.fs import atomic_write_json

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _zip(entries: dict[str, bytes | str], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


class ZipperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kyora-zip-")
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_dir_deterministic_and_excludes(self):
        src = self.base / "src"
        (src / "b").mkdir(parents=True)
        (src / ".git").mkdir()
        (src / "node_modules" / "x").mkdir(parents=True)
        (src / "a.txt").write_text("A", encoding="utf-8")
        (src / "b" / "c.js").write_text("c", encoding="utf-8")
        (src / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (src / "node_modules" / "x" / "i.js").write_text("i", encoding="utf-8")
        if hasattr(os, "symlink"):
            try:
                os.symlink(str(self.base), src / "link")
            except OSError:
                pass
        data1 = zip_dir(src)
        data2 = zip_dir(src)
        self.assertEqual(data1, data2)
        zf = zipfile.ZipFile(io.BytesIO(data1))
        self.assertEqual(zf.namelist(), ["a.txt", "b/c.js"])
        self.assertTrue(all(i.compress_type == zipfile.ZIP_DEFLATED for i in zf.infolist()))
        out = zip_dir(src, self.base / "out" / "x.zip")
        self.assertIsInstance(out, Path)
        self.assertEqual(out.read_bytes(), data1)

    def _assert_rejected(self, data: bytes):
        dest = self.base / "dest"
        dest.mkdir(exist_ok=True)
        before = sorted(p.name for p in dest.iterdir())
        with self.assertRaises(UnsafeZipError):
            unzip_safe(data, dest)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), before, "nothing may be written on rejection")
        self.assertFalse((self.base / "evil.txt").exists())

    def test_zip_slip_rejections(self):
        ok = {"ok.txt": "fine"}
        self._assert_rejected(_zip({**ok, "/abs/evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "../evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "a/../../evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "a\\..\\..\\evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "C:/evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "C:evil.txt": "x"}))
        self._assert_rejected(_zip({**ok, "file.txt:stream": "x"}))
        self._assert_rejected(_zip(ok, symlinks={"link": "/etc/passwd"}))
        self._assert_rejected(b"not a zip at all")

    def test_limits(self):
        with self.assertRaises(UnsafeZipError):
            unzip_safe(_zip({f"f{i}.txt": "x" for i in range(5)}), self.base / "d1", max_files=4)
        with self.assertRaises(UnsafeZipError):
            unzip_safe(_zip({"big.bin": b"0" * 5000}), self.base / "d2", max_total=4000)

    def test_unzip_replace_keeps_git_and_skips_git_entries(self):
        dest = self.base / "ws"
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "HEAD").write_text("keep", encoding="utf-8")
        (dest / "old.txt").write_text("old", encoding="utf-8")
        written = unzip_safe(_zip({"new/a.txt": "a", ".git/hooks/post-commit": "evil", "b.txt": "b"}), dest, replace=True)
        self.assertEqual(sorted(written), ["b.txt", "new/a.txt"])
        self.assertFalse((dest / "old.txt").exists())
        self.assertEqual((dest / ".git" / "HEAD").read_text(encoding="utf-8"), "keep")
        self.assertFalse((dest / ".git" / "hooks").exists())
        # skip_symlinks drops (instead of rejecting) symlink entries of trusted archives
        written = unzip_safe(_zip({"x.txt": "x"}, symlinks={"l": "x.txt"}), self.base / "trusted", skip_symlinks=True)
        self.assertEqual(written, ["x.txt"])

    def test_import_game_zip_strips_single_top_folder(self):
        ws = self.base / "repo"
        (ws / ".git").mkdir(parents=True)
        (ws / "stale.js").write_text("old", encoding="utf-8")
        data = _zip({"my-game/index.html": "<canvas></canvas>", "my-game/src/game.js": "1;",
                     "__MACOSX/my-game/._index.html": "junk", "my-game/.DS_Store": "junk"})
        files = import_game_zip(data, ws)
        self.assertEqual(sorted(files), ["index.html", "src/game.js"])
        self.assertTrue((ws / "index.html").is_file())
        self.assertFalse((ws / "my-game").exists())
        self.assertFalse((ws / "stale.js").exists())
        self.assertTrue((ws / ".git").is_dir())
        # two top-level folders: nothing is stripped
        ws2 = self.base / "repo2"
        files2 = import_game_zip(_zip({"a/index.html": "x", "b/game.js": "y"}), ws2)
        self.assertEqual(sorted(files2), ["a/index.html", "b/game.js"])
        # top-level file next to a folder: nothing is stripped
        files3 = import_game_zip(_zip({"index.html": "x", "src/game.js": "y"}), self.base / "repo3")
        self.assertEqual(sorted(files3), ["index.html", "src/game.js"])
        with self.assertRaises(UnsafeZipError):
            import_game_zip(_zip({"__MACOSX/._x": "junk"}), self.base / "repo4")
        with self.assertRaises(UnsafeZipError):
            import_game_zip(_zip({"g/index.html": "x", "g/../../evil.txt": "y"}), self.base / "repo5")

    def test_list_zip(self):
        data = _zip({f"d/f{i:03d}.txt": "x" for i in range(10)})
        path = self.base / "l.zip"
        path.write_bytes(data)
        self.assertEqual(list_zip(path, limit=3), ["d/f000.txt", "d/f001.txt", "d/f002.txt"])
        self.assertEqual(len(list_zip(data)), 10)


class DeckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kyora-deck-")
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)
        self.store = ProjectStore(self.home / "projects", self.home / "kyora.db")

    def tearDown(self):
        self.store.close()
        if self._old is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old
        self.tmp.cleanup()

    def _populate(self):
        p = self.store.create("Sky <script>alert(1)</script> Runner")
        run = self.store.create_run(p.id, "generate", "Make a <b>catch</b> game & keep it short")
        rd = self.store.run_dir(run)
        atomic_write_json(rd / "intake.json", {"goal": "Catch falling blocks", "genre": "arcade", "platform": "web",
                                               "must_haves": ["touch controls", "<img src=x onerror=alert(1)>"],
                                               "references": [], "risks": ["scope"], "imported_base": False, "notes": ""})
        atomic_write_json(rd / "plan.json", {"title": "Block Catcher", "engine": "vanilla", "summary": "A tiny catcher.",
                                             "acceptance_tests": [{"id": "at-01"}, {"id": "at-02"}]})
        atomic_write_json(rd / "design.json", {"gdd_markdown": "# Block Catcher\n\n- **Fast** rounds\n- <script>x</script>\n",
                                               "narrative": "A sky full of blocks.",
                                               "dialogue": [{"speaker": "Guide", "line": "Catch them all!"}],
                                               "presenter_intro": "Hi"})
        atomic_write_json(rd / "ui_spec.json", {"tokens": {"color": {"primary": "#76b900", "bad": "red;background:url(http://x)"},
                                                           "type": {"body": "16px"}, "spacing": {"s": 4}, "radius": {"m": 8}},
                                                "screens": [{"id": "menu", "name": "Menu", "purpose": "start", "components": []}],
                                                "copy": {"title": "Block Catcher"}})
        gate = {"threshold": 0.94, "passed": 45, "total": 46, "pass_rate": 45 / 46, "ok": True, "required_failures": [],
                "missing_spec_tests": [], "checks": [
                    {"id": "at-01", "group": "browser", "name": "canvas exists", "status": "pass", "required": False},
                    {"id": "js_syntax:src/game.js", "group": "static", "name": "<syntax>", "status": "fail", "required": True}]}
        atomic_write_json(rd / "iter-1" / "gate.json", gate)
        (rd / "iter-1" / "screenshots").mkdir(parents=True)
        (rd / "iter-1" / "screenshots" / "start.png").write_bytes(PNG_1x1)
        run.engine = {"backend": "claude_code", "requested_model": "claude-fable-5-1", "active_model": "claude-opus-5-5",
                      "fallback_used": True}
        run.gate["final"] = {k: gate[k] for k in ("passed", "total", "pass_rate", "ok")}
        self.store.set_state(run, "finalize", iteration=1)
        return p, run, gate

    def test_sections_print_css_and_honesty(self):
        p, run, _ = self._populate()
        html_text = DeckBuilder(p, self.store).build()
        for sid in SECTION_IDS:
            self.assertIn(f'id="{sid}"', html_text)
        self.assertEqual(SECTION_IDS, ("cover", "concept", "design", "architecture", "screenshots", "tests",
                                       "changelog", "honesty"))
        self.assertIn(HONESTY_TEXT, html_text)
        self.assertIn("Every number in this deck was measured by this project's own runs.", html_text)
        self.assertIn("@media print", html_text)
        self.assertRegex(html_text, r"page-break-after:\s*always")
        self.assertIn("ArrowRight", html_text)
        self.assertIn("ArrowLeft", html_text)
        self.assertIn("<svg", html_text)
        for label in ("Kimi K3", "Nemotron-3-Ultra", "Claude Code", "claude-opus-5-5", "browser + node", "gate 94%"):
            self.assertIn(label, html_text)
        self.assertIn("data:image/png;base64,", html_text)
        self.assertIn("97.8%", html_text)
        self.assertIn("Block Catcher", html_text)
        self.assertIn("v1 (this build)", html_text)

    def test_escaping_and_no_external_requests(self):
        p, _, _ = self._populate()
        html_text = DeckBuilder(p, self.store).build()
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)
        self.assertNotIn("<img src=x", html_text)
        self.assertNotIn("<script>x</script>", html_text)
        self.assertNotIn("<b>catch</b>", html_text)
        self.assertIn("&amp; keep it short", html_text)
        self.assertNotRegex(html_text, r"""(src|href)\s*=\s*["']?(https?:)?//""")
        style_block = html_text.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertNotRegex(style_block, r"@import|url\(")
        for style_attr in re.findall(r'style="([^"]*)"', html_text):
            self.assertNotIn("url(", style_attr)
        # the hostile token value is only shown as escaped text
        self.assertIn("<code>red;background:url(http://x)</code>", html_text)
        self.assertIn("Content-Security-Policy", html_text)
        self.assertEqual(html_text.count("<script>"), 1)
        # the invalid colour token is shown as text only, never inside a style attribute
        self.assertNotRegex(html_text, r'style="background:red;')

    def test_changelog_and_slide_view(self):
        p, run, gate = self._populate()
        sm = self.store.slide_manager(p.id)
        sm.prepare_run(run)
        (sm.work_tree() / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
        deck_path = DeckBuilder(p, self.store).write_into_workspace(sm.work_tree())
        self.assertEqual(deck_path, sm.work_tree() / "kyora" / "deck.html")
        self.assertTrue(deck_path.read_text(encoding="utf-8").startswith("<!doctype html>"))
        s1 = sm.commit_slide(run, "Block Catcher", gate, run.engine, [])
        self.store.set_state(run, "complete", result_slide=s1["number"])
        run2 = self.store.create_run(p.id, "modify", "faster blocks", parent_slide=1)
        sm.prepare_run(run2)
        sm.commit_slide(run2, "Faster", None, {"backend": "claude_code", "active_model": "claude-opus-5-5"}, [])
        self.store.set_state(run2, "failed_gate")
        html_text = DeckBuilder(p.id, self.store).build()
        changelog = html_text.split('id="changelog"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(changelog.count("<li>"), 2)
        self.assertIn("v1</span> <strong>Block Catcher</strong>", changelog)
        self.assertIn("parent v1", changelog)
        self.assertIn("no gate", changelog)
        self.assertIn("gate 97.8% (45/46)", changelog)
        self.assertNotIn("this build", html_text)
        v1 = DeckBuilder(p.id, self.store).build(1)
        self.assertEqual(v1.split('id="changelog"', 1)[1].split("</section>", 1)[0].count("<li>"), 1)
        with self.assertRaises(FileNotFoundError):
            DeckBuilder(p.id, self.store).build(7)

    def test_empty_project_deck_is_honest(self):
        p = self.store.create("Nothing yet")
        html_text = DeckBuilder(p, self.store).build()
        self.assertIn("Gate: not measured", html_text)
        self.assertIn("No test results were measured", html_text)
        self.assertIn("No screenshots were captured", html_text)
        self.assertIn(HONESTY_TEXT, html_text)
        self.assertNotRegex(html_text, r"\d+\.\d%")

    def test_markdown_subset_is_escaped(self):
        out = render_markdown("## Title <x>\n\nPara **bold** and `code`\n\n1. one\n2. two\n\n```\n<tag>\n```")
        self.assertIn("<h4>Title &lt;x&gt;</h4>", out)
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<code>code</code>", out)
        self.assertIn("<ol><li>one</li><li>two</li></ol>", out.replace("\n", ""))
        self.assertIn("&lt;tag&gt;", out)


if __name__ == "__main__":
    unittest.main()
