"""Subprocess helpers with process-group termination (POSIX and Windows)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence


def popen(args: Sequence[str], *, cwd=None, env=None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
          stderr=subprocess.PIPE, text: bool = True) -> subprocess.Popen:
    """Start a child in its own process group so the whole tree can be killed."""
    kwargs: dict = dict(cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace", bufsize=1)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(list(args), **kwargs)
    except FileNotFoundError:
        alt = termux_shebang_argv(args, env)
        if alt is None:
            raise
        return subprocess.Popen(alt, **kwargs)


def termux_shebang_argv(args: Sequence[str], env: dict | None = None) -> list[str] | None:
    """Termux without termux-exec: rewrite a script whose shebang names a missing interpreter.

    Android has no /bin/sh or /usr/bin/env; Termux puts them in $PREFIX/bin and normally
    rewrites shebangs through the LD_PRELOADed termux-exec. A process started without it
    (seen in termux-docker) gets ENOENT for npm's ``#!/usr/bin/env node`` claude shim or a
    ``#!/bin/sh`` script. Returns ``[$PREFIX/bin/<interp>, *shebang args, script, *rest]`` or
    None when this is not that case.
    """
    if os.name == "nt" or not args:
        return None
    environ = env if env is not None else os.environ
    prefix = environ.get("PREFIX") or os.environ.get("PREFIX") or ""
    if "com.termux" not in prefix and "com.termux" not in sys.prefix:
        return None
    prefix = prefix or str(Path(sys.prefix))
    script = shutil.which(args[0], path=environ.get("PATH")) if os.sep not in args[0] else args[0]
    if not script or not os.path.isfile(script):
        return None
    try:
        with open(script, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").strip().split()
    if not parts or os.path.exists(parts[0]) or not parts[0].startswith(("/bin/", "/usr/bin/")):
        return None
    interp = os.path.join(prefix, "bin", os.path.basename(parts[0]))
    if not os.path.isfile(interp):
        return None
    return [interp, *parts[1:], script, *list(args[1:])]


def kill_tree(proc: subprocess.Popen, grace_s: float = 10.0) -> None:
    """Terminate ``proc`` and its descendants; escalate to kill after ``grace_s``."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run(args: Sequence[str], *, cwd=None, env=None, timeout: float | None = 120, input_text: str | None = None) -> tuple[int, str, str]:
    """Run a command to completion, returning ``(returncode, stdout, stderr)``.

    A timeout kills the process tree and returns ``-9`` with the partial output.
    """
    proc = popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL)
    out: list[str] = []
    err: list[str] = []

    def _pump(stream, sink):
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
        except Exception:
            pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)
    t_out.start(); t_err.start()
    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except Exception:
            pass
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc, grace_s=3)
        rc = -9
    t_out.join(timeout=5); t_err.join(timeout=5)
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing a finished pipe must never raise
                pass
    return rc, "".join(out), "".join(err)
