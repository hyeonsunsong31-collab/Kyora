#!/usr/bin/env python3
"""Build ``kyora.pyz`` and the release ZIP ``Kyora-AGI-Studio-<version>.zip`` (standard library only).

Usage
-----
  python scripts/build_release.py [--build-dir build] [--dist-dir dist] [--check]

Steps
-----
1. Stage ``<build>/stage``: a copy of the ``kyora`` package (no ``__pycache__``/``*.pyc``,
   no stale ``kyora/defaults``), plus ``config/keys.json`` and ``config/settings.json`` copied into
   ``stage/kyora/defaults/`` so the single-file app carries the committed configuration (the user's
   decision; see README "Settings and keys"), plus a ``stage/__main__.py`` that calls
   ``kyora.__main__.main()``.
2. ``zipapp.create_archive(stage, <build>/kyora.pyz, interpreter='/usr/bin/env python3', compressed=True)``.
   Staged files get a fixed mtime (1980-01-01 00:00 local time) and fixed permission bits so the
   archive is reproducible on a given machine.
3. ``<dist>/Kyora-AGI-Studio-<version>.zip``: ``kyora.pyz`` at the root plus ``config/``, ``kyora/``
   (source), ``tokkio-kyora/``, ``vendor/``, ``launchers/``, ``docs/``, ``scripts/``, ``tests/``
   (without ``tests/.tmp``), ``README.md``, ``LICENSE``, ``CHANGELOG.md`` and ``pyproject.toml``.
   ``.git``, ``__pycache__``, ``node_modules``, ``build/`` and ``dist/`` are excluded. Entries are
   sorted, every timestamp is 1980-01-01 00:00:00, compression is ZIP_DEFLATED. The six vendored
   Tokkio symlinks are stored as symlink entries exactly like the NVIDIA source archive stores them
   (``external_attr = 0o120777 << 16``, link text as the stored payload). Shell scripts get mode 0o755;
   other files keep the git index mode (0o755 or 0o644).

``--check`` then verifies the result for real (exit status 1 on any failure):

* ``<python> <build>/kyora.pyz version`` prints ``kyora <version>``;
* ``<python> <build>/kyora.pyz serve --port <free> --home <temp>`` started with ``cwd=<temp>`` and
  ``KYORA_HOME=<temp>`` (so no ``./config`` is found and the packaged defaults are seeded) answers
  ``GET /api/status`` with 200 within the timeout, its config directory is the seeded one and its
  NVIDIA keys equal ``config/keys.json`` (compared in memory, never printed); ``GET /`` returns
  ``index.html`` (a 404 is reported and tolerated only when ``kyora/web/index.html`` does not exist);
* inside the ZIP: ``kyora.pyz`` equals the built file, ``config/keys.json`` equals the repository file,
  the expected top-level entries exist, the ``vendor/tokkio-5.0.0-ga`` entry count equals the
  ``vendor/VENDOR.md`` manifest and every entry's sha256 matches it, and the symlink entries are
  preserved with mode 0o120000.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipapp
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "kyora"
RELEASE_NAME = "Kyora-AGI-Studio"
PYZ_NAME = "kyora.pyz"
INTERPRETER = "/usr/bin/env python3"
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
INCLUDE_DIRS: tuple[str, ...] = ("config", "kyora", "tokkio-kyora", "vendor", "launchers", "docs", "scripts", "tests")
INCLUDE_FILES: tuple[str, ...] = ("README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml")
DEFAULT_CONFIG_FILES: tuple[str, ...] = ("keys.json", "settings.json")
EXCLUDE_DIR_NAMES = frozenset({".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                               ".kyora", ".venv", "venv", ".idea", ".vscode"})
EXCLUDE_DIR_SUFFIXES: tuple[str, ...] = (".egg-info",)
EXCLUDE_REL_DIRS = frozenset({"tests/.tmp", "kyora/defaults", "build", "dist"})
EXCLUDE_FILE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")
EXCLUDE_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
SHELL_SUFFIXES: tuple[str, ...] = (".sh",)
VENDOR_REL = "vendor/tokkio-5.0.0-ga"
MODE_FILE = 0o100644
MODE_EXEC = 0o100755
MODE_LINK = 0o120777
S_IFLNK = 0o120000


@dataclass(frozen=True)
class Entry:
    """One file of the release ZIP."""

    arcname: str
    source: Path
    mode: int  # full st_mode-style value (type bits + permission bits)
    link_text: str | None = None  # set for symlink entries

    @property
    def is_link(self) -> bool:
        return self.link_text is not None


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- helpers
def read_version(repo: Path = REPO) -> str:
    """Return ``kyora.__version__`` without importing the package."""
    text = (repo / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    if not m:
        raise SystemExit(f"__version__ not found in {repo / PACKAGE / '__init__.py'}")
    return m.group(1)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def load_vendor_tool(repo: Path = REPO):
    """Import ``scripts/vendor_tokkio.py`` by path (its manifest parser is reused)."""
    path = repo / "scripts" / "vendor_tokkio.py"
    spec = importlib.util.spec_from_file_location("kyora_vendor_tokkio", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_index_modes(repo: Path) -> dict[str, int]:
    """``{posix_path: mode}`` from ``git ls-files -s``; empty when git or the repository is unavailable.

    Used so symlinks and executable bits are reproduced even from a Windows checkout where the
    filesystem lost them (``core.symlinks=false`` writes the link text into a plain file).
    """
    git = shutil.which("git")
    if not git or not (repo / ".git").exists():
        return {}
    try:
        proc = subprocess.run([git, "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"note: git ls-files failed ({exc}); using filesystem modes")
        return {}
    if proc.returncode != 0:
        log(f"note: git ls-files exit {proc.returncode}; using filesystem modes")
        return {}
    modes: dict[str, int] = {}
    for record in proc.stdout.decode("utf-8", "replace").split("\0"):
        if not record or "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        try:
            modes[path] = int(meta.split()[0], 8)
        except (ValueError, IndexError):
            continue
    return modes


def _is_excluded_dir(rel: str, name: str, abs_path: Path, skip_roots: list[Path]) -> bool:
    if name in EXCLUDE_DIR_NAMES or name.endswith(EXCLUDE_DIR_SUFFIXES) or rel in EXCLUDE_REL_DIRS:
        return True
    try:
        resolved = abs_path.resolve()
    except OSError:
        return False
    return any(resolved == r or r in resolved.parents for r in skip_roots)


def _is_excluded_file(name: str) -> bool:
    return name.endswith(EXCLUDE_FILE_SUFFIXES) or name in EXCLUDE_FILE_NAMES


def _file_mode(rel: str, path: Path, index_modes: dict[str, int]) -> int:
    if rel.endswith(SHELL_SUFFIXES):
        return MODE_EXEC
    idx = index_modes.get(rel)
    if idx is not None:
        return MODE_EXEC if idx == 0o100755 else MODE_FILE
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o111:
                return MODE_EXEC
        except OSError:
            pass
    return MODE_FILE


def collect_entries(repo: Path, skip_roots: list[Path]) -> list[Entry]:
    """Every file of the release ZIP except ``kyora.pyz``, sorted by archive name."""
    index_modes = git_index_modes(repo)
    entries: list[Entry] = []
    missing = [n for n in INCLUDE_FILES + INCLUDE_DIRS if not (repo / n).exists()]
    if missing:
        raise SystemExit(f"release inputs missing from {repo}: {', '.join(missing)}")

    def add(path: Path) -> None:
        rel = path.relative_to(repo).as_posix()
        if path.is_symlink():
            entries.append(Entry(rel, path, MODE_LINK, os.readlink(path).replace("\\", "/")))
        elif index_modes.get(rel) == S_IFLNK:  # symlink checked out as a plain file (Windows)
            entries.append(Entry(rel, path, MODE_LINK, path.read_text(encoding="utf-8").strip()))
        else:
            entries.append(Entry(rel, path, _file_mode(rel, path, index_modes)))

    for name in INCLUDE_FILES:
        add(repo / name)
    for top in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(repo / top):
            base = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                p = base / d
                rel = p.relative_to(repo).as_posix()
                if _is_excluded_dir(rel, d, p, skip_roots):
                    continue
                if p.is_symlink():  # a symlinked directory is stored as a link, never followed
                    add(p)
                    continue
                keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                if not _is_excluded_file(f):
                    add(base / f)
    entries.sort(key=lambda e: e.arcname)
    seen: set[str] = set()
    for e in entries:
        if e.arcname in seen:
            raise SystemExit(f"duplicate archive entry: {e.arcname}")
        seen.add(e.arcname)
    return entries


# ----------------------------------------------------------------------------- kyora.pyz
def _fixed_mtime() -> float:
    """Epoch seconds of 1980-01-01 00:00:00 *local* time (ZipFile stores local time)."""
    return time.mktime((1980, 1, 1, 0, 0, 0, 0, 1, -1))


def _ignore_for_stage(src_root: Path):
    def ignore(dirpath: str, names: list[str]) -> list[str]:
        rel_dir = Path(dirpath).resolve().relative_to(src_root.resolve()).as_posix()
        out = []
        for n in names:
            rel = n if rel_dir == "." else f"{rel_dir}/{n}"
            if n in EXCLUDE_DIR_NAMES or n.endswith(EXCLUDE_DIR_SUFFIXES) or _is_excluded_file(n):
                out.append(n)
            elif rel == "defaults":  # regenerated below from config/
                out.append(n)
        return out
    return ignore


def stage_package(repo: Path, stage: Path) -> int:
    """Populate ``stage`` for zipapp; returns the number of staged files."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    src = repo / PACKAGE
    shutil.copytree(src, stage / PACKAGE, ignore=_ignore_for_stage(src), symlinks=False)
    defaults = stage / PACKAGE / "defaults"
    defaults.mkdir(parents=True, exist_ok=True)
    for name in DEFAULT_CONFIG_FILES:
        source = repo / "config" / name
        if not source.is_file():
            raise SystemExit(f"missing {source}")
        json.loads(source.read_text(encoding="utf-8"))  # refuse to package invalid JSON
        (defaults / name).write_bytes(source.read_bytes())
    (stage / "__main__.py").write_text(
        '"""kyora.pyz entry point (generated by scripts/build_release.py)."""\n'
        "import sys\n\n"
        "from kyora.__main__ import main\n\n"
        "sys.exit(main())\n",
        encoding="utf-8", newline="\n")
    mtime = _fixed_mtime()
    count = 0
    for dirpath, dirnames, filenames in os.walk(stage):
        for f in filenames:
            p = Path(dirpath) / f
            os.chmod(p, 0o644)
            os.utime(p, (mtime, mtime))
            count += 1
        for d in dirnames:
            os.chmod(Path(dirpath) / d, 0o755)
    for dirpath, dirnames, _ in os.walk(stage, topdown=False):
        for d in dirnames:
            os.utime(Path(dirpath) / d, (mtime, mtime))
    return count


def build_pyz(repo: Path, build_dir: Path) -> Path:
    stage = build_dir / "stage"
    n = stage_package(repo, stage)
    target = build_dir / PYZ_NAME
    if target.exists():
        target.unlink()
    zipapp.create_archive(stage, target=target, interpreter=INTERPRETER, compressed=True)
    if os.name != "nt":
        os.chmod(target, 0o755)
    log(f"built {target} ({human_size(target.stat().st_size)}, {n} staged files, interpreter '{INTERPRETER}')")
    return target


# ----------------------------------------------------------------------------- release zip
def _zipinfo(arcname: str, mode: int, compress: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE_TIME)
    info.create_system = 3  # Unix: the high 16 bits of external_attr are st_mode
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = compress
    return info


def build_zip(repo: Path, pyz: Path, dist_dir: Path, version: str, skip_roots: list[Path]) -> tuple[Path, int, int]:
    """Write the release ZIP; returns ``(path, entry_count, symlink_count)``."""
    entries = collect_entries(repo, skip_roots)
    entries.append(Entry(PYZ_NAME, pyz, MODE_EXEC))
    entries.sort(key=lambda e: e.arcname)
    dist_dir.mkdir(parents=True, exist_ok=True)
    target = dist_dir / f"{RELEASE_NAME}-{version}.zip"
    tmp = target.with_suffix(".zip.tmp")
    links = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for e in entries:
            if e.is_link:
                links += 1
                # Same as the NVIDIA source archive: stored (not deflated), mode 0o120777, payload = link text.
                zf.writestr(_zipinfo(e.arcname, MODE_LINK, zipfile.ZIP_STORED), (e.link_text or "").encode("utf-8"))
            else:
                zf.writestr(_zipinfo(e.arcname, e.mode, zipfile.ZIP_DEFLATED), e.source.read_bytes())
    os.replace(tmp, target)
    return target, len(entries), links


# ----------------------------------------------------------------------------- --check
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    try:
        with _NO_PROXY_OPENER.open(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _check_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["KYORA_HOME"] = str(home)
    for var in ("KYORA_CONFIG_DIR", "PYTHONPATH"):
        env.pop(var, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class CheckReport:
    """Collects ``[OK]``/``[FAIL]``/``[SKIP]`` lines for ``--check``."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, msg: str) -> None:
        log(f"[OK]   {msg}")

    def skip(self, msg: str) -> None:
        log(f"[SKIP] {msg}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        log(f"[FAIL] {msg}")

    def expect(self, cond: bool, ok_msg: str, fail_msg: str) -> bool:
        (self.ok if cond else self.fail)(ok_msg if cond else fail_msg)
        return cond


def check_version(report: CheckReport, python: str, pyz: Path, version: str, work: Path, env: dict) -> None:
    try:
        proc = subprocess.run([python, str(pyz), "version"], cwd=str(work), env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        report.fail(f"'{python} {pyz.name} version' could not run: {exc}")
        return
    out = proc.stdout.decode("utf-8", "replace").strip()
    report.expect(proc.returncode == 0 and f"kyora {version}" in out,
                  f"'{pyz.name} version' -> {out!r}",
                  f"'{pyz.name} version' exit {proc.returncode}: {out!r} {proc.stderr.decode('utf-8', 'replace')[-400:]!r}")


def _nested(d: object, *path: str) -> object:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def check_serve(report: CheckReport, python: str, pyz: Path, repo: Path, work: Path, env: dict,
                timeout_s: float) -> None:
    """Start the packaged server from an empty directory and probe it over HTTP."""
    home = Path(env["KYORA_HOME"])
    port = _free_port()
    log_path = work / "serve.log"
    cmd = [python, str(pyz), "serve", "--port", str(port), "--home", str(home)]
    base = f"http://127.0.0.1:{port}"
    with open(log_path, "wb") as log_fh:
        try:
            proc = subprocess.Popen(cmd, cwd=str(work), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            report.fail(f"cannot start {' '.join(cmd)}: {exc}")
            return
        try:
            status_body = None
            deadline = time.monotonic() + timeout_s
            last_err = "no response"
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    last_err = f"server exited with {proc.returncode}"
                    break
                try:
                    code, body = _get(base + "/api/status", timeout=30)
                    if code == 200:
                        status_body = body
                        break
                    last_err = f"HTTP {code}"
                except (urllib.error.URLError, ConnectionError, socket.timeout, OSError) as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(0.25)
            if status_body is None:
                report.fail(f"GET /api/status did not return 200 within {timeout_s:.0f} s ({last_err})")
                return
            report.ok(f"'{pyz.name} serve' answered GET /api/status 200 on port {port}")
            try:
                status = json.loads(status_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                report.fail(f"/api/status is not JSON: {exc}")
                status = {}
            cfg_dir = Path(str(status.get("config_dir") or ""))
            expected_cfg = home / "config"
            report.expect(cfg_dir.resolve() == expected_cfg.resolve(),
                          f"config seeded into KYORA_HOME ({expected_cfg})",
                          f"config_dir is {cfg_dir}, expected the seeded {expected_cfg} (a ./config or "
                          "KYORA_CONFIG_DIR leaked into the check)")
            # The packaged defaults must be what the seeded config contains (keys compared, never printed).
            code, body = _get(base + "/api/settings?reveal=1", timeout=30)
            repo_keys = json.loads((repo / "config" / "keys.json").read_text(encoding="utf-8"))
            if code != 200:
                report.fail(f"GET /api/settings?reveal=1 -> HTTP {code}")
            else:
                served = json.loads(body.decode("utf-8")).get("keys", {})
                same = all(_nested(served, "nvidia", m, "api_key") == _nested(repo_keys, "nvidia", m, "api_key")
                           for m in ("nemotron", "kimi"))
                report.expect(same, "server uses the NVIDIA keys packaged in kyora.pyz (equal to config/keys.json)",
                              "server's NVIDIA keys differ from config/keys.json: the packaged kyora/defaults "
                              "were not used")
            code, body = _get(base + "/", timeout=30)
            has_index = (repo / PACKAGE / "web" / "index.html").is_file()
            if code == 200 and b"<html" in body[:4096].lower():
                report.ok(f"GET / returned index.html ({len(body)} bytes)")
            elif code == 404 and not has_index:
                report.skip("GET / -> 404: kyora/web/index.html does not exist in this tree (tolerated)")
            else:
                report.fail(f"GET / -> HTTP {code} ({len(body)} bytes) although kyora/web/index.html "
                            f"{'exists' if has_index else 'is absent'}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
    if report.failures:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        log("---- server log (tail) ----\n" + "\n".join(tail) + "\n---------------------------")


def check_zip(report: CheckReport, zip_path: Path, pyz: Path, repo: Path) -> None:
    vt = load_vendor_tool(repo)
    manifest = vt.parse_manifest((repo / "vendor" / "VENDOR.md").read_text(encoding="utf-8"))
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        report.expect(names == sorted(names), "entries are sorted", "entries are not in sorted order")
        report.expect(all(i.date_time == ZIP_DATE_TIME for i in infos), "all timestamps are 1980-01-01 00:00:00",
                      "some entries do not carry the fixed 1980-01-01 timestamp")
        tops = {n.split("/", 1)[0] + ("/" if "/" in n else "") for n in names}
        expected = {d + "/" for d in INCLUDE_DIRS} | set(INCLUDE_FILES) | {PYZ_NAME}
        report.expect(expected <= tops, f"top level has {', '.join(sorted(expected))}",
                      f"top level is missing {sorted(expected - tops)}")
        unexpected = sorted(tops - expected)
        report.expect(not unexpected, "no unexpected top-level entries", f"unexpected top-level entries: {unexpected}")
        bad = [n for n in names if "__pycache__/" in n or n.endswith((".pyc", ".pyo")) or n.startswith("tests/.tmp/")
               or "/node_modules/" in n or n.startswith((".git/", "build/", "dist/"))]
        report.expect(not bad, "no caches, .git, build/, dist/ or tests/.tmp", f"excluded paths present: {bad[:5]}")
        report.expect(zf.read(PYZ_NAME) == pyz.read_bytes(), f"{PYZ_NAME} in the ZIP equals {pyz}",
                      f"{PYZ_NAME} in the ZIP differs from {pyz}")
        report.expect(zf.read("config/keys.json") == (repo / "config" / "keys.json").read_bytes(),
                      "config/keys.json in the ZIP is identical to the repository file",
                      "config/keys.json in the ZIP differs from the repository file")
        with zipfile.ZipFile(pyz) as pz:
            pyz_names = set(pz.namelist())
            report.expect("__main__.py" in pyz_names and "kyora/__main__.py" in pyz_names,
                          f"{PYZ_NAME} has __main__.py and the kyora package", f"{PYZ_NAME} lacks its entry point")
            same_keys = "kyora/defaults/keys.json" in pyz_names and \
                pz.read("kyora/defaults/keys.json") == (repo / "config" / "keys.json").read_bytes()
            report.expect(same_keys, f"{PYZ_NAME} carries kyora/defaults/keys.json identical to config/keys.json",
                          f"{PYZ_NAME} kyora/defaults/keys.json missing or different")
            stray = [n for n in pyz_names if "__pycache__" in n or n.endswith(".pyc")]
            report.expect(not stray, f"{PYZ_NAME} has no __pycache__/*.pyc", f"{PYZ_NAME} contains {stray[:3]}")
        prefix = VENDOR_REL + "/"
        vendor = {i.filename[len(prefix):]: i for i in infos if i.filename.startswith(prefix) and not i.is_dir()}
        report.expect(len(vendor) == len(manifest), f"{len(vendor)} vendor entries = {len(manifest)} in VENDOR.md",
                      f"{len(vendor)} vendor entries in the ZIP but {len(manifest)} in VENDOR.md")
        mismatched = [p for p, h in manifest.items() if p not in vendor or sha256_bytes(zf.read(vendor[p])) != h]
        report.expect(not mismatched, "every vendored entry matches its VENDOR.md sha256",
                      f"{len(mismatched)} vendored entries missing or with a different sha256: {mismatched[:5]}")
        links = [i for i in infos if ((i.external_attr >> 16) & 0o170000) == S_IFLNK]
        src_links = sorted(e.arcname for e in collect_entries(repo, []) if e.is_link)
        report.expect(sorted(i.filename for i in links) == src_links and len(links) > 0,
                      f"{len(links)} symlink entries preserved with mode 0o120000",
                      f"symlink entries {sorted(i.filename for i in links)} != source symlinks {src_links}")
        report.expect(all(i.create_system == 3 and i.compress_type == zipfile.ZIP_STORED for i in links),
                      "symlink entries are stored Unix entries like the source archive",
                      "symlink entries are not stored Unix entries")
        shells = [i for i in infos if i.filename.endswith(".sh")]
        report.expect(all(((i.external_attr >> 16) & 0o777) == 0o755 for i in shells),
                      f"{len(shells)} shell scripts have mode 0o755", "some shell scripts are not 0o755")


def run_checks(python: str, pyz: Path, zip_path: Path, repo: Path, version: str, timeout_s: float) -> int:
    report = CheckReport()
    log("Checking the release...")
    with tempfile.TemporaryDirectory(prefix="kyora-release-check-") as tmp:
        work = Path(tmp)
        env = _check_env(work / "home")
        check_version(report, python, pyz, version, work, env)
        check_serve(report, python, pyz, repo, work, env, timeout_s)
    check_zip(report, zip_path, pyz, repo)
    if report.failures:
        log(f"CHECK FAILED: {len(report.failures)} problem(s)")
        return 1
    log("CHECK OK")
    return 0


# ----------------------------------------------------------------------------- main
def _resolve(arg: str | None, default: str) -> Path:
    return (Path(arg).expanduser() if arg else REPO / default).resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build kyora.pyz and the Kyora release ZIP (standard library only).")
    ap.add_argument("--build-dir", help="where kyora.pyz and the staging tree go (default <repo>/build)")
    ap.add_argument("--dist-dir", help="where the release ZIP goes (default <repo>/dist)")
    ap.add_argument("--check", action="store_true", help="run the built kyora.pyz and verify the ZIP; exit 1 on failure")
    ap.add_argument("--python", default=sys.executable, help="interpreter used by --check (default: this one)")
    ap.add_argument("--check-timeout", type=float, default=15.0, help="seconds to wait for /api/status (default 15)")
    args = ap.parse_args(argv)

    repo = REPO
    build_dir = _resolve(args.build_dir, "build")
    dist_dir = _resolve(args.dist_dir, "dist")
    version = read_version(repo)
    log(f"Kyora {version} release build from {repo}")
    build_dir.mkdir(parents=True, exist_ok=True)
    pyz = build_pyz(repo, build_dir)
    zip_path, count, links = build_zip(repo, pyz, dist_dir, version, [build_dir, dist_dir])
    size = zip_path.stat().st_size
    log(f"wrote {zip_path}")
    log(f"  size:  {size} bytes ({human_size(size)})")
    log(f"  files: {count} entries ({links} symlinks)")
    log(f"  sha256: {sha256_bytes(zip_path.read_bytes())}")
    if args.check:
        return run_checks(args.python, pyz, zip_path, repo, version, args.check_timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
