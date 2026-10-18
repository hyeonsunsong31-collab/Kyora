"""Project storage: ``project.json`` / ``runs/<rid>/run.json`` plus a sqlite index.

The JSON files on disk are the source of truth (schemas in
``docs/architecture.md``); the sqlite tables ``kyora_projects`` and
``kyora_runs`` (in the shared ``kyora.db``) only index them for ordering and for
finding a run's project quickly. The index is rebuilt from disk on start-up, so
deleting ``kyora.db`` never loses a project.

Layout per project (``<root>/<pid>/``)::

    project.json
    repo/          git (or snapshot) working tree = the game, engine cwd
    runs/<rid>/    run.json and every per-run artefact
    cache/         materialised slides, exports, browser results
    attachments/   uploaded files, stored as ``<aid>_<sanitised name>``
    snapshots/     only for the snapshot backend (no git available)

All public methods are thread-safe: one re-entrant lock guards the sqlite
connection and every read-modify-write of ``project.json``.
"""
from __future__ import annotations

import base64
import codecs
import copy
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ..config import DEFAULT_SETTINGS
from ..util.fs import atomic_write_bytes, atomic_write_json, read_json, rm_tree, safe_join
from ..util.ids import new_id, now_iso
from ..util.mime import guess_type as guess_mime
from .zipper import UnsafeZipError, list_zip

log = logging.getLogger("kyora.projects.store")

MAIN_BRANCH = "lineage/main"
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
TEXT_EXCERPT_BYTES = 60 * 1024
IMAGE_INLINE_MAX = 8 * 1024 * 1024
ZIP_LISTING_LIMIT = 400
MAX_NAME_LEN = 120
RUN_KINDS = ("generate", "modify")
RUN_STATES = ("created", "intake", "plan", "awaiting_approval", "design", "build", "test", "gate", "review", "fix",
              "finalize", "complete", "failed_gate", "blocked", "failed", "cancelled")
TERMINAL_STATES = frozenset({"complete", "failed_gate", "failed", "cancelled"})
DEFAULT_GATE_THRESHOLD = float(DEFAULT_SETTINGS["quality"]["min_pass_rate"])

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_TEXT_MIMES = {
    "application/json", "application/javascript", "application/x-javascript", "application/xml",
    "application/x-yaml", "application/yaml", "application/toml", "application/x-sh", "application/x-python",
    "application/typescript", "application/ld+json", "image/svg+xml", "application/x-httpd-php",
}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".scss", ".less", ".py", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".svg",
    ".glsl", ".frag", ".vert", ".wgsl", ".lua", ".gd", ".cs", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".rs",
    ".go", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".tex", ".log", ".gdshader", ".shader", ".hlsl",
}
_ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "application/x-zip", "multipart/x-zip"}


def valid_id(value: str) -> bool:
    """True when ``value`` looks like a Kyora id (no path separators, no dots)."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _require_id(value: str, kind: str) -> str:
    if not valid_id(value):
        raise FileNotFoundError(f"{kind} not found: {value!r}")
    return value


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Return a Windows/POSIX-safe base name for an uploaded file.

    Directory parts are dropped, unicode letters/digits and ``._- `` are kept,
    everything else becomes ``_``; leading dots and trailing dots/spaces are
    removed, reserved Windows device names are prefixed, and the length is
    capped while preserving the extension.
    """
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = unicodedata.normalize("NFC", base)
    out = "".join(ch if (ch.isalnum() or ch in "._- ") else "_" for ch in base)
    out = out.strip().lstrip(".").rstrip(". ")
    out = re.sub(r"_{2,}", "_", out)
    if not out:
        out = "file"
    stem, dot, ext = out.rpartition(".")
    if not dot:
        stem, ext = out, ""
    if stem.upper() in _WIN_RESERVED or out.upper() in _WIN_RESERVED:
        out = "_" + out
        stem = "_" + stem
    if len(out) > max_len:
        ext_part = ("." + ext[:16]) if dot and ext else ""
        out = stem[: max_len - len(ext_part)].rstrip(". ") + ext_part
    return out or "file"


# ----------------------------------------------------------------------------- records
class _Record:
    """Shared ``to_dict``/``from_dict`` that preserves unknown keys in ``extra``."""

    extra: dict

    @classmethod
    def _known(cls) -> list[str]:
        return [f.name for f in fields(cls) if f.name != "extra"]  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        out: dict = {}
        for name in self._known():
            val = getattr(self, name)
            if isinstance(val, list):
                val = [v.to_dict() if isinstance(v, _Record) else copy.deepcopy(v) for v in val]
            else:
                val = copy.deepcopy(val)
            out[name] = val
        for k, v in (self.extra or {}).items():
            if k not in out:
                out[k] = copy.deepcopy(v)
        return out


@dataclass
class Attachment(_Record):
    """One uploaded file, recorded in ``project.json`` ``attachments``."""

    id: str
    name: str
    mime: str
    size: int
    path: str  # relative to the project root, POSIX (``attachments/<aid>_<name>``)
    role: str = "reference"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Attachment":
        d = dict(d or {})
        known = cls._known()
        return cls(id=str(d.get("id", "")), name=str(d.get("name", "")), mime=str(d.get("mime") or "application/octet-stream"),
                   size=int(d.get("size") or 0), path=str(d.get("path", "")), role=str(d.get("role") or "reference"),
                   extra={k: copy.deepcopy(v) for k, v in d.items() if k not in known})


def _default_repo() -> dict:
    return {"backend": "git", "branch": MAIN_BRANCH}


@dataclass
class Project(_Record):
    """``project.json``: identity, repository backend, attachments and slides."""

    id: str
    name: str
    created_at: str
    updated_at: str
    engine: str = "vanilla"
    repo: dict = field(default_factory=_default_repo)
    attachments: list = field(default_factory=list)  # list[Attachment]
    slides: list = field(default_factory=list)  # list[dict] (schema in docs/architecture.md)
    current_slide: int | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        d = dict(d or {})
        known = cls._known()
        repo = d.get("repo") if isinstance(d.get("repo"), dict) else _default_repo()
        cur = d.get("current_slide")
        return cls(
            id=str(d.get("id", "")), name=str(d.get("name", "")),
            created_at=str(d.get("created_at") or ""), updated_at=str(d.get("updated_at") or d.get("created_at") or ""),
            engine=str(d.get("engine") or "vanilla"), repo=copy.deepcopy(repo),
            attachments=[Attachment.from_dict(a) for a in (d.get("attachments") or []) if isinstance(a, dict)],
            slides=[copy.deepcopy(s) for s in (d.get("slides") or []) if isinstance(s, dict)],
            current_slide=int(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None,
            extra={k: copy.deepcopy(v) for k, v in d.items() if k not in known},
        )

    def attachment(self, aid: str) -> Attachment | None:
        """Return the attachment with id ``aid`` or None."""
        for a in self.attachments:
            if a.id == aid:
                return a
        return None

    def slide(self, number: int) -> dict | None:
        """Return the slide dict with ``number`` or None."""
        for s in self.slides:
            if s.get("number") == number:
                return s
        return None


def _default_options() -> dict:
    return {"engine_pref": "auto", "import_zip_attachment": None}


def _default_gate() -> dict:
    return {"threshold": DEFAULT_GATE_THRESHOLD, "final": None}


def _default_judge() -> dict:
    return {"allowed": 0, "denied": 0, "unavailable": 0}


@dataclass
class Run(_Record):
    """``run.json``: one pipeline run (generate or modify) of a project."""

    id: str
    project_id: str
    kind: str = "generate"
    parent_slide: int | None = None
    prompt: str = ""
    attachment_ids: list = field(default_factory=list)
    options: dict = field(default_factory=_default_options)
    state: str = "created"
    iteration: int = 0
    stages: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    iterations: list = field(default_factory=list)
    gate: dict = field(default_factory=_default_gate)
    judge: dict = field(default_factory=_default_judge)
    result_slide: int | None = None
    error: str | None = None
    blocked_reason: str | None = None
    created_at: str = ""
    finished_at: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        d = dict(d or {})
        known = cls._known()
        kwargs: dict[str, Any] = {}
        for name in known:
            if name in d:
                kwargs[name] = copy.deepcopy(d[name])
        kwargs.setdefault("id", "")
        kwargs.setdefault("project_id", "")
        for name, factory in (("options", _default_options), ("gate", _default_gate), ("judge", _default_judge),
                              ("stages", dict), ("artifacts", dict), ("engine", dict)):
            if not isinstance(kwargs.get(name), dict):
                kwargs[name] = factory()
        for name in ("attachment_ids", "iterations"):
            if not isinstance(kwargs.get(name), list):
                kwargs[name] = []
        if not isinstance(kwargs.get("iteration"), int):
            try:
                kwargs["iteration"] = int(kwargs.get("iteration") or 0)
            except (TypeError, ValueError):
                kwargs["iteration"] = 0
        kwargs["extra"] = {k: copy.deepcopy(v) for k, v in d.items() if k not in known}
        return cls(**kwargs)

    def summary(self) -> dict:
        """Small dict for project listings (no stages/iterations detail)."""
        final = (self.gate or {}).get("final") if isinstance(self.gate, dict) else None
        return {"id": self.id, "project_id": self.project_id, "kind": self.kind, "state": self.state,
                "iteration": self.iteration, "parent_slide": self.parent_slide, "result_slide": self.result_slide,
                "prompt": (self.prompt or "")[:200], "created_at": self.created_at, "finished_at": self.finished_at,
                "gate": final, "error": self.error, "blocked_reason": self.blocked_reason,
                "engine": {k: (self.engine or {}).get(k) for k in ("backend", "active_model", "fallback_used")}}


@dataclass(frozen=True)
class ProjectPaths:
    """Filesystem locations of one project."""

    root: Path
    repo: Path
    runs: Path
    cache: Path
    attachments: Path

    @property
    def project_json(self) -> Path:
        return self.root / "project.json"

    @property
    def snapshots(self) -> Path:
        """Snapshot store used when git is unavailable."""
        return self.root / "snapshots"


# ----------------------------------------------------------------------------- helpers
def _is_text_like(mime: str, name: str) -> bool:
    mime = (mime or "").lower()
    return mime.startswith("text/") or mime in _TEXT_MIMES or PurePosixPath(name.lower()).suffix in _TEXT_EXTS


def _is_zip(mime: str, name: str) -> bool:
    return (mime or "").lower() in _ZIP_MIMES or name.lower().endswith(".zip")


def _read_excerpt(path: Path, limit: int = TEXT_EXCERPT_BYTES) -> tuple[str, bool]:
    """Read at most ``limit`` bytes as UTF-8 (partial trailing sequences dropped)."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    truncated = len(raw) > limit
    raw = raw[:limit]
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    text = dec.decode(raw, final=not truncated)
    return text, truncated


def _normalise_mime(mime: str | None, name: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m in _ZIP_MIMES:  # Chrome/Edge on Windows upload ZIPs as application/x-zip-compressed
        return "application/zip"
    if not m or m == "application/octet-stream":
        guess = guess_mime(name)
        if guess:
            return guess
        if name.lower().endswith(".zip"):
            return "application/zip"
        return m or "application/octet-stream"
    return m


class ProjectStore:
    """Projects and runs on disk plus a sqlite index (thread-safe).

    ``vcs`` selects the version backend for *new* projects: ``"auto"`` (git when
    ``git`` is on PATH, else snapshots), ``"git"`` or ``"snapshot"``. The
    ``KYORA_VCS`` environment variable sets the default.
    """

    def __init__(self, root: Path | str, db_path: Path | str | None = None, *, vcs: str | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path) if db_path else ":memory:"
        self.vcs = (vcs or os.environ.get("KYORA_VCS") or "auto").strip().lower()
        if self.vcs not in ("auto", "git", "snapshot"):
            raise ValueError(f"unknown vcs backend {self.vcs!r} (auto|git|snapshot)")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS kyora_projects "
                             "(id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT)")
            self._db.execute("CREATE TABLE IF NOT EXISTS kyora_runs "
                             "(id TEXT PRIMARY KEY, project_id TEXT, state TEXT, created_at TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_kyora_runs_project ON kyora_runs(project_id, created_at)")
            self._db.commit()
        self.reindex()

    # ------------------------------------------------------------------ index
    def _upsert(self, table: str, key: str, values: dict) -> None:
        cols = list(values)
        with self._lock:
            cur = self._db.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?",
                                   [values[c] for c in cols] + [key])
            if cur.rowcount == 0:
                self._db.execute(f"INSERT INTO {table} (id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
                                 [key] + [values[c] for c in cols])
            self._db.commit()

    def _index_project(self, p: Project) -> None:
        self._upsert("kyora_projects", p.id, {"name": p.name, "created_at": p.created_at, "updated_at": p.updated_at})

    def _index_run(self, r: Run) -> None:
        self._upsert("kyora_runs", r.id, {"project_id": r.project_id, "state": r.state, "created_at": r.created_at})

    def reindex(self) -> None:
        """Bring the sqlite index in line with the JSON files on disk."""
        with self._lock:
            known_projects = {row[0] for row in self._db.execute("SELECT id FROM kyora_projects")}
            known_runs = {row[0] for row in self._db.execute("SELECT id FROM kyora_runs")}
            on_disk: set[str] = set()
            for pdir in sorted(self.root.iterdir()) if self.root.is_dir() else []:
                if not pdir.is_dir() or not valid_id(pdir.name) or not (pdir / "project.json").is_file():
                    continue
                on_disk.add(pdir.name)
                if pdir.name not in known_projects:
                    try:
                        self._index_project(self.get(pdir.name))
                    except (OSError, RuntimeError) as exc:
                        log.warning("reindex: skipping project %s: %s", pdir.name, exc)
                runs_dir = pdir / "runs"
                if runs_dir.is_dir():
                    for rdir in sorted(runs_dir.iterdir()):
                        if rdir.name in known_runs or not valid_id(rdir.name):
                            continue
                        data = self._read_json(rdir / "run.json")
                        if isinstance(data, dict):
                            run = Run.from_dict(data)
                            run.project_id = run.project_id or pdir.name
                            self._index_run(run)
            stale = known_projects - on_disk
            for pid in stale:
                self._db.execute("DELETE FROM kyora_projects WHERE id = ?", (pid,))
                self._db.execute("DELETE FROM kyora_runs WHERE project_id = ?", (pid,))
            if stale:
                self._db.commit()

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return read_json(path, default=None)
        except ValueError as exc:
            raise RuntimeError(f"corrupt JSON in {path}: {exc}") from exc

    def close(self) -> None:
        """Close the sqlite connection."""
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error as exc:
                log.warning("closing project index failed: %s", exc)

    # ------------------------------------------------------------------ projects
    def paths(self, pid: str) -> ProjectPaths:
        """Return the :class:`ProjectPaths` of project ``pid`` (no existence check)."""
        _require_id(pid, "project")
        root = self.root / pid
        return ProjectPaths(root=root, repo=root / "repo", runs=root / "runs", cache=root / "cache",
                            attachments=root / "attachments")

    def create(self, name: str) -> Project:
        """Create a project with its directories and an initialised version backend."""
        from .slides import SlideManager, make_backend  # local import: slides imports zipper only

        name = " ".join((name or "").split())
        if not name:
            raise ValueError("project name is required")
        name = name[:MAX_NAME_LEN]
        with self._lock:
            pid = new_id("p")
            while (self.root / pid).exists():
                pid = new_id("p")
            paths = self.paths(pid)
            for d in (paths.repo, paths.runs, paths.cache, paths.attachments):
                d.mkdir(parents=True, exist_ok=True)
            now = now_iso()
            project = Project(id=pid, name=name, created_at=now, updated_at=now)
            try:
                backend = make_backend(paths, self.vcs)
                project.repo = {"backend": backend.kind, "branch": MAIN_BRANCH}
                atomic_write_json(paths.project_json, project.to_dict())
                SlideManager(project, paths, backend, store=self).init()
                project = self.get(pid)
            except Exception:
                rm_tree(paths.root)
                raise
            self._index_project(project)
            log.info("created project %s (%s, %s backend)", pid, name, project.repo.get("backend"))
            return project

    def get(self, pid: str) -> Project:
        """Load ``project.json``; raises FileNotFoundError when the project does not exist."""
        paths = self.paths(pid)
        data = self._read_json(paths.project_json)
        if not isinstance(data, dict):
            raise FileNotFoundError(f"project not found: {pid}")
        return Project.from_dict(data)

    def exists(self, pid: str) -> bool:
        """True when project ``pid`` exists on disk."""
        return valid_id(pid) and (self.root / pid / "project.json").is_file()

    def list(self) -> list[Project]:
        """All projects, newest first."""
        with self._lock:
            rows = self._db.execute("SELECT id FROM kyora_projects ORDER BY created_at DESC, rowid DESC").fetchall()
        out: list[Project] = []
        for (pid,) in rows:
            try:
                out.append(self.get(pid))
            except FileNotFoundError:
                with self._lock:
                    self._db.execute("DELETE FROM kyora_projects WHERE id = ?", (pid,))
                    self._db.commit()
            except RuntimeError as exc:
                log.warning("list: %s", exc)
        return out

    def delete(self, pid: str) -> None:
        """Delete a project directory and its index rows."""
        with self._lock:
            paths = self.paths(pid)
            if not paths.project_json.is_file():
                raise FileNotFoundError(f"project not found: {pid}")
            rm_tree(paths.root)
            if paths.root.exists():
                raise OSError(f"could not delete {paths.root} completely")
            self._db.execute("DELETE FROM kyora_projects WHERE id = ?", (pid,))
            self._db.execute("DELETE FROM kyora_runs WHERE project_id = ?", (pid,))
            self._db.commit()

    def save_project(self, p: Project) -> Project:
        """Write ``project.json`` (bumps ``updated_at``) and update the index."""
        with self._lock:
            paths = self.paths(p.id)
            if not paths.root.is_dir():
                raise FileNotFoundError(f"project not found: {p.id}")
            p.updated_at = now_iso()
            atomic_write_json(paths.project_json, p.to_dict())
            self._index_project(p)
            return p

    def update_project(self, pid: str, fn: Callable[[Project], Any]) -> Project:
        """Atomically load, modify (``fn(project)``) and save a project."""
        with self._lock:
            p = self.get(pid)
            fn(p)
            return self.save_project(p)

    def slide_manager(self, project: Project | str):
        """Return a :class:`kyora.projects.slides.SlideManager` for ``project``."""
        from .slides import SlideManager

        return SlideManager.for_project(self, project)

    # ------------------------------------------------------------------ attachments
    def add_attachment(self, pid: str, filename: str, mime: str | None, data: bytes | bytearray | str,
                       role: str = "reference") -> Attachment:
        """Store an uploaded file under ``attachments/<aid>_<name>`` and record it (64 MB cap)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachment {filename!r} is {len(data)} bytes; the limit is 64 MB")
        data = bytes(data)
        role = (role or "reference").strip().lower()
        if not _ROLE_RE.match(role):
            raise ValueError(f"invalid attachment role {role!r}")
        name = sanitize_filename(filename)
        mime = _normalise_mime(mime, name)
        with self._lock:
            project = self.get(pid)
            paths = self.paths(pid)
            existing = {a.id for a in project.attachments}
            aid = new_id("a")
            while aid in existing:
                aid = new_id("a")
            rel = f"attachments/{aid}_{name}"
            atomic_write_bytes(safe_join(paths.root, "attachments", f"{aid}_{name}"), data)
            att = Attachment(id=aid, name=name, mime=mime, size=len(data), path=rel, role=role)
            project.attachments.append(att)
            self.save_project(project)
            return att

    def attachment_path(self, pid: str, aid: str) -> Path:
        """Absolute path of attachment ``aid``; FileNotFoundError when unknown or missing."""
        project = self.get(pid)
        att = project.attachment(aid)
        if att is None:
            raise FileNotFoundError(f"attachment not found: {aid}")
        path = safe_join(self.paths(pid).root, *att.path.split("/"))
        if not path.is_file():
            raise FileNotFoundError(f"attachment file missing: {att.path}")
        return path

    def attachment_views(self, pid: str, ids: Iterable[str] | None = None) -> list[dict]:
        """Describe attachments for the models.

        Keys: ``id, name, mime, size, role, path`` (relative), ``abs_path``,
        ``text_excerpt`` (<= 60 KB, text-like files), ``truncated``,
        ``image_data_url`` (``image/*`` <= 8 MB), ``zip_listing`` and ``listing``
        (same list, first 400 entries, ZIPs). Keys that do not apply are None.
        ``ids=None`` describes every attachment; an unknown id raises
        FileNotFoundError.
        """
        project = self.get(pid)
        selected: list[Attachment]
        if ids is None:
            selected = list(project.attachments)
        else:
            selected = []
            for aid in ids:
                att = project.attachment(aid)
                if att is None:
                    raise FileNotFoundError(f"attachment not found: {aid}")
                selected.append(att)
        views: list[dict] = []
        for att in selected:
            path = self.attachment_path(pid, att.id)
            view: dict[str, Any] = {"id": att.id, "name": att.name, "mime": att.mime, "size": att.size,
                                    "role": att.role, "path": att.path, "abs_path": str(path),
                                    "text_excerpt": None, "truncated": False, "image_data_url": None,
                                    "zip_listing": None, "listing": None}
            if _is_text_like(att.mime, att.name):
                try:
                    view["text_excerpt"], view["truncated"] = _read_excerpt(path)
                except OSError as exc:
                    log.warning("attachment %s: cannot read excerpt: %s", att.id, exc)
            if att.mime.startswith("image/") and att.size <= IMAGE_INLINE_MAX:
                try:
                    view["image_data_url"] = f"data:{att.mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
                except OSError as exc:
                    log.warning("attachment %s: cannot read image: %s", att.id, exc)
            if _is_zip(att.mime, att.name):
                try:
                    listing = list_zip(path, ZIP_LISTING_LIMIT)
                except (UnsafeZipError, OSError) as exc:
                    view["zip_error"] = str(exc)
                    listing = []
                view["zip_listing"] = listing
                view["listing"] = list(listing)
            views.append(view)
        return views

    # ------------------------------------------------------------------ runs
    def create_run(self, pid: str, kind: str, prompt: str, parent_slide: int | None = None,
                   attachment_ids: Iterable[str] | None = None, options: dict | None = None, *,
                   threshold: float | None = None) -> Run:
        """Create ``runs/<rid>/run.json`` in state ``created`` and index it.

        Validates ``kind`` (generate|modify), that ``parent_slide`` and every
        attachment id (including ``options.import_zip_attachment``) exist.
        ``threshold`` defaults to the default quality gate (0.94); the pipeline
        overwrites it with the configured value.
        """
        if kind not in RUN_KINDS:
            raise ValueError(f"run kind must be one of {RUN_KINDS}, got {kind!r}")
        if prompt is None:
            prompt = ""
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        opts = _default_options()
        if options is not None:
            if not isinstance(options, dict):
                raise ValueError("options must be an object")
            opts.update(copy.deepcopy(options))
        att_ids = [str(a) for a in (attachment_ids or [])]
        with self._lock:
            project = self.get(pid)
            if parent_slide is not None:
                try:
                    parent_slide = int(str(parent_slide).lstrip("vV"))
                except ValueError:
                    raise ValueError(f"invalid parent slide {parent_slide!r}") from None
                if project.slide(parent_slide) is None:
                    raise ValueError(f"parent slide v{parent_slide} does not exist")
            for aid in att_ids + ([opts["import_zip_attachment"]] if opts.get("import_zip_attachment") else []):
                if project.attachment(str(aid)) is None:
                    raise ValueError(f"unknown attachment {aid!r}")
            rid = new_id("r")
            while (self.paths(pid).runs / rid).exists():
                rid = new_id("r")
            gate = _default_gate()
            if threshold is not None:
                gate["threshold"] = float(threshold)
            run = Run(id=rid, project_id=pid, kind=kind, parent_slide=parent_slide, prompt=prompt,
                      attachment_ids=att_ids, options=opts, gate=gate, created_at=now_iso())
            self.run_dir(run).mkdir(parents=True, exist_ok=True)
            self.save_run(run)
            return run

    def run_dir(self, run: Run | str) -> Path:
        """Directory of a run (``<root>/<pid>/runs/<rid>``)."""
        if isinstance(run, str):
            run = self.get_run(run)
        return self.paths(run.project_id).runs / _require_id(run.id, "run")

    def get_run(self, rid: str) -> Run:
        """Load a run by id (index lookup, then a disk scan); FileNotFoundError when missing."""
        _require_id(rid, "run")
        with self._lock:
            row = self._db.execute("SELECT project_id FROM kyora_runs WHERE id = ?", (rid,)).fetchone()
        candidates: list[Path] = []
        if row and valid_id(row[0] or ""):
            candidates.append(self.root / row[0] / "runs" / rid / "run.json")
        else:
            candidates.extend(sorted(self.root.glob(f"*/runs/{rid}/run.json")))
        for path in candidates:
            data = self._read_json(path)
            if isinstance(data, dict):
                run = Run.from_dict(data)
                if not run.project_id:
                    run.project_id = path.parent.parent.parent.name
                if not row:
                    self._index_run(run)
                return run
        raise FileNotFoundError(f"run not found: {rid}")

    def save_run(self, run: Run) -> Run:
        """Write ``run.json`` and update the index."""
        with self._lock:
            path = self.run_dir(run) / "run.json"
            if not self.paths(run.project_id).root.is_dir():
                raise FileNotFoundError(f"project not found: {run.project_id}")
            atomic_write_json(path, run.to_dict())
            self._index_run(run)
            return run

    def list_runs(self, pid: str | None = None, limit: int = 50) -> list[Run]:
        """Runs (of one project or all), newest first."""
        limit = max(1, min(int(limit or 50), 1000))
        with self._lock:
            if pid is not None:
                _require_id(pid, "project")
                rows = self._db.execute("SELECT id, project_id FROM kyora_runs WHERE project_id = ? "
                                        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (pid, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT id, project_id FROM kyora_runs ORDER BY created_at DESC, rowid DESC "
                                        "LIMIT ?", (limit,)).fetchall()
        out: list[Run] = []
        for rid, rpid in rows:
            if not (valid_id(rid) and valid_id(rpid or "")):
                continue
            try:
                data = self._read_json(self.root / rpid / "runs" / rid / "run.json")
            except RuntimeError as exc:
                log.warning("list_runs: %s", exc)
                continue
            if isinstance(data, dict):
                out.append(Run.from_dict(data))
            else:
                with self._lock:
                    self._db.execute("DELETE FROM kyora_runs WHERE id = ?", (rid,))
                    self._db.commit()
        return out

    def set_state(self, run: Run, state: str, **fields_: Any) -> Run:
        """Set ``run.state`` (validated), apply ``fields_`` and save.

        Unknown field names are stored as extra keys of ``run.json``;
        ``finished_at`` is stamped when a terminal state is reached.
        """
        if state not in RUN_STATES:
            raise ValueError(f"unknown run state {state!r}")
        known = set(Run._known())
        with self._lock:
            run.state = state
            for k, v in fields_.items():
                if k in ("id", "project_id"):
                    raise ValueError(f"{k} cannot be changed")
                if k in known:
                    setattr(run, k, v)
                else:
                    run.extra[k] = v
            if state in TERMINAL_STATES and not run.finished_at:
                run.finished_at = now_iso()
            return self.save_run(run)
