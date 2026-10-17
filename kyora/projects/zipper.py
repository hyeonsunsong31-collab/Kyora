"""Safe ZIP creation and extraction (standard library only).

* :func:`zip_dir` writes a deterministic archive (sorted entries, ``ZIP_DEFLATED``)
  and never follows symlinks, so a link inside a workspace cannot leak files from
  outside it.
* :func:`unzip_safe` validates *every* entry before writing anything and rejects
  zip-slip attempts: absolute paths, drive letters, ``..`` components, symlink
  entries, NTFS stream separators and anything that would resolve outside the
  destination. Entries inside a ``.git`` directory are never written (a planted
  ``.git/hooks/*`` would be code execution the next time git runs).
* :func:`import_game_zip` imports a user's game ZIP into a work tree, stripping a
  single shared top-level folder (``my-game/index.html`` -> ``index.html``).
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Sequence

log = logging.getLogger("kyora.projects.zipper")

DEFAULT_EXCLUDE: tuple[str, ...] = (".git", "node_modules", "cache", "__pycache__")
MAX_TOTAL_DEFAULT = 512 * 1024 * 1024
MAX_FILES_DEFAULT = 20000
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Names NTFS maps onto ".git": its 8.3 short name GIT~<n> (and git's hashed form GI7EBA~<n>) and ".git" with
# trailing dots or spaces, which Win32 strips. Git itself refuses them (core.protectNTFS), but archive entries
# are written by Python, so they are refused on every platform (a ZIP made anywhere may be imported on Windows).
_DOTGIT_ALIAS_RE = re.compile(r"^(?:\.git[. ]+|git~\d+|gi7eba~\d+)$", re.IGNORECASE)
# Junk that archivers add and that must not decide the "single top-level folder" question.
_JUNK_PREFIXES = ("__MACOSX/",)
_JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
_COPY_CHUNK = 1024 * 1024


class UnsafeZipError(ValueError):
    """Raised when an archive contains an entry that may not be extracted."""


# ----------------------------------------------------------------------------- create
def _iter_files(src: Path, exclude: Iterable[str]) -> list[tuple[str, Path]]:
    """Return ``(posix_relpath, path)`` for regular files under ``src``, sorted, symlinks skipped."""
    excl = set(exclude or ())
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        keep_dirs = []
        for d in dirnames:
            if d in excl:
                continue
            if (base / d).is_symlink():
                log.info("zip_dir: skipping symlinked directory %s", base / d)
                continue
            keep_dirs.append(d)
        dirnames[:] = sorted(keep_dirs)
        for name in filenames:
            if name in excl:
                continue
            p = base / name
            try:
                st = p.lstat()
            except OSError as exc:
                log.warning("zip_dir: cannot stat %s: %s", p, exc)
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            out.append((p.relative_to(src).as_posix(), p))
    out.sort(key=lambda item: item[0])
    return out


def _zipinfo_for(rel: str, path: Path) -> zipfile.ZipInfo:
    st = path.stat()
    # ZIP timestamps cannot predate 1980.
    mtime = max(st.st_mtime, 315532800.0)
    info = zipfile.ZipInfo(rel, date_time=time.localtime(mtime)[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if (st.st_mode & 0o111) else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def zip_dir(src: Path | str, out: Path | str | None = None,
            exclude: Sequence[str] = DEFAULT_EXCLUDE) -> bytes | Path:
    """Zip the regular files under ``src``.

    Entries are sorted by their POSIX relative path and compressed with
    ``ZIP_DEFLATED``; any path component named in ``exclude`` is skipped and
    symlinks are never followed. Returns the archive bytes when ``out`` is None,
    otherwise writes ``out`` atomically and returns it as a :class:`Path`.
    """
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"not a directory: {src}")
    files = _iter_files(src, exclude)

    def _write(fh: BinaryIO) -> None:
        with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel, path in files:
                info = _zipinfo_for(rel, path)
                with open(path, "rb") as rf, zf.open(info, "w") as wf:
                    shutil.copyfileobj(rf, wf, _COPY_CHUNK)

    if out is None:
        buf = io.BytesIO()
        _write(buf)
        return buf.getvalue()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zip-", suffix=".tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            _write(fh)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError as exc:
                log.warning("zip_dir: could not remove temp file %s: %s", tmp, exc)
    return out


# ----------------------------------------------------------------------------- validate
def _open_zip(data_or_path: bytes | bytearray | memoryview | Path | str | BinaryIO) -> zipfile.ZipFile:
    if isinstance(data_or_path, (bytes, bytearray, memoryview)):
        source: object = io.BytesIO(bytes(data_or_path))
    elif isinstance(data_or_path, (str, Path)):
        source = str(data_or_path)
    else:
        source = data_or_path
    try:
        return zipfile.ZipFile(source)  # type: ignore[arg-type]
    except zipfile.BadZipFile as exc:
        raise UnsafeZipError(f"not a valid ZIP archive: {exc}") from exc


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _normalise_name(raw: str) -> tuple[str, bool]:
    """Validate an entry name; return ``(posix_name, is_dir)`` or raise :class:`UnsafeZipError`."""
    if "\x00" in raw:
        raise UnsafeZipError(f"entry name contains NUL: {raw!r}")
    name = raw.replace("\\", "/")
    is_dir = name.endswith("/")
    if name.startswith("/"):
        raise UnsafeZipError(f"absolute path in archive: {raw!r}")
    if _DRIVE_RE.match(name):
        raise UnsafeZipError(f"drive letter in archive path: {raw!r}")
    if ":" in name:
        raise UnsafeZipError(f"':' is not allowed in archive paths (drive/stream syntax): {raw!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeZipError(f"'..' component in archive path: {raw!r}")
    if any(_DOTGIT_ALIAS_RE.match(p) for p in parts):
        raise UnsafeZipError(f"archive path aliases .git on Windows (8.3 name or trailing dot/space): {raw!r}")
    if os.name == "nt":
        for p in parts:
            if p.split(".")[0].upper() in _WIN_RESERVED or p.endswith((" ", ".")):
                raise UnsafeZipError(f"reserved Windows file name in archive: {raw!r}")
    return "/".join(parts), is_dir


def _in_dotgit(rel: Path) -> bool:
    return any(p.lower() == ".git" for p in rel.parts)


def _is_junk(name: str) -> bool:
    return name.startswith(_JUNK_PREFIXES) or PurePosixPath(name).name in _JUNK_NAMES


def _plan(zf: zipfile.ZipFile, dest: Path, *, max_total: int, max_files: int, strip_prefix: str = "",
          drop_junk: bool = False, skip_symlinks: bool = False) -> list[tuple[zipfile.ZipInfo, str, bool]]:
    """Validate all entries; return ``(info, relative_name, is_dir)`` to extract (no I/O yet)."""
    dest_resolved = dest.resolve()
    plan: list[tuple[zipfile.ZipInfo, str, bool]] = []
    total = 0
    files = 0
    for info in zf.infolist():
        if _is_symlink_entry(info):
            if skip_symlinks:
                log.warning("unzip: skipping symlink entry %s", info.filename)
                continue
            raise UnsafeZipError(f"symlink entry in archive: {info.filename!r}")
        name, is_dir = _normalise_name(info.filename)
        if info.is_dir():
            is_dir = True
        if not name:
            continue
        if drop_junk and _is_junk(name + ("/" if is_dir else "")):
            continue
        if strip_prefix:
            if name == strip_prefix.rstrip("/"):
                continue
            if not name.startswith(strip_prefix):
                raise UnsafeZipError(f"entry outside the stripped folder: {info.filename!r}")
            name = name[len(strip_prefix):]
            if not name:
                continue
        if any(p.lower() == ".git" for p in name.split("/")):  # case-insensitive filesystems too
            log.warning("unzip: skipping entry inside a .git directory: %s", info.filename)
            continue
        target = (dest_resolved / name).resolve()
        try:
            rel = target.relative_to(dest_resolved)
        except ValueError:
            raise UnsafeZipError(f"entry resolves outside the destination: {info.filename!r}") from None
        if _in_dotgit(rel):  # resolve() expands a short name such as GIT~1 that already exists
            raise UnsafeZipError(f"entry resolves into a .git directory: {info.filename!r}")
        if target == dest_resolved:
            continue
        if not is_dir:
            files += 1
            total += max(int(info.file_size), 0)
            if files > max_files:
                raise UnsafeZipError(f"archive has more than {max_files} files")
            if total > max_total:
                raise UnsafeZipError(f"archive expands to more than {max_total} bytes")
        plan.append((info, name, is_dir))
    return plan


def _clear_dest(dest: Path, keep: Iterable[str]) -> None:
    keep_set = set(keep or ())
    for child in dest.iterdir():
        if child.name in keep_set:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, onerror=_on_rm_error)
        else:
            child.unlink()


def _on_rm_error(func, path, exc_info):  # pragma: no cover - Windows read-only files
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def _extract(zf: zipfile.ZipFile, dest: Path, plan: list[tuple[zipfile.ZipInfo, str, bool]], *,
             max_total: int) -> list[str]:
    dest_resolved = dest.resolve()
    written: list[str] = []
    budget = max_total
    for info, name, is_dir in plan:
        target = dest_resolved.joinpath(*name.split("/"))
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after directories were created: an existing symlink in dest must not redirect writes.
        try:
            rel = target.parent.resolve().relative_to(dest_resolved)
        except ValueError:
            raise UnsafeZipError(f"entry resolves outside the destination: {info.filename!r}") from None
        if _in_dotgit(rel):
            raise UnsafeZipError(f"entry resolves into a .git directory: {info.filename!r}")
        if target.is_symlink():
            target.unlink()
        with zf.open(info, "r") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                budget -= len(chunk)
                if budget < 0:
                    raise UnsafeZipError(f"archive expands to more than {max_total} bytes")
                dst.write(chunk)
        mode = (info.external_attr >> 16) & 0o777
        if os.name != "nt" and mode & 0o111:
            try:
                os.chmod(target, 0o755)
            except OSError as exc:
                log.debug("chmod failed for %s: %s", target, exc)
        written.append(name)
    return written


# ----------------------------------------------------------------------------- public extract API
def unzip_safe(data_or_path: bytes | Path | str | BinaryIO, dest: Path | str, *, replace: bool = False,
               keep: Sequence[str] = (".git",), max_total: int = MAX_TOTAL_DEFAULT,
               max_files: int = MAX_FILES_DEFAULT, skip_symlinks: bool = False) -> list[str]:
    """Extract an archive into ``dest`` after validating every entry.

    Rejects (raises :class:`UnsafeZipError`, a ``ValueError``) absolute paths,
    drive letters, ``..`` components, symlink entries (``external_attr`` mode
    ``0o120000``), ``:`` in names and anything resolving outside ``dest``; also
    enforces ``max_files`` and ``max_total`` (declared *and* actual bytes).
    Entries inside ``.git`` directories are skipped. With ``replace=True`` the
    existing contents of ``dest`` are deleted first, except top-level names in
    ``keep``. Nothing is written when validation fails. Returns the extracted
    file paths (POSIX, relative to ``dest``) in archive order.

    ``skip_symlinks=True`` drops symlink entries (logged) instead of rejecting
    the archive; Kyora uses it only for archives it produced itself (``git
    archive`` of a slide), never for user uploads.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with _open_zip(data_or_path) as zf:
        plan = _plan(zf, dest, max_total=max_total, max_files=max_files, skip_symlinks=skip_symlinks)
        if replace:
            _clear_dest(dest, keep)
        return _extract(zf, dest, plan, max_total=max_total)


def _common_top_folder(names: list[tuple[str, bool]]) -> str:
    """Return ``"folder/"`` when every file entry lives under one top-level folder, else ``""``."""
    tops: set[str] = set()
    has_file = False
    for name, is_dir in names:
        parts = name.split("/")
        if not is_dir:
            has_file = True
            if len(parts) < 2:
                return ""
        tops.add(parts[0])
        if len(tops) > 1:
            return ""
    return (tops.pop() + "/") if (has_file and len(tops) == 1) else ""


def import_game_zip(zip_path: Path | str | bytes, workspace: Path | str, *,
                    max_total: int = MAX_TOTAL_DEFAULT, max_files: int = MAX_FILES_DEFAULT) -> list[str]:
    """Replace the work tree contents (keeping ``.git``) with a game ZIP.

    ``__MACOSX/`` and ``.DS_Store``-style junk is ignored; when every remaining
    entry shares a single top-level folder it is stripped. Returns the imported
    file paths relative to ``workspace``.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    with _open_zip(zip_path) as zf:
        names: list[tuple[str, bool]] = []
        for info in zf.infolist():
            if _is_symlink_entry(info):
                raise UnsafeZipError(f"symlink entry in archive: {info.filename!r}")
            name, is_dir = _normalise_name(info.filename)
            if not name or _is_junk(name + ("/" if (is_dir or info.is_dir()) else "")):
                continue
            names.append((name, is_dir or info.is_dir()))
        if not any(not d for _, d in names):
            raise UnsafeZipError("archive contains no files")
        prefix = _common_top_folder(names)
        plan = _plan(zf, workspace, max_total=max_total, max_files=max_files, strip_prefix=prefix, drop_junk=True)
        if not any(not d for _, _, d in plan):
            raise UnsafeZipError("archive contains no importable files")
        _clear_dest(workspace, (".git",))
        return _extract(zf, workspace, plan, max_total=max_total)


def list_zip(path: Path | str | bytes, limit: int = 400) -> list[str]:
    """Return up to ``limit`` file names (no directories) from an archive, in archive order."""
    out: list[str] = []
    with _open_zip(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            out.append(info.filename.replace("\\", "/"))
            if len(out) >= limit:
                break
    return out
