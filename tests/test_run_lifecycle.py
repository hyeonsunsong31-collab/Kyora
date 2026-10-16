"""Regression tests for the run lifecycle and slide storage (cancel, shutdown, lineage branches, ignore rules)."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from kyora.projects.slides import git_available
from kyora.projects.store import ProjectStore


class _Home(unittest.TestCase):
    """A temporary KYORA_HOME with a project store on the backend ``vcs``."""

    vcs = "auto"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kyora-lifecycle-")
        self.home = Path(self.tmp.name)
        self._old_env = {k: os.environ.get(k) for k in ("KYORA_HOME", "GIT_CONFIG_GLOBAL")}
        os.environ["KYORA_HOME"] = str(self.home)
        self.store = ProjectStore(self.home / "projects", self.home / "kyora.db", vcs=self.vcs)

    def tearDown(self):
        self.store.close()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


# ----------------------------------------------------------------------------- ignore rules
class _IgnoredFilesScenario:
    """Slides, exports and previews hold every game file, also the ones ignore rules name."""

    def test_import_keeps_files_named_by_the_games_gitignore(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Ignored")
        sm = store.slide_manager(p.id)
        slide, files = sm.import_zip(_zip({
            "my-game/.gitignore": "dist/\nnode_modules/\n*.log\n",
            "my-game/index.html": '<canvas></canvas><script src="dist/game.js"></script>',
            "my-game/dist/game.js": "window.Game = {start(){}};",
            "my-game/notes.log": "kept",
        }), "my-game.zip")
        self.assertIn("dist/game.js", files)
        self.assertIn("dist/game.js", sm.backend.ls(slide["tag"]))
        self.assertIn("notes.log", sm.backend.ls(slide["tag"]))
        with zipfile.ZipFile(sm.export_zip(slide["number"])) as zf:
            self.assertIn("dist/game.js", zf.namelist())
        self.assertTrue((sm.materialize(slide["number"]) / "dist" / "game.js").is_file())

    def test_engine_written_ignored_files_reach_the_slide_and_rollback_removes_them(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Engine ignore")
        sm = store.slide_manager(p.id)
        wt = sm.work_tree()
        r1 = store.create_run(p.id, "generate", "game")
        sm.prepare_run(r1)
        (wt / ".gitignore").write_text("build/\n", encoding="utf-8")
        (wt / "build").mkdir()
        (wt / "build" / "bundle.js").write_text("// bundle", encoding="utf-8")
        (wt / "index.html").write_text('<script src="build/bundle.js"></script>', encoding="utf-8")
        sm.commit_iteration(r1, 0, None)
        s1 = sm.commit_slide(r1, "v1", None, None)
        self.assertIn("build/bundle.js", sm.backend.ls(s1["tag"]))
        store.set_state(r1, "complete")
        # a cancelled modification that wrote an ignored file must not leak it into the next slide
        r2 = store.create_run(p.id, "modify", "change", parent_slide=1)
        sm.prepare_run(r2)
        (wt / "build" / "stale.js").write_text("// from the cancelled run", encoding="utf-8")
        sm.rollback_working_tree(r2)
        store.set_state(r2, "cancelled")
        self.assertFalse((wt / "build" / "stale.js").exists())
        self.assertTrue((wt / "build" / "bundle.js").is_file())


@unittest.skipUnless(git_available(), "git is not installed")
class GitIgnoredFilesTests(_IgnoredFilesScenario, _Home):
    vcs = "git"

    def test_info_exclude_and_global_excludes_file_do_not_drop_files(self):
        excludes = self.home / "global-ignore"
        excludes.write_text("assets/\n", encoding="utf-8")
        gitconfig = self.home / "gitconfig"
        gitconfig.write_text(f"[core]\n\texcludesFile = {excludes.as_posix()}\n", encoding="utf-8")
        os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        p = self.store.create("Excludes")
        sm = self.store.slide_manager(p.id)
        wt = sm.work_tree()
        (wt / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (wt / ".git" / "info" / "exclude").write_text("sounds/\n", encoding="utf-8")
        slide, _ = sm.import_zip(_zip({"index.html": "<canvas></canvas>", "assets/sprite.svg": "<svg/>",
                                       "sounds/hit.wav": "RIFF"}), "game.zip")
        names = sm.backend.ls(slide["tag"])
        self.assertIn("assets/sprite.svg", names)
        self.assertIn("sounds/hit.wav", names)


class SnapshotIgnoredFilesTests(_IgnoredFilesScenario, _Home):
    vcs = "snapshot"


if __name__ == "__main__":
    unittest.main()
