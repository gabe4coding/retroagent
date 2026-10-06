from fixtures import T1, T2, T3, make_codex_tree

from kb.adapters import codex
from kb.model import ToolCall


def _units(tmp_path):
    sessions, home = make_codex_tree(tmp_path)
    units = {u.key: u for u in codex.discover([sessions, tmp_path / "missing"])}
    return units, codex.load_titles(home)


def test_discover_groups_segments(tmp_path):
    units, _ = _units(tmp_path)
    assert set(units) == {f"codex:{T1}", f"codex:{T2}", f"codex:{T3}"}
    t1 = units[f"codex:{T1}"]
    assert len(t1.paths) == 2 and t1.paths[0].endswith(f"{T1}.jsonl")


def test_titles(tmp_path):
    _, titles = _units(tmp_path)
    assert titles[T1] == "Fetch retry" and titles[T2] == "Explore fetch calls"


def test_parse_merged_thread(tmp_path):
    units, titles = _units(tmp_path)
    s = codex.parse_unit(units[f"codex:{T1}"], titles)
    assert s.id == T1 and s.agent == "codex" and s.title == "Fetch retry"
    assert s.project == "demo-repo" and s.branch == "main" and s.model == "gpt-5.5-codex"
    assert s.started == "2026-09-22T09:41:00Z" and s.ended == "2026-09-22T20:22:00Z"
    assert [t.role for t in s.turns] == ["user", "assistant", "user", "assistant"]
    assert s.turns[0].text == "Add a retry to the fetch client"
    assert s.turns[2].text == "Please log the attempts too"
    assert "thanks, also log the attempts" not in str(s.turns)
    tools = [i for i in s.turns[1].items if isinstance(i, ToolCall)]
    assert [(t.name, t.arg, t.status) for t in tools] == [
        ("exec_command", "npm test", "error"), ("apply_patch", "src/client.ts", "ok")]
    assert tools[0].error_head == "TypeError: x is undefined / at foo"
    assert tools[1].diff == "+2 −1"
    assert s.turns[1].text == "Added retry with backoff."
    assert s.files == ["src/client.ts"]
    assert "event_msg" not in s.skipped


def test_subagent_and_guardian(tmp_path):
    units, titles = _units(tmp_path)
    sub = codex.parse_unit(units[f"codex:{T2}"], titles)
    assert sub.parent == T1 and sub.title == "Explore fetch calls" and sub.project == "demo"
    assert codex.parse_unit(units[f"codex:{T3}"], titles) is None
