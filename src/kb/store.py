"""Write one session (and its subagents) as distilled markdown + slim raw under this host's folders."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from kb.distill import SUMMARY_FIELDS, render_markdown, split_front_matter
from kb.paths import md_rel, raw_rel
from kb.redact import redact
from kb.slimraw import slim
from kb.util import atomic_write


def _summary_fields(path: Path) -> dict:
    if not path.exists():
        return {}
    meta, _ = split_front_matter(path.read_text(encoding="utf-8"))
    return {k: meta[k] for k in SUMMARY_FIELDS if k in meta}


def write_session(root, host: str, s, redactions: Counter, dry_run: bool = False, known=None):
    """Return (md paths relative to root, Counter of output sizes). known: id -> current md path (from the index)."""
    root = Path(root)
    known = known or {}
    sub_files = {sub.id: Path(md_rel(host, sub, parent=s)).name for sub in s.subagents}
    written, sizes = [], Counter()
    for sess, parent in [(s, None)] + [(sub, s) for sub in s.subagents]:
        mrel, rrel = md_rel(host, sess, parent), raw_rel(host, sess, parent)
        old = known.get(sess.id, "")
        keep = _summary_fields(root / mrel) or (_summary_fields(root / old) if old else {})
        md, c1 = redact(render_markdown(sess, host, keep, sub_files if parent is None else {}, raw=rrel))
        raw, c2 = slim(sess.agent, sess.source_paths)
        redactions.update(c1)
        redactions.update(c2)
        sizes["md"] += len(md.encode("utf-8"))
        sizes["raw"] += len(raw)
        if not dry_run:
            atomic_write(root / mrel, md.encode("utf-8"))
            atomic_write(root / rrel, raw)
            if old and old != mrel and (root / old).exists():
                (root / old).unlink()
        written.append(mrel)
    return written, sizes
