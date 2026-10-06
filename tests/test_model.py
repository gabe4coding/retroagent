import os
import re
import time
from pathlib import Path

import pytest

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


def test_agent_messages_are_not_user_prompts():
    from kb.model import Turn
    assert Turn(n=1, role="user", ts="").origin == ""
    s = Session(id="abc", agent="codex")
    s.add_turn("user", "2026-10-06T13:00:00Z", ["real prompt"])
    s.add_turn("user", "2026-10-06T13:00:10Z", ["[from /root/finder] Found 3 calls"], origin="agent")
    s.add_turn("assistant", "2026-10-06T13:01:00Z", ["answer"])
    assert [t.origin for t in s.turns] == ["", "agent", ""]
    assert len(s.turns) == 3 and s.user_turns == 1                      # the agent message is a turn, not a prompt
    only_agents = Session(id="def", agent="codex")
    only_agents.add_turn("user", "2026-10-06T13:00:00Z", ["[from /root] task"], origin="agent")
    assert only_agents.user_turns == 0 and only_agents.first_prompt() == "[from /root] task"   # titles still use it


def test_unit_fingerprint_changes_with_content(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x\n")
    u = Unit(key="k", agent="claude", paths=[str(f)], main=str(f))
    fp1 = u.fingerprint()
    f.write_text("xy\n")
    assert u.fingerprint() != fp1
    os.utime(f, (100, 100))
    assert u.newest_mtime() == 100


def test_fingerprint_is_a_sha1_hex_digest(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x\n")
    fp = Unit(key="k", agent="claude", paths=[str(f)], main=str(f)).fingerprint()
    assert re.fullmatch(r"[0-9a-f]{40}", fp)


def test_fingerprint_accepts_mixed_str_and_path_and_ignores_order(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("1")
    b.write_text("2")
    u1 = Unit(key="k", agent="claude", paths=[str(a), b], main=str(a))
    u2 = Unit(key="k", agent="claude", paths=[str(b), str(a)], main=str(a))
    assert u1.fingerprint() == u2.fingerprint()


def test_fingerprint_changes_when_content_is_rewritten_with_the_same_size_and_mtime(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("aaaa")
    os.utime(f, (100, 100))
    u = Unit(key="k", agent="claude", paths=[str(f)], main=str(f))
    fp1 = u.fingerprint()
    time.sleep(0.02)
    f.write_text("bbbb")
    os.utime(f, (100, 100))
    assert u.fingerprint() != fp1


def test_fingerprint_of_a_deleted_file_raises_oserror(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("x")
    u = Unit(key="k", agent="claude", paths=[str(f)], main=str(f))
    f.unlink()
    with pytest.raises(OSError):
        u.fingerprint()
