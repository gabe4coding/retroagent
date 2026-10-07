"""Hybrid search for the eval, on the product code: kb's managed embedding runtime, kb.embed and Index.find(dense=).

Used by run.py --search scripts/search-eval/embed_search.py. Installs and starts the runtime like `kb embed` (in
~/.cache/retroagent/embed), but keeps the vectors in the flow folder (EMBED_STORE), never in the data clone. The
query timeout is generous (EMBED_TIMEOUT, default 10 s), so a cold model does not turn a case into BM25 alone; the
eval measures ranking quality, and `kb find` itself waits at most embed_runtime.PROBE.
"""
from __future__ import annotations

import os
from pathlib import Path

from kb import config, embed, embed_runtime
from kb.index import Filters


def make_search(idx, flow: Path):
    cfg = config.load()
    ep = embed_runtime.ensure(cfg, wait=True)
    store = embed.Vectors(Path(os.environ.get("EMBED_STORE", flow / "_vectors.sqlite")))
    rep = embed.run_embed(idx.db, store, ep)
    if rep.error:
        raise SystemExit(f"embedding failed: {rep.error}")
    vecs = store.load("session", ep.model)
    allowed = idx.keys("session")
    timeout = float(os.environ.get("EMBED_TIMEOUT", "10"))

    def search(query: str, limit: int) -> list:
        q = embed.embed([embed.query_text(query)], ep.url, ep.key, timeout=timeout)[0]
        return idx.find(query, Filters(), limit, dense=embed.dense(vecs, q, allowed))

    return search
