"""The common session model produced by every adapter."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from kb.util import first_line


@dataclass
class ToolCall:
    name: str
    arg: str = ""
    status: str = "ok"            # ok | error
    error_head: str = ""
    diff: str = ""                # e.g. "+3 −2"
    subagent_id: str = ""
    subagent_note: str = ""


@dataclass
class Turn:
    n: int
    role: str                     # user | assistant
    ts: str
    items: list = field(default_factory=list)   # str (text) or ToolCall, in order

    @property
    def text(self) -> str:
        return "\n\n".join(i for i in self.items if isinstance(i, str))


@dataclass
class Session:
    id: str
    agent: str                    # claude | codex
    source_paths: list = field(default_factory=list)
    cwd: str = ""
    project: str = ""
    branch: str = ""
    started: str = ""
    ended: str = ""
    model: str = ""
    title: str = ""
    parent: str = ""
    turns: list = field(default_factory=list)
    files: list = field(default_factory=list)
    prs: list = field(default_factory=list)
    subagents: list = field(default_factory=list)   # list[Session] (Claude only)
    skipped: dict = field(default_factory=dict)     # unhandled record type -> count

    @property
    def user_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")

    def add_turn(self, role: str, ts: str, items=None) -> Turn:
        t = Turn(n=len(self.turns) + 1, role=role, ts=ts, items=list(items or []))
        self.turns.append(t)
        return t

    def first_prompt(self, limit: int = 80) -> str:
        for t in self.turns:
            if t.role == "user":
                return first_line(t.text, limit)
        return ""


@dataclass
class Unit:
    """The files that make up one session on disk (main + subagents, or Codex segments)."""
    key: str
    agent: str
    paths: list
    main: str

    def fingerprint(self) -> str:
        parts = []
        for p in sorted(self.paths):
            st = os.stat(p)
            parts.append(f"{p}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}")
        return "|".join(parts)

    def newest_mtime(self) -> float:
        return max(os.stat(p).st_mtime for p in self.paths)
