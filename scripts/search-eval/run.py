#!/usr/bin/env python3
"""Retrieval eval for `kb find`: does the session a query is about come back near the top?

Launch:  scripts/search-eval/run.py                      cases.local.jsonl, variant "baseline"
         scripts/search-eval/run.py --variant v1         after a change to src/kb (the code next to this script runs)
         scripts/search-eval/run.py --cases FILE --limit 20
         scripts/search-eval/run.py --variant v3 --search scripts/search-eval/embed_search.py

Cases: each case is {"id", "tags", "query", "expected": [...], "kind"?}.
- kind "session" (the default): expected holds session id prefixes. A hit matches when its id starts with one, or
  its parent's id does (a subagent of the expected session). Only session hits are scored (Index.find), not the
  pages and memories that `kb find` prints first.
- kind "page" or "memory": expected holds paths, scored against Index.find_pages or Index.find_memories.

Metrics per case: recall@5 (the headline), recall@10 and reciprocal rank.

Custom search: --search FILE scores another search instead. FILE defines make_search(idx, flow), which returns
search(query, limit, kind) -> rows. A row has id and parent for a session, path for a page or a memory.

Output: it reads the index read-only, with no model call and no cost. It writes .claude/hillclimb/kb-find/<variant>/
in the layout that the claude-api report builders read (results.jsonl, traces/, errors.jsonl).
Python 3.9, stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))

from kb import config  # noqa: E402
from kb.index import Filters, Index  # noqa: E402

METRICS = [
    {"id": "recall_at_5", "label": "recall@5", "kind": "binary"},
    {"id": "recall_at_10", "label": "recall@10", "kind": "binary"},
    {"id": "rr", "label": "MRR", "kind": "continuous"},
]


def rank_of(hits: list, expected: list, kind: str = "session"):
    for i, h in enumerate(hits, 1):
        if kind != "session":
            if h.get("path") in expected:
                return i
        elif any((h["id"] or "").startswith(p) or (h.get("parent") or "").startswith(p) for p in expected):
            return i
    return None


def bm25_search(idx):
    def search(query: str, limit: int, kind: str = "session") -> list:
        if kind == "page":
            return idx.find_pages(query, "", limit)
        if kind == "memory":
            return idx.find_memories(query, Filters(), limit)
        return idx.find(query, Filters(), limit)
    return search


def _exists(idx, kind: str, key: str) -> bool:
    if kind == "session":
        return idx.get(key) is not None
    table = "pages" if kind == "page" else "memories"
    return idx.db.execute(f"SELECT 1 FROM {table} WHERE path = ?", (key,)).fetchone() is not None


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default=str(HERE / "cases.local.jsonl"))
    ap.add_argument("--variant", default="baseline", help="baseline or v<N>")
    ap.add_argument("--limit", type=int, default=10, help="hits asked from Index.find")
    ap.add_argument("--out", default=str(REPO / ".claude/hillclimb/kb-find"))
    ap.add_argument("--search", help="python file with make_search(idx, flow); default Index.find")
    a = ap.parse_args()

    cfg = config.load()
    idx = Index.open_current(cfg.kb_dir / "index.sqlite")
    if idx is None:
        sys.exit("index missing or outdated; run: kb reindex")
    cases = [json.loads(l) for l in Path(a.cases).read_text().splitlines() if l.strip()]
    missing = [(c["id"], p) for c in cases for p in c["expected"] if not _exists(idx, c.get("kind", "session"), p)]
    if missing:                                   # a gold id that no longer resolves would score as a silent miss
        sys.exit(f"expected ids not in the index: {missing}")

    flow = Path(a.out)
    var = flow / a.variant
    (var / "traces").mkdir(parents=True, exist_ok=True)
    state = flow / "_state.json"
    if not state.exists():
        state.write_text(json.dumps({"flow": "kb-find", "metrics": METRICS, "perf_fields": ["latency_s"]}, indent=2))

    search = bm25_search(idx)
    if a.search:
        spec = importlib.util.spec_from_file_location("search_plugin", a.search)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        search = mod.make_search(idx, flow)

    rows = []
    with open(var / "results.jsonl", "w") as out:
        for c in cases:
            t = time.perf_counter()
            kind = c.get("kind", "session")
            hits = search(c["query"], a.limit, kind)
            lat = round(time.perf_counter() - t, 4)
            r = rank_of(hits, c["expected"], kind)
            grade = {"recall_at_5": float(bool(r and r <= 5)), "recall_at_10": float(bool(r and r <= 10)),
                     "rr": round(1 / r, 4) if r else 0.0}
            row = {"prompt_id": c["id"], "prompt": c["query"], "tags": c["tags"], "status": "ok",
                   "stop_reason": "end_turn", "grade": grade, "latency_s": lat, "model": Path(a.search).stem if a.search else "kb-find-bm25",
                   "usage": {"input_tokens": 0, "output_tokens": 0},
                   "meta": {"rank": r, "expected": c["expected"], "kind": kind,
                            "hits": [h["id"][:8] if kind == "session" else h["path"] for h in hits]}}
            out.write(json.dumps(row) + "\n")
            out.flush()
            listing = "\n".join(f"{i}. {h['id'][:8] if kind == 'session' else h['path']} "
                                f"{'↳' + h['parent'][:8] + ' ' if h.get('parent') else ''}"
                                f"{h.get('project') or ''} | {h.get('title') or h.get('name') or ''} | "
                                f"{h.get('snippet') or ''}" for i, h in enumerate(hits, 1))
            trace = [{"role": "user", "content": c["query"]},
                     {"role": "assistant", "content": f"expected {c['expected']}, rank {r}\n\n{listing or '(no hits)'}"}]
            (var / "traces" / f"{c['id']}_rep0.json").write_text(json.dumps(trace, indent=1, ensure_ascii=False))
            rows.append(row)
    idx.close()

    def line(name, rs):
        n = len(rs)
        k5 = sum(r["grade"]["recall_at_5"] for r in rs)
        lo, hi = wilson(int(k5), n)
        r10 = sum(r["grade"]["recall_at_10"] for r in rs) / n
        mrr = sum(r["grade"]["rr"] for r in rs) / n
        return f"{name:<16} n={n:<3} recall@5 {k5 / n:.2f} [{lo:.2f}-{hi:.2f}]  recall@10 {r10:.2f}  MRR {mrr:.2f}"

    print(line("all", rows))
    for tag in sorted({t for r in rows for t in r["tags"]}):
        print(line(tag, [r for r in rows if tag in r["tags"]]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
