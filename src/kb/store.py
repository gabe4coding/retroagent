"""Write one session and its subagents into this host's folders of the data clone.

Each session gives two files:
- distilled markdown (sessions/<host>/…/*.md): front matter with the session's fields, then the turns as readable
  text (see kb.distill). This is what the index and people read.
- slim raw copy (raw/<host>/…/*.jsonl.gz): the transcript with bulky parts left out, redacted and gzipped
  (see kb.slimraw).

Both are redacted before they are written: secrets are replaced, and the counts go into `redactions`.
"""
from __future__ import annotations

import posixpath
from collections import Counter
from pathlib import Path

from kb.distill import SUMMARY_FIELDS, render_markdown, split_front_matter
from kb.paths import md_rel, month_of, raw_rel
from kb.redact import redact
from kb.slimraw import slim
from kb.util import atomic_write


class PathCollision(Exception):
    """The markdown file a session would be written to already holds another session."""


def read_meta(path: Path) -> dict:
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


def _sub_link(host: str, s, sub) -> str:
    """Link target from a session's markdown to its subagent: a file name, or a relative path to another session's."""
    if sub.elsewhere:
        return posixpath.relpath(sub.elsewhere, posixpath.dirname(md_rel(host, s)))
    return Path(md_rel(host, sub, parent=s)).name


def _drop_copy(root: Path, host: str, s, sub, touched) -> None:
    """Remove the copy an older sync wrote, under this session, of a subagent that another session's file now holds.
    The copy stays until that file is there."""
    mine = md_rel(host, sub, parent=s)
    if mine == sub.elsewhere:
        return
    kept, copy = read_meta(root / sub.elsewhere), read_meta(root / mine)
    if kept.get("id") != sub.id or copy.get("id") != sub.id:
        return
    stale_raw = _old_raw(root, host, copy.get("raw"), kept.get("raw"))       # never the kept file's raw
    if stale_raw is not None and stale_raw.exists():
        stale_raw.unlink()
    (root / mine).unlink()
    if touched is not None:
        touched.add(month_of(mine))


def write_session(root, host: str, s, redactions: Counter, dry_run: bool = False, known=None, touched=None,
                  raw: bool = True):
    """Write the markdown (and the raw copy) of a session and of each of its subagents.

    Returns (md paths relative to root, Counter of output sizes in bytes under "md" and "raw").
    - known: id -> current md path (from the index). When a session's path changes:
      the old markdown and the raw file it names are removed;
      its summary fields move to the new file (the better summary of the two files wins);
      the old month is added to `touched` (a set), so the caller can rebuild that month's catalog.
    - A subagent with `elsewhere` set belongs to another session's file. It is only linked. A copy of it that an
      older sync wrote under this session is removed.
    - raw=False: the raw copies wait (the caller writes them once the session settles). An existing raw copy stays
      as it is. The markdown is the same either way: it names its raw path even before that file exists.
    - Exception to raw=False: a session that moves away from a raw copy it already has gets its new copy now. So no
      stale copy stays behind.
    - dry_run: compute everything, write and remove nothing.
    - Raises PathCollision, before anything is written, when a target markdown file holds a different session.
    """
    root = Path(root)
    sub_files = {sub.id: _sub_link(host, s, sub) for sub in s.subagents}
    plan = _plan_writes(root, host, s, known or {})
    written, sizes = [], Counter()
    for entry in plan:
        _write_one(root, host, entry, sub_files, redactions, sizes, dry_run, raw, touched)
        written.append(entry[2])
    if not dry_run:
        for sub in s.subagents:
            if sub.elsewhere:
                _drop_copy(root, host, s, sub, touched)
    return written, sizes


def _plan_writes(root: Path, host: str, s, known: dict) -> list:
    """Check the targets and read the old files, before anything is written. Raises PathCollision.

    One entry per file to write: (session, parent, md_path, raw_path, old md path, old front matter, summary to keep).
    """
    plan, owners = [], {}
    for sess, parent in [(s, None)] + [(sub, s) for sub in s.subagents if not sub.elsewhere]:
        md_path, raw_path = md_rel(host, sess, parent), raw_rel(host, sess, parent)
        here = read_meta(root / md_path) if (root / md_path).exists() else None
        if owners.setdefault(md_path, sess.id) != sess.id:
            raise PathCollision(f"{md_path} is the file of two sessions, {owners[md_path]} and {sess.id}")
        if here is not None and here.get("id") != sess.id:
            raise PathCollision(f"{md_path} already holds session {here.get('id') or '(no id)'}, not {sess.id}; "
                                f"not overwriting it")
        old = known.get(sess.id, "")
        old_meta = {}
        if old and old != md_path:
            old_meta = read_meta(root / old)
            if old_meta.get("id") != sess.id:       # gone, or no longer this session's file: leave it alone
                old_meta = {}
        keep = max([_summary_fields(here or {}), _summary_fields(old_meta)], key=_rank)
        plan.append((sess, parent, md_path, raw_path, old, old_meta, keep))
    return plan


def _write_one(root: Path, host: str, entry: tuple, sub_files: dict, redactions: Counter, sizes: Counter,
               dry_run: bool, raw: bool, touched) -> None:
    """Write the markdown and, when due, the raw copy of one plan entry. Remove the old files of a moved session."""
    sess, parent, md_path, raw_path, old, old_meta, keep = entry
    md, md_redactions = redact(render_markdown(sess, host, keep, sub_files if parent is None else {}, raw=raw_path))
    redactions.update(md_redactions)
    md_bytes = md.encode("utf-8", errors="replace")      # a lone surrogate in a transcript must not stop the write
    sizes["md"] += len(md_bytes)
    stale_raw = _old_raw(root, host, old_meta.get("raw"), raw_path) if old_meta else None
    stale_raw = stale_raw if stale_raw is not None and stale_raw.exists() else None
    data = None
    if raw or stale_raw is not None:
        data, raw_redactions = slim(sess.agent, sess.source_paths)
        redactions.update(raw_redactions)
        sizes["raw"] += len(data)
    if dry_run:
        return
    atomic_write(root / md_path, md_bytes)
    if data is not None:
        atomic_write(root / raw_path, data)
    if old and old != md_path:
        if old_meta:                                  # raw first: a crash in between is repaired by the next run
            if stale_raw is not None:
                stale_raw.unlink()
            (root / old).unlink()
        if touched is not None:
            try:
                touched.add(month_of(old))
            except IndexError:
                pass
