import os
from collections import Counter

import pytest
from fixtures import AID, SID, T1, T2, make_claude_tree, make_codex_tree

from kb.adapters import claude, codex
from kb.index import AmbiguousId, Filters, Index, run_sql
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
    md = next((root / "sessions").rglob("*_11111111.md"))
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


def test_recent_get_children_sql(kb):
    root, idx = kb
    assert [r["id"] for r in idx.recent(Filters(subagents=False))] == [SID, T1]
    assert idx.get(SID[:8])["title"] == "Fix flaky motion test"
    with pytest.raises(AmbiguousId):
        idx.get("01a0c")
    assert [c["id"] for c in idx.children(SID)] == [AID]
    assert [c["id"] for c in idx.children(T1)] == [T2]
    assert idx.paths_by_id("h")[SID].endswith("_11111111.md")
    cols, rows = run_sql(root / ".kb" / "index.sqlite", "SELECT COUNT(*) AS n FROM sessions")
    assert cols == ["n"] and rows[0][0] == 4
    with pytest.raises(ValueError):
        run_sql(root / ".kb" / "index.sqlite", "DELETE FROM sessions")
