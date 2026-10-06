"""Helpers shared by the adapters."""
from __future__ import annotations

import json
from collections import Counter


def iter_records(path, skipped: Counter):
    """Yield JSON objects from a JSONL file; count unparsable lines instead of failing."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                skipped["<unparsable>"] += 1
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                skipped["<non-object>"] += 1
