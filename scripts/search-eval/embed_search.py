"""Experimental hybrid search for the eval: BM25 (Index.find) fused with embedding similarity.

Used by run.py --search scripts/search-eval/embed_search.py. Needs an OpenAI-compatible /v1/embeddings server on
EMBED_URL (default http://127.0.0.1:8765), for example llama-server with EmbeddingGemma 2. Each session is embedded
from title, summary, tags, decisions and first prompt (not the turns), with the model card's document prompt; vectors
are cached by text hash in EMBED_CACHE (default .claude/hillclimb/kb-find/_embed_cache.json, git-ignored).
EMBED_MODE: hybrid (default; reciprocal rank fusion of both rankings) or dense (embeddings only).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from pathlib import Path

from kb.index import Filters

URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8765") + "/v1/embeddings"
MODE = os.environ.get("EMBED_MODE", "hybrid")
RRF_K = 60            # standard reciprocal rank fusion constant
POOL = 50             # hits taken from each ranking before fusion
DOC_CHARS = 2000


def _embed(texts: list) -> list:
    out = []
    for i in range(0, len(texts), 32):
        req = urllib.request.Request(URL, json.dumps({"input": texts[i:i + 32]}).encode(),
                                     {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = sorted(json.load(r)["data"], key=lambda d: d["index"])
        for d in data:
            v = d["embedding"]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
    return out


def _doc(r) -> str:
    text = "\n".join(p for p in (r["summary"], r["tags"] and "Tags: " + r["tags"],
                                 r["decisions"] and "Decisions: " + r["decisions"],
                                 r["first_prompt"] and "First prompt: " + r["first_prompt"]) if p)
    return f"title: {r['title'] or 'none'} | text: {text}"[:DOC_CHARS]


def make_search(idx, flow: Path):
    rows = idx.db.execute("SELECT s.id, s.parent, s.project, s.title, f.summary, f.tags, f.decisions, f.first_prompt "
                          "FROM sessions s JOIN sessions_fts f ON f.rowid = s.rowid").fetchall()
    meta = {r["id"]: {"id": r["id"], "parent": r["parent"], "project": r["project"], "title": r["title"]}
            for r in rows}
    docs = {r["id"]: _doc(r) for r in rows}
    cache_path = Path(os.environ.get("EMBED_CACHE", flow / "_embed_cache.json"))
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    key = {sid: hashlib.sha1(d.encode()).hexdigest() for sid, d in docs.items()}
    todo = [sid for sid in docs if key[sid] not in cache]
    for sid, v in zip(todo, _embed([docs[s] for s in todo])):
        cache[key[sid]] = v
    if todo:
        cache_path.write_text(json.dumps(cache))
    vecs = {sid: cache[key[sid]] for sid in docs}

    def search(query: str, limit: int) -> list:
        q = _embed([f"task: search result | query: {query}"])[0]
        dense = sorted(vecs, key=lambda sid: -sum(a * b for a, b in zip(q, vecs[sid])))[:POOL]
        if MODE == "dense":
            return [meta[s] for s in dense[:limit]]
        lexical = idx.find(query, Filters(), POOL)
        score = {}
        for ranking in ([h["id"] for h in lexical], dense):
            for rank, sid in enumerate(ranking, 1):
                score[sid] = score.get(sid, 0.0) + 1 / (RRF_K + rank)
        lex = {h["id"]: h for h in lexical}
        best = sorted(score, key=lambda s: (-score[s], s))[:limit]
        return [lex.get(s, meta[s]) for s in best]

    return search
