import json

from test_pages import write_page

from kb import brief, ledger

BODY = """# demo

## Current state
- works (a0000001)

## Errors seen → fixes
- `boom` → fixed it (a0000001)

## Open threads
- one (a0000001)
- two (a0000001)
"""


def _ledger(root, **decisions):
    ledger.save(root, {
        "s-000001": {"category": "Rules", "text": "wait for CI with a watch", "signature": "", "repo": "demo",
                     "sources": [], "weeks": ["2026-W40"]},
        "s-000002": {"category": "", "text": "x" * 400, "signature": "", "repo": "Demo", "sources": [],
                     "weeks": ["2026-W39"]},
        "s-000003": {"category": "", "text": "other repo", "signature": "", "repo": "other", "sources": [],
                     "weeks": ["2026-W40"]}})
    (root / ledger.DECISIONS_REL).write_text(json.dumps(decisions))


def test_the_first_sync_line_comes_before_the_kb_line_while_the_backfill_is_pending(tmp_path):
    text = brief.build(tmp_path, str(tmp_path), first_sync_pending=True)
    assert text.splitlines() == [brief.FIRST_SYNC_LINE, brief.KB_LINE]
    assert brief.build(tmp_path, str(tmp_path)) == brief.KB_LINE


def test_page_pointer_only(tmp_path):
    write_page(tmp_path, "project", "demo", body=BODY)
    assert brief.build(tmp_path, "/work/demo") == (
        "Project page of demo: `kb page demo` (2 open threads, 1 error → fix). Read it when the task needs it.\n"
        + brief.KB_LINE)
    assert brief.build(tmp_path, "/work/nothing") == brief.KB_LINE     # no page: still a pointer to the KB
    _ledger(tmp_path, **{"s-000001": {"state": "applied"}})            # applied or proposed: not shown
    assert len(brief.build(tmp_path, "/work/demo").splitlines()) == 2


def test_accepted_suggestions_for_this_repo(tmp_path):
    (tmp_path / "pages").mkdir()
    _ledger(tmp_path, **{sid: {"state": "accepted"} for sid in ("s-000001", "s-000002", "s-000003")})
    out = brief.build(tmp_path, "/work/demo").splitlines()           # no page: the suggestions still come
    assert out[0] == "Changes the owner accepted for the demo repo (`kb suggestions`):"
    assert out[1].startswith("- [s-000002] xxx") and out[1].endswith("…") and len(out[1]) == brief.LINE_CHARS
    assert out[2] == "- [s-000001] Rules · wait for CI with a watch" and out[3:] == [brief.KB_LINE]
    assert brief.build(tmp_path, "/", project="other").endswith("- [s-000003] other repo\n" + brief.KB_LINE)


def test_items_read_the_repo():
    body = '# W\n\n## Suggested changes\n- [new] Rules · do it · repo "demo" · signature "a b c" (a0000001)\n'
    it = ledger.items(body)[0]
    assert (it["text"], it["repo"], it["signature"]) == ("do it", "demo", "a b c")


def test_the_whole_brief_stays_under_the_limit(tmp_path):
    (tmp_path / "pages").mkdir()
    ledger.save(tmp_path, {f"s-{i:06d}": {"category": "", "text": "y" * 300, "signature": "", "repo": "demo",
                                          "sources": [], "weeks": ["2026-W40"]} for i in range(20)})
    (tmp_path / ledger.DECISIONS_REL).write_text(json.dumps({f"s-{i:06d}": {"state": "accepted"} for i in range(20)}))
    out = brief.build(tmp_path, "/work/demo")
    assert len(out) <= brief.MAX_CHARS + len("- … and 20 more") and out.endswith(brief.KB_LINE)
