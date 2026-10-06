from kb.model import Session
from kb.paths import md_rel, month_of, raw_rel


def test_paths_main_and_sub():
    main = Session(id="11111111-2222", agent="claude", project="Demo App", started="2026-10-06T23:59:00Z")
    sub = Session(id="a1d0ec8d8e94", agent="claude", project="Demo App", started="2026-10-07T00:01:00Z",
                  parent=main.id)
    assert md_rel("h", main) == "sessions/h/claude/2026/10/2026-10-06_demo-app_11111111.md"
    assert md_rel("h", sub, parent=main) == "sessions/h/claude/2026/10/2026-10-06_demo-app_11111111_sub-a1d0ec8d.md"
    assert md_rel("h", sub) == "sessions/h/claude/2026/10/2026-10-07_demo-app_11111111_sub-a1d0ec8d.md"
    assert raw_rel("h", main) == "raw/h/claude/2026/10/11111111-2222.jsonl.gz"
    assert raw_rel("h", sub, parent=main) == "raw/h/claude/2026/10/11111111-2222__sub-a1d0ec8d8e94.jsonl.gz"
    assert month_of("sessions/h/claude/2026/10/x.md") == "2026/10"


def test_unknown_date():
    s = Session(id="x", agent="codex", project="p")
    assert md_rel("h", s) == "sessions/h/codex/0000/00/0000-00-00_p_x.md"
