"""A lock file with stale takeover. The holder touches it during long runs."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class Lock:
    def __init__(self, path, stale_seconds: int = 1800):
        self.path = Path(path)
        self.stale = stale_seconds
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age < self.stale:
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w") as fh:
                json.dump({"pid": os.getpid(), "started": time.time()}, fh)
            self.held = True
            return True
        return False

    def touch(self) -> None:
        if self.held:
            try:
                os.utime(self.path, None)
            except FileNotFoundError:
                pass

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False
