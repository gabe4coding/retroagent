import re

from kb.model import Session
from kb.paths import md_rel, month_of, raw_rel


def test_paths_main_and_sub():
    main = Session(id="11111111-2222", agent="claude", project="Demo App", started="2026-10-06T23:59:00Z")
    sub = Session(id="a1d0ec8d8e94", agent="claude", project="Demo App", started="2026-10-07T00:01:00Z",
                  parent=main.id)
    assert md_rel("h", main) == "sessions/h/claude/2026/10/2026-10-06_demo-app_11112222.md"
    assert md_rel("h", sub, parent=main) == "sessions/h/claude/2026/10/2026-10-06_demo-app_11112222_sub-ec8d8e94.md"
    assert md_rel("h", sub) == "sessions/h/claude/2026/10/2026-10-07_demo-app_11112222_sub-ec8d8e94.md"
    assert raw_rel("h", main) == "raw/h/claude/2026/10/11111111-2222.jsonl.gz"
    assert raw_rel("h", sub, parent=main) == "raw/h/claude/2026/10/11111111-2222__sub-a1d0ec8d8e94.jsonl.gz"
    assert month_of("sessions/h/claude/2026/10/x.md") == "2026/10"


def test_unknown_date():
    s = Session(id="x", agent="codex", project="p")
    assert md_rel("h", s) == "sessions/h/codex/0000/00/0000-00-00_p_x.md"


def test_uuidv7_ids_that_share_their_first_eight_characters_get_different_files():
    a = Session(id="01a0d000-0000-7000-8000-5f3c9a1be7d2", agent="codex", project="demo", started="2026-10-06T10:00:00Z")
    b = Session(id="01a0d000-0001-7123-9abc-0e4b7c2d91a6", agent="codex", project="demo", started="2026-10-06T10:00:30Z")
    assert a.id[:8] == b.id[:8]
    assert md_rel("h", a) == "sessions/h/codex/2026/10/2026-10-06_demo_9a1be7d2.md"
    assert md_rel("h", b) == "sessions/h/codex/2026/10/2026-10-06_demo_7c2d91a6.md"
    sub_a = Session(id="01a0d000-0002-7000-8000-111111111111", agent="codex", project="demo", parent=a.id)
    sub_b = Session(id="01a0d000-0003-7000-8000-222222222222", agent="codex", project="demo", parent=b.id)
    assert md_rel("h", sub_a, parent=a) == "sessions/h/codex/2026/10/2026-10-06_demo_9a1be7d2_sub-11111111.md"
    assert md_rel("h", sub_b, parent=b) != md_rel("h", sub_a, parent=a)


def test_hostile_id_and_parent_cannot_leave_their_folder():
    s = Session(id="../../etc/passwd", agent="claude", project="p", started="2026-10-06T10:00:00Z")
    sub = Session(id="a/b\\c", agent="claude", project="p", started="2026-10-06T10:00:00Z", parent="../x")
    for rel in (md_rel("h", s), raw_rel("h", s), md_rel("h", sub, parent=s), raw_rel("h", sub, parent=s)):
        name = rel.rsplit("/", 1)[1]
        assert ".." not in rel and rel.count("/") == 5 and re.fullmatch(r"[A-Za-z0-9_.-]+", name), rel
    assert raw_rel("h", s) == "raw/h/claude/2026/10/______etc_passwd.jsonl.gz"
    assert raw_rel("h", sub, parent=s) == "raw/h/claude/2026/10/___x__sub-a_b_c.jsonl.gz"
    assert md_rel("h", s) == "sessions/h/claude/2026/10/2026-10-06_p_c_passwd.md"


def test_started_that_is_not_an_iso_date_goes_to_the_unknown_bucket():
    s = Session(id="abcd1234", agent="claude", project="p", started="10/06/2026 1:00 PM")
    assert md_rel("h", s) == "sessions/h/claude/0000/00/0000-00-00_p_abcd1234.md"
    assert raw_rel("h", s) == "raw/h/claude/0000/00/abcd1234.jsonl.gz"
    for bad in ("../../x/2026-10-06", "2026/10/06", "x2026-10-06", "26-10-06T10:00:00Z"):
        assert md_rel("h", Session(id="abcd1234", agent="claude", project="p", started=bad)).startswith(
            "sessions/h/claude/0000/00/"), bad
    ok = Session(id="abcd1234", agent="claude", project="p", started="2026-10-06")
    assert md_rel("h", ok) == "sessions/h/claude/2026/10/2026-10-06_p_abcd1234.md"
