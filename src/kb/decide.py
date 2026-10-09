"""`kb decide`: the questions that wait for the owner, and the owner's answers.

Two kinds of questions:
- a suggestion of the weekly retros (kb.ledger, id s-…) with no decision yet ("proposed"). An accepted suggestion
  shows in the sessions of its repo (`kb brief`). Every machine lists them.
- a memory fix the pages routine proposed (kb.memedits, id m-…). Only the machine that owns the memory's host lists
  it, because accepting it changes that machine's memory file.

Where the answers go:
- decisions/<host>/answers.jsonl in the data clone: one JSON line per answer (kb.ledger.parse_answer). Only this
  host's sync commits it (kb.sync.own_paths), so nothing else ever writes the file.
- .kb/answers.jsonl: a local copy. `kb repair` resets this host's folders to the remote and drops what was not
  pushed yet; the next sync then adds back each line of the copy that the file lost (restore()).
An answer never changes or deletes a line: the newest answer for a suggestion counts.

Only a person answers. `kb decide accept|reject` refuses to run without a terminal (cli.cmd_decide), so an agent in a
session cannot answer for the owner. Session text can hold instructions from others; an accepted suggestion reaches
every later session of its repo.

Later: `kb decide later` hides the brief line about the waiting questions on this machine (.kb/decide.json) for some
days. A question that arrives in that time shows the line again.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from kb import ledger, memedits
from kb.util import atomic_write

BACKUP = "answers.jsonl"                 # in .kb/: the local copy of this host's answers
LATER = "decide.json"                    # in .kb/: {"until": "YYYY-MM-DD", "ids": [...]}
LATER_DAYS = 1


def questions(root, host: str = "") -> tuple:
    """(ids of the suggestions with no decision, (id, entry) of the memory fixes that wait for the owner of `host`).
    With no host, no memory fixes."""
    decided = ledger.decisions(root)
    fixes = memedits.waiting(root, host) if host else []
    return [sid for sid in ledger.load(root) if sid not in decided], fixes


def answer(root, kb_dir, host: str, sid: str, state: str, note: str = "", now=None) -> dict:
    """Record the owner's answer for one suggestion or memory fix and return it. ValueError for an unknown id or
    state. It applies nothing: the caller applies an accepted memory fix first (kb.memedits.apply)."""
    if state not in ledger.ANSWER_STATES:
        raise ValueError(f"answer must be one of {', '.join(ledger.ANSWER_STATES)}")
    known = memedits.load(root) if memedits.is_id(sid) else ledger.load(root)
    if sid not in known:
        raise ValueError(f"no question {sid}; `kb decide` lists the ones that wait for you")
    now = now or dt.datetime.now(dt.timezone.utc)
    record = {"id": sid, "state": state, "date": now.astimezone().date().isoformat(),
              "at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "note": " ".join(note.split())}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    for path in (Path(kb_dir) / BACKUP, Path(root) / ledger.answers_rel(host)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    return record


def restore(root, kb_dir, host: str) -> int:
    """Add back to this host's answers file each line of the local copy that it lost. Returns how many."""
    try:
        kept = (Path(kb_dir) / BACKUP).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    path = Path(root) / ledger.answers_rel(host)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    have = set(text.splitlines())
    lost = [line for line in dict.fromkeys(kept) if line and line not in have and ledger.parse_answer(line)]
    if lost:
        path.parent.mkdir(parents=True, exist_ok=True)
        head = text if not text or text.endswith("\n") else text + "\n"
        atomic_write(path, (head + "".join(line + "\n" for line in lost)).encode("utf-8"))
    return len(lost)


def later(kb_dir, ids, days: int = LATER_DAYS, today=None) -> str:
    """Hide the brief line for these waiting ids for `days` days. Returns the first day it shows again."""
    today = today or dt.date.today()
    until = (today + dt.timedelta(days=max(days, 1))).isoformat()
    data = {"until": until, "ids": sorted(ids)}
    atomic_write(Path(kb_dir) / LATER, (json.dumps(data) + "\n").encode("utf-8"))
    return until


def hidden_until(kb_dir, ids, today=None) -> str:
    """The day the brief line shows again when `kb decide later` hides all of `ids` today, else ""."""
    today = (today or dt.date.today()).isoformat()
    try:
        data = json.loads((Path(kb_dir) / LATER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or not isinstance(data.get("until"), str) or not isinstance(data.get("ids"), list):
        return ""
    return data["until"] if today < data["until"] and set(ids) <= set(data["ids"]) else ""


def brief_line(root, kb_dir, host: str = "", today=None) -> str:
    """The brief's line about the questions that wait for the owner, or ""."""
    suggestions, fixes = questions(root, host)
    ids = suggestions + [eid for eid, _ in fixes]
    if not ids or hidden_until(kb_dir, ids, today):
        return ""
    parts = [f"{n} {one if n == 1 else many}" for n, one, many in (
        (len(suggestions), "retro suggestion", "retro suggestions"), (len(fixes), "memory fix", "memory fixes")) if n]
    verb = "waits" if len(ids) == 1 else "wait"
    return (f"{' and '.join(parts)} {verb} for the user to accept or reject (`kb decide` lists them). At a natural "
            "pause, tell the user in one line. Never answer for the user: only they run `kb decide accept|reject`, "
            "in their own terminal.")
