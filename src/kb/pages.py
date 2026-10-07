"""Pages: markdown the cloud routine writes under pages/ from the sessions, one per project and one retro per week.

A page is front matter (the same JSON-valued lines as a session file) and a markdown body:
  kind: "project" | "retro"     name: the project slug or the ISO week ("2026-W41")
  updated: ISO time             sessions: how many sessions it covers     sources: their short ids
The body starts with a "# " title and is split into "## " sections. The path is fixed by kind and name.
"""
from __future__ import annotations

import re

from kb.distill import split_front_matter

FOLDERS = {"project": "projects", "retro": "retro"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_TITLE = re.compile(r"^# +(.+?)\s*$", re.M)
_SECTION = re.compile(r"^## +(.+?)\s*$", re.M)


def page_rel(kind: str, name: str) -> str:
    """Repo-relative path of a page. Raises ValueError for an unknown kind or an unsafe name."""
    if kind not in FOLDERS:
        raise ValueError(f"kind must be one of {', '.join(FOLDERS)}")
    if kind == "retro":
        if not isinstance(name, str) or not WEEK_RE.match(name):
            raise ValueError(f"a retro is named by its ISO week (2026-W41), not {name!r}")
    elif not isinstance(name, str) or not NAME_RE.match(name) or name.endswith(".") or ".." in name:
        raise ValueError(f"bad page name {name!r}")
    return f"pages/{FOLDERS[kind]}/{name}.md"


def parse_page(text: str):
    """Return (meta, body) with meta = kind, name, title, updated, sessions, sources. Raises ValueError."""
    raw, body = split_front_matter(text)
    if not raw:
        raise ValueError("no front matter")
    kind, name = raw.get("kind"), raw.get("name")
    page_rel(kind, name)
    updated = raw.get("updated", "")
    if not isinstance(updated, str):
        raise ValueError("updated must be text")
    sessions = raw.get("sessions", 0)
    if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < 0:
        raise ValueError("sessions must be a whole number")
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("sources must be a list of short ids")
    m = _TITLE.search(body)
    meta = {"kind": kind, "name": name, "title": m.group(1) if m else name, "updated": updated,
            "sessions": sessions, "sources": sources}
    return meta, body


def sections(body: str) -> list:
    """The "## " sections of a body as (heading, text including the heading line)."""
    marks = list(_SECTION.finditer(body))
    return [(m.group(1), body[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(body)].rstrip())
            for i, m in enumerate(marks)]


def section(body: str, wanted: str):
    """The first section whose heading starts with `wanted` (case-insensitive), or None."""
    key = wanted.strip().casefold()
    for heading, text in sections(body):
        if heading.casefold().startswith(key):
            return text
    return None
