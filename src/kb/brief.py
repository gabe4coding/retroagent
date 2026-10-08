"""`kb brief`: what a new session in a project sees at start (the SessionStart hook, with "brief": true).

On purpose very little, and no facts that can be out of date:
  - a pointer to the project page, with how many open threads and errors → fixes it has, so the agent knows the page
    is there and reads it with `kb page` only when the task needs it;
  - every suggestion accepted for this project's repo (kb.ledger), one line each: a change the owner wants made.
Nothing when the project has no page and no accepted suggestion. The project is the working directory's, as for
`kb hint` (kb.hint.projects). Reads three files of the data clone, never the index: it runs before each session.
"""
from __future__ import annotations

from pathlib import Path

from kb import ledger
from kb.hint import projects
from kb.pages import page_rel, parse_page, section

MAX_CHARS = 1200                 # the whole brief; suggestions past it are counted, not shown
LINE_CHARS = 220                 # one suggestion line


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
    n, m = _bullets(body, "Open threads"), _bullets(body, "Errors seen")
    counts = [f"{n} open thread{'s' * (n != 1)}" if n else "",
              (f"{m} errors → fixes" if m != 1 else "1 error → fix") if m else ""]
    more = " (" + ", ".join(c for c in counts if c) + ")" if n or m else ""
    return f"Project page of {project}: `kb page {project}`{more}. Read it when the task needs it."


def accepted(root: Path, project: str) -> list:
    """The suggestions accepted for this project's repo, oldest first."""
    known, decided = ledger.load(root), ledger.decisions(root)
    rows = [(sid, e) for sid, e in known.items()
            if decided.get(sid, {}).get("state") == "accepted" and e.get("repo", "").lower() == project.lower()]
    return sorted(rows, key=lambda r: (r[1]["weeks"][0], r[0]))


def build(root, cwd: str, project: str = "") -> str:
    """The brief for a session in `cwd` (or for `project`), or ""."""
    root = Path(root)
    names = [project] if project else projects(cwd)
    name = next((n for n in names if n and page_line(root, n)), "") or next((n for n in names if n), "")
    if not name:
        return ""
    head = page_line(root, name)
    todo = accepted(root, name)
    if not todo:
        return head
    lines = ([head] if head else []) + [f"Changes the owner accepted for the {name} repo (`kb suggestions`):"]
    size = sum(len(x) + 1 for x in lines)
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
