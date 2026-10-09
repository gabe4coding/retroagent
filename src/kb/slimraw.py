"""Slim raw copy: drop bulky records, omit images, redact every string, cap long strings, gzip (deterministic).

Redaction happens twice, in this order:
1. Per string leaf, on the decoded text (so a double-encoded JSON argument is seen as text, and a secret that
   straddles the length cap is redacted before the cut).
2. On the serialized line, as a backstop for secrets that need their key to be recognised
   ("password": "..."). It is used only if its result still parses as JSON.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter

from kb.redact import redact

CAP = 20_000
CLAUDE_DROP = {"attachment", "file-history-snapshot", "file-history-delta"}
DATA_URI_LIMIT = 1000
DATA_OMITTED = "[data omitted]"
LEAF_MIN = 8                      # shorter strings cannot hold a secret any rule knows


def _cap_str(s: str) -> str:
    return s if len(s) <= CAP else s[:CAP] + f"[… {len(s) - CAP} chars cut]"


def _cap(obj):
    if isinstance(obj, str):
        return _cap_str(obj)
    if isinstance(obj, list):
        return [_cap(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _cap(v) for k, v in obj.items()}
    return obj


def _strip_images(obj):
    if isinstance(obj, list):
        return [_strip_images(x) for x in obj]
    if isinstance(obj, dict):
        kind = obj.get("type")
        if kind in ("image", "input_image"):
            return {"type": kind, "omitted": True}
        url = obj.get("image_url")
        if isinstance(url, str) and url.startswith("data:"):
            return {"type": kind or "image", "omitted": True}
        return {k: _strip_images(v) for k, v in obj.items()}
    return obj


def _redact_leaf(s: str, counts: Counter) -> str:
    if len(s) < LEAF_MIN:
        return s
    if len(s) > DATA_URI_LIMIT and s.startswith("data:"):
        return DATA_OMITTED
    text, c = redact(s)
    counts.update(c)
    return text


def _redact_leaves(obj, counts: Counter):
    """Redact every string (keys too) and drop 'encrypted_content' at any depth."""
    if isinstance(obj, str):
        return _redact_leaf(obj, counts)
    if isinstance(obj, list):
        return [_redact_leaves(x, counts) for x in obj]
    if isinstance(obj, dict):
        return {_redact_leaf(k, counts): _redact_leaves(v, counts)
                for k, v in obj.items() if k != "encrypted_content"}
    return obj


def _slim_value(agent: str, rec, counts: Counter):
    """The slimmed, redacted JSON value."""
    if agent != "claude" and isinstance(rec, dict) and rec.get("type") == "session_meta" \
            and isinstance(rec.get("payload"), dict):
        rec = {**rec, "payload": {k: v for k, v in rec["payload"].items() if k != "base_instructions"}}
    return _cap(_redact_leaves(_strip_images(rec), counts))


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _slim_line(agent: str, line: str, counts: Counter):
    """Return the output line, or None to drop the record."""
    found: Counter = Counter()                       # merged into counts only when the line is used
    try:
        rec = json.loads(line)
        if agent == "claude" and isinstance(rec, dict) and rec.get("type") in CLAUDE_DROP:
            return None
        out = _dumps(_slim_value(agent, rec, found))
    except (ValueError, RecursionError):
        text, c = redact(line)                       # not JSON (or too deep to walk): redact, then cap
        counts.update(c)
        return _cap_str(text)
    counts.update(found)
    text, c = redact(out)
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return out                                   # the backstop must never break the line
    counts.update(c)
    return text


def slim(agent: str, paths):
    """Return (gzip bytes, Counter of redactions) for the given source files, in order."""
    out, counts = [], Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                res = _slim_line(agent, line, counts)
                if res is not None:
                    out.append(res)
    data = ("\n".join(out) + "\n").encode("utf-8", errors="replace")
    return gzip.compress(data, compresslevel=9, mtime=0), counts
