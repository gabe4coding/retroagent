"""Hybrid search for the eval, on the product code: kb's managed embedding runtime, kb.embed and Index.find(dense=).

Used by run.py --search scripts/search-eval/embed_search.py. Installs and starts the runtime like `kb embed` (in
~/.cache/retroagent/embed), but keeps the vectors in the flow folder (EMBED_STORE), never in the data clone. The
query timeout is generous (EMBED_TIMEOUT, default 10 s), so a cold model does not turn a case into BM25 alone; the
eval measures ranking quality, and `kb find` itself waits at most embed_runtime.PROBE.

EMBED_TURNS=1 (experiment): also embed every user turn of at least TURN_MIN characters, as
"title: <session title> | text: <turn>"; a session's embedding score is then its best match among its summary
document and its turns.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from kb import config, embed, embed_runtime
from kb.index import Filters

TURN_MIN = 40
DIM = int(os.environ.get("EMBED_DIM", "0"))          # experiment: keep only the first DIM numbers (MRL), 0 = all


def _cut(v):
    """The first DIM numbers of v, back to length 1 (EmbeddingGemma 2 is trained for 128/256/512 cuts)."""
    if not DIM:
        return v
    v = list(v[:DIM])
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def _embed_turns(idx, store, ep) -> dict:
    """{session id: [turn vectors]} for the user turns, embedding only the new or changed ones."""
    rows = idx.db.execute("SELECT t.session_id, t.n, t.text, s.title FROM turns t JOIN sessions s ON s.id = t.session_id "
                          "WHERE t.role = 'user' AND length(t.text) >= ?", (TURN_MIN,)).fetchall()
    docs = {f"{r['session_id']}#{r['n']}": embed.doc_text(r["title"], r["text"]) for r in rows}
    have = store.signatures(ep.model)
    todo = [(key, hashlib.sha1(text.encode()).hexdigest(), text) for key, text in docs.items()]
    todo = [t for t in todo if have.get(("turn", t[0])) != t[1]]
    for i in range(0, len(todo), 256):
        chunk = todo[i:i + 256]
        vecs = embed.embed([t[2] for t in chunk], ep.url, ep.key)
        store.put([("turn", key, sha, ep.model, v) for (key, sha, _), v in zip(chunk, vecs)])
    out = {}
    for key, v in store.load("turn", ep.model).items():
        if key in docs:
            out.setdefault(key.rsplit("#", 1)[0], []).append(_cut(v))
    return out


def _dense_sessions(doc_vecs: dict, turn_vecs: dict, q, allowed: set, n: int = embed.POOL) -> list:
    """Session ids by their best similarity among the summary document and the turns."""
    dot = lambda v: sum(a * b for a, b in zip(q, v))
    score = {}
    for sid in allowed:
        cands = ([doc_vecs[sid]] if sid in doc_vecs else []) + turn_vecs.get(sid, [])
        if cands:
            score[sid] = max(dot(v) for v in cands)
    return sorted(score, key=lambda s: (-score[s], s))[:n]


def make_search(idx, flow: Path):
    cfg = config.load()
    ep = embed_runtime.ensure(cfg, wait=True)
    store = embed.Vectors(Path(os.environ.get("EMBED_STORE", flow / "_vectors.sqlite")))
    rep = embed.run_embed(idx.db, store, ep)
    if rep.error:
        raise SystemExit(f"embedding failed: {rep.error}")
    vecs = {kind: {k: _cut(v) for k, v in store.load(kind, ep.model).items()} for kind in embed.KINDS}
    allowed = {kind: idx.keys(kind) for kind in embed.KINDS}
    turns = _embed_turns(idx, store, ep) if os.environ.get("EMBED_TURNS") else None
    timeout = float(os.environ.get("EMBED_TIMEOUT", "10"))

    def search(query: str, limit: int, kind: str = "session") -> list:
        q = _cut(embed.embed([embed.query_text(query)], ep.url, ep.key, timeout=timeout)[0])
        if kind == "session" and turns is not None:
            dense = _dense_sessions(vecs["session"], turns, q, allowed["session"])
        else:
            dense = embed.dense(vecs[kind], q, allowed[kind])
        if kind == "page":
            return idx.find_pages(query, "", limit, dense=dense)
        if kind == "memory":
            return idx.find_memories(query, Filters(), limit, dense=dense)
        return idx.find(query, Filters(), limit, dense=dense)

    return search
