"""The common session model produced by every adapter."""
from __future__ import annotations

import hashlib
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
    origin: str = ""              # "" = typed by the user; "agent" = a message from another agent (Codex)

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
    headless: bool = False                          # started by `claude -p` / `codex exec`, not by a person at a prompt
    # Set only on a subagent that another session's markdown file already holds: the path of that file. The
    # subagent is then linked, not written a second time.
    elsewhere: str = ""

    @property
    def user_turns(self) -> int:
        """Human prompts. A user turn that came from another agent (origin set) is not one."""
        return sum(1 for t in self.turns if t.role == "user" and not t.origin)

    def add_turn(self, role: str, ts: str, items=None, origin: str = "") -> Turn:
        t = Turn(n=len(self.turns) + 1, role=role, ts=ts, items=list(items or []), origin=origin)
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
    key: str                # the id of the unit in the sync state: the main transcript path (Claude), "codex:<thread>"
    agent: str              # claude | codex
    paths: list             # all files of the unit; for Claude, the main transcript first, then the subagents
    main: str               # the main transcript (Claude) or the newest segment (Codex)
    # Claude only: the subagents that several sessions hold a copy of (a resumed or forked session copies them).
    # Subagent id -> {main transcript of each holder: that holder's copy}.
    shared: dict = field(default_factory=dict)
    # The other holders' main transcripts and copies. They are in the fingerprint: they decide who writes a shared one.
    related: list = field(default_factory=list)

    def fingerprint(self) -> str:
        """sha1 of path, size, mtime and ctime of every file (related files too). Raises OSError if a file is gone."""
        parts = []
        for p in sorted(map(str, self.paths + self.related)):
            st = os.stat(p)
            parts.append(f"{p}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}")
        return hashlib.sha1("|".join(parts).encode("utf-8", "surrogateescape")).hexdigest()

    def newest_mtime(self) -> float:
        return max(os.stat(p).st_mtime for p in self.paths + self.related)
