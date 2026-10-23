"""Subprocess helpers with process-group termination (POSIX and Windows)."""
from __future__ import annotations

import codecs
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Sequence


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


CHUNK = 8192  # pumps read at most this many bytes at a time, so one huge line is bounded too
WAIT_STEP_S = 0.25  # how often a cancellable wait looks at the cancel event
PUMP_JOIN_S = 3.0  # after the process ended: how long the pumps may take to drain the pipes
CANCELLED_NOTE = "[cancelled]"


class _Output:
    """What one pipe printed: everything, or (``limit``) the first half and a rolling tail of the second half."""

    def __init__(self, limit: int | None):
        self.limit = limit if limit is None else max(2, int(limit))
        self.head: list[str] = []
        self.head_len = 0
        self.tail: deque[str] = deque()
        self.tail_len = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        with self.lock:
            if self.limit is None:
                self.head.append(text)
                return
            room = self.limit // 2 - self.head_len
            if room > 0:
                self.head.append(text[:room])
                self.head_len += min(room, len(text))
                text = text[room:]
            if not text:
                return
            self.tail.append(text)
            self.tail_len += len(text)
            keep = self.limit - self.limit // 2
            while self.tail_len > keep:
                excess = self.tail_len - keep
                first = self.tail[0]
                if len(first) <= excess:
                    self.tail.popleft()
                    cut = len(first)
                else:
                    self.tail[0] = first[excess:]
                    cut = excess
                self.tail_len -= cut
                self.dropped += cut

    def text(self) -> str:
        with self.lock:
            head, tail = "".join(self.head), "".join(self.tail)
            if self.dropped:
                return f"{head}\n[... {self.dropped} characters omitted ...]\n{tail}"
            return head + tail


def _pump(stream, sink: _Output) -> None:
    """Copy ``stream`` (binary pipe) into ``sink`` as text: UTF-8 with replacement, universal newlines."""
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True)
    read = getattr(stream, "read1", stream.read)  # read1: whatever is there, without waiting for a full chunk
    try:
        while True:
            data = read(CHUNK)
            if not data:
                break
            text = decoder.decode(data)
            if text:
                sink.add(text)
        text = decoder.decode(b"", final=True)
        if text:
            sink.add(text)
    except Exception:  # noqa: BLE001 - a pipe closed under the reader ends the copy
        pass


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except Exception:  # noqa: BLE001 - the child exited or does not read its input
        pass
    try:
        stream.close()
    except Exception:  # noqa: BLE001
        pass


def _end_group(proc: subprocess.Popen, sig: int = getattr(signal, "SIGKILL", 9)) -> None:
    """Kill what is left of ``proc``'s session (POSIX): background children that still hold its pipes.

    Safe only while the leader is not reaped (see :func:`_exited`): its zombie keeps the group id
    reserved, so the signal cannot reach a new session that reused the number.
    """
    if os.name == "nt":
        # no process groups; taskkill /T needs the parent alive, so only a timeout/cancel (kill_tree) ends
        # the tree and run() merely stops waiting for pipes a leftover child still holds
        return
    try:
        os.killpg(proc.pid, sig)
    except OSError:  # ProcessLookupError: nothing left; PermissionError: only zombies left (macOS)
        pass


# waitid(WNOWAIT): Linux, and macOS from Python 3.13; switched off for good if an emulated kernel refuses it
_UNREAPED_WAIT = os.name != "nt" and all(hasattr(os, n) for n in ("waitid", "P_PID", "WEXITED", "WNOWAIT", "WNOHANG"))


def _waitid_exited(pid: int, timeout: float | None) -> bool:
    flags = os.WEXITED | os.WNOWAIT
    if timeout is None:
        os.waitid(os.P_PID, pid, flags)
        return True
    end = time.monotonic() + timeout
    delay = 0.0005  # the same polling Popen.wait(timeout) does
    while os.waitid(os.P_PID, pid, flags | os.WNOHANG) is None:
        left = end - time.monotonic()
        if left <= 0:
            return False
        delay = min(delay * 2, left, 0.05)
        time.sleep(delay)
    return True


def _exited(proc: subprocess.Popen, timeout: float | None) -> bool:
    """Wait up to ``timeout`` s (``None``: no limit) for ``proc`` to exit; ``True`` once it has.

    Where ``os.waitid`` works the leader is left unreaped (``WNOWAIT``) for :func:`_end_group`;
    the caller reaps it with ``proc.wait()`` afterwards. Elsewhere it is reaped here.
    """
    global _UNREAPED_WAIT
    if _UNREAPED_WAIT:
        try:
            return _waitid_exited(proc.pid, timeout)
        except ChildProcessError:
            return True  # already reaped (Popen.poll elsewhere)
        except OSError:  # an emulated kernel without waitid(WNOWAIT): reap as usual from now on
            _UNREAPED_WAIT = False
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def run(args: Sequence[str], *, cwd=None, env=None, timeout: float | None = 120, input_text: str | None = None,
        max_output_chars: int | None = None, cancel: threading.Event | None = None) -> tuple[int, str, str]:
    """Run a command to completion, returning ``(returncode, stdout, stderr)``.

    A timeout kills the process tree and returns ``-9`` with the partial output; so does ``cancel``
    being set (checked every 0.25 s), which also appends ``[cancelled]`` to stderr. Whatever the
    command left running in the background is ended when it returns on POSIX (its whole session; on
    Windows it keeps running unless a timeout or cancel ends the tree), and this always returns
    promptly. ``max_output_chars`` bounds each of stdout and stderr: the first half and the last half
    are kept around a ``[... N characters omitted ...]`` marker (``None``: everything is kept).
    """
    if cancel is not None and cancel.is_set():
        return -9, "", CANCELLED_NOTE
    proc = popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                 text=False)
    out, err = _Output(max_output_chars), _Output(max_output_chars)
    threads: list[tuple[threading.Thread, Any]] = [
        (threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True), proc.stdout),
        (threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True), proc.stderr)]
    if input_text is not None:
        # text mode wrote os.linesep for "\n"; a thread, so a child that never reads cannot stall the timeout
        data = input_text.replace("\n", os.linesep).encode("utf-8", "replace")
        threads.append((threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True), proc.stdin))
    for t, _stream in threads:
        t.start()
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    cancelled = stopped = False
    while True:
        step = None if deadline is None else max(0.0, deadline - time.monotonic())
        if cancel is not None:
            step = WAIT_STEP_S if step is None else min(step, WAIT_STEP_S)
        if _exited(proc, step):
            break
        cancelled = cancel is not None and cancel.is_set()
        if cancelled or (deadline is not None and time.monotonic() >= deadline):
            stopped = True
            if os.name == "nt":
                kill_tree(proc, grace_s=3)  # taskkill /T /F while the parent still exists
            else:
                _end_group(proc, signal.SIGTERM)  # a moment to exit cleanly, then SIGKILL below
                _exited(proc, 3)
            break
    _end_group(proc)
    try:
        rc = proc.wait(timeout=5)  # reaps the leader (left a zombie for _end_group where waitid works)
    except Exception:  # noqa: BLE001 - reaped later by Popen.__del__
        rc = -9
    if stopped:
        rc = -9
    join_by = time.monotonic() + PUMP_JOIN_S
    for t, stream in threads:
        t.join(timeout=max(0.0, join_by - time.monotonic()))
        if t.is_alive():
            continue  # a background child still holds the pipe; closing would block on the reader's lock
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing a finished pipe must never raise
                pass
    stderr = err.text()
    if cancelled:
        stderr += ("\n" if stderr and not stderr.endswith("\n") else "") + CANCELLED_NOTE
    return rc, out.text(), stderr
