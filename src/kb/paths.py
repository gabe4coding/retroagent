"""Repo-relative output paths. Subagent files use the parent's date when the parent is known."""
from __future__ import annotations

from kb.util import slug


def _date(iso: str):
    if len(iso or "") >= 10:
        return iso[:4], iso[5:7], iso[:10]
    return "0000", "00", "0000-00-00"


def md_rel(host: str, s, parent=None) -> str:
    yyyy, mm, day = _date((parent.started if parent is not None else "") or s.started)
    if s.parent:
        stem = f"{day}_{slug(s.project)}_{s.parent[:8]}_sub-{s.id[:8]}"
    else:
        stem = f"{day}_{slug(s.project)}_{s.id[:8]}"
    return f"sessions/{host}/{s.agent}/{yyyy}/{mm}/{stem}.md"


def raw_rel(host: str, s, parent=None) -> str:
    yyyy, mm, _ = _date((parent.started if parent is not None else "") or s.started)
    name = f"{s.parent}__sub-{s.id}" if s.parent else s.id
    return f"raw/{host}/{s.agent}/{yyyy}/{mm}/{name}.jsonl.gz"


def month_of(md_path: str) -> str:
    parts = md_path.split("/")          # sessions/<host>/<agent>/<YYYY>/<MM>/<file>
    return f"{parts[3]}/{parts[4]}"
