"""Repo-relative output paths. Subagent files use the parent's date when the parent is known.

Ids and dates come from transcript files, so nothing from them reaches a path unchecked: ids are cut down to
[A-Za-z0-9_-] and a start time must look like YYYY-MM-DD, or the session goes to the 0000/00 bucket.
"""
from __future__ import annotations

import re

from kb.util import short_id, slug

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _safe(text: str) -> str:
    return _UNSAFE.sub("_", text or "")


def _short(sid: str) -> str:
    return _safe(short_id(sid))


def _date(iso: str):
    if _DAY.match(iso or ""):
        return iso[:4], iso[5:7], iso[:10]
    return "0000", "00", "0000-00-00"


def md_rel(host: str, s, parent=None) -> str:
    yyyy, mm, day = _date((parent.started if parent is not None else "") or s.started)
    if s.parent:
        stem = f"{day}_{slug(s.project)}_{_short(s.parent)}_sub-{_short(s.id)}"
    else:
        stem = f"{day}_{slug(s.project)}_{_short(s.id)}"
    return f"sessions/{host}/{s.agent}/{yyyy}/{mm}/{stem}.md"


def raw_rel(host: str, s, parent=None) -> str:
    yyyy, mm, _ = _date((parent.started if parent is not None else "") or s.started)
    name = f"{_safe(s.parent)}__sub-{_safe(s.id)}" if s.parent else _safe(s.id)
    return f"raw/{host}/{s.agent}/{yyyy}/{mm}/{name}.jsonl.gz"


def month_of(md_path: str) -> str:
    parts = md_path.split("/")          # sessions/<host>/<agent>/<YYYY>/<MM>/<file>
    return f"{parts[3]}/{parts[4]}"
