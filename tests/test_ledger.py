import datetime as dt
import json

from kb import ledger

BODY = """# Week 2026-W40

## What happened
- alpha: work (a0000001)

## Suggested changes
- [new] Automated checks · fix the guard hook patterns in `.claude/settings.json` · signature "pretooluse bash hook bash blocked destructive command" (a0000001, a0000002)
- [s-abc123] **Rules** · wait for CI with `gh pr checks --watch` (a0000002)
- [new] split long sessions with a written checkpoint (a0000003)
"""


def test_items_parse_id_category_signature_and_sources():
    got = ledger.items(BODY)
    assert [(i["id"], i["category"], i["signature"], i["sources"]) for i in got] == [
        ("new", "Automated checks", "pretooluse bash hook bash blocked destructive command", ["a0000001", "a0000002"]),
        ("s-abc123", "Rules", "", ["a0000002"]),
        ("new", "", "", ["a0000003"])]
    assert got[0]["text"] == "fix the guard hook patterns in `.claude/settings.json`"
    assert got[2]["text"] == "split long sessions with a written checkpoint"


def test_check():
    known = {"s-abc123": {"weeks": ["2026-W39"]}}
    sig = "pretooluse bash hook bash blocked destructive command"
    assert ledger.check("r.md", BODY, known, {sig}) == []
    assert ledger.check("r.md", BODY, known, None) == []                 # no index: signatures are not checked
    probs = ledger.check("r.md", BODY, {}, set())
    assert any("no suggestion s-abc123" in p for p in probs)
    assert any("no session has the error signature" in p for p in probs)
    bare = "# W\n\n## Suggested changes\n- no id here (a0000001)\n- [new] (a0000002)\n"
    probs = ledger.check("r.md", bare, {}, None)
    assert "r.md: suggested change 1: start it with [new]" in probs[0] and "no change written" in probs[1]


def test_assign_and_record_are_idempotent_for_a_rewritten_week():
    known = {"s-abc123": {"category": "Rules", "text": "old", "signature": "", "sources": [], "weeks": ["2026-W39"]},
             "s-dead00": {"category": "", "text": "gone", "signature": "", "sources": [], "weeks": ["2026-W40"]}}
    body, found = ledger.assign(BODY, "2026-W40", known)
    assert "[new]" not in body and body.count("[s-") == 3
    ids = [i["id"] for i in found]
    assert ids[1] == "s-abc123" and len(set(ids)) == 3
    led = ledger.record(known, "2026-W40", found)
    assert "s-dead00" not in led                                      # dropped from the rewritten week, no other week
    assert led["s-abc123"]["weeks"] == ["2026-W39", "2026-W40"] and led["s-abc123"]["text"].startswith("wait for CI")
    assert led[ids[0]]["signature"].startswith("pretooluse")
    body2, found2 = ledger.assign(body, "2026-W40", led)                # the same page written again: no change
    assert body2 == body and ledger.record(led, "2026-W40", found2) == led
    # an older week does not overwrite the newer wording
    older = ledger.record(led, "2026-W38", [{**found[1], "text": "older words"}])
    assert older["s-abc123"]["weeks"] == ["2026-W38", "2026-W39", "2026-W40"]
    assert older["s-abc123"]["text"].startswith("wait for CI")


def test_load_save_and_decisions(tmp_path):
    (tmp_path / "pages").mkdir()
    assert ledger.load(tmp_path) == {}
    led = {"s-abc123": {"category": "", "text": "t", "signature": "", "sources": [], "weeks": ["2026-W40"]}}
    ledger.save(tmp_path, led)
    assert ledger.load(tmp_path) == led
    (tmp_path / ledger.DECISIONS_REL).write_text(json.dumps({
        "s-abc123": {"state": "applied", "date": "2026-10-01", "note": "done"},
        "s-bad000": {"state": "maybe"}, "s-0ff000": {"state": "rejected", "date": "yesterday"}}))
    assert ledger.decisions(tmp_path) == {
        "s-abc123": {"state": "applied", "date": "2026-10-01", "note": "done", "source": "", "by": "owner"},
        "s-0ff000": {"state": "rejected", "date": "", "note": "", "source": "", "by": "owner"}}


def test_report_verdicts():
    today = dt.date(2026, 10, 20)
    led = {sid: {"category": "", "text": sid, "signature": sig, "sources": [], "weeks": ["2026-W40"]}
           for sid, sig in [("s-000001", "sig one"), ("s-000002", "sig two"), ("s-000003", "sig three"),
                            ("s-000004", "sig four"), ("s-000005", ""), ("s-000006", "sig six"),
                            ("s-000007", "sig seven")]}
    led["s-000004"]["weeks"] = ["2026-W39", "2026-W40"]
    decided = {"s-000001": {"state": "applied", "date": "2026-10-05"},
               "s-000002": {"state": "applied", "date": "2026-10-05"},
               "s-000003": {"state": "applied", "date": "2026-10-18"},
               "s-000006": {"state": "rejected", "date": ""}}
    seen = {"sig one": {"r1": "2026-10-01T10:00:00Z", "r2": "2026-10-09T10:00:00Z"},
            "sig two": {"r1": "2026-10-01T10:00:00Z"},
            "sig four": {"r3": "2026-10-15T10:00:00Z"},
            "sig seven": {"r4": "2026-09-01T10:00:00Z"}}
    rows = {r["id"]: r for r in ledger.report(led, decided, seen, today)}
    assert rows["s-000001"]["verdict"] == "came back: 1 sessions since 2026-10-05, last 2026-10-09"
    assert rows["s-000002"]["verdict"] == "fixed: not seen since 2026-10-05"
    assert rows["s-000003"]["verdict"] == "applied 2026-10-18: too early to tell"
    assert rows["s-000004"]["verdict"] == "still happening: 1 sessions in 14 days, last 2026-10-15"
    assert rows["s-000005"]["verdict"] == "not measured (no error signature)"
    assert rows["s-000006"]["verdict"] == "rejected"
    assert rows["s-000007"]["verdict"] == "not seen in 14 days (last 2026-09-01)"
    order = [r["id"] for r in ledger.report(led, decided, seen, today)]
    assert order[:2] == ["s-000001", "s-000004"] and order[-1] == "s-000006"


def test_check_decisions():
    known = {"s-000001": {"weeks": ["2026-W40"]}, "s-000002": {"weeks": ["2026-W40"]},
             "s-000003": {"weeks": ["2026-W40"]}}
    dates = {"a0000005": "2026-10-06", "a0000001": "2026-09-22"}

    def lookup(ref):
        return max([dates.get(s, "") for s in ref.shorts] + (["2026-10-02"] if ref.memories else []))

    owner = {"state": "rejected", "note": "not worth it"}
    old = {"s-000002": owner}
    new = {"s-000001": {"state": "applied", "source": "a0000005", "note": "hook fixed", "date": "1999-01-01"},
           "s-000002": owner,
           "s-000003": {"state": "rejected", "source": "memory alpha/no-more-checks"}}
    problems, out = ledger.check_decisions(old, new, known, lookup)
    assert problems == []
    assert out["s-000001"] == {"state": "applied", "source": "a0000005", "note": "hook fixed", "date": "2026-10-06",
                               "by": "routine"}                          # the date comes from the source, not the writer
    assert out["s-000002"] is owner and out["s-000003"]["date"] == "2026-10-02"

    bad = {"s-000002": {"state": "applied", "source": "a0000005"},          # the owner's entry
           "s-000001": {"state": "applied", "source": "a0000001"},          # before W40 was suggested
           "s-000003": {"state": "done", "source": "a0000005"},
           "s-0000ff": {"state": "applied", "source": "a0000005"}}
    problems, _ = ledger.check_decisions(old, bad, known, lookup)
    text = "\n".join(problems)
    for want in ("s-000001: applied on 2026-09-22, before it was suggested", "s-000002: the owner wrote it",
                 "s-000003: state must be one of", "s-0000ff: no such suggestion"):
        assert want in text
    problems, _ = ledger.check_decisions({"s-000001": {"by": "routine"}}, {}, known, lookup)
    assert problems == ["pages/decisions.json: s-000001: removed; change its state instead"]
    problems, _ = ledger.check_decisions(None, {"s-000001": {"state": "applied"}}, known, lookup)
    assert "give its source" in problems[0]
    problems, _ = ledger.check_decisions(None, {"s-000001": {"state": "applied", "source": "a0000009"}}, known, lookup)
    assert "is not in the index" in problems[0]
