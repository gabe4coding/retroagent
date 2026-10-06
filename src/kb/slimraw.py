"""Slim raw copy: drop bulky records, omit images, cap long strings, redact, gzip (deterministic)."""
from __future__ import annotations

import gzip
import json
from collections import Counter

from kb.redact import redact

CAP = 20_000
CLAUDE_DROP = {"attachment", "file-history-snapshot", "file-history-delta"}


def _cap(obj):
    if isinstance(obj, str):
        return obj if len(obj) <= CAP else obj[:CAP] + f"[… {len(obj) - CAP} chars cut]"
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


def slim_record(agent: str, rec: dict):
    """Return the slimmed record, or None to drop it."""
    if agent == "claude":
        if rec.get("type") in CLAUDE_DROP:
            return None
    else:
        p = rec.get("payload")
        if isinstance(p, dict):
            if p.get("type") in ("reasoning", "compaction"):
                p = {k: v for k, v in p.items() if k != "encrypted_content"}
            if rec.get("type") == "session_meta":
                p = {k: v for k, v in p.items() if k != "base_instructions"}
            rec = {**rec, "payload": p}
    return _cap(_strip_images(rec))


def slim(agent: str, paths):
    """Return (gzip bytes, Counter of redactions) for the given source files, in order."""
    out, counts = [], Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                if isinstance(rec, dict):
                    rec = slim_record(agent, rec)
                    if rec is None:
                        continue
                    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
                text, c = redact(line)
                counts.update(c)
                out.append(text)
    data = ("\n".join(out) + "\n").encode("utf-8")
    return gzip.compress(data, compresslevel=9, mtime=0), counts
