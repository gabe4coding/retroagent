"""Sync state (.kb/sync-state.json): what was processed, summary retry counts, last run."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kb.util import atomic_write


@dataclass
class State:
    path: Path
    files: dict = field(default_factory=dict)              # unit key -> fingerprint
    summary_attempts: dict = field(default_factory=dict)   # session id -> failed attempts
    last_ok: str = ""
    last_result: str = ""
    last_error: str = ""

    @classmethod
    def load(cls, path) -> "State":
        path = Path(path)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        return cls(path=path, files=data.get("files", {}), summary_attempts=data.get("summary_attempts", {}),
                   last_ok=data.get("last_ok", ""), last_result=data.get("last_result", ""),
                   last_error=data.get("last_error", ""))

    def save(self) -> None:
        data = {"files": self.files, "summary_attempts": self.summary_attempts, "last_ok": self.last_ok,
                "last_result": self.last_result, "last_error": self.last_error}
        atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
