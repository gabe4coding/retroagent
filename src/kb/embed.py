"""Semantic search data: embed the text of sessions, their user turns, pages and memories, keep the vectors, rank
by similarity.

Vectors live in <root>/.kb/embeddings.sqlite, beside the index but in their own file: `kb reindex` and index schema
changes keep them, and .kb/ never syncs. A row is valid for one model key and one text (sha1); anything else is
embedded again. Vectors keep their first DIM numbers (EmbeddingGemma 2 is trained for such cuts): a third of the
storage and the comparison time for about the same ranking. A session ranks by its best match among its summary
document and its user turns, so a detail said only in the middle of a session can be found. The prompts follow the
EmbeddingGemma 2 model card. Stdlib only: comparing a query with a few thousand vectors takes tens of milliseconds.
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import sqlite3
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import dataclass
from pathlib import Path

STORE = "embeddings.sqlite"  # in <root>/.kb/
KINDS = ("session", "turn", "page", "memory")
DIM = 256                   # numbers kept per vector (of 768)
TURN_MIN = 40               # shorter user turns ("yes", "push it") add only noise
DOC_CHARS = 2000            # text embedded per item; longer ones are cut (BM25 still sees all of it)
BATCH = 32
POOL = 50                   # items each ranking gives to the fusion


class EmbedError(Exception):
    """The embedding server did not give vectors. One line."""


def describe(counts: dict) -> str:
    """'12 sessions, 30 turns, 1 page, 0 memories' from {kind: count}."""
    plural = {"session": "sessions", "turn": "turns", "page": "pages", "memory": "memories"}
    return ", ".join(f"{counts.get(k, 0)} {plural[k] if counts.get(k, 0) != 1 else k}" for k in KINDS)


def model_key(model: str) -> str:
    """What a stored vector is valid for: the model and the cut."""
    return f"{model}/{DIM}"


def query_text(q: str) -> str:
    return f"task: search result | query: {q}"


def doc_text(title: str, text: str) -> str:
    return f"title: {title or 'none'} | text: {text}"[:DOC_CHARS]


def embed(texts: list, url: str, key: str = "", timeout: float = 60.0) -> list:
    """Unit vectors for texts, in order, from an OpenAI-compatible /v1/embeddings server, cut to DIM numbers."""
    out = []
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        req = urllib.request.Request(url + "/v1/embeddings", json.dumps({"input": chunk}).encode("utf-8"), headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)["data"]
            vecs = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            raise EmbedError(" ".join(f"{type(e).__name__}: {e}".split())[:200]) from None
        if len(vecs) != len(chunk) or len({len(v) for v in vecs}) != 1:
            raise EmbedError("embedding server returned a wrong number or size of vectors")
        for v in vecs:
            v = v[:DIM]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
    return out


# ---- what gets embedded

def documents(db) -> list:
    """(kind, key, text) for every session, user turn (key "<session id>#<n>"), page and memory in the index."""
    docs = []
    for r in db.execute("SELECT s.id, f.title, f.summary, f.tags, f.decisions, f.first_prompt "
                        "FROM sessions s JOIN sessions_fts f ON f.rowid = s.rowid"):
        parts = [r["summary"], _list("Tags", r["tags"]), _list("Decisions", r["decisions"]),
                 r["first_prompt"] and "First prompt: " + r["first_prompt"]]
        docs.append(("session", r["id"], doc_text(r["title"], "\n".join(p for p in parts if p))))
    for r in db.execute("SELECT t.session_id, t.n, t.text, s.title FROM turns t JOIN sessions s ON s.id = t.session_id "
                        "WHERE t.role = 'user' AND length(t.text) >= ?", (TURN_MIN,)):
        docs.append(("turn", f"{r['session_id']}#{r['n']}", doc_text(r["title"], r["text"])))
    for r in db.execute("SELECT p.path, f.title, f.body FROM pages p JOIN pages_fts f ON f.rowid = p.rowid"):
        docs.append(("page", r["path"], doc_text(r["title"], r["body"] or "")))
    for r in db.execute("SELECT m.path, f.name, f.description, f.body "
                        "FROM memories m JOIN memories_fts f ON f.rowid = m.rowid"):
        docs.append(("memory", r["path"], doc_text(r["name"], "\n".join(p for p in (r["description"], r["body"]) if p))))
    return docs


def _list(label: str, raw) -> str:
    """Tags and decisions are stored as JSON lists; embed them as plain text."""
    if not raw:
        return ""
    try:
        items = json.loads(raw)
    except ValueError:
        items = raw
    text = "; ".join(str(i) for i in items) if isinstance(items, list) else str(items)
    return f"{label}: {text}" if text else ""


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---- the vector store

class Vectors:
    def __init__(self, path, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            from kb.index import connect_readonly
            self.db = connect_readonly(self.path)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=10)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS vectors(kind TEXT, key TEXT, sha TEXT, model TEXT, "
                            "dim INTEGER, vec BLOB, PRIMARY KEY(kind, key))")
            self.db.commit()

    @classmethod
    def open_readonly(cls, path):
        """The store for a search, or None when it does not exist or cannot be read."""
        if not Path(path).is_file():
            return None
        try:
            return cls(path, readonly=True)
        except sqlite3.Error:
            return None

    def close(self) -> None:
        self.db.close()

    def signatures(self, model: str) -> dict:
        """(kind, key) -> sha of the rows made with model."""
        return {(k, key): sha for k, key, sha in
                self.db.execute("SELECT kind, key, sha FROM vectors WHERE model = ?", (model,))}

    def keys(self) -> set:
        return {(k, key) for k, key in self.db.execute("SELECT kind, key FROM vectors")}

    def put(self, rows) -> None:
        """rows: (kind, key, sha, model, vector). One transaction."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?)",
                                [(k, key, sha, model, len(v), array("f", v).tobytes()) for k, key, sha, model, v in rows])

    def delete(self, pairs) -> None:
        with self.db:
            self.db.executemany("DELETE FROM vectors WHERE kind = ? AND key = ?", list(pairs))

    def load(self, kind: str, model: str) -> dict:
        out = {}
        for key, blob in self.db.execute("SELECT key, vec FROM vectors WHERE kind = ? AND model = ?", (kind, model)):
            v = array("f")
            v.frombytes(blob)
            out[key] = v
        return out

    def counts(self, model: str) -> dict:
        return dict(self.db.execute("SELECT kind, COUNT(*) FROM vectors WHERE model = ? GROUP BY kind", (model,)))


# ---- keeping the store in step with the index

@dataclass
class EmbedReport:
    done: int = 0
    left: int = 0
    removed: int = 0
    error: str = ""


def plan(db, store: Vectors, model: str) -> list:
    """(kind, key, sha, text) of the items without a vector for this model and text."""
    have = store.signatures(model)
    todo = []
    for kind, key, text in documents(db):
        sha = _sha(text)
        if have.get((kind, key)) != sha:
            todo.append((kind, key, sha, text))
    return todo


def prune(db, store: Vectors) -> int:
    """Delete vectors whose item left the index. Returns how many."""
    live = {(kind, key) for kind, key, _ in documents(db)}
    gone = store.keys() - live
    if gone:
        store.delete(gone)
    return len(gone)


def run_embed(db, store: Vectors, endpoint, limit: int = None, deadline: float = None, clock=time.time) -> EmbedReport:
    """Embed what is missing, a batch per transaction, until done, limit items, or the deadline. Never raises
    EmbedError: it lands in the report, and the finished batches stay."""
    model = model_key(endpoint.model)
    rep = EmbedReport(removed=prune(db, store))
    todo = plan(db, store, model)
    if limit is not None:
        todo = todo[:limit]
    for i in range(0, len(todo), BATCH):
        if deadline is not None and clock() >= deadline:
            break
        chunk = todo[i:i + BATCH]
        try:
            vecs = embed([t[3] for t in chunk], endpoint.url, endpoint.key)
        except EmbedError as e:
            rep.error = str(e)
            break
        store.put([(k, key, sha, model, v) for (k, key, sha, _), v in zip(chunk, vecs)])
        rep.done += len(chunk)
    rep.left = len(plan(db, store, model))
    return rep


# ---- ranking

def rank(store: Vectors, model: str, kind: str, qvec, allowed=None, n: int = POOL) -> list:
    """Keys of kind ranked by similarity to qvec, best first. A session scores its best match among its summary
    document and its user turns."""
    key = model_key(model)
    if kind != "session":
        return dense(store.load(kind, key), qvec, allowed, n)
    best = {}
    mul = operator.mul
    for vectors, sid_of in ((store.load("session", key), lambda k: k),
                            (store.load("turn", key), lambda k: k.rsplit("#", 1)[0])):
        for k, v in vectors.items():
            sid = sid_of(k)
            if allowed is None or sid in allowed:
                score = sum(map(mul, qvec, v))
                if score > best.get(sid, -2.0):
                    best[sid] = score
    return sorted(best, key=lambda sid: (-best[sid], sid))[:n]


def dense(vectors: dict, qvec, allowed=None, n: int = POOL) -> list:
    """Keys of the n vectors closest to qvec (dot product of unit vectors), best first; only keys in allowed."""
    mul = operator.mul
    scored = [(sum(map(mul, qvec, v)), key) for key, v in vectors.items() if allowed is None or key in allowed]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [key for _, key in scored[:n]]
