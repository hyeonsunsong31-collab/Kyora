"""Kyora Hub: share one machine's code engine with other Kyora instances on the LAN.

Why: iOS/iSH cannot run Node, so it cannot run Claude Code. A PC or Android
Kyora started with ``kyora hub`` exposes its local code engine under
``/api/code-engine/*``; the phone's Kyora runs intake/plan/design/test/gate
itself and delegates only the BUILD/FIX stage to the hub.

Server side (hub mode only):

* :class:`HubAuth` - ``Authorization: Bearer <hub.token>`` compared with
  ``hmac.compare_digest``; 20 failures from one IP within 60 s lock that IP out
  (HTTP 429) for 60 s.
* :class:`HubJobStore` - ``KYORA_HOME/hub/jobs/<id>/{job.json,state.json,
  result.json,workspace/,run/}``; uploads are unzipped zip-slip-safely.
* :class:`HubWorker` - one thread running jobs sequentially on the hub's *local*
  engine (Claude Code CLI, else the Messages-API engine; never the hub client,
  so hubs cannot loop). Engine events go to the hub's bus with ``hub_job_id``.
* :func:`register_routes` - ``GET status``, ``POST jobs``, ``GET jobs/{id}``,
  ``GET jobs/{id}/events`` (SSE relay), ``GET jobs/{id}/result`` (zip),
  ``POST jobs/{id}/cancel``.

Client side (every Kyora): :class:`HubClientEngine` is a :class:`CodeEngine`
that uploads the workspace, relays the hub's engine/judge events into the local
pipeline, and unpacks the resulting workspace (keeping the local ``.git``).
Nothing is fabricated: an unreachable hub or a hub without an engine is
reported as unavailable with the reason, and the pipeline reports BLOCKED.
"""
from __future__ import annotations

import hmac
import http.client
import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import __version__
from .models.base import Availability, BusSink, CodeEngine, EngineEvent, EngineJob, EngineResult, EngineSink
from .server import Response, event_stream
from .util.fs import atomic_write_json, read_json, rm_tree, which
from .util.ids import new_id, now_iso
from .util.sse import parse_sse

log = logging.getLogger("kyora.hub")

API_PREFIX = "/api/code-engine"
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_EXTRACT_BYTES = 512 * 1024 * 1024
MAX_EXTRACT_FILES = 20000
NO_ENGINE_REASON = "hub has no code engine: install Claude Code and run claude login on the hub"
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
JOB_KINDS = ("build", "fix")
RELAY_TYPES = ["engine.*", "judge.*", "stage.progress"]
#: Reserved path of the result document inside the result zip (never a game file).
RESULT_ENTRY = ".kyora-hub/result.json"
RESULT_DIR = ".kyora-hub"
#: Upload exclusions: these names at any depth ...
UPLOAD_EXCLUDE_ANY: tuple[str, ...] = (".git", "node_modules")
#: ... and these only at the workspace root (a nested ``src/cache/`` is game code).
UPLOAD_EXCLUDE_TOP: tuple[str, ...] = ("cache",)
#: Top-level names the client keeps when it replaces its workspace with the hub result
#: (everything it did not upload, so nothing local is lost).
CLIENT_KEEP: tuple[str, ...] = (".git",) + tuple(n for n in UPLOAD_EXCLUDE_ANY + UPLOAD_EXCLUDE_TOP if n != ".git")
RESULT_EXCLUDE: tuple[str, ...] = (".git", "node_modules")
DEFAULT_PHONE_UI = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_S = 7200.0

_JOB_ID_RE = re.compile(r"^hj_[0-9a-f]{6,32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DOTGIT_ALIAS_RE = re.compile(r"^(?:\.git[. ]+|git~\d+|gi7eba~\d+)$", re.IGNORECASE)  # = zipper's
_COPY_CHUNK = 1024 * 1024


# ============================================================================= helpers
def link_payload(base_url: str, token: str, ui_url: str | None = None) -> str:
    """Return the link a phone opens to pair with a hub.

    ``http://<host>:<port>/#hub=<urlencoded base_url>&token=<token>`` where
    ``<host>:<port>`` is the Kyora UI that should store the pairing - by default
    the phone's *own* Kyora (``http://127.0.0.1:8765``), because the fragment is
    read by the page's script and saved through that instance's ``PUT
    /api/settings`` (``keys.hub.url`` / ``keys.hub.token``). The fragment never
    reaches any server.
    """
    base = (ui_url or DEFAULT_PHONE_UI).rstrip("/")
    return (f"{base}/#hub={urllib.parse.quote(str(base_url).rstrip('/'), safe='')}"
            f"&token={urllib.parse.quote(str(token), safe='')}")


def encode_multipart(fields: Sequence[tuple[str, str | None, str, bytes]]) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body from ``(name, filename, content_type, data)`` tuples.

    Returns ``(body, content_type_header)``. The boundary is random and checked
    against every part so it can never occur inside the payload.
    """
    while True:
        boundary = "----KyoraHub" + secrets.token_hex(16)
        marker = boundary.encode("ascii")
        if not any(marker in data for _, _, _, data in fields):
            break
    out = io.BytesIO()
    for name, filename, ctype, data in fields:
        out.write(b"--" + marker + b"\r\n")
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out.write(disp.encode("utf-8") + b"\r\n")
        out.write(f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        out.write(data)
        out.write(b"\r\n")
    out.write(b"--" + marker + b"--\r\n")
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def zip_tree(src: Path | str, *, exclude_any: Iterable[str] = (), exclude_top: Iterable[str] = (),
             extra: dict[str, bytes] | None = None) -> bytes:
    """Zip the regular files under ``src`` (sorted, deflated, symlinks never followed).

    ``exclude_any`` names are skipped at any depth, ``exclude_top`` only at the
    root; ``extra`` maps archive paths to bytes appended after the tree.
    """
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"not a directory: {src}")
    ex_any = set(exclude_any)
    ex_top = set(exclude_top)
    files: list[tuple[str, Path, os.stat_result]] = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        at_root = base == src
        keep: list[str] = []
        for d in dirnames:
            if d in ex_any or (at_root and d in ex_top):
                continue
            if (base / d).is_symlink():
                log.info("zip: skipping symlinked directory %s", base / d)
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for name in filenames:
            if name in ex_any or (at_root and name in ex_top):
                continue
            path = base / name
            try:
                st = path.lstat()
            except OSError as exc:
                log.warning("zip: cannot stat %s: %s", path, exc)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files.append((path.relative_to(src).as_posix(), path, st))
    files.sort(key=lambda item: item[0])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, path, st in files:
            info = zipfile.ZipInfo(rel, date_time=time.localtime(max(st.st_mtime, 315532800.0))[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if st.st_mode & 0o111 else 0o644)) << 16
            with open(path, "rb") as rf, zf.open(info, "w", force_zip64=st.st_size > 0x7FFFFFFF) as wf:
                shutil.copyfileobj(rf, wf, _COPY_CHUNK)
        for rel, data in (extra or {}).items():
            info = zipfile.ZipInfo(rel, date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _check_entry_name(raw: str) -> tuple[str, bool]:
    """Validate a zip entry name; return ``(posix_relpath, is_dir)`` or raise ``ValueError``."""
    if "\x00" in raw:
        raise ValueError(f"unsafe zip entry (NUL): {raw!r}")
    name = raw.replace("\\", "/")
    is_dir = name.endswith("/")
    if name.startswith("/") or _DRIVE_RE.match(name) or ":" in name:
        raise ValueError(f"unsafe zip entry (absolute/drive/stream path): {raw!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"unsafe zip entry ('..'): {raw!r}")
    # Same rule as zipper: NTFS maps GIT~<n> and ".git" + trailing dots/spaces onto .git, and Win32 strips
    # trailing dots/spaces from every name, so such components could overwrite another file or .git/config.
    if any(_DOTGIT_ALIAS_RE.match(p) or (os.name == "nt" and p.endswith((" ", "."))) for p in parts):
        raise ValueError(f"unsafe zip entry (aliases .git or another name on Windows): {raw!r}")
    return "/".join(parts), is_dir


def fallback_unzip(data: bytes, dest: Path | str, *, replace: bool = False, keep: Sequence[str] = (".git",),
                   max_total: int = MAX_EXTRACT_BYTES, max_files: int = MAX_EXTRACT_FILES) -> list[str]:
    """Minimal zip-slip-safe extraction used when ``kyora.projects.zipper`` cannot be imported.

    Validates every entry before writing (absolute paths, drive letters, ``..``,
    ``:``, symlink entries, paths resolving outside ``dest``, size/count limits)
    and never writes into a ``.git`` directory. ``replace`` empties ``dest``
    first except the top-level names in ``keep``. Raises ``ValueError``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid ZIP archive: {exc}") from exc
    with zf:
        plan: list[tuple[zipfile.ZipInfo, str, bool]] = []
        total = files = 0
        for info in zf.infolist():
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(f"unsafe zip entry (symlink): {info.filename!r}")
            name, is_dir = _check_entry_name(info.filename)
            is_dir = is_dir or info.is_dir()
            if not name:
                continue
            if any(p.lower() == ".git" for p in name.split("/")):
                log.warning("unzip: skipping entry inside a .git directory: %s", info.filename)
                continue
            target = (root / name).resolve()
            try:
                rel = target.relative_to(root)
            except ValueError:
                raise ValueError(f"unsafe zip entry (escapes destination): {info.filename!r}") from None
            if any(p.lower() == ".git" for p in rel.parts):  # an existing short name resolved to .git
                raise ValueError(f"unsafe zip entry (resolves into .git): {info.filename!r}")
            if not is_dir:
                files += 1
                total += max(int(info.file_size), 0)
                if files > max_files or total > max_total:
                    raise ValueError("archive exceeds the extraction limits")
            plan.append((info, name, is_dir))
        if replace:
            keep_set = set(keep or ())
            for child in root.iterdir():
                if child.name in keep_set:
                    continue
                if child.is_dir() and not child.is_symlink():
                    rm_tree(child)
                else:
                    child.unlink()
        written: list[str] = []
        budget = max_total
        for info, name, is_dir in plan:
            target = root.joinpath(*name.split("/"))
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                rel = target.parent.resolve().relative_to(root)
            except ValueError:
                raise ValueError(f"unsafe zip entry (escapes destination): {info.filename!r}") from None
            if any(p.lower() == ".git" for p in rel.parts):
                raise ValueError(f"unsafe zip entry (resolves into .git): {info.filename!r}")
            if target.is_symlink():
                target.unlink()
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    budget -= len(chunk)
                    if budget < 0:
                        raise ValueError("archive exceeds the extraction limits")
                    dst.write(chunk)
            if os.name != "nt" and (info.external_attr >> 16) & 0o111:
                try:
                    os.chmod(target, 0o755)
                except OSError as exc:
                    log.debug("chmod failed for %s: %s", target, exc)
            written.append(name)
    return written


def unzip_into(data: bytes, dest: Path | str, *, replace: bool = False, keep: Sequence[str] = (".git",)) -> list[str]:
    """Extract with ``kyora.projects.zipper.unzip_safe`` (imported lazily), else :func:`fallback_unzip`."""
    try:
        from .projects.zipper import unzip_safe as impl
    except Exception as exc:  # noqa: BLE001 - another module may be missing or broken; fall back, but say so
        log.warning("kyora.projects.zipper unavailable (%s: %s); using the hub's minimal safe unzip",
                    type(exc).__name__, exc)
        return fallback_unzip(data, dest, replace=replace, keep=keep)
    return impl(data, dest, replace=replace, keep=tuple(keep))


def normalize_job(raw: Any) -> dict:
    """Validate a submitted job document and return the normalized copy (raises ``ValueError``)."""
    if not isinstance(raw, dict):
        raise ValueError("job must be a JSON object")
    kind = str(raw.get("kind") or "build")
    if kind not in JOB_KINDS:
        raise ValueError(f"job.kind must be one of {JOB_KINDS}")
    run_id = str(raw.get("run_id") or "")
    if not _SAFE_ID_RE.match(run_id):
        raise ValueError("job.run_id is missing or contains unsupported characters")
    project_id = raw.get("project_id")
    if project_id is not None and not _SAFE_ID_RE.match(str(project_id)):
        raise ValueError("job.project_id contains unsupported characters")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("job.prompt is required")
    system_prompt = raw.get("system_prompt") or ""
    if not isinstance(system_prompt, str):
        raise ValueError("job.system_prompt must be a string")
    max_turns = raw.get("max_turns")
    if max_turns is not None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 0 <= max_turns <= 100000:
            raise ValueError("job.max_turns must be a non-negative integer")
    model = raw.get("model") or ""
    if not isinstance(model, str):
        raise ValueError("job.model must be a string")
    fallbacks = raw.get("fallback_models")
    if fallbacks is not None and (not isinstance(fallbacks, list) or not all(isinstance(m, str) for m in fallbacks)):
        raise ValueError("job.fallback_models must be a list of strings")
    effort = raw.get("effort") or ""
    if not isinstance(effort, str):
        raise ValueError("job.effort must be a string")
    resume = raw.get("resume_session_id")
    if resume is not None and (not isinstance(resume, str) or not _SAFE_ID_RE.match(resume)):
        raise ValueError("job.resume_session_id must be a session id string")
    ctx = raw.get("judge_context") or {}
    if not isinstance(ctx, dict):
        raise ValueError("job.judge_context must be an object")
    return {"kind": kind, "run_id": run_id, "project_id": project_id, "prompt": prompt, "system_prompt": system_prompt,
            "max_turns": max_turns, "model": model, "fallback_models": fallbacks, "effort": effort,
            "resume_session_id": resume or None, "judge_context": ctx}


# ============================================================================= auth
class HubAuth:
    """Bearer-token check with a per-IP failure lockout.

    ``token`` is the expected token or a callable returning it (so a token
    changed in Settings takes effect without a restart). ``max_failures`` bad
    attempts within ``window_s`` lock the client IP out for ``lockout_s``;
    while locked out every request from that IP gets 429, valid token or not.
    """

    def __init__(self, token: str | Callable[[], str], *, max_failures: int = 20, window_s: float = 60.0,
                 lockout_s: float = 60.0, clock: Callable[[], float] = time.monotonic, max_tracked: int = 4096):
        self._token = token
        self.max_failures = int(max_failures)
        self.window_s = float(window_s)
        self.lockout_s = float(lockout_s)
        self._clock = clock
        self._max_tracked = int(max_tracked)
        self._fails: dict[str, deque] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def token(self) -> str:
        """The currently expected token ('' when none is configured)."""
        tok = self._token() if callable(self._token) else self._token
        return str(tok or "")

    def check(self, request) -> int:
        """Return 200 (valid bearer token), 401 (missing/invalid) or 429 (IP locked out)."""
        ip = str(getattr(request, "client_ip", "") or "?")
        now = self._clock()
        with self._lock:
            until = self._locked.get(ip)
            if until is not None:
                if now < until:
                    return 429
                del self._locked[ip]
                self._fails.pop(ip, None)
        expected = self.token()
        presented = request.bearer() if hasattr(request, "bearer") else ""
        if expected and presented and hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8")):
            with self._lock:
                self._fails.pop(ip, None)
            return 200
        with self._lock:
            hist = self._fails.setdefault(ip, deque())
            hist.append(now)
            while hist and now - hist[0] > self.window_s:
                hist.popleft()
            if len(hist) >= self.max_failures:
                self._locked[ip] = now + self.lockout_s
                self._fails.pop(ip, None)
                log.warning("hub: %d failed token attempts from %s within %.0f s; locked out for %.0f s",
                            self.max_failures, ip, self.window_s, self.lockout_s)
            self._prune(now)
        return 401

    def retry_after(self, ip: str) -> int:
        """Seconds until ``ip`` may try again (0 when not locked out)."""
        with self._lock:
            until = self._locked.get(ip)
        if until is None:
            return 0
        return max(0, int(until - self._clock() + 0.999))

    def _prune(self, now: float) -> None:
        if len(self._fails) + len(self._locked) <= self._max_tracked:
            return
        for ip in [ip for ip, until in self._locked.items() if until <= now]:
            del self._locked[ip]
        for ip in [ip for ip, h in self._fails.items() if not h or now - h[-1] > self.window_s]:
            del self._fails[ip]
        while len(self._fails) > self._max_tracked:  # still too many: drop the oldest histories
            del self._fails[next(iter(self._fails))]


# ============================================================================= job store
class HubJobNotFinished(RuntimeError):
    """Raised by :meth:`HubJobStore.result_zip` while a job is queued or running."""


@dataclass
class HubJob:
    """One delegated BUILD/FIX job on the hub."""
    id: str
    dir: Path
    job: dict
    state: str = "queued"
    created_at: str = ""
    updated_at: str = ""
    updated_ts: float = 0.0
    error: str | None = None
    result: dict | None = None
    events_since: int = 0
    workspace_of: str | None = None
    seq: int = 0

    @property
    def run_id(self) -> str:
        return str(self.job.get("run_id") or "")

    @property
    def project_id(self) -> str | None:
        return self.job.get("project_id")

    @property
    def kind(self) -> str:
        return str(self.job.get("kind") or "build")

    @property
    def workspace(self) -> Path:
        """Engine cwd: ``<dir>/workspace``, or the adopted workspace of an earlier job (session resume)."""
        if self.workspace_of:
            return self.dir.parent / self.workspace_of / "workspace"
        return self.dir / "workspace"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def meta(self) -> dict:
        return {"id": self.id, "seq": self.seq, "state": self.state, "created_at": self.created_at,
                "updated_at": self.updated_at, "updated_ts": self.updated_ts, "error": self.error,
                "events_since": self.events_since, "workspace_of": self.workspace_of}

    def to_dict(self) -> dict:
        return {"job_id": self.id, "state": self.state, "kind": self.kind, "run_id": self.run_id,
                "project_id": self.project_id, "created_at": self.created_at, "updated_at": self.updated_at,
                "error": self.error, "result": self.result, "events_since": self.events_since,
                "workspace_of": self.workspace_of}


class HubJobStore:
    """Jobs under ``root`` (``KYORA_HOME/hub/jobs``); thread-safe; survives restarts.

    Jobs found queued/running at start-up are marked failed (the engine run
    they belonged to died with the previous process).
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, HubJob] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        for d in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not d.is_dir() or not _JOB_ID_RE.match(d.name):
                continue
            try:
                job = read_json(d / "job.json", default=None)
                meta = read_json(d / "state.json", default=None)
            except (OSError, ValueError) as exc:
                log.warning("hub: unreadable job %s: %s", d.name, exc)
                continue
            if not isinstance(job, dict) or not isinstance(meta, dict):
                continue
            result = None
            try:
                result = read_json(d / "result.json", default=None)
            except (OSError, ValueError) as exc:
                log.warning("hub: unreadable result for %s: %s", d.name, exc)
            hj = HubJob(id=d.name, dir=d, job=job, state=str(meta.get("state") or "failed"),
                        created_at=str(meta.get("created_at") or ""), updated_at=str(meta.get("updated_at") or ""),
                        updated_ts=float(meta.get("updated_ts") or 0.0), error=meta.get("error"), result=result,
                        events_since=int(meta.get("events_since") or 0), workspace_of=meta.get("workspace_of"),
                        seq=int(meta.get("seq") or 0))
            self._seq = max(self._seq, hj.seq)
            if not hj.terminal:
                hj.state = "failed"
                hj.error = "hub restarted before the job finished"
                hj.updated_at, hj.updated_ts = now_iso(), time.time()
                self._persist(hj)
            self._jobs[hj.id] = hj

    def _persist(self, job: HubJob) -> None:
        atomic_write_json(job.dir / "state.json", job.meta())

    # ------------------------------------------------------------------ API
    def create(self, job_json: dict, workspace_zip: bytes, *, workspace_of: str | None = None,
               events_since: int = 0) -> HubJob:
        """Validate ``job_json``, store it and unpack ``workspace_zip`` safely.

        With ``workspace_of`` (a finished job id) the upload replaces that job's
        workspace instead, so a resumed engine session keeps the same cwd.
        Raises ``ValueError`` for an invalid job or an unsafe/invalid zip; nothing
        is left behind on failure.
        """
        job = normalize_job(job_json)
        if workspace_of is not None:
            prev = self.get(workspace_of)
            if prev is None or not prev.terminal or not prev.workspace.parent.is_dir():
                workspace_of = None
        with self._lock:
            self._seq += 1
            seq = self._seq
            while True:
                job_id = new_id("hj")
                d = self.root / job_id
                try:
                    d.mkdir(parents=False, exist_ok=False)
                    break
                except FileExistsError:
                    continue
        now = now_iso()
        hj = HubJob(id=job_id, dir=d, job=job, state="queued", created_at=now, updated_at=now, updated_ts=time.time(),
                    events_since=int(events_since), workspace_of=workspace_of, seq=seq)
        try:
            atomic_write_json(d / "job.json", job)
            ws = hj.workspace
            ws.mkdir(parents=True, exist_ok=True)
            unzip_into(workspace_zip, ws, replace=workspace_of is not None, keep=(".git",))
            (d / "run").mkdir(exist_ok=True)
            self._persist(hj)
        except BaseException:
            rm_tree(d)
            raise
        with self._lock:
            self._jobs[job_id] = hj
        return hj

    def get(self, job_id: str) -> HubJob | None:
        """The job with ``job_id`` (``None`` when unknown or malformed)."""
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[HubJob]:
        """All known jobs, oldest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: (j.seq, j.created_at, j.id))

    def find_by_session(self, session_id: str) -> HubJob | None:
        """Most recent finished job whose engine result carries ``session_id``."""
        if not session_id:
            return None
        for job in reversed(self.list()):
            if job.terminal and isinstance(job.result, dict) and job.result.get("session_id") == session_id:
                return job
        return None

    def set_state(self, job_id: str, state: str, result: dict | None = None, error: str | None = None) -> HubJob:
        """Move a job to ``state``; ``result`` (EngineResult dict) is written to ``result.json``."""
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if result is not None:
                atomic_write_json(job.dir / "result.json", result)
                job.result = result
            job.state = state
            if error is not None or state in ("queued", "running", "done"):
                job.error = error
            job.updated_at, job.updated_ts = now_iso(), time.time()
            self._persist(job)
            return job

    def result_zip(self, job_id: str) -> bytes:
        """Zip of the job workspace (no ``.git``/``node_modules``) plus ``.kyora-hub/result.json``.

        Raises ``FileNotFoundError`` for an unknown job and
        :class:`HubJobNotFinished` while it is queued or running.
        """
        job = self.get(job_id)
        if job is None:
            raise FileNotFoundError(f"unknown hub job {job_id}")
        if not job.terminal:
            raise HubJobNotFinished(f"job {job_id} is {job.state}")
        doc = {"job_id": job.id, "state": job.state, "error": job.error, "result": job.result}
        extra = {RESULT_ENTRY: json.dumps(doc, ensure_ascii=False, indent=2, default=str).encode("utf-8")}
        ws = job.workspace
        if not ws.is_dir():
            raise FileNotFoundError(f"workspace of job {job_id} is gone")
        return zip_tree(ws, exclude_any=RESULT_EXCLUDE, exclude_top=(RESULT_DIR,), extra=extra)

    def expire(self, hours: float = 24.0) -> list[str]:
        """Delete finished jobs last updated more than ``hours`` ago; return the removed ids.

        A job whose workspace is still used by a newer, unexpired job (session
        resume) is kept until that job expires too.
        """
        cutoff = time.time() - float(hours) * 3600.0
        with self._lock:
            jobs = list(self._jobs.values())
            expired = {j.id for j in jobs if j.terminal and j.updated_ts and j.updated_ts < cutoff}
            needed = {j.workspace_of for j in jobs if j.workspace_of and j.id not in expired}
            removed = []
            for job_id in sorted(expired - needed):
                job = self._jobs.pop(job_id)
                try:
                    rm_tree(job.dir)
                except OSError as exc:
                    log.warning("hub: could not remove expired job %s: %s", job_id, exc)
                removed.append(job_id)
        if removed:
            log.info("hub: expired %d job(s): %s", len(removed), ", ".join(removed))
        return removed


# ============================================================================= worker (hub side)
def select_local_engine(app) -> tuple[CodeEngine | None, Availability | None, dict]:
    """First available *local* engine of ``app``: Claude Code CLI, then the Messages-API engine.

    Never returns the hub client (a hub must not forward to another hub).
    Returns ``(engine, availability, reasons_by_candidate)``.
    """
    reasons: dict[str, str] = {}
    missing = getattr(app, "missing", {}) or {}
    for name, eng in (("claude_code", getattr(app, "claude_engine", None)),
                      ("anthropic_api", getattr(app, "anthropic_engine", None))):
        if eng is None:
            reasons[name] = missing.get(name, "not configured")
            continue
        if isinstance(eng, HubClientEngine) or eng is getattr(app, "hub_client", None):
            reasons[name] = "a hub client cannot run hub jobs"
            continue
        try:
            av = eng.availability()
        except Exception as exc:  # noqa: BLE001 - one broken engine must not hide the others
            log.warning("hub: availability of %s failed: %s", name, exc)
            reasons[name] = f"{type(exc).__name__}: {exc}"
            continue
        if av.available:
            return eng, av, reasons
        reasons[name] = av.reason or "unavailable"
    return None, None, reasons


def engine_status(app) -> dict:
    """The ``engine`` block of ``GET /api/code-engine/status``."""
    eng, av, reasons = select_local_engine(app)
    if eng is None or av is None:
        return {"available": False, "backend": None, "reason": NO_ENGINE_REASON, "details": {"candidates": reasons}}
    out = av.to_dict()
    out["backend"] = av.backend or getattr(eng, "name", "unknown")
    return out


class HubWorker:
    """Runs submitted hub jobs one at a time on the hub's local engine."""

    EXPIRE_EVERY_S = 3600.0

    def __init__(self, app, *, store: HubJobStore | None = None, expire_hours: float = 24.0):
        self.app = app
        self.store = store or HubJobStore(Path(app.home) / "hub" / "jobs")
        self.expire_hours = float(expire_hours)
        self._pending: deque[str] = deque()
        self._cancels: dict[str, threading.Event] = {}
        self._finished: dict[str, threading.Event] = {}
        self._current: str | None = None
        self._cond = threading.Condition(threading.Lock())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_expire = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the worker thread (idempotent) after expiring old jobs."""
        if self._thread and self._thread.is_alive():
            return
        self._expire()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kyora-hub-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the worker; a running job is cancelled (the engine kills its process tree)."""
        self._stop.set()
        with self._cond:
            current = self._current
            self._cond.notify_all()
        if current and current in self._cancels:
            self._cancels[current].set()
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ API
    def submit(self, job_json: dict, workspace_zip: bytes) -> HubJob:
        """Store and enqueue a job (raises ``ValueError`` for an invalid job or zip)."""
        events_since = self.app.bus.last_id()
        workspace_of = None
        resume = job_json.get("resume_session_id") if isinstance(job_json, dict) else None
        if resume:
            prev = self.store.find_by_session(str(resume))
            if prev is not None:
                workspace_of = prev.workspace_of or prev.id
        job = self.store.create(job_json, workspace_zip, workspace_of=workspace_of, events_since=events_since)
        with self._cond:
            self._cancels[job.id] = threading.Event()
            self._finished[job.id] = threading.Event()
            self._pending.append(job.id)
            position = len(self._pending) + (1 if self._current else 0)
            self._cond.notify_all()
        self._publish_job(job)
        self._progress(job, f"hub: {job.kind} job {job.id} queued (position {position})")
        return job

    def cancel(self, job_id: str) -> HubJob | None:
        """Cancel a queued job immediately or signal a running one; ``None`` when unknown."""
        job = self.store.get(job_id)
        if job is None:
            return None
        was_pending = False
        with self._cond:
            ev = self._cancels.setdefault(job_id, threading.Event())
            ev.set()
            if job_id in self._pending:
                self._pending.remove(job_id)
                was_pending = True
        if was_pending or (job.state == "queued" and self._current != job_id):
            job = self.store.set_state(job_id, "cancelled", error="cancelled before it started")
            self._finish(job)
        return self.store.get(job_id)

    def finished_event(self, job_id: str) -> threading.Event:
        """Event set once ``job_id`` is finished (already set for finished/unknown jobs)."""
        with self._cond:
            ev = self._finished.get(job_id)
            if ev is None:
                ev = threading.Event()
                job = self.store.get(job_id)
                if job is None or job.terminal:
                    ev.set()
                self._finished[job_id] = ev
            return ev

    def queue_status(self) -> dict:
        with self._cond:
            return {"running": 1 if self._current else 0, "pending": len(self._pending)}

    def queue_position(self, job_id: str) -> int | None:
        """0 = running, 1.. = waiting, None = not queued."""
        with self._cond:
            if self._current == job_id:
                return 0
            try:
                return list(self._pending).index(job_id) + 1
            except ValueError:
                return None

    # ------------------------------------------------------------------ internals
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._pending and not self._stop.is_set():
                    self._cond.wait(timeout=1.0)
                    if time.monotonic() - self._last_expire > self.EXPIRE_EVERY_S:
                        break
                if self._stop.is_set():
                    break
                job_id = self._pending.popleft() if self._pending else None
                self._current = job_id
            if job_id is None:
                self._expire()
                continue
            try:
                self._run_job(job_id)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive; record the failure
                log.exception("hub: job %s crashed", job_id)
                try:
                    job = self.store.set_state(job_id, "failed", error=f"hub worker error: {type(exc).__name__}: {exc}")
                    self._publish_job(job)
                except Exception as exc2:  # noqa: BLE001
                    log.error("hub: could not record failure of %s: %s", job_id, exc2)
            finally:
                with self._cond:
                    self._current = None
                job = self.store.get(job_id)
                if job is not None:
                    self._finish(job)

    def _finish(self, job: HubJob) -> None:
        with self._cond:
            ev = self._finished.setdefault(job.id, threading.Event())
            self._cancels.pop(job.id, None)
        ev.set()

    def _expire(self) -> None:
        self._last_expire = time.monotonic()
        try:
            self.store.expire(self.expire_hours)
        except Exception as exc:  # noqa: BLE001
            log.warning("hub: job expiry failed: %s", exc)

    def build_engine_job(self, job: HubJob) -> EngineJob:
        """EngineJob for the local engine: hub-side workspace and run dir, hub config defaults."""
        spec = job.job
        cc = getattr(getattr(self.app, "config", None), "claude_code", None)
        ws = job.workspace
        ctx = dict(spec.get("judge_context") or {})
        ctx["workspace"] = str(ws)
        ctx["run_dir"] = str(job.dir / "run")
        ctx["kind"] = job.kind
        ctx["hub_job_id"] = job.id
        if job.project_id and not ctx.get("project_id"):
            ctx["project_id"] = job.project_id
        fallbacks = spec.get("fallback_models")
        if fallbacks is None:
            fallbacks = list(getattr(cc, "fallback_models", []) or [])
        return EngineJob(
            run_id=job.run_id, workspace=ws, prompt=spec.get("prompt") or "", system_prompt=spec.get("system_prompt") or "",
            max_turns=int(spec["max_turns"]) if spec.get("max_turns") is not None else int(getattr(cc, "max_turns", 60)),
            model=spec.get("model") or getattr(cc, "model", "") or "", fallback_models=list(fallbacks),
            effort=spec.get("effort") or getattr(cc, "effort", "max") or "max",
            resume_session_id=spec.get("resume_session_id") if job.workspace_of else None,
            judge_context=ctx, env={}, kind=job.kind, project_id=job.project_id)

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        with self._cond:
            cancel = self._cancels.setdefault(job_id, threading.Event())
        sink = BusSink(self.app.bus, job.run_id, job.project_id, extra={"hub_job_id": job_id})
        if cancel.is_set() or job.terminal:
            if not job.terminal:
                job = self.store.set_state(job_id, "cancelled", error="cancelled before it started")
                self._publish_job(job)
            return
        engine, av, reasons = select_local_engine(self.app)
        ej = self.build_engine_job(job)
        if engine is None:
            sink.on_event(EngineEvent("engine.error", {"error": NO_ENGINE_REASON, "candidates": reasons}))
            res = EngineResult(ok=False, backend="none", requested_model=ej.model, error=NO_ENGINE_REASON)
            job = self.store.set_state(job_id, "failed", result=self._result_doc(res, job, None, {}),
                                       error=NO_ENGINE_REASON)
            self._publish_job(job)
            return
        if job.job.get("resume_session_id") and not job.workspace_of:
            self._progress(job, "hub: the session to resume ran in another workspace (or expired); starting a new session")
        job = self.store.set_state(job_id, "running")
        self._publish_job(job)
        backend = getattr(av, "backend", None) or getattr(engine, "name", "engine")
        self._progress(job, f"hub: running {job.kind} job {job.id} on {backend}")
        judge = getattr(self.app, "judge", None)
        before = self._judge_stats(judge, job.run_id)
        started = time.monotonic()
        state = "done"
        try:
            result = engine.run(ej, sink, cancel)
            if cancel.is_set():
                state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - report the crash instead of losing the job
            log.exception("hub: engine %s crashed on job %s", backend, job_id)
            msg = f"hub engine crashed: {type(exc).__name__}: {exc}"
            sink.on_event(EngineEvent("engine.error", {"error": msg}))
            result = EngineResult(ok=False, backend=getattr(engine, "name", "engine"), requested_model=ej.model,
                                  error=msg, duration_ms=int((time.monotonic() - started) * 1000))
            state = "failed"
        after = self._judge_stats(judge, job.run_id)
        judge_delta = {k: after.get(k, 0) - before.get(k, 0) for k in after} if after else {}
        doc = self._result_doc(result, job, backend, judge_delta)
        error = result.error if (state != "done" or not result.ok) else None
        if state == "cancelled" and not error:
            error = "cancelled"
        job = self.store.set_state(job_id, state, result=doc, error=error)
        self._publish_job(job)

    @staticmethod
    def _judge_stats(judge, run_id: str) -> dict:
        if judge is None or not hasattr(judge, "stats"):
            return {}
        try:
            return dict(judge.stats(run_id) or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("hub: judge stats unavailable: %s", exc)
            return {}

    @staticmethod
    def _result_doc(result: EngineResult, job: HubJob, backend: str | None, judge: dict) -> dict:
        doc = result.to_dict()
        doc["hub"] = {"job_id": job.id, "kind": job.kind, "engine": backend, "kyora_version": __version__,
                      "finished_at": now_iso()}
        doc["judge"] = judge
        return doc

    def _publish_job(self, job: HubJob) -> None:
        try:
            self.app.bus.publish("hub.job", {"job_id": job.id, "run_id": job.run_id, "kind": job.kind,
                                             "state": job.state, "error": job.error})
        except Exception as exc:  # noqa: BLE001
            log.warning("hub: could not publish hub.job: %s", exc)

    def _progress(self, job: HubJob, message: str) -> None:
        try:
            self.app.bus.publish("stage.progress", {"stage": job.kind, "message": message, "hub_job_id": job.id},
                                 run_id=job.run_id, project_id=job.project_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("hub: could not publish progress: %s", exc)


# ============================================================================= routes (hub side)
def register_routes(router, app) -> None:
    """Register ``/api/code-engine/*`` when ``app.hub_mode`` is True.

    Routes are added with server auth ``any`` and guarded by :class:`HubAuth`
    (bearer token + per-IP lockout): the server's built-in ``hub`` level would
    reject bad tokens before any handler could count them. On every app the
    hub client is bound to the live config so Settings changes apply at once.
    """
    client = getattr(app, "hub_client", None)
    if isinstance(client, HubClientEngine):
        client.bind_config(lambda: app.config.hub)
    if not getattr(app, "hub_mode", False):
        return
    auth = HubAuth(lambda: app.config.hub.token)

    def worker() -> HubWorker | None:
        return getattr(app, "hub_worker", None)

    def guarded(handler: Callable, needs_worker: bool = True) -> Callable:
        def wrapper(req, params):
            code = auth.check(req)
            if code == 429:
                wait = auth.retry_after(req.client_ip)
                resp = Response.error(429, "too many failed token attempts from this address; try again later",
                                      retry_after=wait)
                resp.headers.append(("Retry-After", str(wait)))
                return resp
            if code != 200:
                return Response.error(401, "unauthorized: missing or invalid hub token")
            if needs_worker and worker() is None:
                return Response.error(503, "hub worker unavailable: " + str(
                    getattr(app, "missing", {}).get("hub_worker", "not started")))
            return handler(req, params)
        return wrapper

    def job_or_404(params: dict) -> HubJob:
        w = worker()
        job = w.store.get(params.get("id", "")) if w else None
        if job is None:
            raise FileNotFoundError("unknown hub job")
        return job

    def status(req, params):
        w = worker()
        return Response.json({
            "ok": True, "version": __version__, "hub": True, "engine": engine_status(app),
            "node": which("node") is not None, "git": which("git") is not None,
            "platform": getattr(app, "platform", {}),
            "queue": w.queue_status() if w else {"running": 0, "pending": 0},
            "worker": w is not None,
        })

    def create(req, params):
        parts = req.multipart()
        job_part = next((p for p in parts if p.name == "job"), None)
        ws_part = next((p for p in parts if p.name == "workspace"), None)
        if job_part is None or ws_part is None:
            raise ValueError("multipart parts 'job' (JSON) and 'workspace' (zip) are required")
        if len(ws_part.data) > MAX_UPLOAD_BYTES:
            return Response.error(413, f"workspace zip larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        job_json = json.loads(job_part.data.decode("utf-8"))
        job = worker().submit(job_json, ws_part.data)
        return Response.json({"job_id": job.id, "state": job.state, "run_id": job.run_id,
                              "events_since": job.events_since}, status=201)

    def get_job(req, params):
        job = job_or_404(params)
        out = job.to_dict()
        out["queue_position"] = worker().queue_position(job.id)
        return Response.json(out)

    def events(req, params):
        job = job_or_404(params)
        raw = req.query.get("since") or req.headers.get("last-event-id") or "0"
        try:
            since = max(0, int(raw))
        except ValueError:
            raise ValueError("since must be an integer event id") from None
        return Response.sse(event_stream(app.bus, run_id=job.run_id, project_id=None, types=list(RELAY_TYPES),
                                         since=since, stop=worker().finished_event(job.id)))

    def result(req, params):
        job = job_or_404(params)
        try:
            data = worker().store.result_zip(job.id)
        except HubJobNotFinished:
            return Response.error(409, "job not finished", state=job.state)
        return Response.bytes(data, "application/zip",
                              headers=[("Content-Disposition", f'attachment; filename="{job.id}.zip"'),
                                       ("Cache-Control", "no-store")])

    def cancel(req, params):
        job_or_404(params)
        job = worker().cancel(params["id"])
        if job is None:
            raise FileNotFoundError("unknown hub job")
        return Response.json({"job_id": job.id, "state": job.state})

    router.add("GET", f"{API_PREFIX}/status", guarded(status, needs_worker=False), auth="any")
    router.add("POST", f"{API_PREFIX}/jobs", guarded(create), auth="any")
    router.add("GET", f"{API_PREFIX}/jobs/{{id}}", guarded(get_job), auth="any")
    router.add("GET", f"{API_PREFIX}/jobs/{{id}}/events", guarded(events), auth="any")
    router.add("GET", f"{API_PREFIX}/jobs/{{id}}/result", guarded(result), auth="any")
    router.add("POST", f"{API_PREFIX}/jobs/{{id}}/cancel", guarded(cancel), auth="any")


# ============================================================================= client engine
class _HubHTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _wake_reader(resp) -> None:
    """Shut down the socket under an HTTP response so a ``readline`` blocked in another thread returns.

    Only the socket is shut down; the owning thread closes the response itself
    (closing it from here races ``http.client`` internals).
    """
    try:
        sock = resp.fp.raw._sock  # type: ignore[attr-defined]
        sock.shutdown(socket.SHUT_RDWR)
    except Exception as exc:  # noqa: BLE001 - best effort; the read timeout still ends the read
        log.debug("hub client: could not shut down the relay socket: %s", exc)


class _EventRelay(threading.Thread):
    """Background SSE reader with reconnect (``since=<last id>``, backoff 1, 2, 4 .. 15 s)."""

    def __init__(self, opener, url: str, token: str, job_id: str, since: int, read_timeout: float):
        super().__init__(name=f"kyora-hub-relay-{job_id}", daemon=True)
        self.opener = opener
        self.url = f"{url}{API_PREFIX}/jobs/{job_id}/events"
        self.token = token
        self.last_id = int(since)
        self.read_timeout = read_timeout
        self.q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.stop_ev = threading.Event()
        self._resp = None
        self._lock = threading.Lock()

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_ev.is_set():
            got = False
            try:
                req = urllib.request.Request(f"{self.url}?since={self.last_id}", headers={
                    "Authorization": f"Bearer {self.token}", "Accept": "text/event-stream",
                    "Last-Event-ID": str(self.last_id)})
                resp = self.opener.open(req, timeout=self.read_timeout)
                with self._lock:
                    self._resp = resp
                try:
                    for msg in parse_sse(iter(resp.readline, b"")):
                        mid = msg.get("id")
                        if mid and str(mid).isdigit():
                            self.last_id = max(self.last_id, int(mid))
                        got = True
                        self.q.put(("event", msg))
                        if self.stop_ev.is_set():
                            break
                finally:
                    with self._lock:
                        self._resp = None
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.q.put(("eof", None))
            except urllib.error.HTTPError as exc:
                self.q.put(("error", f"HTTP {exc.code}"))
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
                # AttributeError: http.client after the socket was shut down by close()
                if not self.stop_ev.is_set():
                    self.q.put(("error", f"{type(exc).__name__}: {exc}"))
            if got:
                backoff = 1.0
            if self.stop_ev.wait(backoff):
                break
            if not got:
                backoff = min(backoff * 2, 15.0)

    def close(self) -> None:
        self.stop_ev.set()
        with self._lock:
            resp = self._resp
        if resp is not None:
            _wake_reader(resp)


class HubClientEngine(CodeEngine):
    """Code engine that delegates BUILD/FIX to a Kyora Hub on the LAN."""

    name = "hub"
    STATUS_TTL_S = 15.0
    CANCEL_GRACE_S = 60.0

    def __init__(self, hub_cfg, bus, *, status_timeout_s: float = 5.0, poll_s: float = 3.0,
                 read_timeout_s: float = 45.0, request_timeout_s: float = 60.0, upload_timeout_s: float = 900.0):
        self._cfg_source = hub_cfg
        self.bus = bus
        self.status_timeout_s = status_timeout_s
        self.poll_s = poll_s
        self.read_timeout_s = read_timeout_s
        self.request_timeout_s = request_timeout_s
        self.upload_timeout_s = upload_timeout_s
        # The hub lives on the LAN: never route it through an HTTP(S)_PROXY from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._avail_lock = threading.Lock()
        self._avail_cache: tuple[float, tuple[str, str], Availability] | None = None
        self._last_published: tuple | None = None

    # ------------------------------------------------------------------ config
    def bind_config(self, source) -> None:
        """Read hub url/token from ``source`` (HubCfg-like object, Config, or callable) from now on."""
        self._cfg_source = source
        self.invalidate_availability()

    def hub_settings(self) -> tuple[str, str]:
        """Current ``(url, token)``."""
        src = self._cfg_source
        try:
            cfg = src() if callable(src) else src
        except Exception as exc:  # noqa: BLE001
            log.warning("hub client: cannot read hub config: %s", exc)
            return "", ""
        if cfg is not None and not hasattr(cfg, "url") and hasattr(cfg, "hub"):
            cfg = cfg.hub
        if isinstance(cfg, dict):
            url, token = cfg.get("url"), cfg.get("token")
        else:
            url, token = getattr(cfg, "url", ""), getattr(cfg, "token", "")
        return str(url or "").strip().rstrip("/"), str(token or "").strip()

    # ------------------------------------------------------------------ HTTP
    def _http(self, method: str, url: str, token: str, *, data: bytes | None = None,
              headers: dict | None = None, timeout: float = 60.0) -> tuple[int, bytes]:
        """Perform a request; return ``(status, body)`` for any HTTP status. Transport errors raise."""
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": f"kyora/{__version__}"}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                body = b""
            return exc.code, body

    @staticmethod
    def _json(body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            return None

    @staticmethod
    def _http_reason(status: int, body: Any) -> str:
        msg = body.get("error") if isinstance(body, dict) else None
        if status == 401:
            return "hub rejected the token (401): check hub.token"
        if status == 429:
            return "hub locked this client out after repeated bad tokens (429); retry in a minute"
        if status == 404:
            return "hub unreachable: HTTP 404 (is the remote Kyora running with 'kyora hub'?)"
        return f"hub error: HTTP {status}" + (f": {msg}" if msg else "")

    # ------------------------------------------------------------------ availability
    def invalidate_availability(self) -> None:
        with self._avail_lock:
            self._avail_cache = None

    def availability(self) -> Availability:
        """Ask the hub for its engine status (5 s timeout, cached 15 s)."""
        url, token = self.hub_settings()
        if not url:
            return Availability(False, self.name, "hub.url not set", {})
        if not token:
            return Availability(False, self.name, "hub.token not set", {"url": url})
        key = (url, token)
        with self._avail_lock:
            cached = self._avail_cache
            if cached and cached[1] == key and time.monotonic() - cached[0] < self.STATUS_TTL_S:
                return cached[2]
        av = self._probe(url, token)
        with self._avail_lock:
            self._avail_cache = (time.monotonic(), key, av)
        self._publish_status(av, url)
        return av

    def _probe(self, url: str, token: str) -> Availability:
        details: dict[str, Any] = {"url": url}
        try:
            status, body = self._http("GET", f"{url}{API_PREFIX}/status", token, timeout=self.status_timeout_s)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            return Availability(False, self.name, f"hub unreachable: {reason}", details)
        doc = self._json(body)
        if status != 200:
            return Availability(False, self.name, self._http_reason(status, doc), details)
        if not isinstance(doc, dict) or not doc.get("hub"):
            return Availability(False, self.name, "hub unreachable: the response is not a Kyora hub status", details)
        engine = doc.get("engine") if isinstance(doc.get("engine"), dict) else {}
        edetails = engine.get("details") if isinstance(engine.get("details"), dict) else {}
        details.update({
            "remote_version": doc.get("version"), "remote_backend": engine.get("backend"),
            "version": edetails.get("version"), "logged_in": edetails.get("logged_in"),
            "remote_engine": engine, "node": doc.get("node"), "git": doc.get("git"),
            "platform": (doc.get("platform") or {}).get("kind") if isinstance(doc.get("platform"), dict) else None,
            "queue": doc.get("queue"),
        })
        if not engine.get("available"):
            remote_reason = str(engine.get("reason") or "no engine available")
            reason = remote_reason if remote_reason.startswith("hub has no code engine") \
                else f"hub has no code engine: {remote_reason}"
            return Availability(False, self.name, reason, details)
        return Availability(True, self.name, "", details)

    def _publish_status(self, av: Availability, url: str) -> None:
        sig = (av.available, av.reason, url)
        if sig == self._last_published or self.bus is None:
            return
        self._last_published = sig
        try:
            self.bus.publish("hub.status", {"available": av.available, "reason": av.reason, "url": url,
                                            "remote_backend": av.details.get("remote_backend")})
        except Exception as exc:  # noqa: BLE001
            log.debug("hub.status publish failed: %s", exc)

    # ------------------------------------------------------------------ run
    @staticmethod
    def _emit(sink: EngineSink, ev: EngineEvent) -> None:
        try:
            sink.on_event(ev)
        except Exception as exc:  # noqa: BLE001 - a sink bug must not abort the relay
            log.error("hub client: sink failed for %s: %s", ev.type, exc)

    @staticmethod
    def job_payload(job: EngineJob) -> dict:
        """The job document sent to the hub (``env`` is never forwarded)."""
        return {"kind": job.kind, "run_id": job.run_id, "project_id": job.project_id, "prompt": job.prompt,
                "system_prompt": job.system_prompt, "max_turns": job.max_turns, "model": job.model,
                "fallback_models": list(job.fallback_models or []), "effort": job.effort,
                "resume_session_id": job.resume_session_id, "judge_context": dict(job.judge_context or {})}

    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult:
        """Upload the workspace, relay the hub's events, then unpack the hub's workspace locally."""
        started = time.monotonic()
        url, token = self.hub_settings()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        def fail(msg: str, backend: str | None = None, raw: dict | None = None) -> EngineResult:
            self._emit(sink, EngineEvent("engine.error", {"error": msg}))
            return EngineResult(ok=False, backend=backend or self.name, requested_model=job.model, error=msg,
                                duration_ms=elapsed_ms(), raw=raw or {})

        def progress(message: str) -> None:
            self._emit(sink, EngineEvent("stage.progress", {"stage": job.kind, "message": message}))

        if not url:
            return fail("hub.url not set")
        if not token:
            return fail("hub.token not set")
        try:
            timeout_s = float((job.env or {}).get("KYORA_HUB_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        workspace = Path(job.workspace)
        try:
            data = zip_tree(workspace, exclude_any=UPLOAD_EXCLUDE_ANY, exclude_top=UPLOAD_EXCLUDE_TOP)
        except OSError as exc:
            return fail(f"cannot zip the workspace for the hub: {exc}")
        if len(data) > MAX_UPLOAD_BYTES:
            return fail(f"workspace zip is {len(data) // (1024 * 1024)} MB; the hub accepts at most "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        payload = json.dumps(self.job_payload(job), ensure_ascii=False, default=str).encode("utf-8")
        body, ctype = encode_multipart([("job", None, "application/json; charset=utf-8", payload),
                                        ("workspace", "workspace.zip", "application/zip", data)])
        progress(f"hub: uploading workspace ({len(data) / 1048576:.1f} MB) to {url}")
        try:
            status, resp_body = self._http("POST", f"{url}{API_PREFIX}/jobs", token, data=body,
                                           headers={"Content-Type": ctype}, timeout=self.upload_timeout_s)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            self.invalidate_availability()
            return fail(f"hub unreachable: {getattr(exc, 'reason', None) or exc}")
        info = self._json(resp_body)
        if status not in (200, 201) or not isinstance(info, dict) or not info.get("job_id"):
            self.invalidate_availability()
            return fail("hub refused the job: " + self._http_reason(status, info))
        job_id = str(info["job_id"])
        since = int(info.get("events_since") or 0)
        progress(f"hub: job {job_id} accepted")

        relay = _EventRelay(self._opener, url, token, job_id, since, self.read_timeout_s)
        state_box = {"max_id": since}

        def forward(msg: dict) -> None:
            try:
                env = json.loads(msg.get("data") or "")
            except ValueError:
                return
            if not isinstance(env, dict) or not env.get("type"):
                return
            try:
                eid = int(env.get("id") or 0)
            except (TypeError, ValueError):
                eid = 0
            if eid:
                if eid <= state_box["max_id"]:
                    return
                state_box["max_id"] = eid
            data = dict(env.get("data") or {}) if isinstance(env.get("data"), dict) else {}
            owner = data.pop("hub_job_id", None)
            if owner is not None and owner != job_id:
                return  # another hub job of the same run
            if env["type"] == "engine.session" and data.get("backend"):
                data["backend"] = f"hub:{data['backend']}"
            self._emit(sink, EngineEvent(str(env["type"]), data))

        last_doc: dict | None = None
        timed_out = False
        cancel_sent_at: float | None = None
        relay.start()
        try:
            deadline = started + timeout_s
            next_poll = time.monotonic() + self.poll_s
            while True:
                try:
                    kind, item = relay.q.get(timeout=0.25)
                except queue.Empty:
                    kind, item = None, None
                if kind == "event":
                    forward(item)
                elif kind == "error":
                    log.info("hub client: event stream for %s: %s (reconnecting)", job_id, item)
                now = time.monotonic()
                if cancel.is_set() and cancel_sent_at is None:
                    progress(f"hub: cancelling job {job_id}")
                    self._post_cancel(url, token, job_id)
                    cancel_sent_at = now
                if now > deadline and cancel_sent_at is None:
                    timed_out = True
                    self._emit(sink, EngineEvent("engine.error", {
                        "error": f"hub job exceeded KYORA_HUB_TIMEOUT_S={timeout_s:.0f}s; cancelling"}))
                    self._post_cancel(url, token, job_id)
                    cancel_sent_at = now
                if kind in ("eof", "error") or now >= next_poll:
                    next_poll = now + self.poll_s
                    doc = self._get_job(url, token, job_id)
                    if doc is not None:
                        last_doc = doc
                        if doc.get("state") in TERMINAL_STATES:
                            break
                if cancel_sent_at is not None and now - cancel_sent_at > self.CANCEL_GRACE_S:
                    break
                if now > deadline + self.CANCEL_GRACE_S:
                    break
        finally:
            relay.close()
            relay.join(5.0)
        while True:  # events the reader queued before it stopped
            try:
                kind, item = relay.q.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                forward(item)

        state = (last_doc or {}).get("state")
        if state not in TERMINAL_STATES:
            if cancel_sent_at is not None and not timed_out:
                return fail(f"cancelled (the hub did not confirm cancellation of {job_id})")
            reason = "timed out" if timed_out else "lost contact with the hub"
            return fail(f"hub job {job_id} {reason} (last state: {state or 'unknown'})")
        self._drain(url, token, job_id, state_box, forward)

        remote = (last_doc or {}).get("result") if isinstance((last_doc or {}).get("result"), dict) else {}
        hub_raw = {"url": url, "job_id": job_id, "state": state, "remote": remote.get("hub"),
                   "judge": remote.get("judge"), "wall_ms": elapsed_ms()}
        if state == "done":
            try:
                status, zbody = self._http("GET", f"{url}{API_PREFIX}/jobs/{job_id}/result", token,
                                           headers={"Accept": "application/zip"}, timeout=self.upload_timeout_s)
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
                return fail(f"cannot fetch the hub result: {exc}", raw={"hub": hub_raw})
            if status != 200:
                return fail("cannot fetch the hub result: " + self._http_reason(status, self._json(zbody)),
                            raw={"hub": hub_raw})
            try:
                zipped = self._result_from_zip(zbody)
                if zipped:
                    remote = zipped
                unzip_into(zbody, workspace, replace=True, keep=CLIENT_KEEP)
                rm_tree(workspace / RESULT_DIR)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                return fail(f"cannot unpack the hub result into the workspace: {exc}", raw={"hub": hub_raw})
        res = self._rebuild_result(remote, state=state, requested_model=job.model, hub_raw=hub_raw)
        if state == "cancelled":
            res.ok = False
            res.error = (f"hub job exceeded KYORA_HUB_TIMEOUT_S={timeout_s:.0f}s" if timed_out
                         else (res.error or (last_doc or {}).get("error") or "cancelled"))
        elif state == "failed":
            res.ok = False
            res.error = res.error or (last_doc or {}).get("error") or "hub job failed"
            self._emit(sink, EngineEvent("engine.error", {"error": res.error}))
        if not remote:
            res.duration_ms = elapsed_ms()
        return res

    def _get_job(self, url: str, token: str, job_id: str) -> dict | None:
        try:
            status, body = self._http("GET", f"{url}{API_PREFIX}/jobs/{job_id}", token, timeout=self.request_timeout_s)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            log.info("hub client: job poll failed: %s", exc)
            return None
        doc = self._json(body)
        if status == 200 and isinstance(doc, dict):
            return doc
        if status == 404:
            return {"state": "failed", "error": f"hub no longer knows job {job_id}", "result": None}
        log.info("hub client: job poll returned HTTP %s", status)
        return None

    def _post_cancel(self, url: str, token: str, job_id: str) -> None:
        try:
            status, _ = self._http("POST", f"{url}{API_PREFIX}/jobs/{job_id}/cancel", token, data=b"",
                                   timeout=self.request_timeout_s)
            if status != 200:
                log.warning("hub client: cancel of %s returned HTTP %s", job_id, status)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            log.warning("hub client: cancel of %s failed: %s", job_id, exc)

    def _drain(self, url: str, token: str, job_id: str, state_box: dict, forward: Callable[[dict], None]) -> None:
        """Replay persisted events after the last forwarded id (the hub closes the stream for finished jobs)."""
        for _ in range(20):
            before = state_box["max_id"]
            req = urllib.request.Request(f"{url}{API_PREFIX}/jobs/{job_id}/events?since={before}", headers={
                "Authorization": f"Bearer {token}", "Accept": "text/event-stream"})
            try:
                with self._opener.open(req, timeout=15.0) as resp:
                    for msg in parse_sse(iter(resp.readline, b"")):
                        forward(msg)
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
                log.info("hub client: final event replay for %s failed: %s", job_id, exc)
                return
            if state_box["max_id"] == before:
                return

    @staticmethod
    def _result_from_zip(data: bytes) -> dict | None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if RESULT_ENTRY not in zf.namelist():
                    return None
                doc = json.loads(zf.read(RESULT_ENTRY).decode("utf-8"))
        except (zipfile.BadZipFile, ValueError, KeyError) as exc:
            log.warning("hub client: unreadable %s: %s", RESULT_ENTRY, exc)
            return None
        res = doc.get("result") if isinstance(doc, dict) else None
        return res if isinstance(res, dict) else None

    @staticmethod
    def _rebuild_result(doc: dict, *, state: str, requested_model: str, hub_raw: dict) -> EngineResult:
        doc = doc or {}
        remote_backend = str(doc.get("backend") or "unknown")
        num_turns = doc.get("num_turns")
        cost = doc.get("cost_usd")
        return EngineResult(
            ok=bool(doc.get("ok")) and state == "done",
            backend=f"hub:{remote_backend}",
            requested_model=str(doc.get("requested_model") or requested_model or ""),
            active_model=doc.get("active_model"),
            fallback_used=bool(doc.get("fallback_used")),
            session_id=doc.get("session_id"),
            num_turns=int(num_turns) if isinstance(num_turns, (int, float)) else None,
            duration_ms=int(doc.get("duration_ms") or 0),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            result_text=str(doc.get("result_text") or ""),
            error=doc.get("error"),
            permission_denials=list(doc.get("permission_denials") or []),
            raw={"hub": hub_raw},
        )
