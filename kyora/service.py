"""Kyora as a background service: it keeps running after the terminal that started it is closed.

``kyora service install`` starts a *supervisor* that is not attached to any terminal and sets it up to
start again by itself; ``kyora service start`` starts it once. The supervisor (``kyora service run``)
starts ``kyora serve`` (or ``hub``) and starts it again whenever it exits - a crash, an error, a killed
process - after a short, growing pause (1 s up to 60 s; a run that lasted five minutes resets it). It
writes what the server prints to ``KYORA_HOME/logs/service.log`` (size-capped, three old files kept).

Not attached to the terminal:

* POSIX (iSH, Termux, Linux, macOS): the supervisor runs in its own session (``setsid``) and ignores
  SIGHUP, so closing the terminal or ending the shell does not end it.
* Windows: the supervisor runs without a console (``pythonw.exe``, ``DETACHED_PROCESS``), so closing the
  PowerShell window does not end it; installed, Task Scheduler starts it, not the terminal.

Starting by itself (``install``):

* Windows: a Task Scheduler task "Kyora" that starts the supervisor when you log on, with no time limit,
  also on battery, and restarts it if it fails. Where Task Scheduler refuses (policy, no permission),
  the per-user Run key (``HKCU\\...\\CurrentVersion\\Run``) starts it at logon instead.
* POSIX: a marked block in ``~/.profile`` that starts the service whenever a login shell starts - on
  iSH, whenever the iSH app opens.

What no program can do, and Kyora does not claim: keep running while the computer is off, asleep or
logged off, or while iOS has terminated the iSH app (swiped away, restarted phone, memory pressure):
iOS stops every process inside iSH then. Kyora comes back at the next logon or the next time iSH opens.

``kyora service stop`` asks the supervisor and the server to stop (a stop file both watch; the server
shuts down cleanly), and ends them by force after a grace period. ``uninstall`` stops it and removes
the autostart entry; ``status`` says what runs, since when, how often it restarted, and whether the
server answers.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .util.instance_lock import InstanceLock

SERVICE_DIR = "service"
SPEC_NAME = "service.json"
STATE_NAME = "state.json"
INSTALL_NAME = "install.json"
STOP_NAME = "stop"
EARLY_LOG_NAME = "supervisor.err"
LOG_NAME = "service.log"
SUP_LOCK_NAME = "supervisor.lock"
SUP_PID_NAME = "supervisor.pid"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
STABLE_S = 300.0          # a server that ran this long resets the pause before the next start
STOP_GRACE_S = 20.0       # how long a stopping server may take before it is ended by force
POLL_S = 0.5
LOCK_TRIES = 25           # x LOCK_RETRY_S: how long a new supervisor tries to take its lock
LOCK_RETRY_S = 0.2
TASK_NAME = "Kyora"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Kyora"
PROFILE_BEGIN = "# >>> kyora service >>>"
PROFILE_END = "# <<< kyora service <<<"
ENV_SERVICE = "KYORA_SERVICE"
ENV_STOP_FILE = "KYORA_SERVICE_STOP_FILE"
MODES = ("serve", "hub")

# Windows process creation flags (subprocess has the names only on Windows)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


# ----------------------------------------------------------------------------- paths and files
@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def dir(self) -> Path:
        return self.home / SERVICE_DIR

    @property
    def spec(self) -> Path:
        return self.dir / SPEC_NAME

    @property
    def state(self) -> Path:
        return self.dir / STATE_NAME

    @property
    def install(self) -> Path:
        return self.dir / INSTALL_NAME

    @property
    def stop(self) -> Path:
        return self.dir / STOP_NAME

    @property
    def early_log(self) -> Path:
        return self.dir / EARLY_LOG_NAME

    @property
    def log(self) -> Path:
        return self.home / "logs" / LOG_NAME

    def lock(self) -> InstanceLock:
        return InstanceLock(self.dir, SUP_LOCK_NAME, SUP_PID_NAME)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ----------------------------------------------------------------------------- how to run Kyora again
def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _zipapp() -> Path | None:
    """The kyora.pyz this code runs from, if any."""
    for parent in Path(__file__).resolve().parents:
        if parent.suffix == ".pyz" and parent.is_file():
            return parent
    return None


def self_args() -> list[str]:
    """Arguments after the interpreter that run Kyora's command line, from any working directory."""
    pyz = _zipapp()
    if pyz is not None:
        return [str(pyz)]
    root = str(_source_root())
    return ["-c", f"import sys; sys.path.insert(0, {root!r}); from kyora.__main__ import main; sys.exit(main())"]


def work_dir() -> Path:
    pyz = _zipapp()
    return pyz.parent if pyz is not None else _source_root()


def _python_variant(windowless: bool) -> str:
    exe = Path(sys.executable)
    if os.name != "nt":
        return str(exe)
    name = exe.name.lower()
    want = ("pythonw" if windowless else "python") + (exe.suffix or ".exe")
    if name.startswith("python") and (exe.parent / want).is_file():
        return str(exe.parent / want)
    return str(exe)


def windowless_python() -> str:
    """The interpreter for the supervisor: ``pythonw.exe`` on Windows (no console window at all)."""
    return _python_variant(True)


def console_python() -> str:
    """The interpreter for the server: ``python.exe`` on Windows, started without a window."""
    return _python_variant(False)


def supervisor_argv(home: Path) -> list[str]:
    return [windowless_python(), *self_args(), "service", "run", "--home", str(home)]


# ----------------------------------------------------------------------------- the run spec
def default_spec(home: Path, config_dir: Path | str | None = None) -> dict:
    return {"version": 1, "mode": "serve", "args": [], "home": str(home),
            "config": str(config_dir) if config_dir else "", "cwd": str(work_dir()), "written": _now()}


def write_spec(home: Path, mode: str | None, args: list[str] | None, config_dir: Path | str | None) -> dict:
    """Store what the supervisor runs (re-read at every start, so ``restart`` applies a change)."""
    p = Paths(home)
    spec = _read_json(p.spec) or default_spec(home, config_dir)
    if mode:
        spec["mode"] = mode
    if args is not None:
        spec["args"] = [str(a) for a in args]
    if config_dir:
        spec["config"] = str(config_dir)
    spec.update(home=str(home), cwd=str(work_dir()), written=_now(), version=1)
    _write_json(p.spec, spec)
    return spec


def server_argv(spec: dict) -> list[str]:
    mode = spec.get("mode") if spec.get("mode") in MODES else "serve"
    argv = [console_python(), *self_args(), mode, "--home", str(spec.get("home") or "")]
    if spec.get("config"):
        argv += ["--config", str(spec["config"])]
    return argv + [str(a) for a in spec.get("args") or []]


def _port_of(spec: dict) -> int:
    args = [str(a) for a in spec.get("args") or []]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        if a.startswith("--port=") and a[7:].isdigit():
            return int(a[7:])
    try:
        from .config import Config
        return int(Config.load(spec.get("config") or None).server.port)
    except Exception:  # noqa: BLE001 - the default port then
        return 8765


# ----------------------------------------------------------------------------- processes
def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)  # never on Windows: os.kill(pid, 0) terminates the process there
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_tree(pid: int | None) -> None:
    if not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- the log
class RotatingLog:
    """Appends bytes to ``path``; past :data:`LOG_MAX_BYTES` it keeps ``path.1`` .. ``path.N``."""

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        self.path, self.max_bytes, self.backups = Path(path), int(max_bytes), int(backups)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "ab")

    def _rotate(self) -> None:
        self._fh.close()
        for i in range(self.backups - 1, 0, -1):
            src, dst = self.path.with_name(f"{self.path.name}.{i}"), self.path.with_name(f"{self.path.name}.{i + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        self._fh = open(self.path, "ab")

    def write(self, data: bytes) -> None:
        with self._lock:
            try:
                if self._fh.tell() + len(data) > self.max_bytes and self._fh.tell() > 0:
                    self._rotate()
                self._fh.write(data)
                self._fh.flush()
            except OSError:
                pass  # a full disk must not stop the supervisor

    def note(self, text: str) -> None:
        self.write(f"{_now()} [service] {text}\n".encode("utf-8", "replace"))

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


# ----------------------------------------------------------------------------- the supervisor
class Supervisor:
    """Runs the server, starts it again when it exits, stops when the stop file appears."""

    def __init__(self, home: Path, *, backoff: tuple[float, ...] = BACKOFF_S, stable_s: float = STABLE_S,
                 grace_s: float = STOP_GRACE_S, argv_for=server_argv):
        self.paths = Paths(Path(home))
        self.backoff, self.stable_s, self.grace_s = tuple(backoff), float(stable_s), float(grace_s)
        self.argv_for = argv_for
        self.started = time.time()
        self._signalled = threading.Event()
        self.state: dict = {}
        self.child: subprocess.Popen | None = None

    # -- stop requests: the stop file (any platform) or SIGTERM/SIGINT (POSIX)
    def stop_requested(self) -> bool:
        if self._signalled.is_set():
            return True
        try:
            return self.paths.stop.stat().st_mtime >= self.started - 1.0
        except OSError:
            return False

    def _on_signal(self, signum, _frame) -> None:
        self._signalled.set()
        try:  # the server watches the stop file too, so it shuts down cleanly
            self.paths.stop.write_text(f"{_now()} signal {signum}\n", encoding="utf-8")
        except OSError:
            pass

    def _save_state(self, **changes) -> None:
        self.state.update(changes, updated=_now())
        try:
            _write_json(self.paths.state, self.state)
        except OSError:
            pass

    def _pump(self, child: subprocess.Popen, log: RotatingLog) -> None:
        for line in iter(child.stdout.readline, b""):
            log.write(line)
            text = line.decode("utf-8", "replace")
            if text.startswith("Open:"):
                self._save_state(url=text.split(":", 1)[1].strip())
        try:
            child.stdout.close()
        except OSError:
            pass

    def _wait_child(self, child: subprocess.Popen, log: RotatingLog) -> int:
        """Wait for the server; on a stop request give it the grace period, then end it."""
        stop_at = None
        while True:
            try:
                return child.wait(timeout=POLL_S)
            except subprocess.TimeoutExpired:
                pass
            if self.stop_requested():
                if stop_at is None:
                    stop_at = time.monotonic() + self.grace_s
                    log.note(f"stopping the server (pid {child.pid})")
                elif time.monotonic() >= stop_at:
                    log.note(f"the server did not stop within {self.grace_s:.0f} s; ending it")
                    kill_tree(child.pid)
                    try:
                        return child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        return -9

    def _pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop_requested():
            time.sleep(min(POLL_S, max(0.0, end - time.monotonic())))

    def run(self) -> int:
        p = self.paths
        p.dir.mkdir(parents=True, exist_ok=True)
        lock = p.lock()
        # supervisor_running() probes by taking the lock for an instant: retry before deciding that another
        # supervisor holds it (a probe of 'service start' must not make the new supervisor give up)
        for _ in range(LOCK_TRIES):
            if lock.acquire():
                break
            time.sleep(LOCK_RETRY_S)
        else:
            print(f"The Kyora service is already running (pid {lock.holder_pid() or '?'}).", file=sys.stderr)
            return 0
        try:
            p.stop.unlink()  # a stop request from before this supervisor started is done
        except OSError:
            pass
        self.started = time.time()
        handlers = [(getattr(signal, "SIGHUP", None), signal.SIG_IGN),  # a closed terminal; the server inherits it
                    (signal.SIGTERM, self._on_signal), (signal.SIGINT, self._on_signal)]
        for sig, handler in handlers:
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread (tests): the stop file still works
                pass
        log = RotatingLog(p.log)
        log.note(f"supervisor started (pid {os.getpid()}, home {p.home})")
        self.state = {"supervisor_pid": os.getpid(), "started": _now(), "restarts": 0, "running": True}
        self._save_state()
        attempt = 0
        try:
            while not self.stop_requested():
                spec = _read_json(p.spec) or default_spec(p.home)
                argv = self.argv_for(spec)
                # UTF-8 output: on Windows a pipe gets the ANSI code page, and a path with a non-ASCII user name
                # (C:\Users\<name>) would make the server's first print fail
                env = dict(os.environ, **{ENV_SERVICE: "1", ENV_STOP_FILE: str(p.stop), "PYTHONUNBUFFERED": "1",
                                          "PYTHONIOENCODING": "utf-8"})
                flags = CREATE_NO_WINDOW if os.name == "nt" else 0
                t0 = time.monotonic()
                try:
                    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, cwd=spec.get("cwd") or None, env=env,
                                             creationflags=flags)
                except OSError as exc:
                    log.note(f"cannot start the server: {exc}")
                    code = None
                else:
                    self.child = child
                    log.note(f"server started (pid {child.pid}): {spec.get('mode', 'serve')} "
                             f"{' '.join(spec.get('args') or [])}".rstrip())
                    self._save_state(child_pid=child.pid, child_started=_now())
                    pump = threading.Thread(target=self._pump, args=(child, log), daemon=True)
                    pump.start()
                    code = self._wait_child(child, log)
                    pump.join(timeout=5)
                    self.child = None
                uptime = time.monotonic() - t0
                self._save_state(child_pid=None, last_exit=code, last_uptime_s=round(uptime, 1))
                if self.stop_requested():
                    break
                attempt = 0 if uptime >= self.stable_s else attempt
                delay = self.backoff[min(attempt, len(self.backoff) - 1)] if self.backoff else 0.0
                attempt += 1
                self._save_state(restarts=int(self.state.get("restarts") or 0) + 1)
                log.note(f"server exited with code {code} after {uptime:.0f} s; starting it again in {delay:.0f} s")
                self._pause(delay)
            log.note("supervisor stopped")
            return 0
        finally:
            self._save_state(running=False, child_pid=None, stopped=_now())
            log.close()
            lock.release()
            for f in (p.stop, p.lock().pid_path):
                try:
                    f.unlink()
                except OSError:
                    pass


# ----------------------------------------------------------------------------- control
def supervisor_running(home: Path) -> tuple[bool, int | None]:
    p = Paths(home)
    lock = p.lock()
    pid = _int(lock.holder_pid())
    held = lock.is_held() if p.dir.is_dir() else False
    if held is None:  # no working lock call here: the recorded pid decides
        held = pid_alive(pid)
    return bool(held), (pid if held else None)


def _answers(url: str, timeout: float = 3.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url.rstrip("/") + "/api/status", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


def status(home: Path, *, probe: bool = True) -> dict:
    """What runs: supervisor and server pids, address, whether it answers (``probe``), restarts, autostart."""
    p = Paths(home)
    running, pid = supervisor_running(home)
    state = _read_json(p.state)
    spec = _read_json(p.spec)
    url = state.get("url") or (f"http://127.0.0.1:{_port_of(spec)}" if spec else "")
    child = _int(state.get("child_pid"))
    return {"running": running, "supervisor_pid": pid, "server_pid": child if running and pid_alive(child) else None,
            "url": url, "answering": bool(url) and probe and _answers(url),
            "since": state.get("started") if running else None,
            "restarts": state.get("restarts", 0), "last_exit": state.get("last_exit"),
            "installed": _read_json(p.install) or None, "log": str(p.log), "home": str(p.home),
            "mode": spec.get("mode", "serve") if spec else None, "args": spec.get("args", []) if spec else []}


def _wait_for(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _spawn_detached(home: Path) -> int:
    """Start the supervisor so that no terminal owns it; returns its pid."""
    p = Paths(home)
    p.dir.mkdir(parents=True, exist_ok=True)
    argv = supervisor_argv(home)
    err = open(p.early_log, "ab")
    try:
        common = {"stdin": subprocess.DEVNULL, "stdout": err, "stderr": err, "cwd": str(work_dir()),
                  "close_fds": True}
        if os.name == "nt":
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            try:  # leave the terminal's job object too, where that is allowed
                proc = subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError:
                proc = subprocess.Popen(argv, creationflags=flags, **common)
        else:
            try:
                proc = subprocess.Popen(argv, start_new_session=True, **common)
            except OSError:  # setsid refused (an emulator): the supervisor still ignores SIGHUP
                proc = subprocess.Popen(argv, **common)
    finally:
        err.close()
    return proc.pid


def start(home: Path, *, wait_s: float = 60.0, out=print) -> dict:
    """Start the supervisor (through Task Scheduler where it is installed), wait until the server answers."""
    home = Path(home)
    p = Paths(home)
    try:
        p.stop.unlink()
    except OSError:
        pass
    running, pid = supervisor_running(home)
    how = "already running"
    if not running:
        how = "detached"
        if os.name == "nt" and (_read_json(p.install).get("method") == "task"):
            r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True, check=False)
            if r.returncode == 0 and _wait_for(lambda: supervisor_running(home)[0], 15.0):
                how = "Task Scheduler"
        if how == "detached":
            _spawn_detached(home)
            # Python starts slowly under iSH's x86 emulation: give the supervisor time to take its lock
            if not _wait_for(lambda: supervisor_running(home)[0], 120.0):
                early = ""
                try:
                    early = p.early_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                except OSError:
                    pass
                raise RuntimeError("the Kyora service did not start" + (f":\n{early}" if early else ""))
    st = status(home)
    if wait_s > 0 and not st["answering"]:
        def settled() -> bool:
            now = status(home)
            return now["answering"] or not now["running"]
        _wait_for(settled, wait_s, 1.0)
        st = status(home)
    st["started_by"] = how
    return st


def stop(home: Path, *, timeout_s: float = 45.0) -> dict:
    """Ask the supervisor and the server to stop; end them by force after ``timeout_s``."""
    home = Path(home)
    p = Paths(home)
    state = _read_json(p.state)
    running, pid = supervisor_running(home)
    child = _int(state.get("child_pid"))  # also a server left behind by a supervisor that was killed
    if not running and not pid_alive(child):
        return {"stopped": False, "reason": "not running"}  # a 'kyora serve' in a terminal is not the service's
    p.dir.mkdir(parents=True, exist_ok=True)
    p.stop.write_text(f"{_now()} kyora service stop\n", encoding="utf-8")
    done = _wait_for(lambda: not supervisor_running(home)[0] and not pid_alive(child), timeout_s)
    forced = False
    if not done:
        forced = True
        kill_tree(child)
        kill_tree(pid)
        _wait_for(lambda: not supervisor_running(home)[0], 10.0)
    try:
        p.stop.unlink()
    except OSError:
        pass
    return {"stopped": True, "forced": forced}


# ----------------------------------------------------------------------------- autostart
def _xml(text: str) -> str:
    from xml.sax.saxutils import escape
    return escape(str(text), {'"': "&quot;"})


def task_xml(home: Path, user: str) -> str:
    argv = supervisor_argv(home)
    args = subprocess.list2cmdline(argv[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Kyora background service (kyora service run): starts at logon, restarted if it fails. Remove with: kyora service uninstall</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml(argv[0])}</Command>
      <Arguments>{_xml(args)}</Arguments>
      <WorkingDirectory>{_xml(work_dir())}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user() -> str:
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain and user else user


def _install_task(home: Path) -> tuple[bool, str]:
    p = Paths(home)
    xml_path = p.dir / "kyora-task.xml"
    p.dir.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(task_xml(home, _windows_user()), encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                       capture_output=True, text=True, check=False)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def _install_run_key(home: Path) -> tuple[bool, str]:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(supervisor_argv(home)))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def profile_path() -> Path:
    return Path(os.path.expanduser("~")) / ".profile"


def profile_block(home: Path) -> str:
    cmd = " ".join(shlex.quote(a) for a in [sys.executable, *self_args(), "service", "start", "--home", str(home),
                                            "--quiet", "--wait", "0"])
    return (f"{PROFILE_BEGIN}\n"
            "# Starts Kyora's background service when a login shell starts (on iSH: when the app opens).\n"
            "# Added by 'kyora service install'; 'kyora service uninstall' removes it.\n"
            f'if [ -z "${{{ENV_SERVICE}:-}}" ]; then ( {cmd} >/dev/null 2>&1 & ) ; fi\n'
            f"{PROFILE_END}\n")


def _strip_block(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == PROFILE_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == PROFILE_END:
                skipping = False
            continue
        out.append(line)
    return "".join(out)


def _install_profile(home: Path) -> tuple[bool, str]:
    path = profile_path()
    try:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        body = _strip_block(old)
        if body and not body.endswith("\n"):
            body += "\n"
        path.write_text(body + profile_block(home), encoding="utf-8")
        return True, str(path)
    except OSError as exc:
        return False, str(exc)


def install(home: Path, *, method: str = "auto") -> dict:
    """Set up the autostart for this platform (it does not start the service; ``start`` does)."""
    home = Path(home)
    p = Paths(home)
    notes: list[str] = []
    if os.name == "nt":
        chosen = ""
        if method in ("auto", "task"):
            ok, msg = _install_task(home)
            if ok:
                chosen = "task"
                _remove_run_key()
            else:
                notes.append(f"Task Scheduler refused the task ({msg or 'no reason given'})")
                if method == "task":
                    raise RuntimeError(notes[-1])
        if not chosen:
            ok, msg = _install_run_key(home)
            if not ok:
                raise RuntimeError(f"cannot add the Run key either: {msg}")
            chosen = "runkey"
        info = {"method": chosen, "installed": _now(),
                "target": TASK_NAME if chosen == "task" else f"HKCU\\{RUN_KEY}\\{RUN_VALUE}"}
    else:
        ok, msg = _install_profile(home)
        if not ok:
            raise RuntimeError(f"cannot write {profile_path()}: {msg}")
        info = {"method": "profile", "installed": _now(), "target": msg}
    info["notes"] = notes
    _write_json(p.install, info)
    return info


def _remove_run_key() -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass


def uninstall(home: Path) -> dict:
    home = Path(home)
    p = Paths(home)
    stopped = stop(home)
    removed = []
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True,
                           check=False)
        if r.returncode == 0:
            removed.append(f"Task Scheduler task {TASK_NAME}")
        _remove_run_key()
        removed.append(f"HKCU\\{RUN_KEY}\\{RUN_VALUE} (if it was there)")
    else:
        path = profile_path()
        try:
            text = path.read_text(encoding="utf-8")
            if PROFILE_BEGIN in text:
                path.write_text(_strip_block(text), encoding="utf-8")
                removed.append(f"the block in {path}")
        except OSError:
            pass
    try:
        p.install.unlink()
    except OSError:
        pass
    return {"stopped": stopped.get("stopped", False), "removed": removed}


def tail(home: Path, lines: int = 60) -> str:
    try:
        data = Paths(home).log.read_bytes()[-200_000:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-max(1, int(lines)):])


__all__ = ["Paths", "Supervisor", "RotatingLog", "install", "uninstall", "start", "stop", "status", "tail",
           "self_args", "server_argv", "supervisor_argv", "task_xml", "profile_block", "write_spec", "pid_alive",
           "ENV_SERVICE", "ENV_STOP_FILE", "TASK_NAME", "PROFILE_BEGIN", "PROFILE_END"]
