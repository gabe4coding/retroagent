"""Local SQLite FTS5 index, built only from committed markdown (so it covers every host after a pull)."""
from __future__ import annotations

import json
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from kb.distill import parse_markdown
from kb.util import short_id

SCHEMA_VERSION = 3          # bump when the tables change: the index is disposable, update() rebuilds it
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, agent TEXT, host TEXT, project TEXT, cwd TEXT, branch TEXT,
  started TEXT, ended TEXT, model TEXT, turns INTEGER, user_turns INTEGER,
  title TEXT, summary TEXT, tags TEXT, outcome TEXT, decisions TEXT, summary_turns INTEGER,
  files TEXT, prs TEXT, parent TEXT, first_prompt TEXT, md_path TEXT, md_sig TEXT, short TEXT)""",
    "CREATE INDEX IF NOT EXISTS sessions_parent ON sessions(parent)",
    "CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started)",
    "CREATE INDEX IF NOT EXISTS sessions_short ON sessions(short)",
    # FTS rows use the rowid of their base row (sessions.rowid, turns.rowid) so they can be deleted by rowid
    """CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
  id UNINDEXED, title, summary, tags, decisions, first_prompt, tokenize='porter unicode61')""",
    """CREATE TABLE IF NOT EXISTS turns(session_id TEXT, n INTEGER, role TEXT, time TEXT, text TEXT,
  PRIMARY KEY(session_id, n))""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
  session_id UNINDEXED, n UNINDEXED, text, tokenize='porter unicode61')""",
    # files that were read but lost to another file with the same id: remembered so they are not parsed again
    "CREATE TABLE IF NOT EXISTS dups(md_path TEXT PRIMARY KEY, md_sig TEXT, id TEXT)",
    "CREATE INDEX IF NOT EXISTS dups_id ON dups(id)",
)
_TABLES = ("sessions_fts", "turns_fts", "sessions", "turns", "dups")
_COLUMNS = 24
MIN_PREFIX = 4               # shortest id prefix get() accepts (an exact full id may be shorter)
SQL_TIMEOUT = 10.0           # seconds a run_sql query may take
SNIPPET_CHARS = 200          # longest snippet find() returns
_INT64 = 2 ** 63
_LEAN = "id, agent, host, project, started, title, parent"
_SESSION_SNIPPET = "snippet(sessions_fts, -1, '«', '»', '…', 10)"
_TURN_SNIPPET = "snippet(turns_fts, 2, '«', '»', '…', 12)"
_FTS_ERRORS = ("fts5:", "syntax error", "unterminated string", "unknown special query")
_TEXT_FIELDS = ("agent", "host", "project", "cwd", "branch", "started", "ended", "model", "title", "summary",
                "outcome", "parent")
_COUNT_FIELDS = ("turns", "user_turns", "summary_turns")
_LIST_FIELDS = ("tags", "decisions", "files", "prs")
_TERM = re.compile(r"\w[\w.\-/]*", re.U)


@dataclass
class Filters:
    project: str = ""
    agent: str = ""
    host: str = ""
    since: str = ""
    until: str = ""
    tag: str = ""
    subagents: bool = True


class AmbiguousId(Exception):
    pass


def _clean(meta: dict, turns: list):
    """Validate one parsed markdown file. Returns (meta, turns) with safe types, or raises ValueError."""
    sid = meta.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("id must be a non-empty string")
    out = {"id": sid}
    for key in _TEXT_FIELDS:
        v = meta.get(key)
        if v is None:
            out[key] = ""
        elif isinstance(v, str):
            out[key] = v
        elif isinstance(v, (int, float)):
            out[key] = str(v)
        else:
            raise ValueError(f"{key} must be text")
    for key in _COUNT_FIELDS:
        v = meta.get(key)
        if v is None:
            v = 0
        if not isinstance(v, int) or isinstance(v, bool) or not -_INT64 <= v < _INT64:
            raise ValueError(f"{key} must be an integer")
        out[key] = v
    for key in _LIST_FIELDS:
        v = meta.get(key)
        if v is None:
            v = []
        if not isinstance(v, list):
            raise ValueError(f"{key} must be a list")
        items = []
        for item in v:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                item = str(item)
            if not isinstance(item, str):
                raise ValueError(f"{key} must be a list of text")
            items.append(item)
        out[key] = items
    seen = set()
    for t in turns:
        if t["n"] >= _INT64:
            raise ValueError(f"turn number {t['n']} is too large")
        if t["n"] in seen:
            raise ValueError(f"duplicate turn number {t['n']}")
        seen.add(t["n"])
    return out, turns


def _why(e: Exception) -> str:
    return str(e) if isinstance(e, ValueError) and str(e) else f"{type(e).__name__}: {e}"


def _short(snippet: str) -> str:
    """One line of at most SNIPPET_CHARS characters, still showing the match (a 3,000-character token next to it
    would fill the screen)."""
    text = " ".join((snippet or "").split())
    if len(text) <= SNIPPET_CHARS:
        return text
    hit = text.find("«")
    start = max(hit - SNIPPET_CHARS // 3, 0)           # some context before the match
    chunk = text[start: start + SNIPPET_CHARS]
    if start:
        chunk = "…" + chunk[1:]
    if start + SNIPPET_CHARS < len(text):
        chunk = chunk[:-1] + "…"
    return chunk


def fts_queries(text: str) -> list:
    """Safe FTS5 queries for free text: all terms (AND), then any term (OR)."""
    terms = [t.strip(".-/") for t in _TERM.findall(text or "")]
    quoted = ['"' + t.replace('"', "") + '"' for t in terms if t]
    if not quoted:
        return []
    return [" ".join(quoted), " OR ".join(quoted)] if len(quoted) > 1 else quoted


def _probe(uri: str) -> sqlite3.Connection:
    con = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA user_version").fetchone()       # the first read: this is where a WAL database needs its files
    except BaseException:
        con.close()
        raise
    return con


def connect_readonly(path) -> sqlite3.Connection:
    """A connection that cannot write, for a folder that may be read-only (a sandbox). Raises sqlite3.Error.

    A WAL database needs its -wal and -shm files even to be read. They are gone after the last writer closed it, and a
    read-only folder cannot get them back. Then no writer is active, so the file is read as it is (immutable)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        return _probe(uri)
    except sqlite3.OperationalError:
        return _probe(uri + "&immutable=1")


class Index:
    def __init__(self, path, readonly: bool = False):
        """readonly: open an existing index without ever writing (no schema step, update() raises). It sees one
        snapshot for as long as it is open. Otherwise the file and its folder are created if needed."""
        self.path = Path(path)
        self.errors = []                       # (path, error) of files skipped by the last update()
        if readonly:
            self.db = connect_readonly(self.path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=1")
            self.db.execute("BEGIN")           # one snapshot for the whole command, even if a sync commits meanwhile
            self.rebuilt = False
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)   # explicit transactions
        self.db.row_factory = sqlite3.Row
        try:
            self.rebuilt = self._open_schema()     # True when the tables are new, so update() has to fill them
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def open_current(cls, path):
        """A read-only Index of an existing index file with this version's schema. None if the file is missing,
        unreadable or from another version (the caller then has to build it)."""
        if not Path(path).is_file():
            return None
        try:
            idx = cls(path, readonly=True)
        except sqlite3.Error:
            return None
        try:
            if idx.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                return idx
        except sqlite3.Error:
            pass
        idx.close()
        return None

    def _open_schema(self) -> bool:
        # WAL: a reader never waits for the writer (a sync). The mode is kept in the file; a file system that cannot
        # do WAL answers with another mode and the index keeps working the old way.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            have = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            stale = version != SCHEMA_VERSION and bool(have & set(_TABLES))
            if stale:
                for table in _TABLES:
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
            for sql in SCHEMA:
                self.db.execute(sql)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return stale or "sessions" not in have

    def close(self) -> None:
        self.db.close()

    # ---- writing

    def update(self, root) -> int:
        """Re-read changed markdown files, drop deleted ones. Returns the number of sessions changed.

        A file that cannot be read or fails validation is skipped and listed in self.errors as (path, error)."""
        root = Path(root)
        base = root / "sessions"
        self.errors = []
        files = {}
        for md in base.rglob("*.md") if base.is_dir() else []:
            rel = md.relative_to(root).as_posix()
            try:
                st = md.stat()
            except OSError as e:
                self.errors.append((rel, _why(e)))
                continue
            if stat.S_ISREG(st.st_mode):
                files[rel] = (md, f"{st.st_mtime_ns}:{st.st_size}")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            changed = self._update(files)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return changed

    def _update(self, files: dict) -> int:
        known = {r["md_path"]: (r["id"], r["md_sig"])
                 for r in self.db.execute("SELECT id, md_path, md_sig FROM sessions")}
        dups = {r["md_path"]: (r["id"], r["md_sig"]) for r in self.db.execute("SELECT md_path, id, md_sig FROM dups")}
        touched, freed = set(), set()           # session ids changed / ids whose row was removed in this run
        for rel in sorted(files):
            if (known.get(rel) or dups.get(rel) or (None, None))[1] != files[rel][1]:
                self._ingest(rel, files, touched, freed)
        for rel, (sid, _) in known.items():
            if rel not in files and self._delete(sid, only_path=rel):
                touched.add(sid)
                freed.add(sid)
        for rel in dups:
            if rel not in files:
                self.db.execute("DELETE FROM dups WHERE md_path=?", (rel,))
        for sid in sorted(freed):               # a duplicate file takes over from a removed winner
            if self.db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                continue
            for (rel,) in self.db.execute("SELECT md_path FROM dups WHERE id=? ORDER BY md_path", (sid,)).fetchall():
                if rel in files and self._ingest(rel, files, touched, freed) == "own":
                    break
        return len(touched)

    def _ingest(self, rel: str, files: dict, touched: set, freed: set) -> str:
        """Index one markdown file. Returns "own" (its session row now comes from this file), "dup" or "skip"."""
        md, sig = files[rel]
        try:
            meta, turns = _clean(*parse_markdown(md.read_bytes().decode("utf-8", errors="replace")))
        except Exception as e:
            self.errors.append((rel, _why(e)))
            return "skip"
        sid = meta["id"]
        self.db.execute("SAVEPOINT ingest")
        try:
            owner = self.db.execute("SELECT md_path FROM sessions WHERE id=?", (sid,)).fetchone()
            owner = owner["md_path"] if owner else None
            gone = set()
            stale = self.db.execute("SELECT id FROM sessions WHERE md_path=?", (rel,)).fetchone()
            if stale and stale["id"] != sid:
                self._delete(stale["id"])
                gone.add(stale["id"])
            # the first sorted path wins when several files carry the same id
            if owner not in (None, rel) and owner in files and owner < rel:
                self.db.execute("INSERT OR REPLACE INTO dups VALUES (?,?,?)", (rel, sig, sid))
                result = "dup"
            else:
                if owner not in (None, rel) and owner in files:    # this file beats the current owner, which now loses
                    self.db.execute("INSERT OR REPLACE INTO dups VALUES (?,?,?)", (owner, files[owner][1], sid))
                self._delete(sid)
                self._insert(meta, turns, rel, sig)
                self.db.execute("DELETE FROM dups WHERE md_path=?", (rel,))
                result = "own"
        except Exception as e:
            self.db.execute("ROLLBACK TO ingest")
            self.db.execute("RELEASE ingest")
            self.errors.append((rel, _why(e)))
            return "skip"
        self.db.execute("RELEASE ingest")
        touched.update(gone)
        freed.update(gone)
        if result == "own":
            touched.add(sid)
        return result

    def _delete(self, sid: str, only_path=None) -> bool:
        """Remove a session and its turns. With only_path, only when the row still comes from that file."""
        row = self.db.execute("SELECT rowid, md_path FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None or (only_path is not None and row["md_path"] != only_path):
            return False
        # FTS rows share the rowid of their base row, so they go by rowid instead of a table scan
        self.db.execute("DELETE FROM sessions_fts WHERE rowid=?", (row["rowid"],))
        self.db.execute("DELETE FROM turns_fts WHERE rowid IN (SELECT rowid FROM turns WHERE session_id=?)", (sid,))
        self.db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return True

    def _insert(self, m: dict, turns: list, rel: str, sig: str) -> None:
        first = next((t["text"] for t in turns if t["role"] == "user"), "")[:500]
        js = lambda v: json.dumps(v or [], ensure_ascii=False)
        row = (m["id"], m.get("agent", ""), m.get("host", ""), m.get("project", ""), m.get("cwd", ""),
               m.get("branch", ""), m.get("started", ""), m.get("ended", ""), m.get("model", ""),
               m.get("turns", 0), m.get("user_turns", 0), m.get("title", ""), m.get("summary", ""),
               js(m.get("tags")), m.get("outcome", ""), js(m.get("decisions")), m.get("summary_turns", 0),
               js(m.get("files")), js(m.get("prs")), m.get("parent", "") or "", first, rel, sig,
               short_id(m["id"]))
        cur = self.db.execute(f"INSERT INTO sessions VALUES ({','.join('?' * _COLUMNS)})", row)
        self.db.execute("INSERT INTO sessions_fts(rowid, id, title, summary, tags, decisions, first_prompt) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (cur.lastrowid, m["id"], m.get("title", ""), m.get("summary", ""),
                         " ".join(m.get("tags") or []), "\n".join(m.get("decisions") or []), first))
        self.db.executemany("INSERT INTO turns VALUES (?,?,?,?,?)",
                            [(m["id"], t["n"], t["role"], t["time"], t["text"]) for t in turns])
        self.db.execute("INSERT INTO turns_fts(rowid, session_id, n, text) "
                        "SELECT rowid, session_id, n, text FROM turns WHERE session_id=?", (m["id"],))

    # ---- reading

    def _where(self, f: Filters):
        clauses, params = [], []
        if f.project:
            clauses.append("s.project = ? COLLATE NOCASE")
            params.append(f.project)
        if f.agent:
            clauses.append("s.agent = ?")
            params.append(f.agent)
        if f.host:
            clauses.append("s.host = ?")
            params.append(f.host)
        if f.since:
            clauses.append("s.started >= ?")
            params.append(f.since)
        if f.until:
            clauses.append("s.started < ?")
            params.append(f.until)
        if f.tag:
            clauses.append("instr(s.tags, ?) > 0")           # the tag as stored (JSON-quoted); no LIKE wildcards
            params.append(json.dumps(f.tag, ensure_ascii=False))
        if not f.subagents:
            clauses.append("s.parent = ''")
        return "".join(" AND " + c for c in clauses), params

    def find(self, query: str, f: Filters = None, limit: int = 10, raw: bool = False) -> list:
        """Ranked sessions, best first: every word (AND) matches come first, any-word (OR) matches top them up.

        Rows: id, agent, host, project, started, title, parent, snippet, turn (turn is None for a session-level hit).
        raw=True passes the query to FTS5 unchanged; a bad query raises ValueError."""
        f = f or Filters()
        if limit <= 0 or not (query or "").strip():
            return []
        ranked = {}
        for q in [query] if raw else fts_queries(query):
            for cand in self._candidates(q, f, max(200, limit * 20), raw):
                ranked.setdefault(cand["id"], cand)
            if len(ranked) >= limit:
                break
        return [self._hit(c) for c in list(ranked.values())[:limit]]

    def _fts(self, sql: str, params: list, raw: bool) -> list:
        try:
            return self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            msg = str(e)
            if not raw:
                raise
            if msg.startswith("no such column"):         # a column filter that only exists in the other table
                return []
            if any(k in msg for k in _FTS_ERRORS):
                raise ValueError(f"bad FTS query: {msg}") from e
            raise

    def _candidates(self, q: str, f: Filters, k: int, raw: bool) -> list:
        """Sessions matching q, best first. Score = session bm25 + best turn bm25 (negative: lower is better)."""
        where, params = self._where(f)
        best = {}
        for r in self._fts(
                "SELECT sessions_fts.rowid AS rid, sessions_fts.id AS id, "
                "bm25(sessions_fts, 0.0, 10.0, 5.0, 5.0, 5.0, 2.0) AS r "
                "FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.id "
                f"WHERE sessions_fts MATCH ?{where} ORDER BY r, sessions_fts.id LIMIT ?", [q] + params + [k], raw):
            best[r["id"]] = {"id": r["id"], "q": q, "score": r["r"], "srid": r["rid"], "trid": None, "turn": None}
        # best turn of each session, so one long session cannot fill the list
        for r in self._fts(
                "WITH t AS MATERIALIZED (SELECT turns_fts.rowid AS rid, turns_fts.session_id AS id, "
                "turns_fts.n AS n, bm25(turns_fts) AS r FROM turns_fts JOIN sessions s ON s.id = turns_fts.session_id "
                f"WHERE turns_fts MATCH ?{where}), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY r, n) AS rn FROM t) "
                "SELECT id, n, r, rid FROM ranked WHERE rn = 1 ORDER BY r, id LIMIT ?", [q] + params + [k], raw):
            b = best.get(r["id"])
            if b is None:
                best[r["id"]] = {"id": r["id"], "q": q, "score": r["r"], "srid": None, "trid": r["rid"],
                                 "turn": r["n"]}
            else:
                b["score"] += r["r"]
                b["trid"], b["turn"] = r["rid"], r["n"]
        return sorted(best.values(), key=lambda b: (b["score"], b["id"]))

    def _hit(self, c: dict) -> dict:
        """Lean result row. The snippet is made only here, for the sessions that are returned."""
        if c["trid"] is not None:
            sql, rid = f"SELECT {_TURN_SNIPPET} FROM turns_fts WHERE turns_fts MATCH ? AND rowid = ?", c["trid"]
        else:
            sql, rid = f"SELECT {_SESSION_SNIPPET} FROM sessions_fts WHERE sessions_fts MATCH ? AND rowid = ?", c["srid"]
        snip = self.db.execute(sql, (c["q"], rid)).fetchone()
        row = self.db.execute(f"SELECT {_LEAN} FROM sessions WHERE id=?", (c["id"],)).fetchone()
        return {**dict(row), "snippet": _short(snip[0] if snip else ""), "turn": c["turn"]}

    def recent(self, f: Filters = None, limit: int = 20) -> list:
        where, params = self._where(f or Filters(subagents=False))
        sql = f"SELECT {_LEAN} FROM sessions s WHERE 1=1{where} ORDER BY started DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(sql, params + [limit])]

    def get(self, prefix: str):
        """One session by full id, by the start of its id, or by the start of its short id. None when nothing matches.

        An exact full id always wins. Otherwise the prefix needs MIN_PREFIX characters, and several candidates raise
        AmbiguousId (with their full ids). An empty prefix raises ValueError."""
        if not prefix:
            raise ValueError("empty id prefix")
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (prefix,)).fetchone()
        if row is not None:
            return dict(row)
        if len(prefix) < MIN_PREFIX:
            return None
        n = len(prefix)
        rows = self.db.execute("SELECT * FROM sessions WHERE substr(id, 1, ?) = ? OR substr(short, 1, ?) = ? "
                               "ORDER BY started DESC, id LIMIT 6", (n, prefix, n, prefix)).fetchall()
        if len(rows) > 1:
            raise AmbiguousId([r["id"] for r in rows])
        return dict(rows[0]) if rows else None

    def children(self, sid: str) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT id, title FROM sessions WHERE parent=? ORDER BY started", (sid,))]

    def paths_by_id(self, host: str) -> dict:
        return {r["id"]: r["md_path"] for r in self.db.execute("SELECT id, md_path FROM sessions WHERE host=?", (host,))}


def run_sql(path, query: str, limit: int = 200):
    """Read-only SQL (one SELECT/WITH statement, at most SQL_TIMEOUT seconds). Returns (columns, rows)."""
    if not re.match(r"^\s*(select|with)\b", query or "", re.I):
        raise ValueError("only SELECT/WITH queries are allowed")
    con = connect_readonly(path)
    try:
        con.execute("PRAGMA query_only=1")
        deadline = time.monotonic() + SQL_TIMEOUT
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            cur = con.execute(query)
            return [d[0] for d in cur.description or []], cur.fetchmany(limit)
        except (sqlite3.Warning, sqlite3.ProgrammingError) as e:      # Python 3.9 warns, newer versions raise
            if isinstance(e, sqlite3.Warning) or "one statement" in str(e):
                raise ValueError("only one SQL statement is allowed") from None
            raise
        except sqlite3.OperationalError as e:
            if str(e) == "interrupted":
                raise ValueError(f"query aborted after {SQL_TIMEOUT:g} s; make it cheaper") from None
            raise
    finally:
        con.close()
