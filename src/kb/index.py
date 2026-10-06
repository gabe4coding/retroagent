"""Local SQLite FTS5 index, built only from committed markdown (so it covers every host after a pull)."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kb.distill import parse_markdown

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, agent TEXT, host TEXT, project TEXT, cwd TEXT, branch TEXT,
  started TEXT, ended TEXT, model TEXT, turns INTEGER, user_turns INTEGER,
  title TEXT, summary TEXT, tags TEXT, outcome TEXT, decisions TEXT, summary_turns INTEGER,
  files TEXT, prs TEXT, parent TEXT, first_prompt TEXT, md_path TEXT, md_mtime REAL);
CREATE INDEX IF NOT EXISTS sessions_parent ON sessions(parent);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started);
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
  id UNINDEXED, title, summary, tags, decisions, first_prompt, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS turns(session_id TEXT, n INTEGER, role TEXT, time TEXT, text TEXT,
  PRIMARY KEY(session_id, n));
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
  session_id UNINDEXED, n UNINDEXED, text, tokenize='porter unicode61');
"""
_COLUMNS = 23
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


def fts_queries(text: str) -> list:
    """Safe FTS5 queries for free text: all terms (AND), then any term (OR)."""
    terms = [t.strip(".-/") for t in _TERM.findall(text or "")]
    quoted = ['"' + t.replace('"', "") + '"' for t in terms if t]
    if not quoted:
        return []
    return [" ".join(quoted), " OR ".join(quoted)] if len(quoted) > 1 else quoted


class Index:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- writing

    def update(self, root) -> int:
        """Re-read changed markdown files, drop deleted ones. Returns the number of sessions changed."""
        root = Path(root)
        known = {r["md_path"]: (r["id"], r["md_mtime"])
                 for r in self.db.execute("SELECT id, md_path, md_mtime FROM sessions")}
        seen, changed = set(), 0
        base = root / "sessions"
        for md in sorted(base.rglob("*.md")) if base.is_dir() else []:
            rel = md.relative_to(root).as_posix()
            seen.add(rel)
            mtime = md.stat().st_mtime
            if rel in known and known[rel][1] == mtime:
                continue
            meta, turns = parse_markdown(md.read_text(encoding="utf-8"))
            if not meta.get("id"):
                continue
            if rel in known:
                self._delete(known[rel][0])
            self._delete(meta["id"])
            self._insert(meta, turns, rel, mtime)
            changed += 1
        for rel, (sid, _) in known.items():
            if rel not in seen:
                self._delete(sid)
                changed += 1
        self.db.commit()
        return changed

    def _delete(self, sid: str) -> None:
        for sql in ("DELETE FROM sessions WHERE id=?", "DELETE FROM sessions_fts WHERE id=?",
                    "DELETE FROM turns WHERE session_id=?", "DELETE FROM turns_fts WHERE session_id=?"):
            self.db.execute(sql, (sid,))

    def _insert(self, m: dict, turns: list, rel: str, mtime: float) -> None:
        first = next((t["text"] for t in turns if t["role"] == "user"), "")[:500]
        js = lambda v: json.dumps(v or [], ensure_ascii=False)
        row = (m["id"], m.get("agent", ""), m.get("host", ""), m.get("project", ""), m.get("cwd", ""),
               m.get("branch", ""), m.get("started", ""), m.get("ended", ""), m.get("model", ""),
               m.get("turns", 0), m.get("user_turns", 0), m.get("title", ""), m.get("summary", ""),
               js(m.get("tags")), m.get("outcome", ""), js(m.get("decisions")), m.get("summary_turns", 0),
               js(m.get("files")), js(m.get("prs")), m.get("parent", "") or "", first, rel, mtime)
        self.db.execute(f"INSERT INTO sessions VALUES ({','.join('?' * _COLUMNS)})", row)
        self.db.execute("INSERT INTO sessions_fts VALUES (?,?,?,?,?,?)",
                        (m["id"], m.get("title", ""), m.get("summary", ""), " ".join(m.get("tags") or []),
                         "\n".join(m.get("decisions") or []), first))
        for t in turns:
            self.db.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (m["id"], t["n"], t["role"], t["time"], t["text"]))
            self.db.execute("INSERT INTO turns_fts VALUES (?,?,?)", (m["id"], t["n"], t["text"]))

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
            clauses.append("s.tags LIKE ?")
            params.append('%"' + f.tag + '"%')
        if not f.subagents:
            clauses.append("s.parent = ''")
        return "".join(" AND " + c for c in clauses), params

    def find(self, query: str, f: Filters = None, limit: int = 10, raw: bool = False) -> list:
        f = f or Filters()
        for q in ([query] if raw else fts_queries(query)):
            hits = self._find(q, f, limit)
            if hits:
                return hits
        return []

    def _find(self, q: str, f: Filters, limit: int) -> list:
        where, params = self._where(f)
        best = {}
        srows = self.db.execute(
            "SELECT sessions_fts.id AS id, bm25(sessions_fts, 0.0, 10.0, 5.0, 5.0, 5.0, 2.0) AS r, "
            "snippet(sessions_fts, -1, '«', '»', '…', 10) AS snip "
            "FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.id "
            f"WHERE sessions_fts MATCH ?{where} ORDER BY r LIMIT 200", [q] + params).fetchall()
        for r in srows:
            best[r["id"]] = {"score": r["r"], "snip": r["snip"], "turn": None}
        trows = self.db.execute(
            "SELECT turns_fts.session_id AS id, turns_fts.n AS n, bm25(turns_fts) AS r, "
            "snippet(turns_fts, 2, '«', '»', '…', 12) AS snip "
            "FROM turns_fts JOIN sessions s ON s.id = turns_fts.session_id "
            f"WHERE turns_fts MATCH ?{where} ORDER BY r LIMIT 500", [q] + params).fetchall()
        for r in trows:
            b = best.get(r["id"])
            if b is None:
                best[r["id"]] = {"score": r["r"], "snip": r["snip"], "turn": r["n"]}
            elif b["turn"] is None:
                b["score"] += r["r"]
                b["snip"], b["turn"] = r["snip"], r["n"]
        out = []
        for sid, b in sorted(best.items(), key=lambda kv: kv[1]["score"])[:limit]:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            out.append({**dict(row), "snippet": b["snip"], "turn": b["turn"]})
        return out

    def recent(self, f: Filters = None, limit: int = 20) -> list:
        where, params = self._where(f or Filters(subagents=False))
        sql = f"SELECT * FROM sessions s WHERE 1=1{where} ORDER BY started DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(sql, params + [limit])]

    def get(self, prefix: str):
        rows = self.db.execute("SELECT * FROM sessions WHERE substr(id, 1, ?) = ? ORDER BY started DESC LIMIT 6",
                               (len(prefix), prefix)).fetchall()
        if not rows:
            return None
        exact = [r for r in rows if r["id"] == prefix]
        if len(rows) > 1 and not exact:
            raise AmbiguousId([r["id"] for r in rows])
        return dict(exact[0] if exact else rows[0])

    def children(self, sid: str) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT id, title FROM sessions WHERE parent=? ORDER BY started", (sid,))]

    def paths_by_id(self, host: str) -> dict:
        return {r["id"]: r["md_path"] for r in self.db.execute("SELECT id, md_path FROM sessions WHERE host=?", (host,))}


def run_sql(path, query: str, limit: int = 200):
    """Read-only SQL. Returns (columns, rows)."""
    if not re.match(r"^\s*(select|with)\b", query or "", re.I):
        raise ValueError("only SELECT/WITH queries are allowed")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.execute(query)
        return [d[0] for d in cur.description or []], cur.fetchmany(limit)
    finally:
        con.close()
