import os

from kb.model import Session, ToolCall, Unit


def test_session_turn_helpers():
    s = Session(id="abc", agent="claude")
    s.add_turn("user", "2026-10-06T13:00:00Z", ["  first prompt line\nmore"])
    a = s.add_turn("assistant", "2026-10-06T13:01:00Z")
    a.items.append("answer")
    a.items.append(ToolCall(name="Bash", arg="ls"))
    assert [t.n for t in s.turns] == [1, 2]
    assert s.user_turns == 1
    assert s.first_prompt() == "first prompt line"
    assert a.text == "answer"


def test_unit_fingerprint_changes_with_content(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x\n")
    u = Unit(key="k", agent="claude", paths=[str(f)], main=str(f))
    fp1 = u.fingerprint()
    f.write_text("xy\n")
    assert u.fingerprint() != fp1
    os.utime(f, (100, 100))
    assert u.newest_mtime() == 100
