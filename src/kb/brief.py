"""`kb brief`: what a new session in a project sees at start (the SessionStart hook; off with "brief": false).

On purpose very little, and no facts that can be out of date:
  - a pointer to the project page, with how many open threads and errors → fixes it has, so the agent knows the page
    is there and reads it with `kb page` only when the task needs it;
  - every suggestion accepted for this project's repo (kb.ledger), one line each: a change the owner wants made;
  - while the first full backfill is not done (auto_sync_pending), one line that says so: the hook starts it again;
  - when retro suggestions wait for the owner's answer (kb.decide), one line that asks the agent to tell the user,
    unless the user hid it with `kb decide later`;
  - always, last: one line that the past sessions are searchable with `kb find`. Skills trigger only when the agent
    thinks of them; without this line a project with no page gives the agent no sign that the KB exists. The project is the working directory's, as for
`kb hint` (kb.hint.projects). Reads a few small files of the data clone, never the index: it runs before each
session.
"""
from __future__ import annotations

from pathlib import Path

from kb import decide, ledger
from kb.hint import projects
from kb.pages import page_rel, parse_page, section

MAX_CHARS = 1200                 # the whole brief; suggestions past it are counted, not shown
LINE_CHARS = 220                 # one suggestion line
KB_LINE = ("Past Claude Code and Codex sessions of all projects are searchable: `kb find \"<words>\"` "
           "(skill kb-search). Look there before you debug an error seen before or redo past work.")


FIRST_SYNC_LINE = ("The first full retroagent sync is not finished: it runs again in the background now, and "
                   "automatic syncs turn on when it is done. `kb find` sees only the sessions synced so far; "
                   "`kb status` shows what is left.")


def _bullets(body: str, heading: str) -> int:
    part = section(body, heading) or ""
    return sum(1 for line in part.splitlines() if line.startswith("- "))


def page_line(root: Path, project: str) -> str:
    """The pointer to the project's page, or "" when it has none."""
    try:
        rel = page_rel("project", project)
        _, body = parse_page((Path(root) / rel).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    threads, errors = _bullets(body, "Open threads"), _bullets(body, "Errors seen")
    counts = [f"{threads} open thread{'s' * (threads != 1)}" if threads else "",
              (f"{errors} errors → fixes" if errors != 1 else "1 error → fix") if errors else ""]
    more = " (" + ", ".join(c for c in counts if c) + ")" if threads or errors else ""
    return f"Project page of {project}: `kb page {project}`{more}. Read it when the task needs it."


def accepted(root: Path, project: str) -> list:
    """The suggestions accepted for this project's repo, oldest first."""
    known, decided = ledger.load(root), ledger.decisions(root)
    rows = [(sid, e) for sid, e in known.items()
            if decided.get(sid, {}).get("state") == "accepted" and e.get("repo", "").lower() == project.lower()]
    return sorted(rows, key=lambda r: (r[1]["weeks"][0], r[0]))


def build(root, cwd: str, project: str = "", first_sync_pending: bool = False) -> str:
    """The brief for a session in `cwd` (or for `project`): the project part, FIRST_SYNC_LINE while the first full
    backfill is not done, the line about the questions that wait for the owner, then KB_LINE."""
    parts = [_project_part(Path(root), cwd, project), FIRST_SYNC_LINE if first_sync_pending else "",
             decide.brief_line(root, Path(root) / ".kb"), KB_LINE]
    return "\n".join(p for p in parts if p)


def _project_part(root: Path, cwd: str, project: str) -> str:
    """The page pointer and the accepted suggestions, or ""."""
    names = [project] if project else projects(cwd)
    # the first project that has a page, else the first non-empty name
    name = next((n for n in names if n and page_line(root, n)), "") or next((n for n in names if n), "")
    if not name:
        return ""
    head = page_line(root, name)
    todo = accepted(root, name)
    if not todo:
        return head
    lines = ([head] if head else []) + [f"Changes the owner accepted for the {name} repo (`kb suggestions`):"]
    size = sum(len(x) + 1 for x in lines) + len(KB_LINE)
    for i, (sid, e) in enumerate(todo):
        cat = e["category"] + " · " if e.get("category") else ""
        line = f"- [{sid}] {cat}{e.get('text', '')}"
        line = line if len(line) <= LINE_CHARS else line[: LINE_CHARS - 1] + "…"
        if size + len(line) + 1 > MAX_CHARS:
            lines.append(f"- … and {len(todo) - i} more")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)
