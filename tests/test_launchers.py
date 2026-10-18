"""Launcher checks: POSIX shell syntax, PowerShell lint, content requirements and end-to-end argument
passing with a stub ``kyora.pyz``.

Run:  python3 -m unittest tests.test_launchers -v

Optional tools are used when present on PATH and skipped otherwise: ``busybox`` (``busybox ash -n``),
``dash``, ``shellcheck`` and ``pwsh`` (PowerShell parser + running ``kyora.ps1``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHERS = REPO / "launchers"
SH_FILES = sorted(LAUNCHERS.glob("*.sh"))
PS1_FILES = [LAUNCHERS / "install.ps1", LAUNCHERS / "kyora.ps1"]
EXPECTED_SH = {"install-termux.sh", "kyora-termux.sh", "install-ish.sh", "kyora-ish.sh"}
VERSION = "0.1.0"


def _load_lint():
    spec = importlib.util.spec_from_file_location("kyora_ps1_lint", REPO / "scripts" / "ps1_lint.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


LINT = _load_lint()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", 120)
    return subprocess.run(cmd, **kw)


def _make_stub_pyz(root: Path) -> None:
    """``root/kyora.pyz`` printing its argv as JSON; exits 3 when ``--exit3`` is passed."""
    src = root / "_stub_src"
    src.mkdir()
    (src / "__main__.py").write_text(
        "import json, sys\n"
        "print('STUB-ARGV ' + json.dumps(sys.argv[1:]))\n"
        "sys.exit(3 if '--exit3' in sys.argv else 0)\n", encoding="utf-8")
    zipapp.create_archive(src, root / "kyora.pyz")
    shutil.rmtree(src)


def _stub_argv(output: str) -> list:
    for line in output.splitlines():
        if line.startswith("STUB-ARGV "):
            return json.loads(line[len("STUB-ARGV "):])
    raise AssertionError(f"stub did not run; output was:\n{output}")


class ShellSyntaxTests(unittest.TestCase):
    def test_expected_scripts_exist(self):
        self.assertEqual({p.name for p in SH_FILES}, EXPECTED_SH)

    @unittest.skipIf(shutil.which("sh") is None, "no POSIX sh on this platform")
    def test_sh_n(self):
        for f in SH_FILES:
            r = _run(["sh", "-n", str(f)])
            self.assertEqual(r.returncode, 0, f"sh -n {f.name}: {r.stderr.decode()}")

    @unittest.skipIf(shutil.which("busybox") is None, "busybox not installed")
    def test_busybox_ash_n(self):
        for f in SH_FILES:
            r = _run(["busybox", "ash", "-n", str(f)])
            self.assertEqual(r.returncode, 0, f"busybox ash -n {f.name}: {r.stderr.decode()}")

    @unittest.skipIf(shutil.which("dash") is None, "dash not installed")
    def test_dash_n(self):
        for f in SH_FILES:
            r = _run(["dash", "-n", str(f)])
            self.assertEqual(r.returncode, 0, f"dash -n {f.name}: {r.stderr.decode()}")

    @unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck not installed")
    def test_shellcheck(self):
        r = _run(["shellcheck", "-s", "sh", "-S", "warning"] + [str(f) for f in SH_FILES])
        self.assertEqual(r.returncode, 0, r.stdout.decode() + r.stderr.decode())

    def test_posix_hygiene(self):
        bashisms = (r"\[\[", r"(?m)^\s*(function|source|declare|local|pushd|popd)\s", r"\$'", r"<<<", r"\$\{!",
                    r"(?m)^\s*\.\s+\S+\.bash")
        for f in SH_FILES:
            raw = f.read_bytes()
            text = raw.decode("utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"), f"{f.name}: shebang must be #!/bin/sh")
            self.assertNotIn(b"\r", raw, f"{f.name}: CRLF line endings break sh")
            self.assertNotIn("\t", text, f"{f.name}: tabs")
            self.assertIn("set -eu", text, f"{f.name}: missing set -eu")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for b in bashisms:
                m = re.search(b, code)
                self.assertIsNone(m, f"{f.name}: bashism {m.group(0)!r}" if m else "")
            text.encode("ascii")  # plain ASCII for busybox / old terminals


class LauncherContentTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (LAUNCHERS / name).read_text(encoding="utf-8")

    def test_install_ps1_requirements(self):
        t = self.read("install.ps1")
        for needle in ("Set-StrictMode -Version Latest", "$ErrorActionPreference = 'Stop'", "Get-Command",
                       "'winget'", "Python.Python.3.12", "Git.Git", "OpenJS.NodeJS.LTS", "[switch]$WithNode",
                       "Anthropic.ClaudeCode", "irm https://claude.ai/install.ps1 | iex",
                       "[Environment]::GetEnvironmentVariable('Path', 'Machine')",
                       "@('auth', 'status', '--json')", "loggedIn", "claude auth login", "ConvertFrom-Json"):
            self.assertIn(needle, t)
        # never prints the auth status document itself (it contains account details)
        leaks = [ln for ln in t.splitlines() if re.search(r"Write-\w+", ln) and re.search(r"\$(status|text)\b", ln)]
        self.assertEqual(leaks, [], "auth status output must not be printed")

    def test_kyora_ps1_requirements(self):
        t = self.read("kyora.ps1")
        for needle in ("Set-StrictMode -Version Latest", "kyora.pyz", "Split-Path -Parent $PSScriptRoot",
                       "'-m', 'kyora'", "$env:KYORA_HOME", "'--open'", "exit $code",
                       "'serve', 'hub', 'selfcheck', 'bench', 'version'"):
            self.assertIn(needle, t)

    def test_termux_requirements(self):
        inst = self.read("install-termux.sh")
        for needle in ("pkg update -y", "pkg install -y python git nodejs-lts", "--with-claude",
                       "npm install -g @anthropic-ai/claude-code", "claude --version", "no official Android build",
                       "Hub mode", "glibc-runner", "unofficial", "proot-distro", "claude_code.command"):
            self.assertIn(needle, inst)
        run = self.read("kyora-termux.sh")
        for needle in ('TMPDIR="$PREFIX/tmp"', "termux-open-url", "kyora.pyz", "-m kyora", "--hub|hub) cmd=hub"):
            self.assertIn(needle, run)

    def test_ish_requirements(self):
        inst = self.read("install-ish.sh")
        for needle in ("apk update", "apk add python3 git", "Claude Code", "foreground", "QR", "Safari"):
            self.assertIn(needle, inst)
        run = self.read("kyora-ish.sh")
        self.assertIn('echo "Open Safari at http://localhost:$port"', run)
        self.assertIn("port=8765", run)
        self.assertIn('exec python3 "$@"', run)

    def test_no_secrets_echoed(self):
        for f in SH_FILES + PS1_FILES:
            t = f.read_text(encoding="utf-8")
            for secret in ("api_key", "NVIDIA_API_KEY", "KYORA_HUB_TOKEN", "nvapi-"):
                self.assertNotIn(secret, t, f"{f.name} mentions {secret}")


class Ps1LintTests(unittest.TestCase):
    def test_launchers_are_clean(self):
        r = _run([sys.executable, str(REPO / "scripts" / "ps1_lint.py")] + [str(p) for p in PS1_FILES])
        self.assertEqual(r.returncode, 0, r.stdout.decode() + r.stderr.decode())
        self.assertEqual(r.stdout.decode().count("OK: "), 2)

    def lint(self, text: str) -> list[str]:
        return [p.message for p in LINT.lint_text(text, "t.ps1")]

    def assertClean(self, text: str):
        self.assertEqual(self.lint(text), [])

    def assertFinds(self, text: str, fragment: str):
        msgs = self.lint(text)
        self.assertTrue(any(fragment in m for m in msgs), f"expected {fragment!r} in {msgs}")

    def test_valid_constructs(self):
        self.assertClean(
            "<# help { ( [ #>\n"
            "[CmdletBinding()]\n"
            "param([switch]$X)\n"
            "# comment with ) and }\n"
            "$p = ${env:ProgramFiles(x86)}\n"
            "$s = \"a $($v.Name) b $(if ($x) { 'y)' } else { \"z}\" }) c `\" d\"\n"
            "$q = 'it''s ( fine'\n"
            "$h = @\"\n"
            "here { ( [ $($v) \"\n"
            "\"@\n"
            "$g = @'\n"
            "raw { ( '\n"
            "'@\n"
            "Write-Host https://example.com/#anchor\n"
            "$a = @(1, 2); $b = @{ k = 'v' }\n"
            "Get-Item . `\n"
            "  -Force\n")

    def test_unbalanced(self):
        self.assertFinds("param()\nif ($x) {\n", "unclosed '{'")
        self.assertFinds("param()\n$a = (1 + 2]]\n", "closes '('")
        self.assertFinds("param()\n}\n", "unmatched closing '}'")

    def test_strings_and_here_strings(self):
        self.assertFinds("param()\n$s = \"open\n", "unterminated double-quoted string")
        self.assertFinds("param()\n$s = 'open\n", "unterminated single-quoted string")
        self.assertFinds("param()\n$h = @\"\nno end\n", "unterminated here-string")
        self.assertFinds("param()\n$h = @\" text\n\"@\n", "must be the last thing on its line")
        self.assertFinds("param()\n\"@\n", "without a matching header")
        self.assertFinds("param()\n$h = @'\nno end\n", "unterminated here-string @'")

    def test_whitespace_and_encoding(self):
        self.assertFinds("param()\n\tWrite-Host 1\n", "tab character")
        self.assertFinds("param()\r\nWrite-Host 1\n", "mixed line endings")
        self.assertFinds("param()\nWrite-Host 'é'\n".encode("utf-8"), "non-ASCII")
        self.assertEqual([p.message for p in LINT.lint_text(b"\xef\xbb\xbfparam()\nWrite-Host '\xc3\xa9'\n", "t")], [])

    def test_invoke_expression_rules(self):
        self.assertFinds("param()\nInvoke-Expression $cmd\n", "Invoke-Expression is not allowed")
        self.assertFinds("param()\n$x | iex\n", "iex is not allowed")
        self.assertClean("param()\n# iex in a comment is fine\n<# Invoke-Expression #>\n")
        documented = "param()\n# Official Claude Code installer\n$c = 'irm https://claude.ai/install.ps1 | iex'\n"
        self.assertClean(documented)
        self.assertFinds("param()\n$c = 'irm https://claude.ai/install.ps1 | iex'\n", "needs a comment")
        self.assertFinds(documented + "# official\n$d = 'irm https://claude.ai/install.ps1 | iex'\n", "more than one")

    def test_param_first(self):
        self.assertFinds("Set-StrictMode -Version Latest\nparam()\n", "param() block must be the first")
        self.assertClean("#requires -Version 5.1\n<# help #>\n[CmdletBinding(PositionalBinding = $false)]\nparam()\n")

    def test_ps7_operators(self):
        self.assertFinds("param()\ngit status && git log\n", "needs PowerShell 7")
        self.assertFinds("param()\n$a = $b ?? 'x'\n", "needs PowerShell 7")
        self.assertClean("param()\n$a = 'x && y || z ?? w'\n")

    @unittest.skipIf(shutil.which("pwsh") is None, "pwsh not installed")
    def test_pwsh_parser(self):
        for f in PS1_FILES:
            cmd = ("$e = $null; $t = $null; "
                   f"[void][System.Management.Automation.Language.Parser]::ParseFile('{f}', [ref]$t, [ref]$e); "
                   "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
            r = _run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd])
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stdout.decode()} {r.stderr.decode()}")


@unittest.skipIf(os.name == "nt" or shutil.which("sh") is None, "POSIX shell launchers")
class ShellLauncherRunTests(unittest.TestCase):
    """Run the real launchers against the source tree and against a stub release layout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-launch-"))
        self.env = dict(os.environ, KYORA_HOME=str(self.tmp / "home"), PREFIX=str(self.tmp / "prefix"))
        self.env.pop("KYORA_CONFIG_DIR", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def release_root(self) -> Path:
        root = self.tmp / "release"
        (root / "launchers").mkdir(parents=True)
        for f in SH_FILES:
            shutil.copy2(f, root / "launchers" / f.name)
        _make_stub_pyz(root)
        return root

    def test_source_tree_version(self):
        for name in ("kyora-ish.sh", "kyora-termux.sh"):
            r = _run(["sh", str(LAUNCHERS / name), "version"], env=self.env, cwd=str(self.tmp))
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertIn(f"kyora {VERSION}", r.stdout.decode())
        self.assertTrue((self.tmp / "prefix" / "tmp").is_dir(), "kyora-termux.sh must create $PREFIX/tmp")

    def test_release_layout_argument_passing(self):
        root = self.release_root()
        r = _run(["sh", str(root / "launchers" / "kyora-termux.sh"), "--hub", "--port", "9123", "--x"],
                 env=self.env, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(_stub_argv(r.stdout.decode()), ["hub", "--port", "9123", "--x"])
        r = _run(["sh", str(root / "launchers" / "kyora-ish.sh")], env=self.env, cwd=str(self.tmp))
        out = r.stdout.decode()
        self.assertIn("Open Safari at http://localhost:8765", out)
        self.assertEqual(_stub_argv(out), ["serve"])
        r = _run(["sh", str(root / "launchers" / "kyora-ish.sh"), "serve", "--port=9001", "--exit3"],
                 env=self.env, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 3, "exit code of Kyora must be propagated")
        self.assertIn("Open Safari at http://localhost:9001", r.stdout.decode())

    def test_unknown_command_and_ish_hub(self):
        r = _run(["sh", str(LAUNCHERS / "kyora-termux.sh"), "bogus"], env=self.env)
        self.assertEqual(r.returncode, 2)
        r = _run(["sh", str(LAUNCHERS / "kyora-ish.sh"), "hub"], env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot be a Hub", r.stderr.decode())

    def test_installers_with_stub_package_managers(self):
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        log = self.tmp / "calls.log"
        sh = shutil.which("sh") or "/bin/sh"  # Termux has no /bin/sh: a stub must name the real shell
        for tool in ("pkg", "apk"):
            (stubs / tool).write_text(f'#!{sh}\necho "{tool} $*" >> "{log}"\n', encoding="utf-8")
        (stubs / "npm").write_text(f'#!{sh}\necho "npm $*" >> "{log}"\nexit 1\n', encoding="utf-8")
        for name in ("python", "python3"):
            (stubs / name).symlink_to(sys.executable)
        for p in stubs.iterdir():
            if not p.is_symlink():  # macOS chmod follows the link to the (read-only) interpreter
                p.chmod(0o755)
        env = dict(self.env, PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")
        r = _run(["sh", str(LAUNCHERS / "install-termux.sh"), "--with-claude"], env=env)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 0, out + r.stderr.decode())
        self.assertIn("no official Android build", out)
        calls = log.read_text(encoding="utf-8")
        self.assertIn("pkg update -y", calls)
        self.assertIn("npm install -g @anthropic-ai/claude-code", calls)
        r = _run(["sh", str(LAUNCHERS / "install-ish.sh")], env=env)
        self.assertEqual(r.returncode, 0, r.stdout.decode() + r.stderr.decode())
        self.assertIn("apk add python3 git", log.read_text(encoding="utf-8"))
        self.assertIn("foreground", r.stdout.decode())


@unittest.skipIf(shutil.which("pwsh") is None, "pwsh not installed")
class PowerShellLauncherRunTests(unittest.TestCase):
    def test_kyora_ps1_release_layout(self):
        tmp = Path(tempfile.mkdtemp(prefix="kyora-ps1-"))
        try:
            root = tmp / "release"
            (root / "launchers").mkdir(parents=True)
            shutil.copy2(LAUNCHERS / "kyora.ps1", root / "launchers" / "kyora.ps1")
            _make_stub_pyz(root)
            env = dict(os.environ, KYORA_HOME=str(tmp / "home"))
            script = str(root / "launchers" / "kyora.ps1")
            r = _run(["pwsh", "-NoProfile", "-NonInteractive", "-File", script, "serve", "--port", "9"], env=env)
            self.assertEqual(r.returncode, 0, r.stdout.decode() + r.stderr.decode())
            self.assertEqual(_stub_argv(r.stdout.decode()), ["serve", "--open", "--port", "9"])
            r = _run(["pwsh", "-NoProfile", "-NonInteractive", "-File", script, "serve", "--no-open"], env=env)
            self.assertEqual(_stub_argv(r.stdout.decode()), ["serve"])
            r = _run(["pwsh", "-NoProfile", "-NonInteractive", "-File", script, "selfcheck", "--exit3"], env=env)
            self.assertEqual(r.returncode, 3)
            r = _run(["pwsh", "-NoProfile", "-NonInteractive", "-File", script, "bogus"], env=env)
            self.assertEqual(r.returncode, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class LineEndingTests(unittest.TestCase):
    """The repository's .gitattributes against a Windows-default checkout (core.autocrlf=true)."""

    SAMPLES = ["launchers/kyora-ish.sh", "launchers/install-termux.sh", "launchers/kyora.ps1", "kyora/__main__.py",
               "kyora/web/js/app.js", "tests/e2e/ui_smoke.mjs",
               "vendor/tokkio-5.0.0-ga/scripts/debug/capture_debug_info.sh",
               "vendor/tokkio-5.0.0-ga/local-dev-workflow/config.py", "vendor/VENDOR.md"]

    def _git(self, cwd: Path, *args: str) -> None:
        r = _run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main",
                  "-c", "core.hooksPath=/dev/null", *args], cwd=str(cwd))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_autocrlf_checkout_keeps_scripts_lf_and_vendor_byte_identical(self):
        tmp = Path(tempfile.mkdtemp(prefix="kyora-eol-"))
        try:
            original = {}
            shutil.copy2(REPO / ".gitattributes", tmp / ".gitattributes")
            for rel in self.SAMPLES:
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                original[rel] = (REPO / rel).read_bytes().replace(b"\r\n", b"\n")  # as stored in the index
                (tmp / rel).write_bytes(original[rel])
            self._git(tmp, "init", "-q")
            self._git(tmp, "-c", "core.autocrlf=false", "add", "-A")
            self._git(tmp, "-c", "core.autocrlf=false", "commit", "-q", "-m", "x")
            for rel in self.SAMPLES:
                (tmp / rel).unlink()
            self._git(tmp, "-c", "core.autocrlf=true", "checkout", "--", ".")
            for rel in self.SAMPLES:
                data = (tmp / rel).read_bytes()
                if rel.endswith(".ps1"):
                    self.assertEqual(data.count(b"\r\n"), data.count(b"\n"), rel)  # one style: CRLF
                    self.assertEqual(LINT.lint_text(data, rel), [], rel)
                    continue
                # sh would stop at "set -eu\r"; the vendored tree must match VENDOR.md byte for byte
                self.assertFalse(b"\r" in data, f"{rel} was checked out with CR bytes")
                self.assertTrue(data == original[rel], f"{rel} differs from the committed bytes")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
