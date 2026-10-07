import json

import pytest
from test_cli import kb_env, run  # noqa: F401 - fixture
from test_index import put

from kb.distill import dump_front_matter
from kb.index import AmbiguousId, Index
from kb.pages import page_rel, parse_page, section, sections, set_fields

BODY = """# demo

The demo project.

## Current state
- the motion test is stable (a0000001)

## Key decisions
- 2026-10-06 · retry flaky waits instead of sleeping — sleeps hid the race (a0000001)
"""


def write_page(root, kind, name, body=BODY, rel=None, **meta):
    m = {"kind": kind, "name": name, "updated": "2026-10-07T09:00:00Z", "sessions": 1, "sources": ["a0000001"],
         **meta}
    path = root / (rel or page_rel(kind, name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_front_matter(m) + "\n" + body, encoding="utf-8")
    return path


def test_page_rel_and_names():
    assert page_rel("project", "demo-app") == "pages/projects/demo-app.md"
    assert page_rel("retro", "2026-W41") == "pages/retro/2026-W41.md"
    for kind, name in [("project", "../x"), ("project", "a/b"), ("project", "Demo"), ("project", ""),
                       ("project", "x."), ("retro", "2026-41"), ("retro", "demo"), ("other", "demo")]:
        with pytest.raises(ValueError):
            page_rel(kind, name)


def test_parse_page_validates_the_front_matter():
    meta, body = parse_page(dump_front_matter({"kind": "project", "name": "demo", "sessions": 2}) + "\n" + BODY)
    assert meta == {"kind": "project", "name": "demo", "title": "demo", "updated": "", "sessions": 2, "sources": []}
    assert body.startswith("# demo")
    for bad in [{"name": "demo"}, {"kind": "project", "name": "demo", "sessions": True},
                {"kind": "project", "name": "demo", "sources": "a0000001"},
                {"kind": "retro", "name": "demo"}]:
        with pytest.raises(ValueError):
            parse_page(dump_front_matter(bad) + "\n" + BODY)
    with pytest.raises(ValueError, match="front matter"):
        parse_page(BODY)


def test_set_fields_keeps_order_and_body():
    text = dump_front_matter({"kind": "project", "name": "demo", "updated": "x"}) + "\n" + BODY
    out = set_fields(text, {"updated": "2026-10-07T09:00:00Z", "sessions": 2})
    assert out == ('---\nkind: "project"\nname: "demo"\nupdated: "2026-10-07T09:00:00Z"\nsessions: 2\n---\n\n' + BODY)


def test_sections():
    assert [h for h, _ in sections(BODY)] == ["Current state", "Key decisions"]
    assert section(BODY, "key").startswith("## Key decisions\n- 2026-10-06")
    assert section(BODY, "nothing") is None


def test_index_reads_pages_next_to_sessions(tmp_path):
    root = tmp_path / "kb"
    put(root, "h/a.md", "a-1", turns=("flaky waits",))
    write_page(root, "project", "demo")
    write_page(root, "retro", "2026-W40", body="# Week 2026-W40\n\n## Friction\n- flaky waits again (a0000001)\n")
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        assert idx.update(root) == 1 and idx.pages_changed == 2
        assert idx.update(root) == 0 and idx.pages_changed == 0
        hits = idx.find_pages("flaky waits")
        assert {h["name"] for h in hits} == {"demo", "2026-W40"}
        assert "«" in hits[0]["snippet"]
        assert [h["name"] for h in idx.find_pages("flaky", project="DEMO")] == ["demo"]
        assert idx.find_pages("nothing-like-this") == []
        assert idx.page("demo")["path"] == "pages/projects/demo.md"
        assert idx.page("2026-W")["name"] == "2026-W40"
        assert idx.page("pages/retro/2026-W40.md")["kind"] == "retro"
        assert idx.page("zzz") is None
        write_page(root, "project", "demo-two")
        idx.update(root)
        with pytest.raises(AmbiguousId):
            idx.page("dem")
        assert idx.find("flaky")[0]["id"] == "a-1"               # sessions are unchanged
        assert len(idx.pages()) == 3
    finally:
        idx.close()


def test_index_skips_broken_pages_and_drops_removed_ones(tmp_path):
    root = tmp_path / "kb"
    good = write_page(root, "project", "demo")
    write_page(root, "project", "other", rel="pages/projects/wrong-place.md")
    (root / "pages" / "projects" / "notes.md").write_text("# no front matter\n", encoding="utf-8")
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        idx.update(root)
        assert [p["name"] for p in idx.pages()] == ["demo"]
        assert sorted(rel for rel, _ in idx.errors) == ["pages/projects/notes.md", "pages/projects/wrong-place.md"]
        assert idx.pages_changed == 1
        idx.update(root)
        assert idx.pages_changed == 0                            # broken pages are not counted again
        good.unlink()
        idx.update(root)
        assert idx.pages() == [] and idx.pages_changed == 1
        assert idx.find_pages("motion") == []
    finally:
        idx.close()


def test_cli_page(kb_env, capsys):
    write_page(kb_env, "project", "demo")
    code, out = run(capsys, "page")
    assert code == 0 and out.startswith("page     2026-10-07 project demo") and "1 sessions" in out
    code, out = run(capsys, "page", "dem")
    assert code == 0 and out.startswith("pages/projects/demo.md · updated 2026-10-07 09:00 · 1 sessions")
    assert "## Key decisions" in out
    code, out = run(capsys, "page", "demo", "--section", "key decisions")
    assert code == 0 and "## Key decisions" in out and "## Current state" not in out
    code, out = run(capsys, "page", "demo", "--section", "nope")
    assert code == 1 and "sections: Current state, Key decisions" in out
    code, out = run(capsys, "page", "zzz")
    assert code == 1 and out.startswith("no page named zzz")
    code, out = run(capsys, "page", "demo", "--max-chars", "60")
    assert "[… cut at 60 chars" in out


def test_cli_page_lists_nothing_before_the_first_build(kb_env, capsys):
    code, out = run(capsys, "page")
    assert code == 0 and out.startswith("no pages yet")


def test_find_lists_pages_first(kb_env, capsys):
    write_page(kb_env, "project", "demo", body=BODY + "\n## Errors seen → fixes\n- flaky motion test → retry (a0000001)\n")
    code, out = run(capsys, "find", "flaky", "motion")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("page     2026-10-07 project demo") and "[kb page demo]" in lines[0]
    assert lines[1].startswith("55555555 2026-10-06 claude")
    _, out = run(capsys, "find", "flaky", "motion", "--no-pages")
    assert not out.startswith("page")
    _, out = run(capsys, "find", "flaky", "motion", "--agent", "claude")
    assert not out.startswith("page")
    _, out = run(capsys, "find", "flaky", "motion", "--json")
    rows = json.loads(out)
    assert rows[0]["kind"] == "page" and rows[0]["page_kind"] == "project" and rows[1]["short"] == "55555555"
    code, out = run(capsys, "find", "stable")                     # a word only the page has
    assert code == 0 and out.startswith("page")


def test_reindex_and_status_mention_pages(kb_env, capsys):
    _, out = run(capsys, "status")
    assert "pages: none yet" in out
    write_page(kb_env, "project", "demo")
    (kb_env / "pages" / ".state.json").write_text(json.dumps(
        {"last_run": "2026-10-07T09:00:00Z", "pending": {"projects": {"x": []}, "weeks": ["2026-W40"]}}))
    code, out = run(capsys, "reindex")
    assert code == 0 and out.strip() == "indexed 4 sessions, 1 pages"
    _, out = run(capsys, "status")
    assert "pages: last run 2026-10-07T09:00:00Z · 2 pending" in out
