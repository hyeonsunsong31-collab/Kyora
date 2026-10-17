"""One running Kyora per KYORA_HOME.

``App.start()`` marks runs left in an active state as interrupted (blocked). A second Kyora
started on the same home - another port, a ``kyora bench`` next to ``kyora serve``, or a
second listener that the OS let share the port (macOS/BSD ``SO_REUSEADDR`` rules) - would
therefore block the first instance's live runs. The CLI takes this advisory lock before
``app.start()`` and keeps it for the life of the process; the OS drops it when the process
exits, so a crash never leaves a stale lock.

POSIX uses ``flock`` on ``KYORA_HOME/kyora.lock``; Windows locks its first byte with
``msvcrt.locking``. Where the platform has no working lock call (some emulators), Kyora logs
that and runs without it rather than refusing to start.
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

log = logging.getLogger("kyora.instance_lock")

LOCK_NAME = "kyora.lock"
PID_NAME = "kyora.pid"
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLK", errno.EAGAIN)}


class InstanceLock:
    def __init__(self, home: Path | str):
        self.home = Path(home)
        self.path = self.home / LOCK_NAME
        self._fh = None
        self.supported = True

    def acquire(self) -> bool:
        """True when this process now holds the lock (or locking is unsupported here)."""
        self.home.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            fh.close()
            return False
        except OSError as exc:
            if exc.errno in _BUSY_ERRNOS:
                fh.close()
                return False
            log.warning("cannot lock %s on this platform (%s); a second Kyora on this home is not prevented",
                        self.path, exc)
            self.supported = False
        self._fh = fh
        try:
            (self.home / PID_NAME).write_text(f"{os.getpid()}\n", encoding="ascii")
        except OSError as exc:
            log.debug("cannot write %s: %s", PID_NAME, exc)
        return True

    def holder_pid(self) -> str:
        """The pid recorded by the instance that holds the lock ('' when unknown)."""
        try:
            return (self.home / PID_NAME).read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            fh.close()


__all__ = ["InstanceLock", "LOCK_NAME"]
