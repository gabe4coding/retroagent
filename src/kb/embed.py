"""Semantic search data: turn text into vectors (embed it), keep the vectors, and rank items by similarity.

What gets embedded (one vector per item):
- each session: its summary document (summary, tags, decisions and first prompt),
- the longer user turns of each session (TURN_MIN characters or more),
- each page and each memory file.

The store:
- The vectors live in <root>/.kb/embeddings.sqlite. This file is beside the search index, but separate from it. So
  `kb reindex` and a new index schema keep the vectors. The .kb/ folder never syncs.
- A stored vector is reused only for the same model key and the same text (the same sha1 of the text). Anything
  else is embedded again.
- A vector keeps only its first DIM of the model's 768 numbers. EmbeddingGemma 2 is trained for such cuts: about the
  same ranking for a third of the storage and the comparison time. The model key names the cut.

The search:
- A session scores its best match among its summary document and its user turns. So a detail said only in the
  middle of a session can be found.
- With fewer than EXACT_BELOW session and turn vectors, the query is compared with every vector.
- With more, the search has two steps. The sign-bit index (BitIndex) first picks the PREFILTER closest vectors. Then
  the exact comparison runs on those only. So the cost of a search grows slowly with the number of sessions.

Terms:
- unit vector: a vector of length 1. For two unit vectors, the dot product is the similarity (1 = same direction).
- sign-bit index: one bit per number of each vector, 1 when the number is above zero. The count of different bits
  (Hamming distance) is a fast, rough measure of how far apart two vectors are.

The prompts (query_text, doc_text) follow the EmbeddingGemma 2 model card. Standard library only.
"""
from __future__ import annotations

import hashlib
import heapq
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

from kb.util import atomic_write

STORE = "embeddings.sqlite"  # in <root>/.kb/
BITS = "embeddings.bits"     # beside STORE: the sign-bit index of the session and turn vectors
KINDS = ("session", "turn", "page", "memory")
DIM = 256                   # numbers kept per vector (of 768)
TURN_MIN = 40               # shorter user turns ("yes", "push it") add only noise
DOC_CHARS = 2000            # text embedded per item; longer ones are cut (BM25 still sees all of it)
BATCH = 32
POOL = 50                   # items each ranking gives to the fusion
EXACT_BELOW = 5000          # fewer session + turn vectors than this: compare the query with every vector
PREFILTER = 3000            # else: the vectors closest by sign bits, then the exact comparison on those
# Rowids per SQL query in by_rowid. SQLite builds older than 3.32 allow at most 999 "?" variables in one statement.
_ROWIDS_PER_QUERY = 900
# Lower than any score: the dot product of two unit vectors is never below -1.
_NO_SCORE = -2.0


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
            # generation: raised by every write, so a bit index can tell it is current without reading the store
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value INTEGER)")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('generation', 0)")
            # committed vector files already imported, by signature, so an unchanged file is not read again
            self.db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sig TEXT)")
            self.db.commit()

    @classmethod
    def open_readonly(cls, path):
        """The store for a search, or None when it does not exist, cannot be read, or is still being created (the
        first fill makes the file before its tables)."""
        if not Path(path).is_file():
            return None
        try:
            st = cls(path, readonly=True)
        except sqlite3.Error:
            return None
        try:
            ready = st.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vectors'").fetchone()
        except sqlite3.Error:
            ready = None
        if not ready:
            st.close()
            return None
        return st

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
            self._bump()

    def delete(self, pairs) -> None:
        with self.db:
            self.db.executemany("DELETE FROM vectors WHERE kind = ? AND key = ?", list(pairs))
            self._bump()

    def _bump(self) -> None:
        self.db.execute("UPDATE meta SET value = value + 1 WHERE name = 'generation'")

    def load(self, kind: str, model: str) -> dict:
        out = {}
        for key, blob in self.db.execute("SELECT key, vec FROM vectors WHERE kind = ? AND model = ?", (kind, model)):
            v = array("f")
            v.frombytes(blob)
            out[key] = v
        return out

    def rows(self, model: str) -> dict:
        """(kind, key) -> (sha, vector) of every row made with model."""
        out = {}
        for kind, key, sha, blob in self.db.execute("SELECT kind, key, sha, vec FROM vectors WHERE model = ?", (model,)):
            v = array("f")
            v.frombytes(blob)
            out[(kind, key)] = (sha, v)
        return out

    def counts(self, model: str) -> dict:
        return dict(self.db.execute("SELECT kind, COUNT(*) FROM vectors WHERE model = ? GROUP BY kind", (model,)))

    def generation(self):
        """The write counter of the store; None for a store from before it existed (then no bit index is used)."""
        try:
            row = self.db.execute("SELECT value FROM meta WHERE name = 'generation'").fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def by_rowid(self, rowids) -> dict:
        """rowid -> vector, for the given rowids."""
        out, rowids = {}, list(rowids)
        for i in range(0, len(rowids), _ROWIDS_PER_QUERY):
            chunk = rowids[i:i + _ROWIDS_PER_QUERY]
            for rid, blob in self.db.execute(
                    f"SELECT rowid, vec FROM vectors WHERE rowid IN ({','.join('?' * len(chunk))})", chunk):
                v = array("f")
                v.frombytes(blob)
                out[rid] = v
        return out

    def bit_index(self, model: str):
        """The current bit index for model, read once per store object; None when it is missing or stale (then the
        search compares every vector). Never written here: a search may run where .kb/ is read-only."""
        if not hasattr(self, "_bits"):
            self._bits = {}
        if model not in self._bits:
            gen = self.generation()
            self._bits[model] = None if gen is None else BitIndex.read(self.path.with_name(BITS), model, gen)
        return self._bits[model]


def _signs(v) -> int:
    """The sign bits of a vector as one integer, first number in the highest bit."""
    return int("".join("1" if x > 0 else "0" for x in v), 2)


_WIDTH = DIM // 8                   # bytes of sign bits per vector


def _repeat(pattern: bytes, n: int) -> int:
    return int.from_bytes(pattern * (n * _WIDTH // len(pattern)), "big")


class BitIndex:
    """The sign bits of every session and turn vector of one model key. It is the first step of a search in a large
    store: it finds the PREFILTER vectors closest to the query, cheaply, before the exact comparison.

    The file .kb/embeddings.bits holds these blocks, in this order:
    1. Header: one JSON line with format, model, dim, generation and count (the number of vectors).
    2. Bits: _WIDTH (DIM/8) bytes of sign bits per vector, for all vectors.
    3. Rowids: the store rowid of each vector, 8 bytes each (array "q", machine byte order).
    4. Groups: the session id of each vector (a turn's group is its session), joined with newlines, UTF-8.

    In memory, the bits of all vectors are one large integer. So the distance from the query to every vector takes a
    fixed number of big-integer operations, not one Python step per vector."""

    def __init__(self, model: str, generation: int, block: bytes, rowids: list, groups: list):
        self.model, self.generation, self.rowids, self.groups = model, generation, rowids, groups
        self.n = len(rowids)
        self.block = int.from_bytes(block, "big")

    @classmethod
    def build(cls, store: Vectors, model: str) -> "BitIndex":
        bits, rowids, groups = [], [], []
        for rid, kind, key, blob in store.db.execute(
                "SELECT rowid, kind, key, vec FROM vectors WHERE model = ? AND kind IN ('session', 'turn') "
                "ORDER BY rowid", (model,)):
            v = array("f")
            v.frombytes(blob)
            bits.append(_signs(v).to_bytes(_WIDTH, "big"))
            rowids.append(rid)
            groups.append(key.rsplit("#", 1)[0] if kind == "turn" else key)
        return cls(model, store.generation(), b"".join(bits), rowids, groups)

    def write(self, path) -> None:
        head = json.dumps({"format": 2, "model": self.model, "dim": DIM, "generation": self.generation,
                           "count": self.n}).encode("utf-8") + b"\n"
        body = self.block.to_bytes(self.n * _WIDTH, "big") + array("q", self.rowids).tobytes()
        atomic_write(path, head + body + "\n".join(self.groups).encode("utf-8"))

    @classmethod
    def read(cls, path, model: str, generation: int):
        """The index in path when it is for model and the store's generation, else None."""
        try:
            raw = Path(path).read_bytes()
            nl = raw.index(b"\n")
            head = json.loads(raw[:nl])
            if head.get("format") != 2 or head.get("model") != model or head.get("dim") != DIM \
                    or head.get("generation") != generation:
                return None
            n, pos = head["count"], nl + 1
            block = raw[pos: pos + n * _WIDTH]
            pos += n * _WIDTH
            rowids = array("q")
            rowids.frombytes(raw[pos: pos + n * 8])
            groups = raw[pos + n * 8:].decode("utf-8").split("\n") if n else []
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not (len(block) == n * _WIDTH and len(rowids) == n == len(groups)):
            return None
        return cls(model, generation, block, list(rowids), groups)

    def distances(self, qvec) -> list:
        """Hamming distance from the sign bits of qvec to every vector, in index order.

        The bit count is a SWAR popcount ("SIMD within a register"): each step works on all fields of the large
        integer at once, with shifts and masks.
        1. XOR with the query bits, repeated once per vector: a 1 bit where the vector and the query differ.
        2. Count the 1 bits of each 2-bit field, then of each 4-bit field, then of each byte.
        3. Add neighbour fields together: 2 bytes, 4, 8, 16, then 32 bytes (one vector slot of _WIDTH bytes).
        4. Read each vector's count from the last 2 bytes of its slot."""
        n = self.n
        counts = self.block ^ int.from_bytes(_signs(qvec).to_bytes(_WIDTH, "big") * n, "big")
        counts = counts - ((counts >> 1) & _repeat(b"\x55", n))               # count per 2-bit field
        mask_4bit = _repeat(b"\x33", n)
        counts = (counts & mask_4bit) + ((counts >> 2) & mask_4bit)           # per 4-bit field
        counts = (counts + (counts >> 4)) & _repeat(b"\x0f", n)               # per byte
        for shift in (8, 16, 32, 64, 128):                                    # add up the bytes of each vector
            half = shift // 8
            counts = (counts + (counts >> shift)) & _repeat(b"\x00" * half + b"\xff" * half, n)
        slots = counts.to_bytes(n * _WIDTH, "big")                            # each count: last 2 bytes of its slot
        return [hi * 256 + lo for hi, lo in zip(slots[_WIDTH - 2::_WIDTH], slots[_WIDTH - 1::_WIDTH])]

    def candidates(self, qvec, allowed=None, k: int = PREFILTER) -> list:
        """Positions of the k vectors closest to qvec by Hamming distance, among those whose group is allowed."""
        pos = range(self.n) if allowed is None else [i for i, g in enumerate(self.groups) if g in allowed]
        if len(pos) <= k:
            return list(pos)
        dist = self.distances(qvec)
        return heapq.nsmallest(k, pos, key=dist.__getitem__)


def save_bit_index(store: Vectors, model: str) -> None:
    """Write (or remove, when the store is small) the bit index of model. Called after the store changed."""
    path = store.path.with_name(BITS)
    n = store.db.execute("SELECT COUNT(*) FROM vectors WHERE model = ? AND kind IN ('session', 'turn')",
                         (model,)).fetchone()[0]
    if n >= EXACT_BELOW:
        BitIndex.build(store, model).write(path)
    elif path.exists():
        path.unlink()
    if hasattr(store, "_bits"):
        store._bits.pop(model, None)


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
    if rep.done or rep.removed or not store.path.with_name(BITS).exists():
        save_bit_index(store, model)
    return rep


# ---- committed vector files (vectors/<host>/ in the data repo)

def vec_rel(path: str) -> str:
    """The vector file of a session or memory file: sessions/<host>/<rest>.md -> vectors/<host>/sessions/<rest>.vec,
    memories/<host>/<rest>.md -> vectors/<host>/memories/<rest>.vec."""
    top, host, rest = path.split("/", 2)
    return f"vectors/{host}/{top}/{rest[:-3] if rest.endswith('.md') else rest}.vec"


def export_own(root, db, store: Vectors, model: str, host: str):
    """Write the vector files of this host's sessions (summary document and turns) and memories, and delete the files
    of items that are gone. Unchanged files are not rewritten. Returns (written, removed)."""
    from kb import vecfile
    root = Path(root)
    key = model_key(model)
    wanted = _wanted_vec_files(db, store.rows(key), host)
    written = 0
    for rel, items in wanted.items():
        vectors = [v for _, _, (_, v) in items]
        data = vecfile.dumps(key, len(vectors[0]), [(kind, item_key, sha) for kind, item_key, (sha, _) in items],
                             vectors)
        written += atomic_write(root / rel, data)
    return written, _remove_stale_vec_files(root, host, wanted)


def _wanted_vec_files(db, rows: dict, host: str) -> dict:
    """{vector file path: [(kind, item key, (sha, vector))]} for this host's sessions and memories that have vectors.

    A session file lists its summary document first, then its user turns in turn order."""
    turns = {}
    for (kind, item_key), (sha, v) in rows.items():
        if kind == "turn":
            sid, _, n = item_key.rpartition("#")
            turns.setdefault(sid, []).append((int(n) if n.isdigit() else 0, item_key, sha, v))
    wanted = {}
    for sid, md_path in db.execute("SELECT id, md_path FROM sessions WHERE host = ?", (host,)):
        items = [("session", sid, rows[("session", sid)])] if ("session", sid) in rows else []
        items += [("turn", item_key, (sha, v)) for _, item_key, sha, v in sorted(turns.get(sid, []))]
        if items and md_path:
            wanted[vec_rel(md_path)] = items
    for (path,) in db.execute("SELECT path FROM memories WHERE host = ?", (host,)):
        if ("memory", path) in rows:
            wanted[vec_rel(path)] = [("memory", path, rows[("memory", path)])]
    return wanted


def _remove_stale_vec_files(root: Path, host: str, wanted: dict) -> int:
    """Delete this host's vector files that are not in wanted. Returns how many."""
    removed = 0
    base = root / "vectors" / host
    for f in base.rglob("*.vec") if base.is_dir() else []:
        if f.relative_to(root).as_posix() not in wanted:
            f.unlink()
            removed += 1
    return removed


def import_files(root, db, store: Vectors, model: str, own_host: str) -> int:
    """Load the vectors other machines committed (vectors/<host>/, every host but own_host) into the store: only
    those made with model whose text is still the one in the index (same sha). Files seen before with the same
    signature are skipped. Returns how many vectors were added or replaced."""
    from kb import vecfile
    root = Path(root)
    key = model_key(model)
    base = root / "vectors"
    if not base.is_dir():
        return 0
    seen = dict(store.db.execute("SELECT path, sig FROM files"))
    current = {(kind, k): _sha(text) for kind, k, text in documents(db)}
    have = store.signatures(key)
    rows, sigs = [], []
    for host_dir in sorted(p for p in base.iterdir() if p.is_dir() and p.name != own_host):
        for f in sorted(host_dir.rglob("*.vec")):
            rel = f.relative_to(root).as_posix()
            st = f.stat()
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            if seen.get(rel) == sig:
                continue
            sigs.append((rel, sig))
            try:
                file_model, dim, items, vectors = vecfile.loads(f.read_bytes())
            except (OSError, vecfile.VecFileError):
                continue
            if file_model != key:                      # the key names the model and the cut
                continue
            for (kind, k, sha), v in zip(items, vectors):
                if current.get((kind, k)) == sha and have.get((kind, k)) != sha:
                    rows.append((kind, k, sha, key, v))
    if rows:
        store.put(rows)
        save_bit_index(store, key)
    if sigs:
        with store.db:
            store.db.executemany("INSERT OR REPLACE INTO files VALUES (?, ?)", sigs)
    return len(rows)


# ---- ranking

def rank(store: Vectors, model: str, kind: str, qvec, allowed=None, n: int = POOL) -> list:
    """Keys of kind ranked by similarity to qvec, best first. A session scores its best match among its summary
    document and its user turns."""
    key = model_key(model)
    if kind != "session":
        return dense(store.load(kind, key), qvec, allowed, n)
    bits = store.bit_index(key)
    if bits is not None:
        return _rank_two_step(store, bits, qvec, allowed, n)
    best = {}
    mul = operator.mul
    for vectors, sid_of in ((store.load("session", key), lambda k: k),
                            (store.load("turn", key), lambda k: k.rsplit("#", 1)[0])):
        for k, v in vectors.items():
            sid = sid_of(k)
            if allowed is None or sid in allowed:
                score = sum(map(mul, qvec, v))
                if score > best.get(sid, _NO_SCORE):
                    best[sid] = score
    return sorted(best, key=lambda sid: (-best[sid], sid))[:n]


def _rank_two_step(store: Vectors, bits: BitIndex, qvec, allowed, n: int) -> list:
    """Sessions by their best vector, comparing the query exactly with the PREFILTER closest vectors by sign bits."""
    cand = bits.candidates(qvec, allowed)
    vecs = store.by_rowid(bits.rowids[i] for i in cand)
    best = {}
    mul = operator.mul
    for i in cand:
        v = vecs.get(bits.rowids[i])
        if v is None:                       # the store changed after the index was read
            continue
        score, sid = sum(map(mul, qvec, v)), bits.groups[i]
        if score > best.get(sid, _NO_SCORE):
            best[sid] = score
    return sorted(best, key=lambda sid: (-best[sid], sid))[:n]


def dense(vectors: dict, qvec, allowed=None, n: int = POOL) -> list:
    """Keys of the n vectors closest to qvec (dot product of unit vectors), best first; only keys in allowed."""
    mul = operator.mul
    scored = [(sum(map(mul, qvec, v)), key) for key, v in vectors.items() if allowed is None or key in allowed]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [key for _, key in scored[:n]]
