"""catalog/<host>/<YYYY-MM>.jsonl: one line per session, derived from markdown front matter."""
from __future__ import annotations

import json
from pathlib import Path

from kb.distill import split_front_matter
from kb.util import atomic_write

FIELDS = ["id", "agent", "host", "project", "cwd", "branch", "started", "ended", "turns", "user_turns", "model",
          "title", "summary", "tags", "outcome", "decisions", "files", "prs", "parent"]


def write_catalog(root, host: str, months) -> None:
    root = Path(root)
    for month in sorted(months):                       # "YYYY/MM"
        yyyy, mm = month.split("/")
        rows = []
        for md in sorted((root / "sessions" / host).glob(f"*/{yyyy}/{mm}/*.md")):
            meta, _ = split_front_matter(md.read_text(encoding="utf-8"))
            row = {k: meta.get(k) for k in FIELDS}
            row["files"] = (row.get("files") or [])[:10]
            row["md"] = md.relative_to(root).as_posix()
            row["raw"] = meta.get("raw", "")
            rows.append(row)
        rows.sort(key=lambda r: (r.get("started") or "", r.get("id") or ""))
        path = root / "catalog" / host / f"{yyyy}-{mm}.jsonl"
        if rows:
            atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8"))
        elif path.exists():
            path.unlink()
