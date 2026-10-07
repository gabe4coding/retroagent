import json

from test_index import put

from kb.distill import dump_front_matter
from kb.freshness import (Ref, bullet_date, index_lookup, is_stale, newest, parse_tail, stale_settings, stamp,
                          sweep)
from kb.index import Index

DATES = {"a0000001": "2026-06-01", "a0000002": "2026-10-01", "m/old-fact": "2026-05-01"}


def lookup(ref):
    found = [DATES[s] for s in ref.shorts if s in DATES] + [DATES[m] for m in ref.memories if m in DATES]
    return max(found, default="")


def test_parse_tail():
    assert parse_tail("- fact (a0000001)") == ("- fact", Ref(["a0000001"], [], ""), "a0000001")
    assert parse_tail("- fact (a0000001, a0000002)")[1].shorts == ["a0000001", "a0000002"]
    assert parse_tail("- fact (memory m/old-fact)")[1] == Ref([], ["m/old-fact"], "")
    text, ref, content = parse_tail("- fact (a0000001, memory m/old-fact · 2026-06-01)")
    assert (text, ref, content) == ("- fact", Ref(["a0000001"], ["m/old-fact"], "2026-06-01"), "a0000001, memory m/old-fact")
    assert parse_tail("- fact with no source")[1] is None
    assert parse_tail("- see PR (#12)")[1] is None                             # parentheses that cite nothing
    assert parse_tail("- `f(a0000001)` is called twice")[1] is None           # parentheses not at the end
    assert bullet_date("- fact (a0000001 · 2026-06-01)") == "2026-06-01" and bullet_date("- fact (a0000001)") == ""


PAGE = """# demo

Intro (a0000001).

## Current state
- works (a0000001, a0000002)
- writer's own date is replaced (a0000001 · 2030-01-01)
- unknown source (ffffffff)

## Errors seen → fixes
- `boom` → restart it (memory m/old-fact)

## History
- 2026-01 to 2026-03: early work (a0000001)
"""


def test_stamp_dates_dated_sections_only_and_reports_the_undated():
    body, undated = stamp(PAGE, lookup)
    assert "- works (a0000001, a0000002 · 2026-10-01)" in body
    assert "- writer's own date is replaced (a0000001 · 2026-06-01)" in body
    assert "- unknown source (ffffffff)\n" in body
    assert "- `boom` → restart it (memory m/old-fact · 2026-05-01)" in body
    assert "Intro (a0000001).\n" in body and "- 2026-01 to 2026-03: early work (a0000001)\n" in body
    assert undated == ["unknown source (ffffffff)"]
    assert stamp(body, lookup) == (body, undated)                             # the same output when run twice


def test_is_stale_and_newest():
    assert is_stale("2026-01-01", "2026-04-02", 90) and not is_stale("2026-01-01", "2026-03-31", 90)
    assert not is_stale("", "2026-04-02", 90) and not is_stale("2026-01-01", "", 90)
    assert newest(stamp(PAGE, lookup)[0]) == "2026-10-01"


def page(rows: dict, history: bool = False) -> str:
    out = ["# demo", ""]
    for section, bullets in rows.items():
        out += [f"## {section}"] + [f"- {text} (a0000001 · {date})" for text, date in bullets] + [""]
    if history:
        out += ["## History", "- 2026-01: start (a0000001)", ""]
    return "\n".join(out)


def test_sweep_moves_old_bullets_of_aging_sections_to_history():
    body = page({"Current state": [("new", "2026-10-01"), ("31 days", "2026-08-31"), ("29 days", "2026-09-02")],
                 "Key decisions": [("2025-01-01 · decided", "2026-01-01")],
                 "Important files": [("`a.py` — main", "2026-01-01")],
                 "Errors seen → fixes": [("`x` → y", "2026-07-02"), ("`z` → w", "2026-07-04")],
                 "Open threads": [("undated (a0000001)", "")]}, history=True)
    body = body.replace(" (a0000001 · )", "")
    out, moved = sweep(body, 90, 30)
    assert moved == ["- unconfirmed since 2026-08-31 (Current state): 31 days (a0000001 · 2026-08-31)",
                     "- unconfirmed since 2026-07-02 (Errors seen): `x` → y (a0000001 · 2026-07-02)"]
    assert "- 29 days (a0000001 · 2026-09-02)" in out and "- `z` → w (a0000001 · 2026-07-04)" in out
    assert "decided (a0000001 · 2026-01-01)" in out and "`a.py` — main (a0000001 · 2026-01-01)" in out
    assert "- undated (a0000001)" in out
    assert out.endswith("## History\n- 2026-01: start (a0000001)\n" + "\n".join(moved) + "\n")


def test_sweep_adds_history_and_keeps_a_dormant_page():
    out, moved = sweep(page({"Current state": [("new", "2026-10-01"), ("old", "2026-01-01")]}), 90, 30)
    assert moved and out.endswith("## History\n" + moved[0] + "\n")
    dormant = page({"Current state": [("a", "2025-01-01"), ("b", "2025-01-20")]})
    assert sweep(dormant, 90, 30) == (dormant, [])


def test_index_lookup(tmp_path):
    root = tmp_path / "kb"
    put(root, "h/claude/2026/06/2026-06-01_demo_a0000001.md", "a0000001", project="demo",
        started="2026-06-01T10:00:00Z")
    put(root, "h/claude/2026/06/2026-06-01_demo_b0000001.md", "11111111-b0000001", project="demo",
        started="2026-06-02T10:00:00Z")
    put(root, "h/claude/2026/06/2026-06-01_demo_b0000002.md", "22222222-b0000001", project="demo",
        started="2026-06-03T10:00:00Z")
    for name, meta in (("noted.md", {"modified": "2026-07-01T00:00:00Z", "origin_session": ""}),
                       ("from-session.md", {"modified": "", "origin_session": "a0000001"})):
        m = {"kind": "memory", "agent": "claude", "host": "h", "project": "demo", "cwd": "/w/demo", "folder": "-w-demo",
             "file": name, "name": name[:-3], "description": "", "type": "project", **meta}
        p = root / "memories" / "h" / "claude" / "-w-demo" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_front_matter(m) + "\nA fact.\n", encoding="utf-8")
    idx = Index(tmp_path / "index.sqlite")
    try:
        idx.update(root)
        look = index_lookup(idx)
        assert look(Ref(["a0000001"], [], "")) == "2026-06-01"
        assert look(Ref(["b0000001"], [], "")) == ""                           # two sessions match: skipped
        assert look(Ref([], ["demo/noted"], "")) == "2026-07-01"
        assert look(Ref([], ["demo/not"], "")) == "2026-07-01"                 # a ref the writer cut
        assert look(Ref([], ["demo/from-session"], "")) == "2026-06-01"        # no modified: its session's start
        assert look(Ref(["a0000001"], ["demo/noted"], "")) == "2026-07-01"
    finally:
        idx.close()


def test_stale_settings(tmp_path):
    assert stale_settings(tmp_path) == (90, 30)
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "config.json").write_text(json.dumps({"stale_days": 120, "stale_days_current": 0}))
    assert stale_settings(tmp_path) == (120, 30)
