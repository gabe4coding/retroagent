"""An exclusive lock on a file (fcntl.flock). The kernel drops it when the process dies."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


class Lock:
    """Held while this object has the lock file open and flock'ed. The file is never deleted.

    A crashed or killed holder cannot leave a stale lock: the kernel releases it. The file content
    ({"pid", "started"}) is informational only. `stale_seconds` is accepted for compatibility and unused.
    """

    def __init__(self, path, stale_seconds: int = 1800):
        self.path = Path(path)
        self.stale = stale_seconds
        self.held = False
        self._fd = None

    def acquire(self) -> bool:
        if self.held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)  # not inherited by child processes
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": os.getpid(), "started": time.time()}).encode("utf-8"))
        except OSError:
            pass  # informational only
        self._fd = fd
        self.held = True
        return True

    def touch(self) -> None:
        """No-op: a held flock cannot go stale. Kept so long runs can keep calling it."""

    def release(self) -> None:
        if not self.held:
            return
        fd, self._fd, self.held = self._fd, None, False
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
