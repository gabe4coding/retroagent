import os
import sqlite3
import time
from collections import Counter

import pytest
from fixtures import AID, SID, T1, T2, make_claude_tree, make_codex_tree

from kb.adapters import claude, codex
from kb.distill import dump_front_matter
from kb.index import AmbiguousId, Filters, Index, fts_queries, run_sql
from kb.store import write_session


def build_kb(tmp_path):
    """Write the Claude and Codex fixtures into <tmp>/kb as host 'h'. Returns the root."""
    root = tmp_path / "kb"
    s = claude.parse_unit(claude.discover(make_claude_tree(tmp_path / "c"))[0])
    write_session(root, "h", s, Counter())
    sessions, home = make_codex_tree(tmp_path / "x")
    titles = codex.load_titles(home)
    for u in codex.discover([sessions]):
        cs = codex.parse_unit(u, titles)
        if cs:
            write_session(root, "h", cs, Counter())
    return root


def put(root, rel, sid, turns=("hello world",), **meta):
    """Write one synthetic session markdown under <root>/sessions/<rel>. Turns alternate user/assistant."""
    m = {"id": sid, "agent": "claude", "host": "h", "project": "demo", "started": "2026-10-06T10:00:00Z",
         "title": "title " + str(sid), "turns": len(turns), "user_turns": (len(turns) + 1) // 2, "tags": [],
         "parent": "", **meta}
    body = "".join(f"## [{i}] {'user' if i % 2 else 'assistant'} · 10:00\n\n{t}\n\n" for i, t in enumerate(turns, 1))
    path = root / "sessions" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_front_matter(m) + "\n" + body, encoding="utf-8")
    return path


@pytest.fixture
def empty(tmp_path):
    root = tmp_path / "kb"
    idx = Index(root / ".kb" / "index.sqlite")
    yield root, idx
    idx.close()


@pytest.fixture
def kb(tmp_path):
    root = build_kb(tmp_path)
    idx = Index(root / ".kb" / "index.sqlite")
    assert idx.update(root) == 4          # claude main + sub, codex T1 + T2
    yield root, idx
    idx.close()


def test_update_is_incremental(kb):
    root, idx = kb
    assert idx.update(root) == 0
    md = next((root / "sessions").rglob("*_55555555.md"))
    md.write_text(md.read_text(encoding="utf-8").replace("Retro done.", "Retro finished."), encoding="utf-8")
    st = md.stat()
    os.utime(md, (st.st_atime, st.st_mtime + 10))
    assert idx.update(root) == 1
    assert idx.find("finished")[0]["id"] == SID
    md.unlink()
    assert idx.update(root) == 1 and idx.get(SID) is None


def test_find_ranks_and_snippets(kb):
    _, idx = kb
    hits = idx.find("flaky motion")
    assert hits[0]["id"] == SID and "«" in hits[0]["snippet"]
    sub = idx.find("setTimeout")
    assert sub[0]["id"] == AID and sub[0]["turn"] == 2


def test_find_filters_and_fallback(kb):
    _, idx = kb
    assert [h["id"] for h in idx.find("retry", Filters(agent="codex"))] == [T1]
    assert idx.find("retry", Filters(agent="claude")) == []
    assert idx.find("fetch zzznotthere")[0]["id"] in (T1, T2)     # AND finds nothing, OR fallback
    assert idx.find("") == []
    idx.find('a "b" (c) *d* OR NEAR')                              # must not raise


def test_few_and_matches_do_not_outrank_strong_or_matches(empty):
    root, idx = empty
    filler = " ".join(f"word{i}" for i in range(300))
    put(root, "a/weak.md", "weak", turns=(f"deploy {filler} flaky",))       # every word, once, in a long turn
    put(root, "a/strong.md", "strong", turns=("flaky flaky flaky",), title="flaky test fix")
    idx.update(root)
    assert [h["id"] for h in idx.find("deploy flaky")] == ["strong", "weak"]   # OR ranking: AND has < limit hits
    assert [h["id"] for h in idx.find("deploy flaky", limit=1)] == ["weak"]    # AND fills the list: AND ranking


def test_fts_queries_drop_stopwords():
    assert fts_queries("how did I fix the flaky test") == ['"fix" "flaky" "test"', '"fix" OR "flaky" OR "test"']
    assert fts_queries("analisi dei test più lenti") == ['"analisi" "test" "lenti"', '"analisi" OR "test" OR "lenti"']
    assert fts_queries("the") == ['"the"']                          # only stopwords: keep them


def test_recent_get_children_sql(kb):
    root, idx = kb
    assert [r["id"] for r in idx.recent(Filters(subagents=False))] == [SID, T1]
    assert idx.get(SID[:8])["title"] == "Fix flaky motion test"
    with pytest.raises(AmbiguousId):
        idx.get("01a0c")
    assert [c["id"] for c in idx.children(SID)] == [AID]
    assert [c["id"] for c in idx.children(T1)] == [T2]
    assert idx.paths_by_id("h")[SID].endswith("_55555555.md")
    cols, rows = run_sql(root / ".kb" / "index.sqlite", "SELECT COUNT(*) AS n FROM sessions")
    assert cols == ["n"] and rows[0][0] == 4
    with pytest.raises(ValueError):
        run_sql(root / ".kb" / "index.sqlite", "DELETE FROM sessions")


# ---- review fixes

def test_moved_session_is_visible_after_one_update(empty):
    root, idx = empty
    old = put(root, "h/2026-10/old_aaaaaaaa.md", "aaaaaaaa-1")
    assert idx.update(root) == 1
    put(root, "h/2026-11/new_aaaaaaaa.md", "aaaaaaaa-1")
    old.unlink()
    changed = idx.update(root)
    assert idx.get("aaaaaaaa-1")["md_path"] == "sessions/h/2026-11/new_aaaaaaaa.md"
    assert changed == 1                                       # one session moved, not "one added, one removed"
    assert idx.update(root) == 0


def test_duplicate_id_has_a_stable_winner_and_the_loser_is_not_reparsed(empty, monkeypatch):
    root, idx = empty
    b = put(root, "h/b.md", "dup-1", title="from b")
    idx.update(root)
    assert idx.get("dup-1")["md_path"] == "sessions/h/b.md"
    a = put(root, "h/a.md", "dup-1", title="from a")          # sorts first, so it wins whatever the history
    idx.update(root)
    assert idx.get("dup-1")["md_path"] == "sessions/h/a.md"
    calls = []
    import kb.index
    real = kb.index.parse_markdown
    monkeypatch.setattr(kb.index, "parse_markdown", lambda text: calls.append(1) or real(text))
    assert idx.update(root) == 0 and calls == []              # the loser is remembered, not parsed again
    a.unlink()
    assert idx.update(root) == 1                              # the loser takes over in the same update
    assert idx.get("dup-1")["md_path"] == "sessions/h/b.md" and idx.get("dup-1")["title"] == "from b"
    b.unlink()
    idx.update(root)
    assert idx.get("dup-1") is None


def test_duplicate_ids_found_in_one_run_pick_the_first_sorted_path(empty):
    root, idx = empty
    put(root, "h/z.md", "dup-1", title="from z")
    put(root, "h/m.md", "dup-1", title="from m")
    put(root, "h/a.md", "dup-1", title="from a")
    assert idx.update(root) == 1 and idx.get("dup-1")["title"] == "from a"
    assert idx.update(root) == 0
    (root / "sessions/h/a.md").unlink()
    assert idx.update(root) == 1 and idx.get("dup-1")["title"] == "from m"   # next in sorted order takes over
    (root / "sessions/h/m.md").unlink()
    assert idx.update(root) == 1 and idx.get("dup-1")["title"] == "from z"
    assert idx.db.execute("SELECT COUNT(*) FROM dups").fetchone()[0] == 0


def test_bad_files_are_skipped_and_reported_not_fatal(empty):
    root, idx = empty
    put(root, "h/good.md", "good-1", turns=("fine session about retries",))
    odd = put(root, "h/odd_utf8.md", "odd-1", turns=("before after",))
    odd.write_bytes(odd.read_bytes().replace(b"before after", b"before \xff\xfe after"))
    put(root, "h/id_list.md", ["a"])
    put(root, "h/tags_int.md", "tags-1", tags=5)
    put(root, "h/dup_header.md", "dup-1", turns=("one", "two"))
    dup = root / "sessions/h/dup_header.md"
    dup.write_text(dup.read_text(encoding="utf-8") + "## [2] user · 10:05\n\nagain\n", encoding="utf-8")
    huge = put(root, "h/huge_turn.md", "huge-1")
    huge.write_text(huge.read_text(encoding="utf-8") + "## [99999999999999999999] user · 10:05\n\nbig\n", encoding="utf-8")
    put(root, "h/no_id.md", "")
    put(root, "h/count_bad.md", "count-1", user_turns="many")
    put(root, "h/files_bad.md", "files-1", files={"a": 1})

    assert idx.update(root) == 2                                        # good + the one with invalid UTF-8 (replaced)
    assert idx.get("good-1") is not None
    assert "�" in idx.db.execute("SELECT text FROM turns WHERE session_id='odd-1'").fetchone()[0]
    bad = {"h/id_list.md", "h/tags_int.md", "h/dup_header.md", "h/huge_turn.md", "h/no_id.md", "h/count_bad.md",
           "h/files_bad.md"}
    assert {p for p, _ in idx.errors} == {"sessions/" + b for b in bad}
    assert all(isinstance(e, str) and e for _, e in idx.errors)
    for sid in ("tags-1", "dup-1", "huge-1", "count-1", "files-1"):
        assert idx.get(sid) is None
        assert idx.db.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)).fetchone()[0] == 0
    assert idx.find("retries")[0]["id"] == "good-1"

    put(root, "h/tags_int.md", "tags-1", tags=["ok"])                   # fixed files are picked up, errors reset
    assert idx.update(root) == 1
    assert idx.get("tags-1") is not None and "sessions/h/tags_int.md" not in {p for p, _ in idx.errors}
    assert len(idx.errors) == len(bad) - 1


def test_failed_insert_rolls_back_only_that_file(empty, monkeypatch):
    root, idx = empty
    put(root, "h/a.md", "a-1", turns=("alpha one", "alpha two"))
    put(root, "h/b.md", "b-1", turns=("bravo one", "bravo two"))
    put(root, "h/c.md", "c-1", turns=("charlie one", "charlie two"))
    real = Index._insert

    def flaky(self, m, turns, rel, sig):
        real(self, m, turns, rel, sig)
        if m["id"] == "b-1":
            raise RuntimeError("boom after inserting")

    monkeypatch.setattr(Index, "_insert", flaky)
    assert idx.update(root) == 2
    assert [p for p, _ in idx.errors] == ["sessions/h/b.md"] and "boom" in idx.errors[0][1]
    assert idx.get("b-1") is None and idx.get("a-1") and idx.get("c-1")
    counts = [idx.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("sessions", "sessions_fts", "turns", "turns_fts")]
    assert counts == [2, 2, 4, 4]


def test_escaped_exception_rolls_back_the_whole_update(empty, monkeypatch):
    root, idx = empty
    put(root, "h/a.md", "a-1")
    put(root, "h/b.md", "b-1")
    import kb.index
    real = kb.index.parse_markdown
    calls = []

    def interrupted(text):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(text)

    monkeypatch.setattr(kb.index, "parse_markdown", interrupted)
    with pytest.raises(KeyboardInterrupt):
        idx.update(root)
    assert not idx.db.in_transaction
    assert idx.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    monkeypatch.setattr(kb.index, "parse_markdown", real)
    assert idx.update(root) == 2


def _counts(idx):
    return [idx.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sessions", "sessions_fts", "turns", "turns_fts")]


def test_fts_rows_share_rowids_with_the_base_tables(empty):
    root, idx = empty
    for i in range(5):
        put(root, f"h/s{i}.md", f"s-{i}", turns=(f"first {i}", f"second {i}", f"third {i}"))
    idx.update(root)
    put(root, "h/s2.md", "s-2", turns=("only one turn left",))
    idx.update(root)
    (root / "sessions/h/s0.md").unlink()
    idx.update(root)
    assert _counts(idx) == [4, 4, 10, 10]
    assert idx.db.execute("SELECT COUNT(*) FROM sessions s JOIN sessions_fts f ON f.rowid = s.rowid "
                          "WHERE f.id = s.id").fetchone()[0] == 4
    assert idx.db.execute("SELECT COUNT(*) FROM turns t JOIN turns_fts f ON f.rowid = t.rowid "
                          "WHERE f.session_id = t.session_id AND f.n = t.n").fetchone()[0] == 10
    assert idx.find("second") and "s-2" not in [h["id"] for h in idx.find("second")] and idx.find("left")[0]["id"] == "s-2"


def test_deletes_go_by_rowid_and_new_ids_are_not_deleted_first(empty):
    root, idx = empty
    put(root, "h/a.md", "a-1", turns=("alpha", "beta"))
    idx.update(root)
    stmts = []
    idx.db.set_trace_callback(stmts.append)
    put(root, "h/b.md", "b-1", turns=("gamma", "delta"))
    idx.update(root)
    assert [s for s in stmts if s.startswith("DELETE FROM sessions") or s.startswith("DELETE FROM turns")] == []
    stmts.clear()
    (root / "sessions/h/a.md").unlink()
    idx.update(root)
    deletes = [s for s in stmts if s.startswith("DELETE FROM turns_fts") or s.startswith("DELETE FROM sessions_fts")]
    assert deletes and all("rowid" in s for s in deletes)


def test_building_300_sessions_of_20_turns_is_fast(empty):
    root, idx = empty
    for i in range(300):
        put(root, f"h/2026-10/s{i:03}.md", f"sess-{i:03}",
            turns=tuple(f"turn {j} of session {i} about topic{i % 7} and widget{j}" for j in range(20)))
    t0 = time.perf_counter()
    assert idx.update(root) == 300
    build = time.perf_counter() - t0
    for i in range(0, 300, 3):                                   # re-index 100 changed sessions
        put(root, f"h/2026-10/s{i:03}.md", f"sess-{i:03}", turns=("rewritten",) * 20)
    t0 = time.perf_counter()
    assert idx.update(root) == 100
    rebuild = time.perf_counter() - t0
    assert build < 5 and rebuild < 5, f"build {build:.2f}s, rebuild {rebuild:.2f}s"


OLD_SCHEMA = """
CREATE TABLE sessions(
  id TEXT PRIMARY KEY, agent TEXT, host TEXT, project TEXT, cwd TEXT, branch TEXT,
  started TEXT, ended TEXT, model TEXT, turns INTEGER, user_turns INTEGER,
  title TEXT, summary TEXT, tags TEXT, outcome TEXT, decisions TEXT, summary_turns INTEGER,
  files TEXT, prs TEXT, parent TEXT, first_prompt TEXT, md_path TEXT, md_mtime REAL);
CREATE VIRTUAL TABLE sessions_fts USING fts5(
  id UNINDEXED, title, summary, tags, decisions, first_prompt, tokenize='porter unicode61');
CREATE TABLE turns(session_id TEXT, n INTEGER, role TEXT, time TEXT, text TEXT, PRIMARY KEY(session_id, n));
CREATE VIRTUAL TABLE turns_fts USING fts5(session_id UNINDEXED, n UNINDEXED, text, tokenize='porter unicode61');
INSERT INTO sessions(id, md_path, md_mtime) VALUES ('stale-1', 'sessions/h/gone.md', 1.0);
INSERT INTO sessions_fts(rowid, id, title) VALUES (7, 'stale-1', 'stale');
"""


@pytest.mark.parametrize("version", [0, 1, 2])
def test_index_from_another_schema_version_is_dropped_and_rebuilt(tmp_path, version):
    import kb.index
    path = tmp_path / "kb" / ".kb" / "index.sqlite"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA + f"PRAGMA user_version={version};")
    con.close()
    root = tmp_path / "kb"
    put(root, "h/a.md", "a-1")
    idx = Index(path)
    try:
        assert idx.rebuilt is True
        assert idx.db.execute("PRAGMA user_version").fetchone()[0] == kb.index.SCHEMA_VERSION
        cols = [r[1] for r in idx.db.execute("PRAGMA table_info(sessions)")]
        assert "md_sig" in cols and "short" in cols
        assert idx.get("stale-1") is None
        assert idx.update(root) == 1 and idx.get("a-1")
    finally:
        idx.close()
    idx = Index(path)                                              # same version: kept as it is
    try:
        assert idx.rebuilt is False and idx.get("a-1") and idx.update(root) == 0
    finally:
        idx.close()


def test_change_detection_uses_mtime_ns_and_size(empty):
    root, idx = empty
    md = put(root, "h/a.md", "a-1", turns=("short",))
    st = md.stat()
    assert idx.update(root) == 1
    put(root, "h/a.md", "a-1", turns=("a much longer text than before",))
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns))             # same mtime, different size
    assert md.stat().st_mtime_ns == st.st_mtime_ns and md.stat().st_size != st.st_size
    assert idx.update(root) == 1
    assert idx.find("longer")[0]["id"] == "a-1"
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns + 1))         # a 1 ns touch is still a change
    assert idx.update(root) == 1
    st = md.stat()
    assert idx.db.execute("SELECT md_sig FROM sessions").fetchone()[0] == f"{st.st_mtime_ns}:{st.st_size}"


def test_one_long_session_does_not_crowd_out_other_sessions(empty):
    root, idx = empty
    put(root, "h/big.md", "big-0", turns=("start",) + ("retry retry retry",) * 1200)
    for i in range(20):
        put(root, f"h/o{i:02}.md", f"other-{i:02}",
            turns=("start", "a rather longer note that mentions retry only once among many other plain words"))
    idx.update(root)
    ids = [h["id"] for h in idx.find("retry", limit=10)]
    assert len(ids) == 10 and len(set(ids)) == 10 and ids[0] == "big-0"
    everyone = [h["id"] for h in idx.find("retry", limit=50)]
    assert len(everyone) == 21 and len(set(everyone)) == 21


def test_and_hits_come_first_then_or_hits_top_up(empty):
    root, idx = empty
    put(root, "h/a.md", "sess-a", title="flaky checkout", turns=("start", "the motion library is slow"))
    put(root, "h/b.md", "sess-b", title="other thing", turns=("start", "flaky and motion in one turn"))
    idx.update(root)
    assert [h["id"] for h in idx.find("flaky motion")] == ["sess-b", "sess-a"]
    assert [h["id"] for h in idx.find("flaky motion", limit=1)] == ["sess-b"]
    assert [h["id"] for h in idx.find("flaky motion", Filters(project="nothing"))] == []


def test_find_and_recent_rows_are_lean(kb):
    _, idx = kb
    hit = idx.find("flaky motion")[0]
    assert set(hit) == {"id", "agent", "host", "project", "started", "title", "parent", "snippet", "turn"}
    assert hit["id"] == SID and hit["title"] == "Fix flaky motion test" and hit["agent"] == "claude"
    sub = idx.find("setTimeout")[0]
    assert sub["id"] == AID and sub["parent"] == SID and sub["turn"] == 2
    assert set(idx.recent(Filters(subagents=False))[0]) == {"id", "agent", "host", "project", "started", "title", "parent"}
    assert {"summary", "first_prompt", "files", "md_path"} <= set(idx.get(SID))   # get() still returns the whole row


def test_tag_filter_treats_underscore_and_percent_literally(empty):
    root, idx = empty
    put(root, "h/a.md", "a-1", tags=["a_b"], turns=("start", "needle"))
    put(root, "h/b.md", "b-1", tags=["axb", "100%"], turns=("start", "needle"))
    put(root, "h/c.md", "c-1", tags=["a"], turns=("start", "needle"))
    idx.update(root)
    ids = lambda tag: sorted(h["id"] for h in idx.find("needle", Filters(tag=tag)))
    assert ids("a_b") == ["a-1"] and ids("a") == ["c-1"] and ids("100%") == ["b-1"]
    assert ids("a%") == [] and ids("_") == [] and ids("%") == []
    assert [r["id"] for r in idx.recent(Filters(tag="a_b"))] == ["a-1"]


def test_raw_fts_errors(kb):
    _, idx = kb
    assert idx.find("text:setTimeout", raw=True)[0]["id"] == AID   # sessions_fts has no `text` column: no hits there
    assert idx.find("title:flaky", raw=True)[0]["id"] == SID       # turns_fts has no `title` column: no hits there
    assert idx.find("nosuchcolumn:flaky", raw=True) == []
    assert idx.find("", raw=True) == []
    for bad in ('"unterminated', "flaky AND", "flaky (", "NEAR(flaky", "*"):
        with pytest.raises(ValueError, match="bad FTS query"):
            idx.find(bad, raw=True)


def test_get_rejects_an_empty_prefix(kb):
    _, idx = kb
    with pytest.raises(ValueError):
        idx.get("")


def test_run_sql_rejects_multiple_statements(kb):
    root, _ = kb
    path = root / ".kb" / "index.sqlite"
    for query in ("SELECT 1; SELECT 2", "SELECT 1; DELETE FROM sessions"):
        with pytest.raises(ValueError, match="one SQL statement"):
            run_sql(path, query)
    assert run_sql(path, "SELECT 1 AS one;")[1][0][0] == 1          # a trailing semicolon is fine


def test_run_sql_cannot_write(kb):
    root, _ = kb
    path = root / ".kb" / "index.sqlite"
    with pytest.raises(sqlite3.Error):
        run_sql(path, "WITH gone AS (SELECT id FROM sessions) DELETE FROM sessions WHERE id IN (SELECT id FROM gone)")
    with pytest.raises(sqlite3.Error):
        run_sql(path, "WITH x AS (SELECT 1) INSERT INTO sessions(id) VALUES ('z')")
    assert run_sql(path, "SELECT COUNT(*) FROM sessions")[1][0][0] == 4


def test_run_sql_opens_paths_with_uri_characters(tmp_path):
    path = tmp_path / "a#b c%41?d" / "index.sqlite"
    Index(path).close()
    assert run_sql(path, "SELECT COUNT(*) AS n FROM sessions") == (["n"], [(0,)])


def test_run_sql_aborts_a_runaway_query(kb, monkeypatch):
    from kb import index as kbindex
    root, _ = kb
    monkeypatch.setattr(kbindex, "SQL_TIMEOUT", 0.5)
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match="aborted after"):
        run_sql(root / ".kb" / "index.sqlite",
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c")
    assert time.perf_counter() - t0 < 5


# ---- short ids

def test_short_column_is_filled_and_indexed(kb):
    _, idx = kb
    rows = dict(idx.db.execute("SELECT id, short FROM sessions").fetchall())
    assert rows == {SID: "55555555", AID: "e94ad30f", T1: "92987a24", T2: "00000002"}
    assert any("short" in [c[2] for c in idx.db.execute(f"PRAGMA index_info({i[1]})")]
               for i in idx.db.execute("PRAGMA index_list(sessions)"))
    import kb.index
    assert kb.index.SCHEMA_VERSION >= 3                                  # old index files are rebuilt


V7A, V7B = "01a0d000-0000-7000-8000-5f3c9a1be7d2", "01a0d000-0001-7123-9abc-0e4b7c2d91a6"   # same first 8 characters


def test_get_resolves_a_short_id_even_when_the_first_eight_characters_collide(empty):
    root, idx = empty
    put(root, "h/a.md", V7A)
    put(root, "h/b.md", V7B)
    idx.update(root)
    assert idx.get("9a1be7d2")["id"] == V7A and idx.get("7c2d91a6")["id"] == V7B
    assert idx.get("9a1b")["id"] == V7A and idx.get("7c2d")["id"] == V7B       # a prefix of the short id
    assert idx.get(V7A)["id"] == V7A and idx.get(V7A[:20])["id"] == V7A       # full id, or a longer prefix of it
    with pytest.raises(AmbiguousId) as e:
        idx.get("01a0d000")                                                    # the old 8-character prefix is ambiguous
    assert sorted(e.value.args[0]) == sorted([V7A, V7B])


def test_get_needs_four_characters_unless_it_is_an_exact_id(empty):
    root, idx = empty
    put(root, "h/a.md", "abc-1")
    put(root, "h/b.md", "ab")
    put(root, "h/c.md", "abcd1234-0000")
    idx.update(root)
    assert idx.get("ab")["id"] == "ab"                                         # an exact full id works whatever its length
    assert idx.get("abc") is None and idx.get("a") is None                     # too short to be a prefix
    assert idx.get("abc-1")["id"] == "abc-1"
    assert idx.get("abcd")["id"] == "abcd1234-0000"
    assert idx.get("zzzz") is None


def test_an_exact_id_wins_over_longer_ids_that_start_with_it(empty):
    root, idx = empty
    put(root, "h/a.md", "dup-1")
    put(root, "h/b.md", "dup-12")
    idx.update(root)
    assert idx.get("dup-1")["id"] == "dup-1"
    with pytest.raises(AmbiguousId):
        idx.get("dup-")


def test_a_short_id_and_an_id_prefix_that_hit_the_same_session_are_not_ambiguous(empty):
    root, idx = empty
    put(root, "h/a.md", "abcd1234abcd1234")                                     # short = "abcd1234", id starts with it too
    idx.update(root)
    assert idx.get("abcd1234")["id"] == "abcd1234abcd1234"


# ---- read-only readers, WAL, short snippets

def _freeze(kb_dir, keep_wal_files=True):
    """Make a folder read-only like a sandbox does: checkpoint the WAL, 0o444 on the files, 0o555 on the folder."""
    con = sqlite3.connect(kb_dir / "index.sqlite")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    if not keep_wal_files:                                          # a clean close deletes them on most builds of SQLite
        for suffix in ("-wal", "-shm"):
            if (kb_dir / f"index.sqlite{suffix}").exists():
                (kb_dir / f"index.sqlite{suffix}").unlink()
    for f in kb_dir.iterdir():
        if f.is_file():
            f.chmod(0o444)
    kb_dir.chmod(0o555)


def _thaw(kb_dir):
    kb_dir.chmod(0o755)
    for f in kb_dir.iterdir():
        if f.is_file():
            f.chmod(0o644)


def test_the_writer_switches_the_index_to_wal(empty):
    _, idx = empty
    assert idx.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_open_current_gives_a_read_only_index_only_for_a_current_file(tmp_path, kb):
    root, idx = kb
    path = root / ".kb" / "index.sqlite"
    ro = Index.open_current(path)
    try:
        assert ro is not None and ro.rebuilt is False and ro.find("flaky motion")[0]["id"] == SID
        assert ro.get(SID[:8])["title"] == "Fix flaky motion test" and len(ro.recent(Filters(subagents=False))) == 2
        with pytest.raises(sqlite3.OperationalError):               # it cannot write, not even to update itself
            ro.update(root)
    finally:
        ro.close()
    assert Index.open_current(tmp_path / "missing" / "index.sqlite") is None            # no file
    old = tmp_path / "old.sqlite"
    con = sqlite3.connect(old)
    con.executescript(OLD_SCHEMA + "PRAGMA user_version=1;")
    con.close()
    assert Index.open_current(old) is None                                              # another schema version
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a database" * 100)
    assert Index.open_current(junk) is None                                             # not a database at all


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("keep_wal_files", [True, False])
def test_a_read_only_folder_can_still_be_read(tmp_path, kb, keep_wal_files):
    root, idx = kb
    idx.close()
    kb_dir = root / ".kb"
    _freeze(kb_dir, keep_wal_files)
    try:
        ro = Index.open_current(kb_dir / "index.sqlite")
        try:
            assert ro is not None and ro.find("flaky motion")[0]["id"] == SID
        finally:
            ro.close()
        cols, rows = run_sql(kb_dir / "index.sqlite", "SELECT COUNT(*) AS n FROM sessions")
        assert rows[0][0] == 4
    finally:
        _thaw(kb_dir)


def test_a_reader_is_not_blocked_by_a_writer_in_a_write_transaction(kb):
    root, writer = kb
    path = root / ".kb" / "index.sqlite"
    writer.db.execute("BEGIN IMMEDIATE")
    writer.db.execute("DELETE FROM turns")
    writer.db.execute("DELETE FROM sessions")                                           # not committed yet
    try:
        started = time.monotonic()
        ro = Index.open_current(path)
        try:
            assert ro.find("flaky motion")[0]["id"] == SID and ro.get(SID[:8]) is not None     # the last committed state
        finally:
            ro.close()
        assert run_sql(path, "SELECT COUNT(*) FROM sessions")[1][0][0] == 4
        assert time.monotonic() - started < 3                                           # no waiting for the writer
    finally:
        writer.db.execute("ROLLBACK")


def test_a_snippet_is_one_line_of_at_most_200_characters(empty):
    root, idx = empty
    put(root, "h/a.md", "big-1", turns=("alpha " + "z" * 3000 + " beta needle", "ok"))
    put(root, "h/b.md", "multi-2", turns=("line one\n\n   line   two\tneedle\nline three", "ok"))
    idx.update(root)
    for sid in ("big-1", "multi-2"):
        hit = next(h for h in idx.find("needle") if h["id"] == sid)
        snip = hit["snippet"]
        assert 0 < len(snip) <= 200 and "needle" in snip.lower() and "«" in snip, sid
        assert snip == " ".join(snip.split()), sid                                      # whitespace collapsed
