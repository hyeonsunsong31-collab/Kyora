"""Filesystem helpers and platform detection (standard library only)."""
from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


def kyora_home() -> Path:
    """Runtime data directory: ``$KYORA_HOME`` or ``~/.kyora``."""
    env = os.environ.get("KYORA_HOME")
    base = Path(env).expanduser() if env else Path.home() / ".kyora"
    base.mkdir(parents=True, exist_ok=True)
    return base


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def safe_join(root: Path, *parts: str) -> Path:
    """Join ``parts`` under ``root`` and refuse any path that escapes ``root``."""
    root = Path(root).resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PermissionError(f"path escapes root: {'/'.join(parts)}")
    return candidate


def _on_rm_error(func, path, exc_info):  # pragma: no cover - windows read-only files
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def rm_tree(path: Path) -> None:
    if Path(path).exists():
        shutil.rmtree(path, onerror=_on_rm_error)


def which(cmd: str) -> str | None:
    """``shutil.which`` that also resolves Windows ``.cmd``/``.exe`` launchers."""
    found = shutil.which(cmd)
    if found:
        return found
    if os.name == "nt":
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            found = shutil.which(cmd + ext)
            if found:
                return found
    return None


def platform_info() -> dict:
    """Describe the host: windows / termux / ish / linux / macos."""
    sysname = platform.system().lower()
    is_windows = os.name == "nt"
    is_termux = bool(os.environ.get("TERMUX_VERSION")) or os.path.isdir("/data/data/com.termux")
    is_ish = False
    try:
        if sysname == "linux" and os.path.exists("/proc/ish"):
            is_ish = True
        elif sysname == "linux" and platform.machine() in ("i686", "i386") and os.path.exists("/etc/alpine-release"):
            # iSH emulates 32-bit x86 Alpine; this is the most common signature
            is_ish = True
    except Exception:
        pass
    kind = "windows" if is_windows else "termux" if is_termux else "ish" if is_ish else "macos" if sysname == "darwin" else "linux"
    return {
        "kind": kind,
        "system": sysname,
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "is_windows": is_windows,
        "is_termux": is_termux,
        "is_ish": is_ish,
        "frozen_zipapp": Path(sys.argv[0]).suffix == ".pyz" if sys.argv else False,
    }


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
