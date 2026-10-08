"""Sync state (.kb/sync-state.json): what was processed, raw copies that wait, summary retry counts, quarantined
files, last run."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kb.util import atomic_write


@dataclass
class State:
    """What the sync remembers between runs. The field names are the JSON keys on disk.

    A unit is the set of files of one session on disk: a transcript plus its subagent files (kb.model.Unit).
    Its fingerprint is a hash of the paths, sizes and times of those files: a new fingerprint means the unit changed.
    """
    path: Path
    files: dict = field(default_factory=dict)              # unit key -> fingerprint when done: skip it until it changes
    raw_pending: dict = field(default_factory=dict)        # unit key -> fingerprint whose markdown is written, raw not yet
    summary_attempts: dict = field(default_factory=dict)   # "<session id>:<turns>" -> unusable answers: stop retrying
    quarantine: dict = field(default_factory=dict)         # repo path -> first-seen ISO time (held back by gitleaks)
    last_ok: str = ""                                      # time of the last run that finished (kb status)
    last_result: str = ""                                  # the report line of that run (kb status)
    last_error: str = ""                                   # the first error of the last run, or "" (kb status)

    @classmethod
    def load(cls, path) -> "State":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        def _dict(key):
            v = data.get(key)
            return v if isinstance(v, dict) else {}

        def _times(key):
            return {str(k): v if isinstance(v, str) else "" for k, v in _dict(key).items()}

        def _text(key):
            v = data.get(key)
            return v if isinstance(v, str) else ""

        return cls(path=path, files=_dict("files"), raw_pending=_dict("raw_pending"),
                   summary_attempts=_dict("summary_attempts"), quarantine=_times("quarantine"), last_ok=_text("last_ok"),
                   last_result=_text("last_result"), last_error=_text("last_error"))

    def save(self) -> None:
        data = {"files": self.files, "raw_pending": self.raw_pending, "summary_attempts": self.summary_attempts,
                "quarantine": self.quarantine, "last_ok": self.last_ok, "last_result": self.last_result,
                "last_error": self.last_error}
        atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
