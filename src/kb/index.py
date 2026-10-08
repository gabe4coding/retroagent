"""The local search index: one SQLite file with full-text search (FTS5) over the markdown in the data clone.

It is built only from the markdown files in git. After a pull, it covers the sessions of every machine. It holds:
- sessions (from sessions/), with one row per turn,
- memories (the memory files each machine copies under memories/),
- pages (the project pages and retros the pages routine writes under pages/).

The index is disposable: it never syncs, and update() rebuilds it from the markdown when its schema changes.

Terms:
- signature: "<mtime_ns>:<size>" of a markdown file. A new signature means the file changed and is read again.
- winner and duplicate: several files can hold the same session id (a session copied to another host). The most
  complete file wins and fills the sessions row. The others are duplicates: the dups table remembers them, so they
  are not parsed again, and one of them takes over when the winner goes away.
"""
from __future__ import annotations

import json
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from kb.distill import parse_markdown, split_front_matter
from kb.pages import page_rel, parse_page
from kb.util import short_id

SCHEMA_VERSION = 6          # bump when the tables change: the index is disposable, update() rebuilds it
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
    "CREATE TABLE IF NOT EXISTS dups(md_path TEXT PRIMARY KEY, md_sig TEXT, id TEXT, turns INTEGER, ended TEXT)",
    "CREATE INDEX IF NOT EXISTS dups_id ON dups(id)",
    """CREATE TABLE IF NOT EXISTS pages(path TEXT PRIMARY KEY, kind TEXT, name TEXT, title TEXT, updated TEXT,
  sessions INTEGER, sig TEXT)""",
    "CREATE INDEX IF NOT EXISTS pages_name ON pages(name)",
    # pages_fts rows use the rowid of their pages row
    """CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  path UNINDEXED, name, title, body, tokenize='porter unicode61')""",
    """CREATE TABLE IF NOT EXISTS memories(path TEXT PRIMARY KEY, ref TEXT, agent TEXT, host TEXT, project TEXT,
  name TEXT, description TEXT, type TEXT, origin_session TEXT, modified TEXT, sig TEXT)""",
    "CREATE INDEX IF NOT EXISTS memories_ref ON memories(ref)",
    # memories_fts rows use the rowid of their memories row
    """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  path UNINDEXED, name, description, body, tokenize='porter unicode61')""",
)
_TABLES = ("sessions_fts", "turns_fts", "sessions", "turns", "dups", "pages_fts", "pages", "memories_fts", "memories")
_COLUMNS = 24                # number of columns of the sessions table: keep it equal to the CREATE TABLE above
MIN_PREFIX = 4               # shortest id prefix get() accepts (an exact full id may be shorter)
SQL_TIMEOUT = 10.0           # seconds a run_sql query may take
SNIPPET_CHARS = 200          # longest snippet find() returns
FUSE_POOL = 50               # hits each ranking gives to the fusion when a dense ranking is passed
RRF_K = 60                   # reciprocal rank fusion constant
_INT64 = 2 ** 63             # SQLite stores an INTEGER in 64 bits: a value must be in [-_INT64, _INT64)
_SESSION_LIST_COLUMNS = "id, agent, host, project, started, title, parent"   # the columns of a session in a list
_FIRST_PROMPT_CHARS = 500    # characters of the first user prompt kept in the sessions row (and searched)
_LOOKUP_ROWS = 6             # rows a name or prefix lookup reads: enough to list the candidates of an ambiguous name
_SESSION_SNIPPET = "snippet(sessions_fts, -1, '«', '»', '…', 10)"
_PAGE_SNIPPET = "snippet(pages_fts, 3, '«', '»', '…', 12)"
_PAGE_ROW = "p.path, p.kind, p.name, p.title, p.updated, p.sessions"
_MEMORY_SNIPPET = "snippet(memories_fts, -1, '«', '»', '…', 12)"
_MEMORY_ROW = "m.path, m.ref, m.agent, m.host, m.project, m.name, m.description, m.type, m.origin_session, m.modified"
_MEMORY_TEXT = ("agent", "host", "project", "name", "description", "type", "origin_session", "modified")
_TURN_SNIPPET = "snippet(turns_fts, 2, '«', '»', '…', 12)"
_FTS_ERRORS = ("fts5:", "syntax error", "unterminated string", "unknown special query")
_TEXT_FIELDS = ("agent", "host", "project", "cwd", "branch", "started", "ended", "model", "title", "summary",
                "outcome", "parent")
_COUNT_FIELDS = ("turns", "user_turns", "summary_turns")
_LIST_FIELDS = ("tags", "decisions", "files", "prs")
_TERM = re.compile(r"\w[\w.\-/]*", re.U)
# Function words of English and Italian queries: in the OR fallback they match almost every session and push the
# real hits out. Dropped from free-text queries unless the query has nothing else.
_STOPWORDS = frozenset("""
a about after all also am an and any are as at be been before but by can could did do does doing done for from had
has have how i if in into is it its last me my no not of on or our so than that the their them then there these
they this those to too was we were what when where which who why will with would you your
al alla alle allo ai agli che chi come con cosa da dal dalla dei del della delle dello di e ed gli ha ho il in la
le lo ma mi nel nella nelle non per più quando se si sono su sul sulla un una uno
""".split())
_QUOTED = re.compile(r'"([^"]*)"')


@dataclass
class Filters:
    project: str = ""
    agent: str = ""
    host: str = ""
    since: str = ""
    until: str = ""
    tag: str = ""
    subagents: bool = True
    role: str = ""                 # "user" or "assistant": only turns by that role match (find)


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


def _phrase(text: str) -> str:
    """The terms of `text` as one FTS5 phrase ("" when it has none)."""
    terms = [t for t in (t.strip(".-/") for t in _TERM.findall(text)) if t]
    return '"' + " ".join(terms) + '"' if terms else ""


def fts_queries(text: str) -> list:
    """Safe FTS5 queries for free text, best first: the exact phrase, all terms (AND), then any term (OR).
    A "quoted" part of the text stays one phrase in the AND and OR queries. Stopwords are dropped from the single
    words of the AND and OR queries (never from the phrase or a quoted part), unless nothing else is left."""
    text = text or ""
    pieces = _QUOTED.split(text)                    # odd items were inside quotes
    terms = []                                      # (FTS5 phrase, is a single stopword)
    for i, piece in enumerate(pieces):
        inside_quotes = i % 2 == 1
        for p in [piece] if inside_quotes else _TERM.findall(piece):
            phrase = _phrase(p)
            if phrase:
                terms.append((phrase, not inside_quotes and p.strip(".-/").lower() in _STOPWORDS))
    if not terms:
        return []
    kept = [p for p, stop in terms if not stop] or [p for p, _ in terms]
    return list(dict.fromkeys([_phrase(text), " ".join(kept), " OR ".join(kept)]))


def fuse(lexical: list, dense: list, limit: int, k: int = RRF_K) -> list:
    """Reciprocal rank fusion of two ranked key lists: best first, ties by key."""
    score = {}
    for ranking in (lexical, dense):
        for rank, key in enumerate(ranking, 1):
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda key: (-score[key], key))[:limit]


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


def _beats(new: tuple, current: tuple) -> bool:
    """True when file `new` wins over file `current`. Both hold the same session id.

    rank = (turns, ended, path). More turns win, then a later end, then the first path in sort order."""
    if new[0] != current[0]:
        return new[0] > current[0]
    if new[1] != current[1]:
        return new[1] > current[1]
    return new[2] < current[2]


def _rank_of(row, path: str) -> tuple:
    """The rank (turns, ended, path) of a sessions or dups row."""
    return row["turns"] or 0, row["ended"] or "", path


class Index:
    def __init__(self, path, readonly: bool = False):
        """readonly: open an existing index without ever writing (no schema step, update() raises). It sees one
        snapshot for as long as it is open. Otherwise the file and its folder are created if needed."""
        self.path = Path(path)
        self.errors = []                       # (path, error) of files skipped by the last update()
        self.pages_changed = 0                 # pages added, changed or removed by the last update()
        self.memories_changed = 0              # memories added, changed or removed by the last update()
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
        """Re-read changed markdown files, drop deleted ones. Returns the number of sessions changed (the numbers of
        pages and memories changed are in self.pages_changed and self.memories_changed).

        A file that cannot be read or fails validation is skipped and listed in self.errors as (path, error)."""
        root = Path(root)
        self.errors = []
        files, pages, mems = self._scan(root, "sessions"), self._scan(root, "pages"), self._scan(root, "memories")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            changed = self._update(files)
            self.pages_changed = self._update_pages(pages)
            self.memories_changed = self._update_memories(mems)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return changed

    def _scan(self, root: Path, folder: str) -> dict:
        """{repo-relative path: (path, signature)} of the markdown files under root/folder."""
        files = {}
        base = root / folder
        for md in base.rglob("*.md") if base.is_dir() else []:
            rel = md.relative_to(root).as_posix()
            try:
                st = md.stat()
            except OSError as e:
                self.errors.append((rel, _why(e)))
                continue
            if stat.S_ISREG(st.st_mode):
                files[rel] = (md, f"{st.st_mtime_ns}:{st.st_size}")
        return files

    # Sessions: each session id has one winner file, which fills its sessions row. Other files with the same id are
    # duplicates, remembered in the dups table (see the module docstring and _beats).

    def _update(self, files: dict) -> int:
        """Bring the sessions in step with the files. Returns the number of session ids that changed."""
        known = {r["md_path"]: (r["id"], r["md_sig"])
                 for r in self.db.execute("SELECT id, md_path, md_sig FROM sessions")}
        dups = {r["md_path"]: (r["id"], r["md_sig"]) for r in self.db.execute("SELECT md_path, id, md_sig FROM dups")}
        touched, freed = set(), set()           # session ids changed / ids whose row was removed in this run
        self._ingest_changed(files, known, dups, touched, freed)
        self._drop_removed(files, known, dups, touched, freed)
        self._promote_duplicates(files, touched, freed)
        self._recheck_winners(files, touched, freed)
        return len(touched)

    def _ingest_changed(self, files: dict, known: dict, dups: dict, touched: set, freed: set) -> None:
        """Read each file that is new or has a new signature."""
        for rel in sorted(files):
            if (known.get(rel) or dups.get(rel) or (None, None))[1] != files[rel][1]:
                self._ingest(rel, files, touched, freed)

    def _drop_removed(self, files: dict, known: dict, dups: dict, touched: set, freed: set) -> None:
        """Remove the sessions and the duplicates whose file is gone."""
        for rel, (sid, _) in known.items():
            if rel not in files and self._delete(sid, only_path=rel):
                touched.add(sid)
                freed.add(sid)
        for rel in dups:
            if rel not in files:
                self.db.execute("DELETE FROM dups WHERE md_path=?", (rel,))

    def _promote_duplicates(self, files: dict, touched: set, freed: set) -> None:
        """A duplicate file takes over from a removed winner: the most complete one that can be read."""
        for sid in sorted(freed):
            if self.db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                continue
            for rel in self._dups(sid):
                if rel in files and self._ingest(rel, files, touched, freed) == "own":
                    break

    def _recheck_winners(self, files: dict, touched: set, freed: set) -> None:
        """A winner that changed may now lose to a remembered duplicate: compare it with the best one."""
        with_dups = {r[0] for r in self.db.execute("SELECT DISTINCT id FROM dups")}
        for sid in sorted(touched & with_dups):
            winner = self.db.execute("SELECT md_path, turns, ended FROM sessions WHERE id=?", (sid,)).fetchone()
            if winner is None:
                continue
            for rel in self._dups(sid):
                if rel in files:
                    best = self.db.execute("SELECT turns, ended FROM dups WHERE md_path=?", (rel,)).fetchone()
                    if _beats(_rank_of(best, rel), _rank_of(winner, winner["md_path"])):
                        self._ingest(rel, files, touched, freed)
                    break

    def _dups(self, sid: str) -> list:
        """Paths of the files that lost to the winner of this session id, the most complete first."""
        return [r[0] for r in self.db.execute(
            "SELECT md_path FROM dups WHERE id=? ORDER BY turns DESC, ended DESC, md_path", (sid,)).fetchall()]

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
            winner = self.db.execute("SELECT md_path, turns, ended FROM sessions WHERE id=?", (sid,)).fetchone()
            winner_path = winner["md_path"] if winner else None
            new_rank = (meta.get("turns") or 0, meta.get("ended") or "", rel)
            removed_ids = set()
            # this file held another session id before: that session's row goes
            previous = self.db.execute("SELECT id FROM sessions WHERE md_path=?", (rel,)).fetchone()
            if previous and previous["id"] != sid:
                self._delete(previous["id"])
                removed_ids.add(previous["id"])
            # another file that still exists wins this id now: compare the two
            has_rival = winner_path not in (None, rel) and winner_path in files
            if has_rival and _beats(_rank_of(winner, winner_path), new_rank):
                self.db.execute("INSERT OR REPLACE INTO dups VALUES (?,?,?,?,?)", (rel, sig, sid, *new_rank[:2]))
                result = "dup"
            else:
                if has_rival:                   # this file beats the current winner, which becomes a duplicate
                    self.db.execute("INSERT OR REPLACE INTO dups VALUES (?,?,?,?,?)",
                                    (winner_path, files[winner_path][1], sid, *_rank_of(winner, winner_path)[:2]))
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
        touched.update(removed_ids)
        freed.update(removed_ids)
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
        first = next((t["text"] for t in turns if t["role"] == "user"), "")[:_FIRST_PROMPT_CHARS]
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

    def _update_pages(self, files: dict) -> int:
        known = {r["path"]: r["sig"] for r in self.db.execute("SELECT path, sig FROM pages")}
        changed = 0
        for rel in sorted(files):
            if known.get(rel) != files[rel][1]:
                self._delete_page(rel)
                changed += rel in known             # a broken page is tried again each time, but counted once
                md, sig = files[rel]
                try:
                    meta, body = parse_page(md.read_bytes().decode("utf-8", errors="replace"))
                    if page_rel(meta["kind"], meta["name"]) != rel:
                        raise ValueError(f"a {meta['kind']} page named {meta['name']} belongs in "
                                         f"{page_rel(meta['kind'], meta['name'])}")
                except Exception as e:
                    self.errors.append((rel, _why(e)))
                    continue
                changed += rel not in known
                cur = self.db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)",
                                      (rel, meta["kind"], meta["name"], meta["title"], meta["updated"],
                                       meta["sessions"], sig))
                self.db.execute("INSERT INTO pages_fts(rowid, path, name, title, body) VALUES (?,?,?,?,?)",
                                (cur.lastrowid, rel, meta["name"], meta["title"], body))
        for rel in known:
            if rel not in files:
                self._delete_page(rel)
                changed += 1
        return changed

    def _delete_page(self, rel: str) -> None:
        row = self.db.execute("SELECT rowid FROM pages WHERE path=?", (rel,)).fetchone()
        if row is not None:
            self.db.execute("DELETE FROM pages_fts WHERE rowid=?", (row["rowid"],))
            self.db.execute("DELETE FROM pages WHERE path=?", (rel,))

    def _update_memories(self, files: dict) -> int:
        known = {r["path"]: r["sig"] for r in self.db.execute("SELECT path, sig FROM memories")}
        changed = 0
        for rel in sorted(files):
            if known.get(rel) == files[rel][1]:
                continue
            self._delete_memory(rel)
            changed += rel in known                 # a broken file is tried again each time, but counted once
            try:
                meta, body = parse_memory(rel, files[rel][0].read_bytes().decode("utf-8", errors="replace"))
            except Exception as e:
                self.errors.append((rel, _why(e)))
                continue
            changed += rel not in known
            cur = self.db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                  (rel, meta["ref"], *(meta[k] for k in _MEMORY_TEXT), files[rel][1]))
            self.db.execute("INSERT INTO memories_fts(rowid, path, name, description, body) VALUES (?,?,?,?,?)",
                            (cur.lastrowid, rel, meta["name"], meta["description"], body))
        for rel in known:
            if rel not in files:
                self._delete_memory(rel)
                changed += 1
        return changed

    def _delete_memory(self, rel: str) -> None:
        row = self.db.execute("SELECT rowid FROM memories WHERE path=?", (rel,)).fetchone()
        if row is not None:
            self.db.execute("DELETE FROM memories_fts WHERE rowid=?", (row["rowid"],))
            self.db.execute("DELETE FROM memories WHERE path=?", (rel,))

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

    def find(self, query: str, f: Filters = None, limit: int = 10, raw: bool = False, dense: list = None) -> list:
        """Ranked sessions, best first. Sessions with the exact phrase come first. The rest: the every-word (AND)
        ranking when it fills the list, else the any-word (OR) ranking, which holds every AND match too, so a few weak
        AND matches cannot push stronger OR matches out. dense: session ids ranked by embedding similarity (already
        filtered); fused with that ranking.

        Rows: id, agent, host, project, started, title, parent, snippet, turn (turn is None for a session-level hit).
        raw=True passes the query to FTS5 unchanged; a bad query raises ValueError."""
        f = f or Filters()
        if limit <= 0 or not (query or "").strip():
            return []
        # Many more candidates than asked: _candidates cuts the session-field list and the turn list to `pool` each,
        # then adds up the two scores of a session. A large pool keeps a session in both lists, so its sum is right.
        pool = max(200, limit * 20, FUSE_POOL)
        queries = [query] if raw else fts_queries(query)
        ranked = {}
        if len(queries) > 1:                           # [phrase, AND, OR]: the phrase is a strong signal on its own
            for c in self._candidates(queries[0], f, pool, raw):
                ranked.setdefault(c["id"], c)
            queries = queries[1:]
        cands = []
        for q in queries:
            cands = self._candidates(q, f, pool, raw)
            if len(ranked) + len(cands) >= limit:
                break
        for c in cands:
            ranked.setdefault(c["id"], c)
        lexical = list(ranked.values())
        if dense is None:
            return [self._hit(c) for c in lexical[:limit]]
        by_id = {c["id"]: c for c in lexical[:FUSE_POOL]}
        hits = (self._hit(by_id[sid]) if sid in by_id else self._dense_hit(sid)
                for sid in fuse([c["id"] for c in lexical[:FUSE_POOL]], dense, limit))
        return [h for h in hits if h is not None]

    def _dense_hit(self, sid: str):
        """A session only the embedding ranking found: its summary's start stands in for a snippet. None when the
        session is no longer in the index (its vector is older)."""
        row = self.db.execute(f"SELECT {_SESSION_LIST_COLUMNS}, summary FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        return {**d, "snippet": _short(d.pop("summary") or ""), "turn": None}

    def keys(self, kind: str, f: Filters = None, project: str = "") -> set:
        """Keys a search may return under these filters: session ids, page paths or memory paths."""
        f = f or Filters()
        if kind == "session":
            where, params = self._where(f)
            sql = f"SELECT s.id FROM sessions s WHERE 1=1{where}"
        elif kind == "page":
            where, params = self._page_where(project)
            sql = f"SELECT p.path FROM pages p WHERE 1=1{where}"
        else:
            where, params = self._memory_where(f)
            sql = f"SELECT m.path FROM memories m WHERE 1=1{where}"
        return {r[0] for r in self.db.execute(sql, params)}

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
        """Sessions matching q, best first. Score = session bm25 + best turn bm25 (negative: lower is better).

        Each row also keeps the FTS rowid of its session match and of its best turn, so _hit() can make the snippet."""
        where, params = self._where(f)
        best = {}
        # session fields (title, summary, …) have no role, so a role filter matches turns only
        # bm25 weights, one per sessions_fts column: id 0, title 10, summary 5, tags 5, decisions 5, first_prompt 2
        for r in [] if f.role else self._fts(
                "SELECT sessions_fts.rowid AS rid, sessions_fts.id AS id, "
                "bm25(sessions_fts, 0.0, 10.0, 5.0, 5.0, 5.0, 2.0) AS r "
                "FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.id "
                f"WHERE sessions_fts MATCH ?{where} ORDER BY r, sessions_fts.id LIMIT ?", [q] + params + [k], raw):
            best[r["id"]] = {"id": r["id"], "q": q, "score": r["r"], "session_fts_rowid": r["rid"],
                             "turn_fts_rowid": None, "turn": None}
        # best turn of each session, so one long session cannot fill the list
        join, role = ("JOIN turns tu ON tu.rowid = turns_fts.rowid ", " AND tu.role = ?") if f.role else ("", "")
        for r in self._fts(
                "WITH t AS MATERIALIZED (SELECT turns_fts.rowid AS rid, turns_fts.session_id AS id, "
                "turns_fts.n AS n, bm25(turns_fts) AS r FROM turns_fts JOIN sessions s ON s.id = turns_fts.session_id "
                f"{join}WHERE turns_fts MATCH ?{where}{role}), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY r, n) AS rn FROM t) "
                "SELECT id, n, r, rid FROM ranked WHERE rn = 1 ORDER BY r, id LIMIT ?",
                [q] + params + ([f.role] if f.role else []) + [k], raw):
            b = best.get(r["id"])
            if b is None:
                best[r["id"]] = {"id": r["id"], "q": q, "score": r["r"], "session_fts_rowid": None,
                                 "turn_fts_rowid": r["rid"], "turn": r["n"]}
            else:
                b["score"] += r["r"]
                b["turn_fts_rowid"], b["turn"] = r["rid"], r["n"]
        return sorted(best.values(), key=lambda b: (b["score"], b["id"]))

    def _hit(self, c: dict) -> dict:
        """Lean result row. The snippet is made only here, for the sessions that are returned."""
        if c["turn_fts_rowid"] is not None:
            sql = f"SELECT {_TURN_SNIPPET} FROM turns_fts WHERE turns_fts MATCH ? AND rowid = ?"
            rid = c["turn_fts_rowid"]
        else:
            sql = f"SELECT {_SESSION_SNIPPET} FROM sessions_fts WHERE sessions_fts MATCH ? AND rowid = ?"
            rid = c["session_fts_rowid"]
        snip = self.db.execute(sql, (c["q"], rid)).fetchone()
        row = self.db.execute(f"SELECT {_SESSION_LIST_COLUMNS} FROM sessions WHERE id=?", (c["id"],)).fetchone()
        return {**dict(row), "snippet": _short(snip[0] if snip else ""), "turn": c["turn"]}

    @staticmethod
    def _page_where(project: str):
        return (" AND p.kind = 'project' AND p.name = ? COLLATE NOCASE", [project]) if project else ("", [])

    def find_pages(self, query: str, project: str = "", limit: int = 3, raw: bool = False, dense: list = None) -> list:
        """Ranked pages, best first, the same way as find(). With project, only that project's page.
        dense: page paths ranked by embedding similarity (already filtered); fused with the BM25 ranking.

        Rows: path, kind, name, title, updated, sessions, snippet."""
        if limit <= 0 or not (query or "").strip():
            return []
        where, params = self._page_where(project)
        wanted = limit
        pool = FUSE_POOL if dense is not None else wanted      # the fusion needs a longer BM25 ranking
        ranked = {}
        for q in [query] if raw else fts_queries(query):
            # bm25 weights, one per pages_fts column: path 0, name 5, title 5, body 1
            for r in self._fts(
                    f"SELECT {_PAGE_ROW}, {_PAGE_SNIPPET} AS snippet, bm25(pages_fts, 0.0, 5.0, 5.0, 1.0) AS r "
                    f"FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid WHERE pages_fts MATCH ?{where} "
                    "ORDER BY r, p.path LIMIT ?", [q] + params + [pool], raw):
                ranked.setdefault(r["path"], {**{k: r[k] for k in r.keys() if k != "r"},
                                              "snippet": _short(r["snippet"])})
            if len(ranked) >= pool:
                break
        if dense is None:
            return list(ranked.values())[:wanted]
        rows = []
        for path in fuse(list(ranked)[:pool], dense, wanted):
            if path in ranked:
                rows.append(ranked[path])
            else:
                r = self.db.execute(f"SELECT {_PAGE_ROW} FROM pages p WHERE p.path = ?", (path,)).fetchone()
                rows.append({**dict(r), "snippet": _short(r["title"] or "")})
        return rows

    def pages(self) -> list:
        return [dict(r) for r in self.db.execute(f"SELECT {_PAGE_ROW} FROM pages p ORDER BY p.kind, p.name")]

    def page(self, name: str):
        """One page by name ("demo-app", "2026-W41"), by path, or by the start of its name. None when nothing
        matches; several candidates raise AmbiguousId (with their names)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("empty page name")
        for sql in ("p.name = ?", "p.path = ?", "p.name = ? COLLATE NOCASE"):
            rows = self.db.execute(f"SELECT {_PAGE_ROW} FROM pages p WHERE {sql} ORDER BY p.path", (name,)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
            if rows:
                raise AmbiguousId([r["path"] for r in rows])
        rows = self.db.execute(f"SELECT {_PAGE_ROW} FROM pages p WHERE substr(lower(p.name), 1, ?) = lower(?) "
                               f"ORDER BY p.name LIMIT {_LOOKUP_ROWS}", (len(name), name)).fetchall()
        if len(rows) > 1:
            raise AmbiguousId([r["name"] for r in rows])
        return dict(rows[0]) if rows else None

    def _memory_where(self, f: Filters):
        clauses, params = [], []
        for col in ("project", "agent", "host"):
            if getattr(f, col):
                clauses.append(f"m.{col} = ?" + (" COLLATE NOCASE" if col == "project" else ""))
                params.append(getattr(f, col))
        return "".join(" AND " + c for c in clauses), params

    def find_memories(self, query: str, f: Filters = None, limit: int = 3, raw: bool = False,
                      dense: list = None) -> list:
        """Ranked memories, best first, the same way as find(). Project, agent and host filters apply.
        dense: memory paths ranked by embedding similarity (already filtered); fused with the BM25 ranking.

        Rows: path, ref, agent, host, project, name, description, type, origin_session, modified, snippet."""
        if limit <= 0 or not (query or "").strip():
            return []
        where, params = self._memory_where(f or Filters())
        wanted = limit
        pool = FUSE_POOL if dense is not None else wanted      # the fusion needs a longer BM25 ranking
        ranked = {}
        for q in [query] if raw else fts_queries(query):
            # bm25 weights, one per memories_fts column: path 0, name 5, description 5, body 1
            for r in self._fts(
                    f"SELECT {_MEMORY_ROW}, {_MEMORY_SNIPPET} AS snippet, bm25(memories_fts, 0.0, 5.0, 5.0, 1.0) AS r "
                    f"FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid WHERE memories_fts MATCH ?"
                    f"{where} ORDER BY r, m.path LIMIT ?", [q] + params + [pool], raw):
                ranked.setdefault(r["path"], {**{k: r[k] for k in r.keys() if k != "r"},
                                              "snippet": _short(r["snippet"])})
            if len(ranked) >= pool:
                break
        if dense is None:
            return list(ranked.values())[:wanted]
        rows = []
        for path in fuse(list(ranked)[:pool], dense, wanted):
            if path in ranked:
                rows.append(ranked[path])
            else:
                r = self.db.execute(f"SELECT {_MEMORY_ROW} FROM memories m WHERE m.path = ?", (path,)).fetchone()
                rows.append({**dict(r), "snippet": _short(r["description"] or "")})
        return rows

    def memories(self, f: Filters = None) -> list:
        where, params = self._memory_where(f or Filters())
        return [dict(r) for r in self.db.execute(
            f"SELECT {_MEMORY_ROW} FROM memories m WHERE 1=1{where} ORDER BY m.project, m.ref, m.host", params)]

    def memory(self, ref: str):
        """One memory by path, by ref ("sessions-kb/prefer-small-prs"), by name or by the start of its ref. None when
        nothing matches; several candidates raise AmbiguousId (with their paths)."""
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("empty memory name")
        for sql in ("m.path = ?", "m.ref = ?", "m.name = ?", "m.ref = ? COLLATE NOCASE", "m.name = ? COLLATE NOCASE"):
            rows = self.db.execute(f"SELECT {_MEMORY_ROW} FROM memories m WHERE {sql} ORDER BY m.path",
                                   (ref,)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
            if rows:
                raise AmbiguousId([r["path"] for r in rows])
        rows = self.db.execute(f"SELECT {_MEMORY_ROW} FROM memories m WHERE substr(lower(m.ref), 1, ?) = lower(?) "
                               f"ORDER BY m.path LIMIT {_LOOKUP_ROWS}", (len(ref), ref)).fetchall()
        if len(rows) > 1:
            raise AmbiguousId([r["path"] for r in rows])
        return dict(rows[0]) if rows else None

    def recent(self, f: Filters = None, limit: int = 20) -> list:
        where, params = self._where(f or Filters(subagents=False))
        sql = f"SELECT {_SESSION_LIST_COLUMNS} FROM sessions s WHERE 1=1{where} ORDER BY started DESC LIMIT ?"
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
                               f"ORDER BY started DESC, id LIMIT {_LOOKUP_ROWS}", (n, prefix, n, prefix)).fetchall()
        if len(rows) > 1:
            raise AmbiguousId([r["id"] for r in rows])
        return dict(rows[0]) if rows else None

    def children(self, sid: str) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT id, title FROM sessions WHERE parent=? ORDER BY started", (sid,))]

    def paths_by_id(self, host: str) -> dict:
        return {r["id"]: r["md_path"] for r in self.db.execute("SELECT id, md_path FROM sessions WHERE host=?", (host,))}


def parse_memory(rel: str, text: str):
    """(meta, body) of a KB memory file, meta with every _MEMORY_TEXT field as text and its ref. Raises ValueError.

    The path must be memories/<host>/<agent>/… with the host and agent of the front matter. The ref is
    "<project or agent>/<file without .md>": short, and the same on every machine."""
    meta, body = split_front_matter(text)
    if meta.get("kind") != "memory":
        raise ValueError("not a memory file (kind must be \"memory\")")
    out = {}
    for key in _MEMORY_TEXT + ("file",):
        v = meta.get(key)
        out[key] = v if isinstance(v, str) else ("" if v is None else json.dumps(v, ensure_ascii=False))
    parts = rel.split("/")
    if len(parts) < 4 or parts[1] != out["host"] or parts[2] != out["agent"]:
        raise ValueError(f"a memory of host {out['host'] or '?'} and agent {out['agent'] or '?'} does not belong in {rel}")
    stem = out["file"][:-3] if out["file"].endswith(".md") else out["file"]
    out["ref"] = f"{out['project'] or out['agent']}/{stem or parts[-1][:-3]}"
    return out, body


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
