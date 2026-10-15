"""Release build: ``scripts/build_release.py`` with temporary --build-dir/--dist-dir and --check.

Run:  python3 -m unittest tests.test_release_build -v      (about 10-30 s)

The real repository is built once (``--check`` starts the packaged server, so this also proves that
``kyora.pyz`` runs from an empty directory with its packaged defaults). Reproducibility and the
exclusion rules are tested on a small synthetic repository so concurrent edits cannot make them flaky.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build_release.py"
S_IFLNK = 0o120000


def _load_build_release():
    spec = importlib.util.spec_from_file_location("kyora_build_release", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


BR = _load_build_release()
VERSION = BR.read_version(REPO)


def _mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _source_symlinks() -> dict[str, str]:
    """``{repo-relative path: link text}`` of the symlinks under vendor/ (from the filesystem or git)."""
    links = {}
    for dirpath, dirnames, filenames in os.walk(REPO / "vendor"):
        for name in filenames + dirnames:
            p = Path(dirpath) / name
            if p.is_symlink():
                links[p.relative_to(REPO).as_posix()] = os.readlink(p).replace("\\", "/")
    if not links:  # e.g. a Windows checkout without symlink support
        for rel, mode in BR.git_index_modes(REPO).items():
            if mode == S_IFLNK and rel.startswith("vendor/"):
                links[rel] = (REPO / rel).read_text(encoding="utf-8").strip()
    return links


class ReleaseBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="kyora-release-test-"))
        cls.build = cls.tmp / "build"
        cls.dist = cls.tmp / "dist"
        env = dict(os.environ, KYORA_HOME=str(cls.tmp / "home"), PYTHONIOENCODING="utf-8")
        env.pop("KYORA_CONFIG_DIR", None)
        cls.proc = subprocess.run([sys.executable, str(SCRIPT), "--build-dir", str(cls.build), "--dist-dir",
                                   str(cls.dist), "--check"], cwd=str(cls.tmp), env=env, capture_output=True,
                                  timeout=600)
        cls.out = cls.proc.stdout.decode("utf-8", "replace") + cls.proc.stderr.decode("utf-8", "replace")
        cls.pyz = cls.build / "kyora.pyz"
        cls.zip_path = cls.dist / f"Kyora-AGI-Studio-{VERSION}.zip"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_check_passes(self):
        self.assertEqual(self.proc.returncode, 0, self.out)
        self.assertIn("CHECK OK", self.out)
        self.assertNotIn("[FAIL]", self.out)
        self.assertIn("server uses the NVIDIA keys packaged in kyora.pyz", self.out)
        self.assertIn(str(self.zip_path), self.out)

    def test_pyz_version_runs_from_empty_dir(self):
        with tempfile.TemporaryDirectory() as work:
            env = dict(os.environ, KYORA_HOME=str(Path(work) / "home"))
            r = subprocess.run([sys.executable, str(self.pyz), "version"], cwd=work, env=env,
                               capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(r.stdout.decode().strip(), f"kyora {VERSION}")
        self.assertTrue(self.pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n"))

    def test_pyz_contents(self):
        with zipfile.ZipFile(self.pyz) as z:
            names = set(z.namelist())
            self.assertIn("__main__.py", names)
            self.assertIn("kyora/__main__.py", names)
            self.assertIn("kyora/web/index.html", names)
            self.assertEqual(z.read("kyora/defaults/keys.json"), (REPO / "config" / "keys.json").read_bytes())
            self.assertEqual(z.read("kyora/defaults/settings.json"), (REPO / "config" / "settings.json").read_bytes())
            self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])

    def test_zip_layout_and_keys(self):
        with zipfile.ZipFile(self.zip_path) as z:
            names = z.namelist()
            self.assertEqual(names, sorted(names))
            tops = {n.split("/", 1)[0] for n in names}
            for d in ("config", "kyora", "tokkio-kyora", "vendor", "launchers", "docs", "scripts", "tests"):
                self.assertIn(d, tops)
            for f in ("kyora.pyz", "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml"):
                self.assertIn(f, names)
            self.assertNotIn(".git", tops)
            self.assertNotIn("build", tops)
            self.assertNotIn("dist", tops)
            self.assertFalse([n for n in names if "__pycache__/" in n or n.endswith(".pyc")
                              or n.startswith("tests/.tmp/") or "/node_modules/" in n])
            self.assertEqual(z.read("config/keys.json"), (REPO / "config" / "keys.json").read_bytes())
            self.assertEqual(z.read("kyora.pyz"), self.pyz.read_bytes())
            self.assertIn("launchers/kyora.ps1", names)
            self.assertIn("scripts/build_release.py", names)
            for info in z.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0), info.filename)
                if info.filename.endswith(".sh"):
                    self.assertEqual(_mode(info), 0o100755, info.filename)

    def test_vendored_symlinks_preserved(self):
        expected = _source_symlinks()
        self.assertEqual(len(expected), 6, "the vendored Tokkio tree has six symlinks")
        with zipfile.ZipFile(self.zip_path) as z:
            links = {i.filename: i for i in z.infolist() if (_mode(i) & 0o170000) == S_IFLNK}
            self.assertEqual(set(links), set(expected))
            for name, info in links.items():
                self.assertEqual(_mode(info), 0o120777)
                self.assertEqual(info.create_system, 3)
                self.assertEqual(z.read(info).decode("utf-8"), expected[name])

    def test_file_count_and_vendor_manifest(self):
        m = re.search(r"files: (\d+) entries \((\d+) symlinks\)", self.out)
        self.assertIsNotNone(m, self.out)
        vt = BR.load_vendor_tool(REPO)
        manifest = vt.parse_manifest((REPO / "vendor" / "VENDOR.md").read_text(encoding="utf-8"))
        with zipfile.ZipFile(self.zip_path) as z:
            names = z.namelist()
            self.assertEqual(int(m.group(1)), len(names))
            self.assertEqual(int(m.group(2)), 6)
            vendor = [n for n in names if n.startswith("vendor/tokkio-5.0.0-ga/")]
            self.assertEqual(len(vendor), len(manifest))
            self.assertIn("vendor/VENDOR.md", names)
            sample = "vendor/tokkio-5.0.0-ga/src/llm-rag/src/bot.py"
            self.assertEqual(hashlib.sha256(z.read(sample)).hexdigest(), manifest["src/llm-rag/src/bot.py"])


def _make_mini_repo(root: Path) -> None:
    for name in ("README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    files = {
        "config/keys.json": json.dumps({"nvidia": {"base_url": "x"}}),
        "config/settings.json": json.dumps({"server": {"port": 1}}),
        "kyora/__init__.py": '__version__ = "9.9.9"\n',
        "kyora/__main__.py": "def main(argv=None):\n    print('mini')\n    return 0\n",
        "kyora/web/index.html": "<html></html>\n",
        "kyora/__pycache__/x.cpython-311.pyc": "junk",
        "kyora/stray.pyc": "junk",
        "kyora/defaults/keys.json": "{\"stale\": true}",
        "tokkio-kyora/README.md": "overlay\n",
        "vendor/tree/real.txt": "real\n",
        "launchers/run.sh": "#!/bin/sh\necho hi\n",
        "docs/a.md": "a\n",
        "scripts/s.py": "print(1)\n",
        "tests/test_x.py": "pass\n",
        "tests/.tmp/scratch.txt": "scratch\n",
        "tests/node_modules/pkg/index.js": "junk\n",
        "build/old.txt": "old\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@unittest.skipIf(os.name == "nt", "creating symlinks needs privileges on Windows")
class MiniRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-mini-release-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _make_mini_repo(self.repo)
        os.symlink("real.txt", self.repo / "vendor" / "tree" / "link.txt")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, tag: str) -> tuple[bytes, bytes]:
        build_dir = self.tmp / f"build-{tag}"
        dist_dir = self.tmp / f"dist-{tag}"
        build_dir.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            pyz = BR.build_pyz(self.repo, build_dir)
            zip_path, _, _ = BR.build_zip(self.repo, pyz, dist_dir, "9.9.9", [build_dir, dist_dir])
        return pyz.read_bytes(), zip_path.read_bytes()

    def test_reproducible(self):
        pyz1, zip1 = self.build("a")
        os.utime(self.repo / "docs" / "a.md", (1_700_000_000, 1_700_000_000))  # source mtimes must not matter
        pyz2, zip2 = self.build("b")
        self.assertEqual(hashlib.sha256(pyz1).hexdigest(), hashlib.sha256(pyz2).hexdigest())
        self.assertEqual(hashlib.sha256(zip1).hexdigest(), hashlib.sha256(zip2).hexdigest())

    def test_exclusions_modes_and_links(self):
        self.build("c")
        with zipfile.ZipFile(self.tmp / "dist-c" / "Kyora-AGI-Studio-9.9.9.zip") as z:
            names = z.namelist()
            infos = {i.filename: i for i in z.infolist()}
        for bad in ("kyora/__pycache__/x.cpython-311.pyc", "kyora/stray.pyc", "kyora/defaults/keys.json",
                    "tests/.tmp/scratch.txt", "tests/node_modules/pkg/index.js", "build/old.txt"):
            self.assertNotIn(bad, names)
        for good in ("kyora.pyz", "kyora/__main__.py", "tests/test_x.py", "vendor/tree/real.txt", "launchers/run.sh"):
            self.assertIn(good, names)
        self.assertEqual(_mode(infos["launchers/run.sh"]), 0o100755)
        self.assertEqual(_mode(infos["kyora.pyz"]), 0o100755)
        self.assertEqual(_mode(infos["docs/a.md"]), 0o100644)
        link = infos["vendor/tree/link.txt"]
        self.assertEqual(_mode(link), 0o120777)
        self.assertEqual(link.compress_type, zipfile.ZIP_STORED)
        with zipfile.ZipFile(self.tmp / "build-c" / "kyora.pyz") as pz:
            self.assertEqual(json.loads(pz.read("kyora/defaults/keys.json")), {"nvidia": {"base_url": "x"}})
            self.assertNotIn("kyora/stray.pyc", pz.namelist())

    @unittest.skipIf(shutil.which("git") is None, "git not installed")
    def test_symlink_checked_out_as_plain_file(self):
        """A Windows checkout (core.symlinks=false) writes the link text into a file; git's index still says 120000."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        for cmd in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "mini"]):
            subprocess.run(["git", "-C", str(self.repo)] + cmd, check=True, capture_output=True, env=env, timeout=60)
        link = self.repo / "vendor" / "tree" / "link.txt"
        link.unlink()
        link.write_text("real.txt", encoding="utf-8")
        entries = {e.arcname: e for e in BR.collect_entries(self.repo, [])}
        self.assertTrue(entries["vendor/tree/link.txt"].is_link)
        self.assertEqual(entries["vendor/tree/link.txt"].link_text, "real.txt")


if __name__ == "__main__":
    unittest.main()
