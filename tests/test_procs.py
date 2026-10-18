"""kyora.util.procs: the Termux shebang fallback (Android has no /bin/sh or /usr/bin/env)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from kyora.util import procs


@unittest.skipIf(os.name == "nt", "shebangs are a POSIX concept")
class TermuxShebangTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-procs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = self.tmp / "data" / "data" / "com.termux" / "files" / "usr"
        (self.prefix / "bin").mkdir(parents=True)
        for name in ("sh", "env"):
            (self.prefix / "bin" / name).write_text("", encoding="utf-8")
        self.bin = self.tmp / "npm-bin"
        self.bin.mkdir()

    def script(self, name: str, shebang: str) -> Path:
        p = self.bin / name
        p.write_text(f"#!{shebang}\necho hi\n", encoding="utf-8")
        p.chmod(0o755)
        return p

    def env(self) -> dict:
        return {"PREFIX": str(self.prefix), "PATH": str(self.bin)}

    def test_env_node_shim_is_run_through_prefix_env(self):
        # npm's claude shim says "#!/usr/bin/env node"; a name that is missing on this host stands in for
        # /usr/bin/env, which is missing on Android
        (self.prefix / "bin" / "kyora-missing-env").write_text("", encoding="utf-8")
        claude = self.script("claude", "/usr/bin/kyora-missing-env node")
        argv = procs.termux_shebang_argv(["claude", "-p", "--model", "m"], self.env())
        self.assertEqual(argv, [str(self.prefix / "bin" / "kyora-missing-env"), "node", str(claude),
                                "-p", "--model", "m"])
        if not os.path.exists("/usr/bin/env"):  # a real Android/Termux host
            env_shim = self.script("claude2", "/usr/bin/env node")
            self.assertEqual(procs.termux_shebang_argv(["claude2"], self.env())[:3],
                             [str(self.prefix / "bin" / "env"), "node", str(env_shim)])

    def test_popen_falls_back_when_the_interpreter_is_missing(self):
        # Python stands in for the interpreter: busybox sh (Alpine/iSH) picks its applet from the name it
        # was started under, so a renamed symlink to it would not be a shell
        prefix_py = self.prefix / "bin" / "kyora-missing-python"
        os.symlink(sys.executable, prefix_py)
        s = self.bin / "hello"
        s.write_text("#!/bin/kyora-missing-python\nimport sys\nprint('hello', sys.argv[1])\n", encoding="utf-8")
        s.chmod(0o755)
        rc, out, _err = procs.run([str(s), "termux"], env={**os.environ, "PREFIX": str(self.prefix)}, timeout=30)
        self.assertEqual((rc, out.strip()), (0, "hello termux"))

    def test_bin_sh_script_by_path(self):
        cmd = self.script("claude.cmd", "/bin/sh")
        if os.path.exists("/bin/sh"):  # outside Termux the interpreter exists: nothing to rewrite
            self.assertIsNone(procs.termux_shebang_argv([str(cmd)], self.env()))
        else:
            self.assertEqual(procs.termux_shebang_argv([str(cmd), "x"], self.env()),
                             [str(self.prefix / "bin" / "sh"), str(cmd), "x"])

    def test_missing_interpreter_is_mapped_into_prefix(self):
        # /bin/kyora-missing-sh does not exist anywhere; Termux would ship it as $PREFIX/bin/kyora-missing-sh
        (self.prefix / "bin" / "kyora-missing-sh").write_text("", encoding="utf-8")
        s = self.script("tool", "/bin/kyora-missing-sh -e")
        self.assertEqual(procs.termux_shebang_argv(["tool", "a"], self.env()),
                         [str(self.prefix / "bin" / "kyora-missing-sh"), "-e", str(s), "a"])

    def test_not_termux_or_not_a_script_is_left_alone(self):
        self.script("tool", "/bin/kyora-missing-sh")
        self.assertIsNone(procs.termux_shebang_argv(["tool"], {"PREFIX": "/usr", "PATH": str(self.bin)})
                          if "com.termux" not in sys.prefix else None)
        binary = self.bin / "native"
        binary.write_bytes(b"\x7fELF....")
        binary.chmod(0o755)
        self.assertIsNone(procs.termux_shebang_argv(["native"], self.env()))
        self.assertIsNone(procs.termux_shebang_argv(["no-such-command"], self.env()))
        self.script("odd", "/opt/elsewhere/python")  # only /bin and /usr/bin are Termux's missing dirs
        self.assertIsNone(procs.termux_shebang_argv(["odd"], self.env()))


if __name__ == "__main__":
    unittest.main()
