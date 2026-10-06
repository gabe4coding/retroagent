"""catalog/<host>/<YYYY-MM>.jsonl: one line per session, derived from markdown front matter."""
from __future__ import annotations

import json
from pathlib import Path

from kb.distill import split_front_matter
from kb.util import atomic_write

FIELDS = ["id", "agent", "host", "project", "cwd", "branch", "started", "ended", "turns", "user_turns", "model",
          "title", "summary", "tags", "outcome", "decisions", "files", "prs", "parent"]


def write_catalog(root, host: str, months) -> list:
    """Rewrite the catalog file of each month ("YYYY/MM"). Returns [(md path relative to root, why)] of the files left out.

    A markdown file that cannot be read or parsed is skipped, never an error for the whole run. When every file of a
    month is skipped the month's old catalog file stays as it is."""
    root = Path(root)
    skipped = []
    for month in sorted(months):                       # "YYYY/MM"
        yyyy, mm = month.split("/")
        rows = []
        files = sorted((root / "sessions" / host).glob(f"*/{yyyy}/{mm}/*.md"))
        for md in files:
            try:
                meta, _ = split_front_matter(md.read_text(encoding="utf-8", errors="replace"))
                row = {k: meta.get(k) for k in FIELDS}
                row["files"] = (row.get("files") or [])[:10]
                row["md"] = md.relative_to(root).as_posix()
                row["raw"] = meta.get("raw", "")
                json.dumps(row, ensure_ascii=False)    # a value that cannot be written is found here, not later
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the catalog
                skipped.append((md.relative_to(root).as_posix(), " ".join(f"{type(e).__name__}: {e}".split())))
                continue
            rows.append(row)
        rows.sort(key=lambda r: (str(r.get("started") or ""), str(r.get("id") or "")))     # odd values must not break the sort
        path = root / "catalog" / host / f"{yyyy}-{mm}.jsonl"
        if rows:
            lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            atomic_write(path, lines.encode("utf-8", errors="replace"))     # a lone surrogate must not stop the catalog
        elif path.exists() and not files:
            path.unlink()
    return skipped
