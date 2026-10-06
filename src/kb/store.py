"""Write one session (and its subagents) as distilled markdown + slim raw under this host's folders."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from kb.distill import SUMMARY_FIELDS, render_markdown, split_front_matter
from kb.paths import md_rel, month_of, raw_rel
from kb.redact import redact
from kb.slimraw import slim
from kb.util import atomic_write


class PathCollision(Exception):
    """The markdown file a session would be written to already holds another session."""


def _meta(path: Path) -> dict:
    """Front matter of a markdown file. {} when the file is missing, unreadable or has no front matter."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return split_front_matter(text)[0]


def _summary_fields(meta: dict) -> dict:
    return {k: meta[k] for k in SUMMARY_FIELDS if k in meta}


def _rank(fields: dict):
    """Which summary is better: one with text beats a blank one, then the one that covers more turns."""
    turns = fields.get("summary_turns")
    turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else 0
    return bool(str(fields.get("summary") or "").strip()), turns


def _old_raw(root: Path, host: str, value, new_rel: str):
    """The raw file an old markdown points to, if it is one of this host's raw files and not the new one."""
    if not isinstance(value, str) or value == new_rel or not value.endswith(".jsonl.gz"):
        return None
    parts = value.split("/")                      # raw/<host>/<agent>/<YYYY>/<MM>/<name>
    if len(parts) != 6 or parts[0] != "raw" or parts[1] != host or any(p in ("", ".", "..") for p in parts):
        return None
    return root / value


def write_session(root, host: str, s, redactions: Counter, dry_run: bool = False, known=None, touched=None):
    """Return (md paths relative to root, Counter of output sizes).

    known: id -> current md path (from the index). When a session's path changes, the old markdown and the raw file it
    names are removed, its summary fields move to the new file (the better of the two files wins), and the old month
    is added to `touched` (a set) so the caller can rebuild that month's catalog.
    Raises PathCollision, before anything is written, when a target markdown file holds a different session.
    """
    root = Path(root)
    known = known or {}
    sub_files = {sub.id: Path(md_rel(host, sub, parent=s)).name for sub in s.subagents}
    plan, owners = [], {}
    for sess, parent in [(s, None)] + [(sub, s) for sub in s.subagents]:
        mrel, rrel = md_rel(host, sess, parent), raw_rel(host, sess, parent)
        here = _meta(root / mrel) if (root / mrel).exists() else None
        if owners.setdefault(mrel, sess.id) != sess.id:
            raise PathCollision(f"{mrel} is the file of two sessions, {owners[mrel]} and {sess.id}")
        if here is not None and here.get("id") != sess.id:
            raise PathCollision(f"{mrel} already holds session {here.get('id') or '(no id)'}, not {sess.id}; "
                                f"not overwriting it")
        old = known.get(sess.id, "")
        old_meta = {}
        if old and old != mrel:
            old_meta = _meta(root / old)
            if old_meta.get("id") != sess.id:       # gone, or no longer this session's file: leave it alone
                old_meta = {}
        keep = max([_summary_fields(here or {}), _summary_fields(old_meta)], key=_rank)
        plan.append((sess, parent, mrel, rrel, old, old_meta, keep))
    written, sizes = [], Counter()
    for sess, parent, mrel, rrel, old, old_meta, keep in plan:
        md, c1 = redact(render_markdown(sess, host, keep, sub_files if parent is None else {}, raw=rrel))
        raw, c2 = slim(sess.agent, sess.source_paths)
        redactions.update(c1)
        redactions.update(c2)
        md_bytes = md.encode("utf-8", errors="replace")      # a lone surrogate in a transcript must not stop the write
        sizes["md"] += len(md_bytes)
        sizes["raw"] += len(raw)
        if not dry_run:
            atomic_write(root / mrel, md_bytes)
            atomic_write(root / rrel, raw)
            if old and old != mrel:
                if old_meta:                                  # raw first: a crash in between is repaired by the next run
                    stale_raw = _old_raw(root, host, old_meta.get("raw"), rrel)
                    if stale_raw is not None and stale_raw.exists():
                        stale_raw.unlink()
                    (root / old).unlink()
                if touched is not None:
                    try:
                        touched.add(month_of(old))
                    except IndexError:
                        pass
        written.append(mrel)
    return written, sizes
