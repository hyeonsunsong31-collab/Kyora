"""The background service: Kyora keeps running after the terminal that started it is closed.

The supervisor is exercised with a stand-in server (a tiny Python child) for the restart and stop logic,
and with the real ``kyora serve`` for the end-to-end path: started detached, its server killed and started
again, stopped cleanly. On POSIX the end-to-end test also checks that the supervisor is in a session of its
own and survives SIGHUP (what a closed terminal sends). Autostart: the Windows Task Scheduler definition
is checked as XML here (CI registers it for real on Windows), the ~/.profile block for real with a
temporary HOME.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (drops the developer's key variables)
from kyora import service
from tests import copy_config

REPO = Path(__file__).resolve().parent.parent
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(pred, timeout: float = 30.0, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _answers(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class RotatingLogTests(unittest.TestCase):
    def test_old_output_is_kept_in_numbered_files_and_the_size_stays_capped(self):
        with tempfile.TemporaryDirectory() as d:
            log = service.RotatingLog(Path(d) / "service.log", max_bytes=100, backups=2)
            for i in range(30):
                log.write(f"line {i:02d} ................\n".encode())
            log.close()
            names = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(names, ["service.log", "service.log.1", "service.log.2"])
            for p in Path(d).iterdir():
                self.assertLessEqual(p.stat().st_size, 100)
            self.assertIn("line 29", (Path(d) / "service.log").read_text())


class SupervisorTests(unittest.TestCase):
    """Restart and stop logic with a stand-in server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _supervisor(self, code: str, **kw) -> service.Supervisor:
        argv = [sys.executable, "-c", code]
        return service.Supervisor(self.home, backoff=(0.05, 0.1), stable_s=60, grace_s=2,
                                  argv_for=lambda spec: argv, **kw)

    def _run_in_thread(self, sup: service.Supervisor) -> threading.Thread:
        t = threading.Thread(target=sup.run, daemon=True)
        t.start()
        self.addCleanup(lambda: (service.Paths(self.home).stop.write_text("x"), t.join(15)))
        return t

    def test_a_server_that_exits_is_started_again_and_its_output_is_logged(self):
        sup = self._supervisor("print('Open:  http://127.0.0.1:9', flush=True); raise SystemExit(3)")
        t = self._run_in_thread(sup)
        state = service.Paths(self.home).state
        self.assertTrue(_until(lambda: json.loads(state.read_text()).get("restarts", 0) >= 3 if state.exists()
                               else False, 20), "the server was not started again")
        service.Paths(self.home).stop.write_text("stop")
        t.join(15)
        self.assertFalse(t.is_alive(), "the supervisor did not stop")
        st = json.loads(state.read_text())
        self.assertEqual((st["last_exit"], st["running"], st["url"]), (3, False, "http://127.0.0.1:9"))
        log = service.Paths(self.home).log.read_text()
        self.assertIn("Open:  http://127.0.0.1:9", log)
        self.assertIn("server exited with code 3", log)
        self.assertIn("supervisor stopped", log)

    def test_the_server_writes_utf8_so_a_non_ascii_path_cannot_crash_it(self):
        # on Windows a pipe would otherwise get the ANSI code page: a Korean user folder in a printed path
        # made the first print raise, and the server would crash and restart for ever
        code = ("import os, sys; print(os.environ.get('PYTHONIOENCODING'), os.environ.get('KYORA_SERVICE'), "
                "'C:/Users/\\ud64d\\uae38\\ub3d9', flush=True); raise SystemExit(0)")
        sup = self._supervisor(code)
        t = self._run_in_thread(sup)
        log = service.Paths(self.home).log
        self.assertTrue(_until(lambda: log.exists() and "C:/Users/" in log.read_text(encoding="utf-8"), 20))
        service.Paths(self.home).stop.write_text("stop")
        t.join(15)
        text = log.read_text(encoding="utf-8")
        self.assertIn("utf-8 1 C:/Users/\ud64d\uae38\ub3d9", text)

    def test_a_stop_request_lets_the_server_finish_and_ends_it_after_the_grace_period(self):
        # this stand-in ignores the stop file: after grace_s the supervisor ends it by force
        sup = self._supervisor("import time\nprint('up', flush=True)\nwhile True: time.sleep(0.1)")
        t = self._run_in_thread(sup)
        state = service.Paths(self.home).state
        self.assertTrue(_until(lambda: state.exists() and json.loads(state.read_text()).get("child_pid"), 20))
        child = json.loads(state.read_text())["child_pid"]
        t0 = time.monotonic()
        service.Paths(self.home).stop.write_text("stop")
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(time.monotonic() - t0, 1.5, "the grace period was skipped")
        self.assertFalse(service.pid_alive(child), "the server was left running")
        self.assertIn("did not stop within", service.Paths(self.home).log.read_text())

    def test_a_second_supervisor_on_the_same_home_does_not_start(self):
        sup = self._supervisor("import time\nwhile True: time.sleep(0.1)")
        self._run_in_thread(sup)
        self.assertTrue(_until(lambda: service.supervisor_running(self.home)[0], 20))
        self.assertEqual(service.Supervisor(self.home).run(), 0)  # returns at once: one supervisor per home

    def test_a_status_probe_while_the_supervisor_starts_does_not_make_it_give_up(self):
        # supervisor_running() takes the lock for an instant; a supervisor starting at that moment retries
        p = service.Paths(self.home)
        p.dir.mkdir(parents=True)
        probe = p.lock()
        self.assertTrue(probe.acquire())  # a probe holds the lock while the supervisor starts
        sup = self._supervisor("import time\nwhile True: time.sleep(0.1)")
        t = self._run_in_thread(sup)
        time.sleep(0.5)
        probe.release()
        self.assertTrue(_until(lambda: p.state.exists() and json.loads(p.state.read_text()).get("child_pid"), 20),
                        "the supervisor gave up because of a momentary probe")
        self.assertTrue(t.is_alive())

    def test_an_old_stop_file_does_not_stop_a_new_supervisor(self):
        p = service.Paths(self.home)
        p.dir.mkdir(parents=True)
        p.stop.write_text("left over")
        old = time.time() - 3600
        os.utime(p.stop, (old, old))
        sup = self._supervisor("import time\nwhile True: time.sleep(0.1)")
        self._run_in_thread(sup)
        self.assertTrue(_until(lambda: p.state.exists() and json.loads(p.state.read_text()).get("child_pid"), 20))
        self.assertTrue(service.supervisor_running(self.home)[0])


class CommandTests(unittest.TestCase):
    def test_kyora_is_started_the_same_way_from_any_working_directory(self):
        args = service.self_args()
        self.assertEqual(args[0], "-c")  # a source checkout here
        self.assertIn(repr(str(REPO)), args[1])
        with mock.patch.object(service, "_zipapp", return_value=Path("/opt/Kyora/kyora.pyz")):
            self.assertEqual(service.self_args(), [str(Path("/opt/Kyora/kyora.pyz"))])
            self.assertEqual(service.work_dir(), Path("/opt/Kyora"))
        spec = {"mode": "hub", "home": "/h", "config": "/c", "args": ["--port", "9000"]}
        argv = service.server_argv(spec)
        self.assertEqual(argv[-7:], ["hub", "--home", "/h", "--config", "/c", "--port", "9000"])
        self.assertEqual(service._port_of(spec), 9000)
        sup = service.supervisor_argv(Path("/h"))
        self.assertEqual(sup[-4:], ["service", "run", "--home", str(Path("/h"))])

    @unittest.skipIf(os.name == "nt", "the source-tree command is checked on POSIX; CI runs it on Windows")
    def test_the_command_runs_from_another_directory(self):
        out = subprocess.run([sys.executable, *service.self_args(), "version"], capture_output=True, text=True,
                             cwd=tempfile.gettempdir(), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("kyora ", out.stdout)

    def test_windows_task_has_no_time_limit_runs_on_battery_and_restarts(self):
        xml = service.task_xml(Path(r"C:\Users\me\.kyora"), r"PC\me")
        root = ET.fromstring(xml.split("\n", 1)[1])  # the declaration says UTF-16 (the file is written so)
        f = lambda path: root.find(path, TASK_NS).text  # noqa: E731
        self.assertEqual(f("t:Triggers/t:LogonTrigger/t:UserId"), r"PC\me")
        self.assertEqual(f("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(f("t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
        self.assertEqual(f("t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(f("t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(f("t:Settings/t:StopIfGoingOnBatteries"), "false")
        self.assertEqual(f("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(f("t:Settings/t:RestartOnFailure/t:Interval"), "PT1M")
        self.assertIn("service run --home", f("t:Actions/t:Exec/t:Arguments"))

    def test_profile_block_is_added_once_and_removed_without_touching_the_rest(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(service, "profile_path", return_value=Path(d) / ".profile"):
            prof = Path(d) / ".profile"
            prof.write_text("export EDITOR=vi\n")
            home = Path(d) / "kyhome"
            self.assertTrue(service._install_profile(home)[0])
            self.assertTrue(service._install_profile(home)[0])
            text = prof.read_text()
            self.assertEqual(text.count(service.PROFILE_BEGIN), 1)
            self.assertTrue(text.startswith("export EDITOR=vi\n"))
            self.assertIn("service start --home", text)
            self.assertIn('if [ -z "${KYORA_SERVICE:-}" ]', text, "the service's own shells do not start it again")
            self.assertEqual(service._strip_block(text), "export EDITOR=vi\n")

    @unittest.skipIf(os.name == "nt", "Windows uses Task Scheduler / the Run key (registered for real on CI)")
    def test_install_and_uninstall_use_the_profile_on_posix(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(service, "profile_path", return_value=Path(d) / ".profile"):
            home = Path(d) / "kyhome"
            info = service.install(home)
            self.assertEqual(info["method"], "profile")
            self.assertEqual(json.loads((home / "service" / "install.json").read_text())["method"], "profile")
            res = service.uninstall(home)
            self.assertNotIn(service.PROFILE_BEGIN, (Path(d) / ".profile").read_text())
            self.assertTrue(res["removed"])
            self.assertFalse((home / "service" / "install.json").exists())


class EndToEndTests(unittest.TestCase):
    """The real Kyora server under the supervisor, started detached as ``kyora service start`` does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kyora-service-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.home = self.tmp / "home"
        self.cfg = self.tmp / "config"
        copy_config(REPO / "config", self.cfg)
        self.port = _free_port()
        self.env = {k: v for k, v in os.environ.items() if k not in ("KYORA_HOME", "KYORA_CONFIG_DIR")}
        self.env["HOME"] = str(self.tmp)  # never the developer's ~/.profile

    def kyora(self, *args, timeout=240) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *service.self_args(), "service", *args, "--home", str(self.home)],
                              capture_output=True, text=True, env=self.env, cwd=str(self.tmp), timeout=timeout)

    def _cleanup(self):
        self.kyora("stop", "--quiet", timeout=120)
        st = service.status(self.home)
        service.kill_tree(st.get("server_pid"))
        service.kill_tree(st.get("supervisor_pid"))

    def test_runs_detached_restarts_its_server_and_stops_cleanly(self):
        self.addCleanup(self._cleanup)
        r = self.kyora("start", "--config", str(self.cfg), "--port", str(self.port), "--wait", "120")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("answering", r.stdout)
        self.assertTrue(_answers(self.port))
        st = service.status(self.home)
        sup, server = st["supervisor_pid"], st["server_pid"]
        self.assertTrue(sup and server, st)
        if os.name != "nt":
            self.assertNotEqual(os.getsid(sup), os.getsid(0), "the supervisor must not be in this terminal's session")
            os.kill(sup, signal.SIGHUP)  # what a closed terminal sends
            time.sleep(1)
            self.assertTrue(service.pid_alive(sup), "SIGHUP ended the supervisor")
        service.kill_tree(server)  # the server dies: the supervisor starts it again
        self.assertTrue(_until(lambda: service.status(self.home)["restarts"] >= 1 and _answers(self.port), 120),
                        service.tail(self.home))
        st = service.status(self.home)
        self.assertNotEqual(st["server_pid"], server)
        self.assertEqual(st["supervisor_pid"], sup)
        r = self.kyora("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Kyora service stopped.", r.stdout)
        self.assertNotIn("by force", r.stdout, "the server should stop cleanly on the stop file")
        self.assertFalse(_answers(self.port))
        self.assertFalse(service.supervisor_running(self.home)[0])
        self.assertEqual(json.loads(service.Paths(self.home).state.read_text())["last_exit"], 0)
        self.assertEqual(self.kyora("status").returncode, 3)

    def test_stop_leaves_a_kyora_serve_of_a_terminal_alone(self):
        proc = subprocess.Popen([sys.executable, *service.self_args(), "serve", "--home", str(self.home), "--config",
                                 str(self.cfg), "--port", str(self.port)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=self.env)
        self.addCleanup(lambda: (proc.kill(), proc.wait(10)))
        self.assertTrue(_until(lambda: _answers(self.port), 120))
        r = self.kyora("stop")
        self.assertIn("was not running", r.stdout)
        self.assertIsNone(proc.poll())
        self.assertTrue(_answers(self.port))


class StopFileTests(unittest.TestCase):
    def test_the_server_shuts_down_cleanly_when_the_stop_file_appears(self):
        tmp = Path(tempfile.mkdtemp(prefix="kyora-stopfile-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        copy_config(REPO / "config", tmp / "config")
        port, stop_file = _free_port(), tmp / "stop"
        env = dict(os.environ, **{service.ENV_STOP_FILE: str(stop_file), service.ENV_SERVICE: "1"})
        proc = subprocess.Popen([sys.executable, *service.self_args(), "serve", "--home", str(tmp / "home"),
                                 "--config", str(tmp / "config"), "--port", str(port)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(_until(lambda: _answers(port), 120))
        stop_file.write_text("stop")
        self.assertEqual(proc.wait(30), 0)
        out = proc.stdout.read()
        proc.stdout.close()
        self.assertIn("Stopping (kyora service stop)", out)
        self.assertNotIn("Kyora runs while this terminal stays open", out, "no terminal hint under the service")


if __name__ == "__main__":
    unittest.main()
