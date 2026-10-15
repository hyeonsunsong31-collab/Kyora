"""Tests for kyora.projects.store and kyora.projects.slides (real git + snapshot backend)."""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from kyora.projects.slides import GitBackend, SnapshotBackend, VersionError, git_available
from kyora.projects.store import (MAX_ATTACHMENT_BYTES, TEXT_EXCERPT_BYTES, Attachment, Project, ProjectStore, Run,
                                  sanitize_filename)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAME_OK = FIXTURES / "game-ok"
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class _TempHome(unittest.TestCase):
    """Creates a temp KYORA_HOME for every test."""

    vcs = "auto"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kyora-store-")
        self.home = Path(self.tmp.name)
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)
        self.store = ProjectStore(self.home / "projects", self.home / "kyora.db", vcs=self.vcs)

    def tearDown(self):
        self.store.close()
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self.tmp.cleanup()


class RecordTests(unittest.TestCase):
    def test_project_round_trip_preserves_unknown_keys(self):
        d = {"id": "p_1", "name": "Sky", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
             "engine": "vanilla", "repo": {"backend": "git", "branch": "lineage/main", "x": 1},
             "attachments": [{"id": "a_1", "name": "r.png", "mime": "image/png", "size": 3,
                              "path": "attachments/a_1_r.png", "role": "reference", "future": True}],
             "slides": [{"number": 1, "tag": "slide/v1", "sha": "abc", "parent": None}],
             "current_slide": 1, "custom": {"k": [1, 2]}}
        p = Project.from_dict(d)
        self.assertIsInstance(p.attachments[0], Attachment)
        self.assertEqual(p.to_dict(), d)
        self.assertEqual(list(p.to_dict())[:9], ["id", "name", "created_at", "updated_at", "engine", "repo",
                                                  "attachments", "slides", "current_slide"])

    def test_run_round_trip_and_defaults(self):
        r = Run.from_dict({"id": "r_1", "project_id": "p_1", "kind": "modify", "new_field": "kept"})
        d = r.to_dict()
        self.assertEqual(d["new_field"], "kept")
        self.assertEqual(d["state"], "created")
        self.assertEqual(d["gate"], {"threshold": 0.94, "final": None})
        self.assertEqual(d["options"], {"engine_pref": "auto", "import_zip_attachment": None})
        self.assertEqual(d["judge"], {"allowed": 0, "denied": 0, "unavailable": 0})
        for key in ("id", "project_id", "kind", "parent_slide", "prompt", "attachment_ids", "options", "state",
                    "iteration", "stages", "artifacts", "engine", "iterations", "gate", "judge", "result_slide",
                    "error", "blocked_reason", "created_at", "finished_at"):
            self.assertIn(key, d)
        self.assertEqual(Run.from_dict(d).to_dict(), d)

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Users\\me\\ref image.png"), "ref image.png")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename(".hidden"), "hidden")
        self.assertEqual(sanitize_filename("a<b>:c|d?.js"), "a_b_c_d_.js")
        self.assertEqual(sanitize_filename(""), "file")
        long = sanitize_filename("x" * 300 + ".png")
        self.assertTrue(long.endswith(".png"))
        self.assertLessEqual(len(long), 100)


class StoreTests(_TempHome):
    def test_crud_and_order(self):
        a = self.store.create("First game")
        b = self.store.create("  Second   game ")
        self.assertEqual(b.name, "Second game")
        self.assertTrue(a.id.startswith("p_"))
        paths = self.store.paths(a.id)
        for d in (paths.repo, paths.runs, paths.cache, paths.attachments):
            self.assertTrue(d.is_dir(), d)
        self.assertIn(a.repo["backend"], ("git", "snapshot"))
        self.assertEqual(a.repo["branch"], "lineage/main")
        self.assertEqual([p.id for p in self.store.list()], [b.id, a.id])
        got = self.store.get(a.id)
        self.assertEqual(got.name, "First game")
        with self.assertRaises(FileNotFoundError):
            self.store.get("p_missing")
        with self.assertRaises(FileNotFoundError):
            self.store.get("../etc")
        with self.assertRaises(ValueError):
            self.store.create("   ")
        self.store.delete(a.id)
        self.assertFalse(paths.root.exists())
        self.assertEqual([p.id for p in self.store.list()], [b.id])
        with self.assertRaises(FileNotFoundError):
            self.store.delete(a.id)

    def test_index_rebuilt_from_disk(self):
        p = self.store.create("Persisted")
        r = self.store.create_run(p.id, "generate", "hello")
        self.store.close()
        store2 = ProjectStore(self.home / "projects", self.home / "other.db", vcs=self.vcs)
        try:
            self.assertEqual([x.id for x in store2.list()], [p.id])
            self.assertEqual(store2.get_run(r.id).prompt, "hello")
            self.assertEqual([x.id for x in store2.list_runs(p.id)], [r.id])
        finally:
            store2.close()
        self.store = ProjectStore(self.home / "projects", self.home / "kyora.db", vcs=self.vcs)

    def test_attachments_and_views(self):
        p = self.store.create("Attach")
        big_text = ("line of design notes\n" * 4000).encode("utf-8")  # > 60 KB
        self.assertGreater(len(big_text), TEXT_EXCERPT_BYTES)
        t = self.store.add_attachment(p.id, "notes.md", "text/markdown", big_text)
        img = self.store.add_attachment(p.id, "../ref image.png", "", PNG_1x1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(450):
                zf.writestr(f"game/assets/f{i:03d}.txt", "x")
        z = self.store.add_attachment(p.id, "game.zip", "application/octet-stream", buf.getvalue(), role="import")
        self.assertEqual(img.name, "ref image.png")
        self.assertEqual(img.mime, "image/png")
        self.assertEqual(z.mime, "application/zip")
        self.assertEqual(z.role, "import")
        self.assertTrue(img.path.startswith(f"attachments/{img.id}_"))
        self.assertTrue(self.store.attachment_path(p.id, img.id).is_file())
        proj = self.store.get(p.id)
        self.assertEqual([a.id for a in proj.attachments], [t.id, img.id, z.id])

        views = {v["id"]: v for v in self.store.attachment_views(p.id, [t.id, img.id, z.id])}
        tv = views[t.id]
        self.assertTrue(tv["text_excerpt"].startswith("line of design notes"))
        self.assertLessEqual(len(tv["text_excerpt"].encode("utf-8")), TEXT_EXCERPT_BYTES)
        self.assertTrue(tv["truncated"])
        self.assertIsNone(tv["image_data_url"])
        self.assertIsNone(tv["zip_listing"])
        iv = views[img.id]
        self.assertTrue(iv["image_data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(iv["image_data_url"].split(",", 1)[1]), PNG_1x1)
        zv = views[z.id]
        self.assertEqual(len(zv["zip_listing"]), 400)
        self.assertEqual(zv["zip_listing"], zv["listing"])
        self.assertEqual(zv["listing"][0], "game/assets/f000.txt")
        for v in views.values():
            for key in ("id", "name", "mime", "size", "role", "path", "text_excerpt", "image_data_url",
                        "zip_listing", "listing"):
                self.assertIn(key, v)
        self.assertEqual(len(self.store.attachment_views(p.id)), 3)
        with self.assertRaises(FileNotFoundError):
            self.store.attachment_views(p.id, ["a_nope"])
        with self.assertRaises(FileNotFoundError):
            self.store.attachment_path(p.id, "a_nope")
        with self.assertRaises(ValueError):
            self.store.add_attachment(p.id, "x.txt", "text/plain", b"x", role="Bad Role!")

    def test_attachment_size_cap(self):
        p = self.store.create("Cap")
        with self.assertRaises(ValueError):
            self.store.add_attachment(p.id, "huge.bin", None, bytearray(MAX_ATTACHMENT_BYTES + 1))
        self.assertEqual(self.store.get(p.id).attachments, [])

    def test_runs(self):
        p = self.store.create("Runs")
        att = self.store.add_attachment(p.id, "a.txt", "text/plain", b"hello")
        r1 = self.store.create_run(p.id, "generate", "make a game", None, [att.id], {"engine_pref": "claude_code"})
        time.sleep(0.01)
        r2 = self.store.create_run(p.id, "generate", "second")
        self.assertTrue(r1.id.startswith("r_"))
        self.assertEqual(r1.state, "created")
        self.assertEqual(r1.iteration, 0)
        self.assertEqual(r1.options["engine_pref"], "claude_code")
        self.assertIn("import_zip_attachment", r1.options)
        self.assertTrue((self.store.run_dir(r1) / "run.json").is_file())
        self.assertEqual(self.store.get_run(r1.id).attachment_ids, [att.id])
        self.assertEqual([r.id for r in self.store.list_runs(p.id)], [r2.id, r1.id])
        self.assertEqual([r.id for r in self.store.list_runs()][:2], [r2.id, r1.id])
        self.store.set_state(r1, "build", iteration=1, custom_note="x")
        again = self.store.get_run(r1.id)
        self.assertEqual((again.state, again.iteration), ("build", 1))
        self.assertEqual(again.to_dict()["custom_note"], "x")
        self.assertIsNone(again.finished_at)
        self.store.set_state(again, "complete", result_slide=None)
        self.assertIsNotNone(self.store.get_run(r1.id).finished_at)
        with self.assertRaises(ValueError):
            self.store.set_state(again, "exploded")
        with self.assertRaises(ValueError):
            self.store.create_run(p.id, "remix", "x")
        with self.assertRaises(ValueError):
            self.store.create_run(p.id, "modify", "x", parent_slide=3)
        with self.assertRaises(ValueError):
            self.store.create_run(p.id, "generate", "x", attachment_ids=["a_missing"])
        with self.assertRaises(ValueError):
            self.store.create_run(p.id, "generate", "x", options={"import_zip_attachment": "a_missing"})
        with self.assertRaises(FileNotFoundError):
            self.store.get_run("r_unknown")


def _copy_fixture(dst: Path) -> None:
    for item in GAME_OK.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


class _SlidesScenario:
    """Shared slide scenario run against both backends."""

    vcs = "auto"
    expected_backend = ""

    def _gate(self, passed, total):
        return {"passed": passed, "total": total, "pass_rate": passed / total, "ok": passed / total >= 0.94,
                "threshold": 0.94, "checks": [{"id": "x"}]}

    def test_full_lineage(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Sky Runner")
        self.assertEqual(p.repo["backend"], self.expected_backend)
        sm = store.slide_manager(p.id)
        self.assertEqual(sm.backend.kind, self.expected_backend)
        wt = sm.work_tree()

        # v1: generate on lineage/main
        r1 = store.create_run(p.id, "generate", "a catch game")
        info = sm.prepare_run(r1)
        self.assertEqual(info["branch"], "lineage/main")
        self.assertIsNone(info["base_sha"])
        self.assertEqual(store.get_run(r1.id).options["branch"], "lineage/main")
        _copy_fixture(wt)
        it_sha = sm.commit_iteration(r1, 1, self._gate(5, 6))
        self.assertTrue(it_sha)
        shots_dir = store.run_dir(r1) / "iter-1" / "screenshots"
        shots_dir.mkdir(parents=True)
        (shots_dir / "start.png").write_bytes(PNG_1x1)
        from kyora.projects.deck import DeckBuilder
        DeckBuilder(store.get(p.id), store).write_into_workspace(wt)
        s1 = sm.commit_slide(r1, "Initial generation", self._gate(46, 46),
                             {"backend": "claude_code", "active_model": "claude-opus-5-5", "fallback_used": True},
                             [shots_dir / "start.png"])
        self.assertEqual((s1["number"], s1["tag"], s1["parent"], s1["run_id"]), (1, "slide/v1", None, r1.id))
        self.assertEqual(s1["gate"]["passed"], 46)
        self.assertNotIn("checks", s1["gate"])
        self.assertEqual(s1["screenshots"], [f"runs/{r1.id}/iter-1/screenshots/start.png"])
        self.assertEqual(r1.result_slide, 1)
        proj = store.get(p.id)
        self.assertEqual(proj.current_slide, 1)
        self.assertEqual(len(proj.slides), 1)
        tags = sm.backend.list_tags()
        self.assertEqual(tags[0]["name"], "slide/v1")
        self.assertEqual(tags[0]["meta"]["title"], "Initial generation")
        self.assertEqual(tags[0]["meta"]["number"], 1)
        self.assertEqual(tags[0]["sha"], s1["sha"])
        self.assertIn("src/game.js", sm.backend.ls("slide/v1"))

        # v2: modify on top of v1 (head of lineage/main -> same branch)
        r2 = store.create_run(p.id, "modify", "add a power-up", parent_slide=1)
        info2 = sm.prepare_run(r2)
        self.assertEqual(info2["branch"], "lineage/main")
        self.assertFalse(info2["created_branch"])
        self.assertEqual(info2["base_sha"], s1["sha"])
        (wt / "src" / "powerup.js").write_text("window.PowerUp = 1;\n", encoding="utf-8")
        s2 = sm.commit_slide(r2, "Power-up", self._gate(40, 46), {"backend": "claude_code"}, [])
        self.assertEqual((s2["number"], s2["parent"], s2["branch"]), (2, 1, "lineage/main"))

        # v3: modify v1 again -> v1 is no longer the head -> new lineage branch
        r3 = store.create_run(p.id, "modify", "alternative", parent_slide=1)
        info3 = sm.prepare_run(r3)
        self.assertEqual(info3["branch"], f"lineage/{r3.id}")
        self.assertTrue(info3["created_branch"])
        self.assertFalse((wt / "src" / "powerup.js").exists())
        self.assertEqual(sm.backend.current_branch(), f"lineage/{r3.id}")
        (wt / "alt.txt").write_text("alt", encoding="utf-8")
        s3 = sm.commit_slide(r3, "Alternative", None, None, [])
        self.assertEqual((s3["number"], s3["parent"], s3["branch"]), (3, 1, f"lineage/{r3.id}"))
        self.assertEqual(sm.backend.branch_sha("lineage/main"), s2["sha"])
        self.assertEqual([s["number"] for s in sm.list_slides()], [1, 2, 3])

        # archive content
        out = Path(self.tmp.name) / "v2.zip"  # type: ignore[attr-defined]
        sm.backend.archive_zip("slide/v2", out)
        names = [n for n in zipfile.ZipFile(out).namelist() if not n.endswith("/")]
        self.assertIn("src/powerup.js", names)
        self.assertIn("index.html", names)
        self.assertNotIn("alt.txt", names)

        # materialize + reuse
        m1 = sm.materialize(1)
        self.assertEqual(m1, store.paths(p.id).cache / "slides" / "v1")
        self.assertTrue((m1 / "index.html").is_file())
        self.assertFalse((m1 / "src" / "powerup.js").exists())
        probe = m1 / "probe.txt"
        probe.write_text("still here", encoding="utf-8")
        self.assertEqual(sm.materialize("v1"), m1)
        self.assertTrue(probe.exists(), "cached materialization must be reused while the sha matches")
        (store.paths(p.id).cache / "slides" / "v1.sha").write_text("stale\n", encoding="utf-8")
        sm.materialize(1)
        self.assertFalse(probe.exists(), "stale marker must trigger a fresh extraction")
        with self.assertRaises(FileNotFoundError):
            sm.materialize(9)

        # export zip includes the committed deck
        z = sm.export_zip(1)
        self.assertEqual(z.name, "sky-runner-v1.zip")
        znames = zipfile.ZipFile(z).namelist()
        self.assertIn("kyora/deck.html", znames)
        self.assertIn(".kyora/manifest.json", znames)
        deck = zipfile.ZipFile(z).read("kyora/deck.html").decode("utf-8")
        self.assertIn('id="architecture"', deck)

        # rollback: cancelled run on top of v2
        r4 = store.create_run(p.id, "modify", "will be cancelled", parent_slide=2)
        info4 = sm.prepare_run(r4)
        self.assertEqual(info4["base_sha"], s2["sha"])
        (wt / "src" / "game.js").write_text("broken", encoding="utf-8")
        (wt / "junk.txt").write_text("junk", encoding="utf-8")
        sm.commit_iteration(r4, 1, None)
        (wt / "more-junk.txt").write_text("junk", encoding="utf-8")
        sm.rollback_working_tree(r4)
        self.assertFalse((wt / "junk.txt").exists())
        self.assertFalse((wt / "more-junk.txt").exists())
        self.assertTrue((wt / "src" / "powerup.js").exists())
        self.assertIn("window.Game", (wt / "src" / "game.js").read_text(encoding="utf-8"))
        self.assertEqual(sm.backend.head(), s2["sha"])
        self.assertEqual(len(store.get(p.id).slides), 3)

        # generate on a project that has slides -> fresh, empty lineage
        r5 = store.create_run(p.id, "generate", "start over")
        info5 = sm.prepare_run(r5)
        self.assertEqual(info5["branch"], f"lineage/{r5.id}")
        self.assertTrue(info5["fresh_tree"])
        self.assertEqual([x.name for x in wt.iterdir() if x.name != ".git"], [])
        (wt / "index.html").write_text("<!doctype html><canvas></canvas>", encoding="utf-8")
        sm.rollback_working_tree(r5)
        self.assertEqual([x.name for x in wt.iterdir() if x.name != ".git"], [])

        # prepare_run is idempotent for a prepared (resumed) run
        again = sm.prepare_run(store.get_run(r3.id))
        self.assertEqual(again["branch"], f"lineage/{r3.id}")

    def test_rollback_of_first_run_empties_tree(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Empty")
        sm = store.slide_manager(p.id)
        r = store.create_run(p.id, "generate", "x")
        sm.prepare_run(r)
        _copy_fixture(sm.work_tree())
        sm.commit_iteration(r, 1, None)
        sm.rollback_working_tree(r)
        self.assertEqual([x.name for x in sm.work_tree().iterdir() if x.name != ".git"], [])
        self.assertIsNone(sm.backend.head())
        # the next generate starts cleanly on lineage/main
        r2 = store.create_run(p.id, "generate", "y")
        self.assertEqual(sm.prepare_run(r2)["branch"], "lineage/main")
        _copy_fixture(sm.work_tree())
        s = sm.commit_slide(r2, "First", None, None, [])
        self.assertEqual(s["number"], 1)

    def test_import_zip_strips_top_folder(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Imported")
        sm = store.slide_manager(p.id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for f in sorted(GAME_OK.rglob("*")):
                if f.is_file():
                    zf.write(f, "my-game/" + f.relative_to(GAME_OK).as_posix())
            zf.writestr("__MACOSX/my-game/._index.html", "junk")
        slide, files = sm.import_zip(buf.getvalue(), "my-game.zip")
        self.assertEqual(slide["title"], "Imported my-game.zip")
        self.assertIsNone(slide["gate"])
        self.assertEqual(slide["engine"]["backend"], "import")
        self.assertIn("index.html", files)
        self.assertIn("src/game.js", sm.backend.ls("slide/v1"))
        self.assertTrue((sm.work_tree() / "index.html").is_file())
        # a second import (slides exist) goes onto a new lineage branch
        slide2, _ = sm.import_zip(buf.getvalue(), "again.zip")
        self.assertEqual(slide2["number"], 2)
        self.assertTrue(slide2["branch"].startswith("lineage/import-"))
        self.assertEqual(sm.backend.branch_sha("lineage/main"), slide["sha"])
        # a malicious zip is rejected before the repository changes
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "x")
        head = sm.backend.head()
        with self.assertRaises(ValueError):
            sm.import_zip(bad.getvalue(), "evil.zip")
        self.assertEqual(sm.backend.head(), head)
        self.assertEqual(len(store.get(p.id).slides), 2)


@unittest.skipUnless(git_available(), "git is not installed")
class GitSlidesTests(_SlidesScenario, _TempHome):
    vcs = "git"
    expected_backend = "git"

    def test_git_repo_details(self):
        p = self.store.create("Git details")
        repo = self.store.paths(p.id).repo
        self.assertTrue((repo / ".git").is_dir())
        b = GitBackend(repo)
        self.assertEqual(b.current_branch(), "lineage/main")
        self.assertFalse(b.has_commits())
        self.assertIsNone(b.head())
        (repo / "a.txt").write_text("a\r\nb\n", encoding="utf-8", newline="")
        sha = b.commit("first", {"k": "v"})
        self.assertEqual(len(sha), 40)
        b.tag("slide/v1", sha, {"number": 1, "title": "t<>"})
        self.assertEqual(b.list_tags()[0]["meta"], {"number": 1, "title": "t<>"})
        with self.assertRaises(VersionError):
            b.tag("slide/v1", sha, {})
        with self.assertRaises(VersionError):
            b.checkout("--orphan")
        # core.autocrlf=false keeps bytes exact
        out = Path(self.tmp.name) / "a.zip"
        b.archive_zip("slide/v1", out)
        self.assertEqual(zipfile.ZipFile(out).read("a.txt"), b"a\r\nb\n")


class SnapshotSlidesTests(_SlidesScenario, _TempHome):
    vcs = "snapshot"
    expected_backend = "snapshot"

    def test_snapshot_store_layout(self):
        p = self.store.create("Snap")
        paths = self.store.paths(p.id)
        self.assertTrue((paths.snapshots / "state.json").is_file())
        self.assertFalse((paths.repo / ".git").exists())
        b = SnapshotBackend(paths.repo, paths.snapshots)
        (paths.repo / "x.txt").write_text("1", encoding="utf-8")
        sha = b.commit("c1")
        self.assertEqual(sha, "snap-1")
        self.assertTrue((paths.snapshots / "1" / "x.txt").is_file())
        state = json.loads((paths.snapshots / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["branches"]["lineage/main"], "snap-1")
        with self.assertRaises(VersionError):
            b.checkout("slide/v9")


if __name__ == "__main__":
    unittest.main()
