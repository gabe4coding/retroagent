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


def test_page_pointer_only(tmp_path):
    write_page(tmp_path, "project", "demo", body=BODY)
    assert brief.build(tmp_path, "/work/demo") == (
        "Project page of demo: `kb page demo` (2 open threads, 1 error → fix). Read it when the task needs it.")
    assert brief.build(tmp_path, "/work/nothing") == ""
    _ledger(tmp_path, **{"s-000001": {"state": "applied"}})            # applied or proposed: not shown
    assert "\n" not in brief.build(tmp_path, "/work/demo")


def test_accepted_suggestions_for_this_repo(tmp_path):
    (tmp_path / "pages").mkdir()
    _ledger(tmp_path, **{sid: {"state": "accepted"} for sid in ("s-000001", "s-000002", "s-000003")})
    out = brief.build(tmp_path, "/work/demo").splitlines()           # no page: the suggestions still come
    assert out[0] == "Changes the owner accepted for the demo repo (`kb suggestions`):"
    assert out[1].startswith("- [s-000002] xxx") and out[1].endswith("…") and len(out[1]) == brief.LINE_CHARS
    assert out[2] == "- [s-000001] Rules · wait for CI with a watch" and len(out) == 3
    assert brief.build(tmp_path, "/", project="other").endswith("- [s-000003] other repo")


def test_items_read_the_repo():
    body = '# W\n\n## Suggested changes\n- [new] Rules · do it · repo "demo" · signature "a b c" (a0000001)\n'
    it = ledger.items(body)[0]
    assert (it["text"], it["repo"], it["signature"]) == ("do it", "demo", "a b c")
