"""Regression tests for the run lifecycle and slide storage (cancel, shutdown, lineage branches, ignore rules)."""
from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import kyora.pipeline.runner as runner_module
from kyora.pipeline.events import EventBus
from kyora.pipeline.runner import RunManager, Runner
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

    def test_node_modules_and_pycache_never_reach_the_slide(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Node modules")
        sm = store.slide_manager(p.id)
        wt = sm.work_tree()
        r = store.create_run(p.id, "generate", "game")
        sm.prepare_run(r)
        for name, text in {".gitignore": "node_modules/\ndist/\n__pycache__/\n",
                           "index.html": '<script src="dist/game.js"></script>',
                           "dist/game.js": "window.Game = {};", "lib/node_modules.js": "// a file, kept",
                           "node_modules/pkg/index.js": "// npm", "src/node_modules/dep/x.js": "// npm",
                           "tools/__pycache__/t.cpython-313.pyc": "pyc"}.items():
            (wt / name).parent.mkdir(parents=True, exist_ok=True)
            (wt / name).write_text(text, encoding="utf-8")
        sm.commit_iteration(r, 0, None)
        s1 = sm.commit_slide(r, "v1", None, None)
        shipped = [".gitignore", "dist/game.js", "index.html", "lib/node_modules.js"]
        self.assertEqual(sm.backend.ls(s1["tag"]), shipped)
        with zipfile.ZipFile(sm.export_zip(s1["number"])) as zf:
            self.assertEqual(sorted(n for n in zf.namelist() if not n.endswith("/")), shipped)
        self.assertEqual(_files(sm.materialize(s1["number"])), shipped)


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

    def test_node_modules_an_older_commit_tracked_leave_the_next_commit(self):
        p = self.store.create("Old modules")
        sm = self.store.slide_manager(p.id)
        wt = sm.work_tree()
        (wt / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
        (wt / "node_modules" / "old").mkdir(parents=True)
        (wt / "node_modules" / "old" / "i.js").write_text("// npm", encoding="utf-8")
        sm.backend._run("add", "-A", "-f")  # as commits staged before node_modules were left out
        sm.backend._run("commit", "-q", "-m", "older commit")
        self.assertIn("node_modules/old/i.js", sm.backend.ls("HEAD"))
        sha = sm.backend.commit("next")
        self.assertEqual(sm.backend.ls(sha), ["index.html"])


class SnapshotIgnoredFilesTests(_IgnoredFilesScenario, _Home):
    vcs = "snapshot"


# ----------------------------------------------------------------------------- lineage branches
def _files(wt: Path) -> list[str]:
    return sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*") if p.is_file() and ".git" not in p.parts)


class _LineageScenario:
    """No two unfinished runs share a branch; resume restores the run's own commit; rollback owns its tip."""

    def _blocked_first_generation(self, text: str = "GAME A"):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Lineage")
        sm = store.slide_manager(p.id)
        a = store.create_run(p.id, "generate", "game A")
        self.assertEqual(sm.prepare_run(a)["branch"], "lineage/main")
        (sm.work_tree() / "index.html").write_text(text, encoding="utf-8")
        a_sha = sm.commit_iteration(a, 0, None)
        store.set_state(a, "blocked", blocked_reason="no browser")
        return store, p, sm, a, a_sha

    def test_generation_does_not_reuse_main_held_by_a_blocked_run(self):
        store, p, sm, a, a_sha = self._blocked_first_generation()
        wt = sm.work_tree()
        b = store.create_run(p.id, "generate", "game B")
        info = sm.prepare_run(b)
        self.assertEqual(info["branch"], f"lineage/{b.id}")
        self.assertIsNone(info["base_sha"])
        self.assertEqual(_files(wt), [])
        (wt / "index.html").write_text("GAME B", encoding="utf-8")
        sm.commit_iteration(b, 0, None)
        s1 = sm.commit_slide(b, "Game B", None, None)
        store.set_state(b, "complete")
        self.assertEqual(sm.backend.branch_sha("lineage/main"), a_sha)
        # resuming A tests A's game, not B's
        sm.prepare_run(store.get_run(a.id))
        self.assertEqual(sm.backend.current_branch(), "lineage/main")
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "GAME A")
        # cancelling A only removes A's own commits; B's slide branch stays
        self.assertTrue(sm.rollback_working_tree(store.get_run(a.id)))
        self.assertIsNone(sm.backend.branch_sha("lineage/main"))
        self.assertEqual(sm.backend.branch_sha(s1["branch"]), s1["sha"])

    def test_modify_of_a_head_held_by_a_blocked_run_gets_its_own_branch(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Modify")
        sm = store.slide_manager(p.id)
        wt = sm.work_tree()
        g = store.create_run(p.id, "generate", "game")
        sm.prepare_run(g)
        (wt / "index.html").write_text("v1", encoding="utf-8")
        v1 = sm.commit_slide(g, "v1", None, None)
        store.set_state(g, "complete")
        a = store.create_run(p.id, "modify", "make it blue", parent_slide=1)
        self.assertEqual(sm.prepare_run(a)["branch"], "lineage/main")
        store.set_state(a, "blocked", blocked_reason="Kimi K3 request failed (HTTP 503)")
        b = store.create_run(p.id, "modify", "add sound", parent_slide=1)
        info_b = sm.prepare_run(b)
        self.assertEqual(info_b["branch"], f"lineage/{b.id}")
        self.assertEqual(info_b["base_sha"], v1["sha"])
        (wt / "index.html").write_text("v1 + sound (B)", encoding="utf-8")
        sm.commit_iteration(b, 0, None)
        sm.commit_slide(b, "v2 sound", None, None)
        store.set_state(b, "complete")
        sm.prepare_run(store.get_run(a.id))
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "v1")
        self.assertEqual(sm.backend.branch_sha("lineage/main"), v1["sha"])

    def test_resume_restores_the_runs_last_commit(self):
        store, p, sm, a, a_sha = self._blocked_first_generation()
        wt = sm.work_tree()
        (wt / "index.html").write_text("half-written fix", encoding="utf-8")
        (wt / "junk.js").write_text("junk", encoding="utf-8")
        info = sm.prepare_run(store.get_run(a.id))
        self.assertEqual(info["branch"], "lineage/main")
        self.assertEqual(_files(wt), ["index.html"])
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "GAME A")
        self.assertEqual(sm.backend.head(), a_sha)

    def test_resumed_fresh_generation_does_not_inherit_an_earlier_runs_files(self):
        store: ProjectStore = self.store  # type: ignore[attr-defined]
        p = store.create("Fresh resume")
        sm = store.slide_manager(p.id)
        wt = sm.work_tree()
        a = store.create_run(p.id, "generate", "game A")
        sm.prepare_run(a)
        (wt / "index.html").write_text("GAME A", encoding="utf-8")
        (wt / "src").mkdir()
        (wt / "src" / "a.js").write_text("// A only", encoding="utf-8")
        a_sha = sm.commit_iteration(a, 0, None)
        store.set_state(a, "failed", error="RuntimeError: boom")  # failed without a rollback
        b = store.create_run(p.id, "generate", "game B")
        info = sm.prepare_run(b)
        self.assertEqual((info["branch"], info["base_sha"], info["fresh_tree"]), ("lineage/main", a_sha, True))
        self.assertEqual(_files(wt), [])
        store.set_state(b, "blocked", blocked_reason="Kimi K3 unreachable")  # blocked before any commit
        info = sm.prepare_run(store.get_run(b.id))  # resume
        self.assertEqual(info["branch"], "lineage/main")
        self.assertEqual(sm.backend.head(), a_sha)
        self.assertEqual(_files(wt), [])
        (wt / "index.html").write_text("GAME B", encoding="utf-8")
        b_sha = sm.commit_iteration(store.get_run(b.id), 0, None)
        self.assertEqual(sm.backend.ls(b_sha), ["index.html"])
        # once the fresh run has committed, resume restores its own commit as it is
        (wt / "junk.js").write_text("junk", encoding="utf-8")
        sm.prepare_run(store.get_run(b.id))
        self.assertEqual(_files(wt), ["index.html"])
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "GAME B")

    def test_resume_refuses_a_branch_that_moved_and_rollback_leaves_it(self):
        from kyora.projects.slides import LineageMoved
        store, p, sm, a, a_sha = self._blocked_first_generation()
        wt = sm.work_tree()
        (wt / "index.html").write_text("someone else's commit", encoding="utf-8")
        other = sm.backend.commit("not run A")
        with self.assertRaises(LineageMoved):
            sm.prepare_run(store.get_run(a.id))
        self.assertFalse(sm.rollback_working_tree(store.get_run(a.id)))
        self.assertEqual(sm.backend.branch_sha("lineage/main"), other)
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "someone else's commit")

    def test_import_does_not_commit_onto_a_blocked_runs_branch(self):
        store, p, sm, a, a_sha = self._blocked_first_generation()
        slide, _ = sm.import_zip(_zip({"index.html": "<canvas></canvas>"}), "game.zip")
        self.assertTrue(slide["branch"].startswith("lineage/import-"))
        self.assertEqual(sm.backend.branch_sha("lineage/main"), a_sha)
        sm.prepare_run(store.get_run(a.id))
        self.assertEqual((sm.work_tree() / "index.html").read_text(encoding="utf-8"), "GAME A")

    def test_rollback_without_touching_the_tree_moves_only_the_runs_branch(self):
        store, p, sm, a, a_sha = self._blocked_first_generation()
        wt = sm.work_tree()
        b = store.create_run(p.id, "generate", "game B")
        sm.prepare_run(b)
        (wt / "index.html").write_text("B building", encoding="utf-8")
        self.assertTrue(sm.rollback_working_tree(store.get_run(a.id), touch_tree=False))
        self.assertIsNone(sm.backend.branch_sha("lineage/main"))
        self.assertEqual(sm.backend.current_branch(), f"lineage/{b.id}")
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "B building")
        # a run whose branch is checked out is not rolled back without the tree
        self.assertFalse(sm.rollback_working_tree(store.get_run(b.id), touch_tree=False))
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "B building")


@unittest.skipUnless(git_available(), "git is not installed")
class GitLineageTests(_LineageScenario, _Home):
    vcs = "git"


class SnapshotLineageTests(_LineageScenario, _Home):
    vcs = "snapshot"


# ----------------------------------------------------------------------------- run manager / runner
def _fail_stage(*_a, **_k):
    raise AssertionError("stage must not run")


class _RunnerHome(_Home):
    """A real store and event bus with stubbed stages (see ``_patch``)."""

    vcs = "git" if git_available() else "snapshot"

    def setUp(self):
        super().setUp()
        cfg = SimpleNamespace(quality=SimpleNamespace(min_pass_rate=0.94, max_fix_iterations=3),
                              claude_code=SimpleNamespace(effort="max"))
        self.bus = EventBus(None)
        self.app = SimpleNamespace(store=self.store, bus=self.bus, config=cfg, tokkio=None, nemotron=None, judge=None)
        self.narrations: list[str] = []

    def tearDown(self):
        self.bus.close()
        super().tearDown()

    def _patch(self, **stages):
        names = ("intake_stage", "plan_stage", "design_stage", "build_stage", "fix_stage", "test_stage", "gate_stage",
                 "review_stage", "finalize_stage", "select_engine")
        values = {n: stages.get(n, _fail_stage) for n in names if hasattr(runner_module, n)}
        values["narrate"] = lambda ctx, text: self.narrations.append(text)
        values["show_view"] = lambda *a, **k: None
        p = mock.patch.multiple(runner_module, **values)
        p.start()
        self.addCleanup(p.stop)

    def _blocked_after_build(self, text: str = "BUILT"):
        """A first generation that built and committed iteration 0, then blocked at TEST."""
        p = self.store.create("Runs")
        sm = self.store.slide_manager(p.id)
        run = self.store.create_run(p.id, "generate", "a game")
        sm.prepare_run(run)
        (sm.work_tree() / "index.html").write_text(text, encoding="utf-8")
        sha = sm.commit_iteration(run, 0, None)
        run.iterations.append({"n": 0, "kind": "build", "commit": sha, "built": True})
        for st in ("intake", "plan", "design"):
            run.stages[st] = {"status": "done"}
        self.store.set_state(run, "blocked", blocked_reason="no browser")
        return p, sm, run, sha


class CancelTests(_RunnerHome):
    """cancel() never touches the work tree while another run of the project is executing."""

    def _running_a(self, rm: RunManager, pid: str, sm):
        """Run A executes (worker holds the project lock) with uncommitted fix work in the tree."""
        a = self.store.create_run(pid, "generate", "game A")
        info = sm.prepare_run(a)
        wt = sm.work_tree()
        (wt / "index.html").write_text("A v1", encoding="utf-8")
        sm.commit_iteration(a, 0, None)
        (wt / "index.html").write_text("A v2 (fix in progress)", encoding="utf-8")
        (wt / "src").mkdir(exist_ok=True)
        (wt / "src" / "game.js").write_text("// written by the engine", encoding="utf-8")
        plock = rm._project_lock(pid)
        plock.acquire()
        self.addCleanup(plock.release)
        rm._running[a.id] = threading.Event()
        return a, info["branch"]

    def _assert_a_untouched(self, sm, branch):
        wt = sm.work_tree()
        self.assertEqual(sm.backend.current_branch(), branch)
        self.assertEqual((wt / "index.html").read_text(encoding="utf-8"), "A v2 (fix in progress)")
        self.assertTrue((wt / "src" / "game.js").is_file())

    def test_cancel_of_a_queued_run_leaves_the_running_tree_alone(self):
        rm = RunManager(self.app, workers=1)  # workers not started: B waits in the queue
        p = self.store.create("Cancel queued")
        sm = self.store.slide_manager(p.id)
        a, a_branch = self._running_a(rm, p.id, sm)
        b = self.store.create_run(p.id, "modify", "game B")
        self.assertTrue(rm.submit(b.id))
        self.assertTrue(rm.cancel(b.id))
        self.assertEqual(self.store.get_run(b.id).state, "cancelled")
        self._assert_a_untouched(sm, a_branch)

    def test_cancel_of_a_blocked_run_moves_only_its_branch_while_another_run_executes(self):
        rm = RunManager(self.app, workers=1)
        p, sm, b, b_sha = self._blocked_after_build("B built")  # B holds lineage/main
        a, a_branch = self._running_a(rm, p.id, sm)
        self.assertNotEqual(a_branch, "lineage/main")
        self.assertTrue(rm.cancel(b.id))
        self.assertEqual(self.store.get_run(b.id).state, "cancelled")
        self._assert_a_untouched(sm, a_branch)
        self.assertIsNone(sm.backend.branch_sha("lineage/main"))  # B's own commits are gone

    def test_cancel_never_resets_a_checked_out_branch_while_another_run_executes(self):
        rm = RunManager(self.app, workers=1)
        p, sm, b, b_sha = self._blocked_after_build("B built")
        # another run executes on the same checked-out branch (e.g. prepared before branches were exclusive)
        plock = rm._project_lock(p.id)
        plock.acquire()
        self.addCleanup(plock.release)
        (sm.work_tree() / "index.html").write_text("A v2 (fix in progress)", encoding="utf-8")
        self.assertTrue(rm.cancel(b.id))
        self.assertEqual(self.store.get_run(b.id).state, "cancelled")
        self.assertEqual((sm.work_tree() / "index.html").read_text(encoding="utf-8"), "A v2 (fix in progress)")
        self.assertEqual(sm.backend.branch_sha("lineage/main"), b_sha)

    def test_cancel_of_a_blocked_run_on_an_idle_project_resets_the_tree(self):
        rm = RunManager(self.app, workers=1)
        p, sm, b, b_sha = self._blocked_after_build("B built")
        (sm.work_tree() / "leftover.js").write_text("x", encoding="utf-8")
        self.assertTrue(rm.cancel(b.id))
        self.assertEqual(_files(sm.work_tree()), [])
        self.assertIsNone(sm.backend.branch_sha("lineage/main"))


class ShutdownTests(_RunnerHome):
    """Stopping Kyora interrupts runs (blocked, resumable, work kept); it never cancels them."""

    def test_stop_blocks_a_running_run_without_rolling_it_back(self):
        waiting = threading.Event()

        def build(ctx):
            (ctx.workspace / "index.html").write_text("<script>40 minutes of engine output</script>", encoding="utf-8")

        def test(ctx, n):  # like collector.wait: blocks until results arrive or the run is cancelled
            waiting.set()
            while not ctx.cancel.wait(0.05):
                pass
            raise runner_module.RunCancelled()

        self._patch(intake_stage=lambda ctx: {}, plan_stage=lambda ctx: {"title": "t", "acceptance_tests": []},
                    design_stage=lambda ctx: {}, build_stage=build, test_stage=test,
                    select_engine=lambda ctx: SimpleNamespace(name="claude_code"))
        p = self.store.create("Stop")
        rm = RunManager(self.app, workers=1)
        rm.start()
        run = self.store.create_run(p.id, "generate", "make a game")
        rm.submit(run.id)
        self.assertTrue(waiting.wait(10))
        sm = self.store.slide_manager(p.id)
        main = sm.backend.branch_sha("lineage/main")
        self.assertIsNotNone(main)
        rm.stop()
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "blocked")
        self.assertTrue(after.blocked_reason.startswith("interrupted: Kyora stopped"))
        self.assertEqual(sm.backend.branch_sha("lineage/main"), main)
        self.assertTrue((sm.work_tree() / "index.html").is_file())
        types = [e.type for e in self.bus.replay(0, run_id=run.id)]
        self.assertIn("run.blocked", types)
        self.assertNotIn("run.cancelled", types)
        # after a restart the run is still resumable
        rm2 = RunManager(self.app, workers=1)
        rm2._recover_interrupted()
        self.assertEqual(self.store.get_run(run.id).state, "blocked")
        self.assertTrue(rm2.resume(run.id))

    def test_a_run_waiting_for_the_project_lock_does_not_start_after_stop(self):
        self._patch()  # any stage call fails the run
        p = self.store.create("Queued at stop")
        rm = RunManager(self.app, workers=1)
        plock = rm._project_lock(p.id)
        plock.acquire()  # another run of the project is executing
        rm.start()
        run = self.store.create_run(p.id, "generate", "make a game")
        rm.submit(run.id)
        for _ in range(200):  # until the worker took the run and waits for the project lock
            if rm._queue.empty():
                break
            threading.Event().wait(0.01)
        rm._stopping.set()
        plock.release()
        rm.stop()
        self.assertEqual(self.store.get_run(run.id).state, "created")  # marked blocked on the next start
        rm2 = RunManager(self.app, workers=1)
        rm2._recover_interrupted()
        self.assertEqual(self.store.get_run(run.id).state, "blocked")


class BuildNarrationTests(_RunnerHome):
    """The build is announced only after an engine was selected, naming the real backend and effort."""

    def _run_until_build(self, select_engine, effort: str):
        self.app.config.claude_code.effort = effort

        def build(ctx):
            raise runner_module.StageBlocked("stop after the announcement", "build")

        self._patch(intake_stage=lambda ctx: {}, plan_stage=lambda ctx: {"title": "t", "acceptance_tests": []},
                    design_stage=lambda ctx: {}, build_stage=build, select_engine=select_engine)
        p = self.store.create("Narration")
        run = self.store.create_run(p.id, "generate", "a game")
        Runner(self.app, run, threading.Event()).execute()
        return [t for t in self.narrations if "building" in t], self.store.get_run(run.id)

    def test_names_the_selected_backend_and_configured_effort(self):
        said, _ = self._run_until_build(lambda ctx: SimpleNamespace(name="anthropic_api"), "high")
        self.assertEqual(said, ["The Anthropic API engine is now building your game at high effort."])
        def hub(backend, details=None):
            av = SimpleNamespace(details={"remote_backend": backend, "remote_engine": {"details": details or {}}})
            return lambda ctx: SimpleNamespace(name="hub", availability=lambda: av)

        said, _ = self._run_until_build(hub("claude_code"), "max")
        self.assertEqual(said[-1], "Claude Code on the Kyora Hub is now building your game at maximum effort.")
        said, _ = self._run_until_build(hub("openai_agent", {"model": "z-ai/glm-5.3"}), "max")
        self.assertEqual(said[-1], "Kyora's own code engine on the Kyora Hub is now building your game with z-ai/glm-5.3.")
        said, _ = self._run_until_build(lambda ctx: SimpleNamespace(name="hub"), "max")  # status unknown
        self.assertEqual(said[-1], "The code engine on the Kyora Hub is now building your game at maximum effort.")

    def test_nothing_is_announced_when_no_engine_is_available(self):
        def no_engine(ctx):
            raise runner_module.StageBlocked("no code engine is available", "build")

        said, run = self._run_until_build(no_engine, "max")
        self.assertEqual(said, [])
        self.assertEqual(run.state, "blocked")
        self.assertEqual(run.blocked_reason, "no code engine is available")


class RunnerResumeTests(_RunnerHome):
    def test_resume_on_a_moved_branch_blocks_instead_of_testing_another_game(self):
        p, sm, run, sha = self._blocked_after_build()
        (sm.work_tree() / "index.html").write_text("ANOTHER GAME", encoding="utf-8")
        other = sm.backend.commit("someone else")
        self._patch()
        self.store.set_state(run, "created", blocked_reason=None)  # what RunManager.resume does
        Runner(self.app, self.store.get_run(run.id), threading.Event()).execute()
        after = self.store.get_run(run.id)
        self.assertEqual(after.state, "blocked")
        self.assertIn("moved", after.blocked_reason)
        self.assertEqual(sm.backend.branch_sha("lineage/main"), other)


if __name__ == "__main__":
    unittest.main()
