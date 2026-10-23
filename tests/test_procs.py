"""kyora.util.procs: the Termux shebang fallback (Android has no /bin/sh or /usr/bin/env) and ``run``:
background children, bounded output, cancel and timeouts."""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from kyora.util import procs

PY = sys.executable
# generous: a slow runner (iSH) may take 5x longer; a hang would take the full 60/300 s of the children
PROMPT_S = 30


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



# A command that starts a background child and exits at once, like ``sleep 60 & echo started``. The child
# writes its pid, then appends a tick to a file every 0.2 s for 60 s.
_TICKER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\n" \
          "for _ in range(300):\n    open(sys.argv[2], 'a').write('.')\n    time.sleep(0.2)\n"
_PARENT = ("import subprocess, sys, time\n"
           "out = (sys.stdout, sys.stderr) if sys.argv[4] == 'pipes' else (subprocess.DEVNULL, subprocess.DEVNULL)\n"
           "kw = {'stdout': out[0], 'stderr': out[1]}\n"
           "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]], stdin=subprocess.DEVNULL, **kw)\n"
           "while not open(sys.argv[3], 'a+').tell():\n    time.sleep(0.05)\n"  # the child is running
           "print('started', flush=True)\n"
           "time.sleep(float(sys.argv[5]))\n")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "ticker.py").write_text(_TICKER, encoding="utf-8")
        (self.tmp / "parent.py").write_text(_PARENT, encoding="utf-8")
        self.pid_file, self.ticks = self.tmp / "child.pid", self.tmp / "ticks"
        self.addCleanup(self._kill_child)

    def _kill_child(self):
        try:
            os.kill(int(self.pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def background(self, mode: str, parent_sleep: float = 0, **kw):
        argv = [PY, str(self.tmp / "parent.py"), str(self.tmp / "ticker.py"), str(self.pid_file), str(self.ticks),
                mode, str(parent_sleep)]
        t = time.monotonic()
        res = procs.run(argv, cwd=self.tmp, **kw)
        return res, time.monotonic() - t

    def assert_child_stopped(self, tree_killed: bool = False):
        if os.name == "nt":
            if tree_killed:  # taskkill /T ended it: the cleanup must not signal a pid Windows may reuse
                self.pid_file.unlink()
            return  # no process groups: the orphaned child cannot be found once its parent exited
        time.sleep(0.5)
        size = self.ticks.stat().st_size
        time.sleep(3)  # a live child adds a tick every 0.2 s
        self.assertEqual(self.ticks.stat().st_size, size, "the background child is still running")
        self.pid_file.unlink()  # it is gone: the cleanup must not signal a pid the system may reuse

    def test_background_child_holding_the_pipes_does_not_hang(self):
        (rc, out, _err), took = self.background("pipes", timeout=120)
        self.assertEqual((rc, out.strip()), (0, "started"))
        self.assertLess(took, PROMPT_S)
        self.assert_child_stopped()

    def test_detached_background_child_is_stopped(self):
        # like "nohup sleep 40 >/dev/null 2>&1 &": the child holds no pipe but is part of the command
        (rc, out, _err), took = self.background("devnull", timeout=120)
        self.assertEqual((rc, out.strip()), (0, "started"))
        self.assertLess(took, PROMPT_S)
        self.assert_child_stopped()

    def test_timeout_with_a_background_child_returns(self):
        # first: how long the two Pythons take to start here (seconds on a slow device), so the timeout
        # below comes after "started"
        (rc, _out, _err), startup = self.background("pipes", timeout=120)
        self.assertEqual(rc, 0)
        if os.name == "nt":
            self._kill_child()
        else:
            self.assert_child_stopped()
        try:
            self.ticks.unlink()
        except OSError:  # Windows: the child just killed may hold it for a moment
            pass
        timeout = max(2.0, 3 * startup)
        (rc, out, _err), took = self.background("pipes", parent_sleep=300, timeout=timeout)
        self.assertEqual((rc, out.strip()), (-9, "started"))
        self.assertLess(took, timeout + PROMPT_S)
        self.assert_child_stopped(tree_killed=True)

    @unittest.skipUnless(procs._UNREAPED_WAIT, "needs os.waitid with WNOWAIT")
    def test_the_session_is_killed_before_its_leader_is_reaped(self):
        # a reaped leader frees its pid, and so its process-group id, for a new session: the group kill must
        # come while the leader is still a zombie of ours
        seen = []
        real = procs._end_group

        def checking(proc, *args):
            state = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            seen.append(state is not None and state.si_pid == proc.pid)
            return real(proc, *args)
        procs._end_group = checking
        self.addCleanup(setattr, procs, "_end_group", real)
        (rc, out, _err), _took = self.background("pipes", timeout=120)
        self.assertEqual((rc, out.strip()), (0, "started"))
        self.assertEqual(seen, [True])
        self.assert_child_stopped()
        self.assertEqual(procs.run([PY, "-c", "raise SystemExit(3)"]), (3, "", ""))  # the exit code survives

    def test_cancel_kills_the_command(self):
        cancel = threading.Event()
        marker = self.tmp / "printed"

        def cancel_once_printed():  # not a fixed delay: Python may take seconds to start on a slow device
            give_up = time.monotonic() + 120
            while not marker.exists() and time.monotonic() < give_up:
                time.sleep(0.05)
            cancel.set()  # "before" is in the pipe already: the pump drains it after the kill

        threading.Thread(target=cancel_once_printed, daemon=True).start()
        code = "import sys, time; print('before', flush=True); open(sys.argv[1], 'w').close(); time.sleep(60)"
        t = time.monotonic()
        rc, out, err = procs.run([PY, "-c", code, str(marker)], timeout=None, cancel=cancel)
        self.assertLess(time.monotonic() - t, PROMPT_S)
        self.assertEqual((rc, out.strip()), (-9, "before"))
        self.assertTrue(err.endswith("[cancelled]"), err)
        # already cancelled: nothing is started
        self.assertEqual(procs.run([PY, "-c", "raise SystemExit(3)"], cancel=cancel), (-9, "", "[cancelled]"))

    def test_output_is_bounded_head_and_tail(self):
        code = "import sys; sys.stdout.write('x' * 2000000 + 'END'); sys.stderr.write('e' * 5000)"
        rc, out, err = procs.run([PY, "-c", code], timeout=120, max_output_chars=1000)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "x" * 500 + "\n[... 1999003 characters omitted ...]\n" + "x" * 497 + "END")
        self.assertEqual(err, "e" * 500 + "\n[... 4000 characters omitted ...]\n" + "e" * 500)
        rc, out, err = procs.run([PY, "-c", "print('a' * 5000)"], timeout=120)  # default: everything
        self.assertEqual(out, "a" * 5000 + "\n")

    def test_rolling_tail(self):
        sink = procs._Output(10)
        for chunk in ("abc", "defgh", "ijklmnop", "qr", "", "stuvwxyz"):
            sink.add(chunk)
        self.assertEqual(sink.text(), "abcde\n[... 16 characters omitted ...]\nvwxyz")
        sink = procs._Output(None)
        sink.add("a\n")
        sink.add("b")
        self.assertEqual(sink.text(), "a\nb")

    def test_text_decoding_and_input(self):
        rc, out, err = procs.run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], input_text="a\nb\n",
                                 timeout=60)
        self.assertEqual((rc, out.strip()), (0, repr("a\nb\n")))
        code = "import sys; sys.stdout.buffer.write(b'x\\r\\ny\\xc3'); sys.stderr.buffer.write('\\u00e9'.encode())"
        rc, out, err = procs.run([PY, "-c", code], timeout=60)
        self.assertEqual((rc, out, err), (0, "x\ny\ufffd", "\u00e9"))  # universal newlines, UTF-8 with replace

    def test_input_the_child_never_reads_cannot_stall_the_timeout(self):
        t = time.monotonic()
        rc, _out, _err = procs.run([PY, "-c", "import time; time.sleep(60)"], input_text="x" * (8 * 1024 * 1024),
                                   timeout=2)
        self.assertEqual(rc, -9)
        self.assertLess(time.monotonic() - t, PROMPT_S)


if __name__ == "__main__":
    unittest.main()
