"""Freshness of project page bullets: the date each fact was last confirmed, and the move of old facts to History.

Each bullet of a dated section ends with its sources and a date, in parentheses:
  - <text> (<short>, <short>, memory <ref> · YYYY-MM-DD)

`kb pages finish` computes the date from the index: the newest of the sources' dates. A session's date is its start.
A memory's date is its modified time, else the start of the session that wrote it. The date never comes from the
writer: stamp() replaces any date it finds. A bullet whose sources cannot be found gets no date.

A bullet is stale when its date is more than N days older than the newest bullet date on the same page. Age is
relative to the page, not to today. So a dormant project keeps its page, and a page that is not written cannot become
stale. N depends on the section (the defaults are below; pages/config.json can change them):
- "Current state": stale_days_current (STALE_DAYS_CURRENT, 30).
- "Open threads": stale_days_threads (STALE_DAYS_THREADS, 30).
- "Errors seen": stale_days (STALE_DAYS, 90).
"Key decisions" and "Important files" never become stale: they stay true until a session replaces them.

sweep() moves the stale bullets to History as:
  - unconfirmed since YYYY-MM-DD (<section>): <text> (<sources · date>)

Nothing here imports the index: `kb hint` reads its bullets with parse_tail() and their age with is_stale().
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATED = ("Current state", "Key decisions", "Important files", "Errors seen", "Open threads")
AGING = ("Current state", "Errors seen", "Open threads")     # "Key decisions" and "Important files" never age out
HISTORY = "History"
STALE_DAYS = 90                  # default of pages/config.json `stale_days`
STALE_DAYS_CURRENT = 30          # default of pages/config.json `stale_days_current`
STALE_DAYS_THREADS = 30          # default of pages/config.json `stale_days_threads`
_TAIL = re.compile(r"\s*\(([^()]*)\)(\.?)\s*$")       # a writer sometimes ends the bullet with a period
_DATE = re.compile(r"\s*·\s*(\d{4}-\d{2}-\d{2})\s*$")
_SHORT = re.compile(r"\b[0-9a-f]{8}\b")
_MEMORY = re.compile(r"\bmemory\s+([^\s,·]+)")
_HEADING = re.compile(r"^## +(.+?)\s*$")


@dataclass
class Ref:
    shorts: list = field(default_factory=list)       # 8-character session ids
    memories: list = field(default_factory=list)     # memory refs, maybe cut
    date: str = ""                                   # the date in the parentheses, "" when none


def parse_tail(line: str):
    """(text before the parentheses, Ref, parentheses content without its date) of a "- " bullet, or
    (line, None, "") when it cites no session and no memory. A period after the parentheses is dropped."""
    m = _TAIL.search(line)
    if not m:
        return line, None, ""
    inner = m.group(1)
    d = _DATE.search(inner)
    content = inner[: d.start()].rstrip() if d else inner.strip()
    ref = Ref(_SHORT.findall(content), _MEMORY.findall(content), d.group(1) if d else "")
    if not (ref.shorts or ref.memories):
        return line, None, ""
    return line[: m.start()].rstrip(), ref, content


def bullet_date(line: str) -> str:
    """The date in a bullet's parentheses, or ""."""
    ref = parse_tail(line)[1]
    return ref.date if ref else ""


def _key(heading: str, keys) -> str:
    """The key of keys that heading starts with (case-insensitive, like kb.pages.section), or ""."""
    low = heading.casefold()
    return next((k for k in keys if low.startswith(k.casefold())), "")


def _walk(body: str):
    """(line, the DATED key of the section it is in or "", the heading of that section) for each line."""
    key, heading = "", ""
    for line in body.split("\n"):
        h = _HEADING.match(line)
        if h:
            heading = h.group(1)
            key = _key(heading, DATED)
        yield line, (key if not h else ""), heading


def stamp(body: str, lookup):
    """(body with every bullet of a DATED section dated by lookup(Ref) -> "YYYY-MM-DD" or "", the bullets left
    undated). Other lines are kept as they are; a date the writer put in the parentheses is replaced."""
    out, undated = [], []
    for line, key, _ in _walk(body):
        if key and line.startswith("- "):
            text, ref, content = parse_tail(line)
            if ref is None:
                undated.append(line[2:])
            else:
                date = lookup(ref)
                line = f"{text} ({content} · {date})" if date else f"{text} ({content})"
                if not date:
                    undated.append(line[2:])
        out.append(line)
    return "\n".join(out), undated


def _day(s: str):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def is_stale(date: str, newest: str, days: int) -> bool:
    """True when date is more than days older than newest. A missing or bad date is never stale."""
    a, b = _day(date), _day(newest)
    return a is not None and b is not None and (b - a).days > days


def newest(body: str) -> str:
    """The newest bullet date of the DATED sections of a body, or ""."""
    dates = [bullet_date(line) for line, key, _ in _walk(body) if key and line.startswith("- ")]
    return max((d for d in dates if _day(d)), default="")


def sweep(body: str, stale_days: int = STALE_DAYS, stale_days_current: int = STALE_DAYS_CURRENT,
          stale_days_threads: int = STALE_DAYS_THREADS):
    """(body with the stale bullets of the AGING sections moved to the end of History, the moved History lines).
    History is added at the end when the page has none."""
    newest_date = newest(body)
    limit_by_section = {"Current state": stale_days_current, "Open threads": stale_days_threads}
    keep, moved = [], []
    for line, key, _ in _walk(body):
        if key in AGING and line.startswith("- "):
            text, ref, content = parse_tail(line)
            if ref is not None and is_stale(ref.date, newest_date, limit_by_section.get(key, stale_days)):
                moved.append(f"- unconfirmed since {ref.date} ({key}): {text[2:]} ({content} · {ref.date})")
                continue
        keep.append(line)
    if not moved:
        return body, []
    _append_to_history(keep, moved)
    return "\n".join(keep), moved


def _append_to_history(lines: list, moved: list) -> None:
    """Add the moved lines at the end of the History section of lines (in place). Add the section when it is missing."""
    start = next((i for i, l in enumerate(lines) if _HEADING.match(l) and _key(_HEADING.match(l).group(1),
                                                                               (HISTORY,))), None)
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"## {HISTORY}"] + moved + [""]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
        last = end
        while last > start + 1 and not lines[last - 1].strip():
            last -= 1
        lines[last:last] = moved


def index_lookup(idx):
    """lookup(Ref) for stamp(): the newest date of a bullet's sources in the index, or "". A short id that matches
    no session or several is skipped. A memory ref may be cut by the writer, so a ref also matches by its start. A
    memory with no modified time counts with the start of the session that wrote it, else not at all. (Not with the
    day a sync committed it: a first sync commits old memories on one day, which would make the whole page look
    newer than it is and age out its other bullets.)"""
    from kb.index import AmbiguousId

    def lookup(ref: Ref) -> str:
        dates = []
        for short in ref.shorts:
            try:
                row = idx.get(short)
            except (AmbiguousId, ValueError):
                row = None
            if row and row.get("started"):
                dates.append(row["started"][:10])
        for mref in ref.memories:
            rows = idx.db.execute(
                "SELECT m.modified, s.started FROM memories m LEFT JOIN sessions s ON s.id = m.origin_session "
                "WHERE m.ref = ? OR substr(lower(m.ref), 1, ?) = lower(?)", (mref, len(mref), mref)).fetchall()
            dates += [(r[0] or r[1] or "")[:10] for r in rows]
        return max((d for d in dates if _day(d)), default="")

    return lookup


def stale_settings(root) -> tuple:
    """(stale_days, stale_days_current) from <root>/pages/config.json, each its default when missing or not a whole
    number of at least 1. For readers like `kb hint`; `kb pages finish` reads them with kb.routine.load_settings."""
    try:
        raw = json.loads((Path(root) / "pages" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}

    def whole(key, default):
        v = raw.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else default

    return whole("stale_days", STALE_DAYS), whole("stale_days_current", STALE_DAYS_CURRENT)
