"""Repository "slides": every generation/modification is a commit tagged ``slide/vN``.

Two interchangeable backends implement :class:`VersionBackend`:

* :class:`GitBackend` - the real thing: ``git`` subprocesses (always with a fixed
  identity, ``core.autocrlf=false`` and signing disabled), annotated tags whose
  message is the slide's JSON meta, lineage branches (``lineage/main``,
  ``lineage/<rid>``) and ``git archive --format=zip`` for downloads/previews.
  Commits stage every file of the work tree, including files that the game's
  ``.gitignore``, ``.git/info/exclude`` or the user's global excludes file
  ignore: the gate measures the whole tree, so the slide must hold all of it.
  Only directories the gate never measures (:data:`UNSHIPPED_DIRS`, e.g.
  ``node_modules``) stay out of every slide, at any depth.
* :class:`SnapshotBackend` - used when git is not installed: every commit is a
  full copy under ``snapshots/<n>/``; the same API, with branch/tag pointers in
  ``snapshots/state.json``. The UI reports which backend a project uses.

:class:`SlideManager` implements the lineage rules on top of a backend:

* generate without slides works on ``lineage/main``; a generate when slides
  exist starts a fresh, empty lineage ``lineage/<rid>``;
* modify checks out the parent slide; if the parent is not the head of its
  lineage branch, a new branch ``lineage/<rid>`` is created from it, so stacking
  on an old slide never rewrites history;
* a branch held by another unfinished run (prepared, not ended, no slide yet)
  is never shared: the new run gets its own ``lineage/<rid>`` instead;
* every run records the commit it last left on its branch (``options.tip``);
  resume restores exactly that commit (or raises :class:`LineageMoved` when the
  branch moved on) and rollback only resets a branch whose tip is still the run's.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..pipeline.quality import SKIP_DIRS
from ..util import procs
from ..util.fs import atomic_write_json, atomic_write_text, read_json, rm_tree, which
from ..util.ids import new_id, now_iso, slug
from .zipper import import_game_zip, unzip_safe, zip_dir

if TYPE_CHECKING:  # pragma: no cover
    from .store import Project, ProjectPaths, ProjectStore, Run

log = logging.getLogger("kyora.projects.slides")

MAIN_BRANCH = "lineage/main"
TAG_PREFIX = "slide/v"
_TAG_RE = re.compile(r"^slide/v(\d+)$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}^~-]{0,200}$")
_UNSET: Any = object()
_GATE_KEYS = ("pass_rate", "passed", "total", "ok", "threshold", "required_failures", "missing_spec_tests")
_ENGINE_KEYS = ("backend", "requested_model", "active_model", "fallback_used", "ok", "engine_errors")
# Directories the gate never measures (quality.SKIP_DIRS): never part of a slide, export or preview, at any depth.
UNSHIPPED_DIRS: tuple[str, ...] = tuple(sorted(SKIP_DIRS - {".git"}))
_UNSHIPPED_GLOBS: tuple[str, ...] = tuple(f"**/{d}/**" for d in UNSHIPPED_DIRS)


class VersionError(RuntimeError):
    """A version-control operation failed (message includes the backend error)."""


class LineageMoved(VersionError):
    """A resumed run's branch no longer points at the commit the run left there."""


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.match(ref) or ".." in ref:
        raise VersionError(f"invalid ref {ref!r}")
    return ref


def _on_rm_error(func, path, exc_info):  # pragma: no cover - Windows read-only files
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def clear_work_tree(root: Path, keep: tuple[str, ...] = (".git",)) -> None:
    """Delete everything inside ``root`` except the top-level names in ``keep``."""
    root = Path(root)
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        return
    for child in root.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, onerror=_on_rm_error)
        else:
            try:
                child.unlink()
            except OSError:
                os.chmod(child, stat.S_IWRITE)
                child.unlink()


def copy_tree(src: Path, dst: Path, exclude_top: tuple[str, ...] = (".git",),
              exclude_dirs: tuple[str, ...] = ()) -> None:
    """Copy regular files/dirs from ``src`` into ``dst``.

    Symlinks, the top-level names in ``exclude_top`` and directories named in
    ``exclude_dirs`` (at any depth) are skipped.
    """
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        rel = base.relative_to(src)
        keep = []
        for d in dirnames:
            if (rel == Path(".") and d in exclude_top) or d in exclude_dirs or (base / d).is_symlink():
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            if rel == Path(".") and name in exclude_top:
                continue
            p = base / name
            if p.is_symlink() or not p.is_file():
                continue
            shutil.copy2(p, target_dir / name)


# ----------------------------------------------------------------------------- backend API
class VersionBackend(ABC):
    """Minimal version-control API used by :class:`SlideManager`."""

    kind: str = "abstract"

    def __init__(self, work_tree: Path):
        self.work_tree = Path(work_tree)

    @abstractmethod
    def init(self) -> None:
        """Create the repository (idempotent); HEAD on ``lineage/main``."""

    @abstractmethod
    def checkout(self, ref: str) -> None:
        """Force-checkout ``ref`` (branch attaches HEAD; tag/sha detaches) and remove untracked and ignored files."""

    @abstractmethod
    def checkout_orphan(self, name: str) -> None:
        """Point HEAD at the new, unborn branch ``name`` with an empty index and work tree."""

    @abstractmethod
    def commit(self, message: str, meta: dict | None = None) -> str:
        """Commit every change in the work tree, ignored files included (empty commits allowed); return the sha."""

    @abstractmethod
    def tag(self, name: str, sha: str, meta: dict) -> None:
        """Create an annotated tag ``name`` at ``sha`` whose message is ``meta`` as JSON."""

    @abstractmethod
    def archive_zip(self, ref: str, out_path: Path) -> Path:
        """Write the tree of ``ref`` as a ZIP to ``out_path``."""

    @abstractmethod
    def ls(self, ref: str) -> list[str]:
        """Sorted POSIX paths of the files in ``ref``."""

    @abstractmethod
    def reset_to(self, ref: str | None) -> None:
        """Hard-reset the current branch and work tree to ``ref``; None = unborn, empty tree."""

    @abstractmethod
    def current_branch(self) -> str | None:
        """Name of the branch HEAD points at (also when unborn) or None when detached."""

    @abstractmethod
    def head(self) -> str | None:
        """Sha of HEAD or None when the current branch has no commits."""

    @abstractmethod
    def create_branch(self, name: str, ref: str) -> None:
        """Create branch ``name`` at ``ref`` (error when it exists)."""

    @abstractmethod
    def set_branch(self, name: str, sha: str | None) -> None:
        """Point branch ``name`` at ``sha`` (None deletes it) without touching the work tree.

        Refused while HEAD is on ``name`` (that would desynchronise the work tree).
        """

    @abstractmethod
    def list_tags(self) -> list[dict]:
        """``[{name, sha, meta, date}]`` sorted by slide number then name."""

    @abstractmethod
    def has_commits(self) -> bool:
        """True when any commit exists in the repository."""

    @abstractmethod
    def resolve(self, ref: str | None) -> str | None:
        """Commit sha of ``ref`` or None when it does not resolve."""

    def branch_sha(self, name: str) -> str | None:
        """Sha at the tip of branch ``name`` or None (missing or unborn)."""
        return self.resolve(f"refs/heads/{name}")

    @staticmethod
    def _tag_sort_key(item: dict) -> tuple:
        m = _TAG_RE.match(item.get("name", ""))
        return (0, int(m.group(1)), "") if m else (1, 0, item.get("name", ""))


# ----------------------------------------------------------------------------- git
class GitBackend(VersionBackend):
    """Version backend driving the ``git`` command line."""

    kind = "git"
    BASE_CONFIG: tuple[str, ...] = (
        "-c", "user.name=Kyora", "-c", "user.email=kyora@localhost", "-c", "core.autocrlf=false",
        "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
    )

    def __init__(self, work_tree: Path, git: str | None = None):
        super().__init__(work_tree)
        self.git = git or which("git")
        if not self.git:
            raise VersionError("git is not installed")

    def _config(self) -> list[str]:
        return [*self.BASE_CONFIG,
                "-c", "core.quotepath=false", "-c", "safe.directory=*", "-c", f"core.hooksPath={os.devnull}",
                "-c", "advice.detachedHead=false", "-c", f"init.defaultBranch={MAIN_BRANCH}",
                "-c", "core.fsmonitor=false", "-c", "gc.autoDetach=false", "-c", "core.longpaths=true"]

    def _env(self) -> dict:
        env = dict(os.environ)
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR"):
            env.pop(var, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # never let git discover a repository above the work tree (e.g. KYORA_HOME inside a checkout)
        env["GIT_CEILING_DIRECTORIES"] = str(self.work_tree.resolve().parent)
        return env

    def _run(self, *args: str, check: bool = True, input_text: str | None = None, timeout: float = 300,
             need_repo: bool = True) -> tuple[int, str, str]:
        if need_repo and not (self.work_tree / ".git").exists():
            raise VersionError(f"no git repository at {self.work_tree}")
        argv = [self.git, *self._config(), *args]
        rc, out, err = procs.run(argv, cwd=self.work_tree, env=self._env(), timeout=timeout, input_text=input_text)
        if check and rc != 0:
            raise VersionError(f"git {args[0]} failed ({rc}): {(err or out).strip()[:2000]}")
        return rc, out, err

    def init(self) -> None:
        self.work_tree.mkdir(parents=True, exist_ok=True)
        if (self.work_tree / ".git").exists():
            return
        rc, _, _ = self._run("init", "-q", "-b", MAIN_BRANCH, check=False, need_repo=False)
        if rc != 0:  # git < 2.28 has no -b
            self._run("init", "-q", need_repo=False)
            rc2, _, _ = self._run("checkout", "-q", "-b", MAIN_BRANCH, check=False)
            if rc2 != 0:
                self._run("symbolic-ref", "HEAD", f"refs/heads/{MAIN_BRANCH}")

    def resolve(self, ref: str | None) -> str | None:
        if not ref:
            return None
        rc, out, _ = self._run("rev-parse", "--verify", "-q", f"{_check_ref(ref)}^{{commit}}", check=False)
        return out.strip() or None if rc == 0 else None

    def head(self) -> str | None:
        return self.resolve("HEAD")

    def current_branch(self) -> str | None:
        rc, out, _ = self._run("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return out.strip() or None if rc == 0 else None

    def has_commits(self) -> bool:
        _, out, _ = self._run("rev-list", "-n", "1", "--all", check=False)
        return bool(out.strip())

    def checkout(self, ref: str) -> None:
        self._run("checkout", "-q", "-f", _check_ref(ref), "--")
        self._run("clean", "-fdxq")  # -x: an ignored leftover would otherwise reach the next commit (add -f)

    def checkout_orphan(self, name: str) -> None:
        _check_ref(name)
        if self.branch_sha(name):
            raise VersionError(f"branch {name} already exists")
        self._run("symbolic-ref", "HEAD", f"refs/heads/{name}")
        self._run("read-tree", "--empty")
        clear_work_tree(self.work_tree)

    def commit(self, message: str, meta: dict | None = None) -> str:
        text = (message or "Kyora commit").strip()
        if meta:
            text += "\n\nKyora-Meta: " + json.dumps(meta, sort_keys=True, ensure_ascii=False, default=str)
        # -f: ignore rules must not drop game files from the slide; the directories the gate skips never enter it
        self._run("add", "-A", "-f", "--", ".", *(f":(exclude,glob){g}" for g in _UNSHIPPED_GLOBS), timeout=600)
        self._run("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *(f":(glob){g}" for g in _UNSHIPPED_GLOBS),
                  timeout=600)  # entries an older commit tracked there
        self._run("commit", "-q", "--allow-empty", "--no-verify", "--cleanup=verbatim", "-F", "-",
                  input_text=text + "\n")
        sha = self.head()
        if not sha:
            raise VersionError("commit did not produce a HEAD")
        return sha

    def tag(self, name: str, sha: str, meta: dict) -> None:
        payload = json.dumps(meta or {}, sort_keys=True, ensure_ascii=False, default=str)
        self._run("tag", "-a", _check_ref(name), _check_ref(sha), "-F", "-", input_text=payload + "\n")

    def archive_zip(self, ref: str, out_path: Path) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.{new_id()}.tmp")
        try:
            self._run("archive", "--format=zip", "-o", str(tmp), _check_ref(ref), timeout=900)
            os.replace(tmp, out)
        finally:
            if tmp.exists():
                tmp.unlink()
        return out

    def ls(self, ref: str) -> list[str]:
        _, out, _ = self._run("ls-tree", "-r", "-z", "--name-only", _check_ref(ref))
        return sorted(p for p in out.split("\x00") if p)

    def reset_to(self, ref: str | None) -> None:
        if ref:
            self._run("reset", "-q", "--hard", _check_ref(ref))
            self._run("clean", "-fdxq")
            return
        branch = self.current_branch()
        if branch and self.branch_sha(branch):
            self._run("update-ref", "-d", f"refs/heads/{branch}")
        self._run("read-tree", "--empty")
        clear_work_tree(self.work_tree)

    def create_branch(self, name: str, ref: str) -> None:
        self._run("branch", _check_ref(name), _check_ref(ref))

    def set_branch(self, name: str, sha: str | None) -> None:
        ref = f"refs/heads/{_check_ref(name)}"
        if self.current_branch() == name:
            raise VersionError(f"branch {name} is checked out")
        if sha:
            self._run("update-ref", ref, _check_ref(sha))
        elif self.branch_sha(name):
            self._run("update-ref", "-d", ref)

    def list_tags(self) -> list[dict]:
        fmt = "%(refname:short)%1f%(objectname)%1f%(*objectname)%1f%(creatordate:iso-strict)%1f%(contents)%1e"
        _, out, _ = self._run("for-each-ref", f"--format={fmt}", "refs/tags/")
        tags: list[dict] = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\r\n")
            if not rec:
                continue
            parts = rec.split("\x1f", 4)
            if len(parts) < 5:
                continue
            name, obj, peeled, date, contents = parts
            meta: Any
            try:
                meta = json.loads(contents.strip()) if contents.strip() else {}
            except ValueError:
                meta = {"message": contents.strip()}
            tags.append({"name": name, "sha": peeled or obj, "meta": meta, "date": date})
        tags.sort(key=self._tag_sort_key)
        return tags


# ----------------------------------------------------------------------------- snapshot
class SnapshotBackend(VersionBackend):
    """Git-free fallback: each commit is a full copy under ``<store>/<n>/``.

    Branches, tags and HEAD live in ``<store>/state.json``. Commit ids are
    ``snap-<n>`` (honestly not git hashes).
    """

    kind = "snapshot"

    def __init__(self, work_tree: Path, store_dir: Path):
        super().__init__(work_tree)
        self.store_dir = Path(store_dir)
        self._lock = threading.RLock()

    # state ------------------------------------------------------------------
    @property
    def _state_path(self) -> Path:
        return self.store_dir / "state.json"

    def _load(self) -> dict:
        st = read_json(self._state_path, default=None)
        if not isinstance(st, dict):
            raise VersionError(f"snapshot store not initialised at {self.store_dir}")
        return st

    def _save(self, st: dict) -> None:
        atomic_write_json(self._state_path, st)

    def _dir(self, sha: str) -> Path:
        m = re.match(r"^snap-(\d+)$", sha or "")
        if not m:
            raise VersionError(f"invalid snapshot id {sha!r}")
        return self.store_dir / m.group(1)

    def _resolve(self, st: dict, ref: str | None) -> str | None:
        if not ref:
            return None
        if ref == "HEAD":
            return st["head"].get("sha")
        if ref.startswith("refs/heads/"):
            return st["branches"].get(ref[len("refs/heads/"):])
        if ref.startswith("refs/tags/"):
            t = st["tags"].get(ref[len("refs/tags/"):])
            return t["sha"] if t else None
        if ref in st["tags"]:
            return st["tags"][ref]["sha"]
        if ref in st["branches"]:
            return st["branches"][ref]
        if ref in st["commits"]:
            return ref
        return None

    def _restore(self, sha: str | None) -> None:
        clear_work_tree(self.work_tree)
        if sha:
            copy_tree(self._dir(sha), self.work_tree, exclude_top=())

    # API ----------------------------------------------------------------------
    def init(self) -> None:
        with self._lock:
            self.work_tree.mkdir(parents=True, exist_ok=True)
            self.store_dir.mkdir(parents=True, exist_ok=True)
            if not self._state_path.exists():
                self._save({"version": 1, "next": 1, "commits": {}, "tags": {}, "branches": {},
                            "head": {"branch": MAIN_BRANCH, "sha": None}})

    def resolve(self, ref: str | None) -> str | None:
        with self._lock:
            return self._resolve(self._load(), ref)

    def head(self) -> str | None:
        with self._lock:
            return self._load()["head"].get("sha")

    def current_branch(self) -> str | None:
        with self._lock:
            return self._load()["head"].get("branch")

    def has_commits(self) -> bool:
        with self._lock:
            return bool(self._load()["commits"])

    def checkout(self, ref: str) -> None:
        with self._lock:
            st = self._load()
            if ref in st["branches"]:
                sha = st["branches"][ref]
                self._restore(sha)
                st["head"] = {"branch": ref, "sha": sha}
            else:
                sha = self._resolve(st, ref)
                if not sha:
                    raise VersionError(f"unknown ref {ref!r}")
                self._restore(sha)
                st["head"] = {"branch": None, "sha": sha}
            self._save(st)

    def checkout_orphan(self, name: str) -> None:
        _check_ref(name)
        with self._lock:
            st = self._load()
            if st["branches"].get(name):
                raise VersionError(f"branch {name} already exists")
            clear_work_tree(self.work_tree)
            st["head"] = {"branch": name, "sha": None}
            self._save(st)

    def commit(self, message: str, meta: dict | None = None) -> str:
        with self._lock:
            st = self._load()
            n = int(st.get("next") or 1)
            sha = f"snap-{n}"
            target = self.store_dir / str(n)
            if target.exists():
                rm_tree(target)
            copy_tree(self.work_tree, target, exclude_top=(".git",), exclude_dirs=UNSHIPPED_DIRS)
            st["commits"][sha] = {"parent": st["head"].get("sha"), "message": (message or "").strip(),
                                  "meta": meta or {}, "date": now_iso(), "branch": st["head"].get("branch")}
            st["next"] = n + 1
            if st["head"].get("branch"):
                st["branches"][st["head"]["branch"]] = sha
            st["head"]["sha"] = sha
            self._save(st)
            return sha

    def tag(self, name: str, sha: str, meta: dict) -> None:
        with self._lock:
            st = self._load()
            if name in st["tags"]:
                raise VersionError(f"tag {name} already exists")
            if sha not in st["commits"]:
                raise VersionError(f"unknown snapshot {sha!r}")
            st["tags"][name] = {"sha": sha, "meta": meta or {}, "date": now_iso()}
            self._save(st)

    def archive_zip(self, ref: str, out_path: Path) -> Path:
        sha = self.resolve(ref)
        if not sha:
            raise VersionError(f"unknown ref {ref!r}")
        return Path(zip_dir(self._dir(sha), out_path, exclude=(".git",)))  # type: ignore[arg-type]

    def ls(self, ref: str) -> list[str]:
        sha = self.resolve(ref)
        if not sha:
            raise VersionError(f"unknown ref {ref!r}")
        root = self._dir(sha)
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink())

    def reset_to(self, ref: str | None) -> None:
        with self._lock:
            st = self._load()
            sha = self._resolve(st, ref) if ref else None
            if ref and not sha:
                raise VersionError(f"unknown ref {ref!r}")
            self._restore(sha)
            branch = st["head"].get("branch")
            if branch:
                if sha:
                    st["branches"][branch] = sha
                else:
                    st["branches"].pop(branch, None)
            st["head"]["sha"] = sha
            self._save(st)

    def create_branch(self, name: str, ref: str) -> None:
        _check_ref(name)
        with self._lock:
            st = self._load()
            if name in st["branches"]:
                raise VersionError(f"branch {name} already exists")
            sha = self._resolve(st, ref)
            if not sha:
                raise VersionError(f"unknown ref {ref!r}")
            st["branches"][name] = sha
            self._save(st)

    def set_branch(self, name: str, sha: str | None) -> None:
        _check_ref(name)
        with self._lock:
            st = self._load()
            if st["head"].get("branch") == name:
                raise VersionError(f"branch {name} is checked out")
            if sha:
                resolved = self._resolve(st, sha)
                if not resolved:
                    raise VersionError(f"unknown ref {sha!r}")
                st["branches"][name] = resolved
            else:
                st["branches"].pop(name, None)
            self._save(st)

    def list_tags(self) -> list[dict]:
        with self._lock:
            st = self._load()
        tags = [{"name": k, "sha": v["sha"], "meta": v.get("meta") or {}, "date": v.get("date")}
                for k, v in st["tags"].items()]
        tags.sort(key=self._tag_sort_key)
        return tags


def git_available() -> bool:
    """True when a ``git`` executable is on PATH."""
    return which("git") is not None


def make_backend(paths: "ProjectPaths", kind: str = "auto") -> VersionBackend:
    """Build a backend for a project: ``auto`` picks git when installed, else snapshots."""
    kind = (kind or "auto").lower()
    if kind in ("auto", "git"):
        git = which("git")
        if git:
            return GitBackend(paths.repo, git)
        if kind == "git":
            raise VersionError("this project uses the git backend but git is not installed (install git)")
    if kind in ("auto", "snapshot"):
        return SnapshotBackend(paths.repo, paths.snapshots)
    raise VersionError(f"unknown version backend {kind!r}")


# ----------------------------------------------------------------------------- locks
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(root).resolve()))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def _summarise(d: dict | None, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(d, dict):
        return None
    return {k: d[k] for k in keys if k in d}


def _slide_number(value: Any) -> int:
    try:
        n = int(str(value).strip().lstrip("vV"))
    except (TypeError, ValueError):
        raise FileNotFoundError(f"slide not found: {value!r}") from None
    if n < 1:
        raise FileNotFoundError(f"slide not found: {value!r}")
    return n


# ----------------------------------------------------------------------------- slide manager
class SlideManager:
    """Lineage-aware slide operations for one project (thread-safe per project)."""

    def __init__(self, project: "Project", paths: "ProjectPaths", backend: VersionBackend,
                 store: "ProjectStore | None" = None):
        self.project = project
        self.paths = paths
        self.backend = backend
        self.store = store
        self._lock = _project_lock(paths.root)

    @classmethod
    def for_project(cls, store: "ProjectStore", project: "Project | str") -> "SlideManager":
        """Open the manager for ``project`` using the backend recorded in ``project.json``."""
        proj = store.get(project) if isinstance(project, str) else project
        paths = store.paths(proj.id)
        backend = make_backend(paths, str((proj.repo or {}).get("backend") or "auto"))
        return cls(proj, paths, backend, store)

    # project state --------------------------------------------------------------
    def _fresh_project(self) -> "Project":
        if self.store is not None:
            self.project = self.store.get(self.project.id)
        return self.project

    def _update_project(self, fn) -> "Project":
        if self.store is not None:
            self.project = self.store.update_project(self.project.id, fn)
        else:
            fn(self.project)
            self.project.updated_at = now_iso()
            atomic_write_json(self.paths.project_json, self.project.to_dict())
        return self.project

    def _save_run(self, run: "Run") -> None:
        if self.store is not None:
            self.store.save_run(run)

    # basics ---------------------------------------------------------------------
    def init(self) -> None:
        """Initialise the backend and record it in ``project.json``."""
        with self._lock:
            self.backend.init()
            branch = self.backend.current_branch() or MAIN_BRANCH

            def _apply(p):
                p.repo = {**(p.repo or {}), "backend": self.backend.kind, "branch": branch}
            self._update_project(_apply)

    def work_tree(self) -> Path:
        """The live working tree (engine cwd)."""
        return self.paths.repo

    def list_slides(self) -> list[dict]:
        """Slides of the project ordered by number."""
        return sorted((dict(s) for s in self._fresh_project().slides), key=lambda s: int(s.get("number") or 0))

    def get_slide(self, n: int | str) -> dict:
        """Slide ``n`` (``3`` or ``"v3"``); FileNotFoundError when it does not exist."""
        num = _slide_number(n)
        slide = self._fresh_project().slide(num)
        if slide is None:
            raise FileNotFoundError(f"slide v{num} not found")
        return dict(slide)

    def _is_lineage_head(self, slide: dict, sha: str) -> str | None:
        branch = slide.get("branch")
        if branch and self.backend.branch_sha(branch) == sha:
            return branch
        return None

    def _base_on_slide(self, slide: dict, new_branch: str,
                       claimed: frozenset[str] | set[str] = frozenset()) -> tuple[str, str, bool]:
        """Check out ``slide``; returns ``(branch, sha, created_branch)``.

        The slide's own branch is reused only when the slide is its head and no
        other unfinished run holds it (``claimed``); otherwise ``new_branch`` is created.
        """
        sha = self.backend.resolve(slide.get("tag") or "") or slide.get("sha")
        if not sha or not self.backend.resolve(sha):
            raise VersionError(f"slide v{slide.get('number')} is missing from the repository")
        branch = self._is_lineage_head(slide, sha)
        if branch and branch not in claimed:
            self.backend.checkout(branch)
            return branch, sha, False
        self.backend.create_branch(new_branch, sha)
        self.backend.checkout(new_branch)
        return new_branch, sha, True

    # runs -----------------------------------------------------------------------
    def _claimed_branches(self, run: "Run | None" = None) -> set[str]:
        """Branches held by other unfinished runs: prepared, not ended and no slide committed yet."""
        if self.store is None:
            return set()
        from .store import TERMINAL_STATES
        out: set[str] = set()
        for other in self.store.list_runs(self.project.id, limit=1000):
            if run is not None and other.id == run.id:
                continue
            o = other.options if isinstance(other.options, dict) else {}
            if o.get("prepared") and o.get("branch") and other.state not in TERMINAL_STATES \
                    and other.result_slide is None:
                out.add(str(o["branch"]))
        return out

    @staticmethod
    def _run_tip(run: "Run") -> str | None:
        """The commit the run last left on its branch (its base before its first commit)."""
        opts = run.options if isinstance(run.options, dict) else {}
        if "tip" in opts:
            return opts["tip"]
        # runs prepared before ``tip`` was recorded: the last iteration commit, else the base
        commits = [rec.get("commit") for rec in (run.iterations or []) if isinstance(rec, dict) and rec.get("commit")]
        return commits[-1] if commits else opts.get("base_sha")

    def _record_tip(self, run: "Run | None", sha: str | None) -> None:
        if run is None or not isinstance(run.options, dict) or not run.options.get("prepared"):
            return
        run.options["tip"] = sha
        self._save_run(run)

    def _resume_prepared(self, run: "Run", opts: dict) -> dict:
        """Restore the work tree to the commit this run last left on its branch."""
        branch = opts["branch"]
        tip = self._run_tip(run)
        actual = self.backend.branch_sha(branch)
        if actual != tip:
            raise LineageMoved(
                f"branch {branch} moved since this run last committed (the run left it at "
                f"{(tip or 'no commit')[:12]}, it is now at {(actual or 'no commit')[:12]}); another run or an "
                f"import changed it. Cancel this run and start a new one.")
        if tip:
            self.backend.checkout(branch)  # forced: the tree is exactly the run's last commit
            if opts.get("fresh_tree") and tip == opts.get("base_sha"):
                # a fresh generation that has not committed yet starts empty, as prepare_run left it
                clear_work_tree(self.work_tree())
        else:
            self.backend.checkout_orphan(branch)
        if opts.get("tip", _UNSET) != tip:
            opts["tip"] = tip
            self._save_run(run)
        log.info("run %s resumed on %s at %s", run.id, branch, tip)
        return self._prep_info(opts)

    def prepare_run(self, run: "Run") -> dict:
        """Put the work tree at the run's starting point and record it in ``run.options``.

        Records ``branch``, ``base_sha``, ``base_slide``, ``created_branch``,
        ``fresh_tree``, ``tip`` and ``prepared``; saves the run when a store is
        attached. Calling it again for a prepared run (resume) restores the commit
        the run last left on its branch and raises :class:`LineageMoved` when the
        branch no longer points there.
        """
        with self._lock:
            opts = run.options if isinstance(run.options, dict) else {}
            run.options = opts
            if opts.get("prepared") and opts.get("branch"):
                return self._resume_prepared(run, opts)
            project = self._fresh_project()
            claimed = self._claimed_branches(run)
            parent_n = run.parent_slide
            if parent_n is None and run.kind == "modify" and project.current_slide is not None:
                parent_n = run.parent_slide = int(project.current_slide)
            if parent_n is not None:
                slide = self.get_slide(parent_n)
                branch, base, created = self._base_on_slide(slide, f"lineage/{run.id}", claimed)
                fresh = False
            else:
                fresh = True
                if not project.slides and MAIN_BRANCH not in claimed:
                    branch, created = MAIN_BRANCH, False
                    base = self.backend.branch_sha(MAIN_BRANCH)
                    if base:
                        self.backend.checkout(MAIN_BRANCH)
                        clear_work_tree(self.work_tree())  # fresh generation; the commit records the removal
                    elif self.backend.current_branch() != MAIN_BRANCH or self.backend.has_commits():
                        self.backend.checkout_orphan(MAIN_BRANCH)
                    else:
                        clear_work_tree(self.work_tree())
                else:
                    branch, base, created = f"lineage/{run.id}", None, True
                    self.backend.checkout_orphan(branch)
            opts.update({"branch": branch, "base_sha": base, "base_slide": parent_n, "created_branch": created,
                         "fresh_tree": fresh, "prepared": True, "vcs": self.backend.kind, "tip": base})
            self._save_run(run)

            def _apply(p):
                p.repo = {**(p.repo or {}), "branch": branch}
            self._update_project(_apply)
            log.info("run %s prepared on %s (base %s, parent %s)", run.id, branch, base, parent_n)
            return self._prep_info(opts)

    def _prep_info(self, opts: dict) -> dict:
        return {k: opts.get(k) for k in ("branch", "base_sha", "base_slide", "created_branch", "fresh_tree")} | {
            "work_tree": str(self.work_tree())}

    def _warn_branch(self, run: "Run | None") -> None:
        if run is None:
            return
        expected = (run.options or {}).get("branch")
        current = self.backend.current_branch()
        if expected and current != expected:
            log.warning("run %s expected branch %s but the work tree is on %s", run.id, expected, current)

    def commit_iteration(self, run: "Run", n: int, gate: dict | None) -> str:
        """Commit the work tree after build/fix iteration ``n`` (empty commits allowed)."""
        with self._lock:
            self._warn_branch(run)
            meta = {"kind": "iteration", "run_id": run.id, "iteration": int(n), "gate": _summarise(gate, _GATE_KEYS)}
            sha = self.backend.commit(f"Kyora run {run.id}: iteration {n}", meta)
            self._record_tip(run, sha)
            return sha

    def commit_slide(self, run: "Run | None", title: str, gate: dict | None, engine: dict | None,
                     screenshots: list | None = None, *, parent: Any = _UNSET) -> dict:
        """Commit the work tree as slide ``v<N>`` (N = max + 1), tag it and record it.

        ``parent`` defaults to ``run.parent_slide``. Screenshot paths are stored
        relative to the project root. Returns the slide dict (schema in
        docs/architecture.md, plus ``branch``).
        """
        with self._lock:
            self._warn_branch(run)
            project = self._fresh_project()
            numbers = [int(s.get("number") or 0) for s in project.slides]
            for t in self.backend.list_tags():
                m = _TAG_RE.match(t.get("name", ""))
                if m:
                    numbers.append(int(m.group(1)))
            number = max(numbers, default=0) + 1
            tag = f"{TAG_PREFIX}{number}"
            if parent is _UNSET:
                parent = run.parent_slide if run is not None else None
            parent = int(parent) if parent is not None else None
            title = " ".join(str(title or f"Slide v{number}").split())[:200]
            branch = self.backend.current_branch()
            shots = self._relative_screenshots(screenshots or [])
            created_at = now_iso()
            meta = {"number": number, "title": title, "run_id": run.id if run is not None else None, "parent": parent,
                    "gate": _summarise(gate, _GATE_KEYS), "engine": _summarise(engine, _ENGINE_KEYS),
                    "branch": branch, "created_at": created_at}
            sha = self.backend.commit(f"Slide v{number}: {title}", meta)
            self.backend.tag(tag, sha, meta)
            slide = {"number": number, "tag": tag, "sha": sha, "parent": parent, "run_id": meta["run_id"],
                     "title": title, "created_at": created_at, "gate": meta["gate"], "engine": meta["engine"],
                     "screenshots": shots, "branch": branch}

            def _apply(p):
                p.slides.append(dict(slide))
                p.current_slide = number
                p.repo = {**(p.repo or {}), "branch": branch or (p.repo or {}).get("branch")}
            self._update_project(_apply)
            if run is not None:
                run.result_slide = number
                self._record_tip(run, sha)
            log.info("slide v%d committed (%s) on %s", number, sha[:12], branch)
            return dict(slide)

    def _relative_screenshots(self, shots: list) -> list[str]:
        root = self.paths.root.resolve()
        out: list[str] = []
        for s in shots:
            p = Path(str(s))
            if not p.is_absolute():
                p = root / p
            try:
                out.append(p.resolve().relative_to(root).as_posix())
            except ValueError:
                log.warning("screenshot outside the project ignored: %s", s)
        return out

    def rollback_working_tree(self, run: "Run", *, touch_tree: bool = True) -> bool:
        """Discard the run's changes: its branch goes back to the run's starting point.

        A run that was never prepared left nothing behind. Only a branch whose tip
        is still the run's own last commit (``options.tip``) is reset; a branch that
        another run or an import moved on is left alone. When the work tree is on
        the run's branch it is reset and cleaned too, but only with ``touch_tree``
        (False while another run of the project uses the work tree); otherwise only
        the branch ref moves. Returns True when nothing of the run remains.
        """
        with self._lock:
            opts = run.options or {}
            if not opts.get("prepared"):
                return True
            branch = opts.get("branch")
            base = opts.get("base_sha")
            tip = self._run_tip(run)
            actual = self.backend.branch_sha(branch) if branch else None
            if not branch or actual != tip:
                log.warning("run %s: %s is at %s, not at this run's commit %s; left as it is",
                            run.id, branch, actual, tip)
                return False
            if self.backend.current_branch() == branch:
                if not touch_tree:
                    log.warning("run %s: %s is checked out while another run uses the work tree; left as it is",
                                run.id, branch)
                    return False
                self.backend.reset_to(base)
            elif actual != base:
                self.backend.set_branch(branch, base)
            self._record_tip(run, base)
            log.info("run %s rolled back to %s on %s", run.id, base, branch)
            return True

    # previews / exports ------------------------------------------------------------
    def materialize(self, n: int | str) -> Path:
        """Extract slide ``n`` to ``cache/slides/v<n>/`` (reused while the sha marker matches)."""
        with self._lock:
            slide = self.get_slide(n)
            num = int(slide["number"])
            sha = slide.get("sha") or self.backend.resolve(slide.get("tag") or "")
            base = self.paths.cache / "slides"
            dest = base / f"v{num}"
            marker = base / f"v{num}.sha"
            try:
                cached = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
            except OSError:
                cached = ""
            if dest.is_dir() and sha and cached == sha:
                return dest
            base.mkdir(parents=True, exist_ok=True)
            token = new_id()
            tmp_zip = base / f".v{num}-{token}.zip"
            tmp_dir = base / f".v{num}-{token}"
            try:
                self.backend.archive_zip(slide.get("tag") or sha, tmp_zip)
                unzip_safe(tmp_zip, tmp_dir, skip_symlinks=True)
                if marker.exists():
                    marker.unlink()
                rm_tree(dest)
                os.replace(tmp_dir, dest)
                atomic_write_text(marker, (sha or "") + "\n")
            finally:
                if tmp_zip.exists():
                    tmp_zip.unlink()
                rm_tree(tmp_dir)
            return dest

    def export_zip(self, n: int | str) -> Path:
        """ZIP of slide ``n`` (the committed tree, including ``kyora/deck.html`` when committed)."""
        with self._lock:
            slide = self.get_slide(n)
            num = int(slide["number"])
            sha = slide.get("sha") or ""
            out = self.paths.cache / "exports" / f"{slug(self.project.name)}-v{num}.zip"
            marker = out.with_name(out.name + ".sha")
            try:
                if out.is_file() and sha and marker.read_text(encoding="utf-8").strip() == sha:
                    return out
            except OSError:
                pass
            self.backend.archive_zip(slide.get("tag") or sha, out)
            atomic_write_text(marker, sha + "\n")
            return out

    # import -------------------------------------------------------------------------
    def import_zip(self, source: bytes | Path | str, filename: str, *, parent: int | None = None) -> tuple[dict, list[str]]:
        """Import a game ZIP as a new slide titled ``Imported <filename>`` (gate None).

        The archive is validated and extracted to a staging directory first, so
        a rejected ZIP leaves the repository untouched. Without ``parent`` it
        lands on ``lineage/main`` (no slides yet) or a new lineage branch;
        with ``parent`` it stacks on that slide like a modification.
        Returns ``(slide, imported_files)``.
        """
        display = (str(filename or "game.zip").replace("\\", "/").split("/")[-1] or "game.zip")[:120]
        with self._lock:
            staging = self.paths.cache / f".import-{new_id()}"
            try:
                files = import_game_zip(source, staging)
                project = self._fresh_project()
                claimed = self._claimed_branches()  # never commit onto a blocked run's branch
                if parent is not None:
                    slide = self.get_slide(parent)
                    self._base_on_slide(slide, f"lineage/import-{new_id()}", claimed)
                elif not project.slides and MAIN_BRANCH not in claimed:
                    if self.backend.branch_sha(MAIN_BRANCH):
                        self.backend.checkout(MAIN_BRANCH)
                    elif self.backend.current_branch() != MAIN_BRANCH or self.backend.has_commits():
                        self.backend.checkout_orphan(MAIN_BRANCH)
                else:
                    self.backend.checkout_orphan(f"lineage/import-{new_id()}")
                clear_work_tree(self.work_tree())
                copy_tree(staging, self.work_tree(), exclude_top=(".git",))
            finally:
                rm_tree(staging)
            slide = self.commit_slide(None, f"Imported {display}", None,
                                      {"backend": "import", "requested_model": None, "active_model": None,
                                       "fallback_used": False}, [], parent=parent)
            return slide, files
