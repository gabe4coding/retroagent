from fixtures import AID, GH_TOKEN, SID, make_claude_tree

from kb.adapters import claude
from kb.distill import render_markdown
from kb.model import ToolCall
from kb.paths import md_rel, raw_rel

EXPECTED_MAIN = """---
id: "11111111-2222-3333-4444-555555555555"
agent: "claude"
host: "test-host"
project: "demo"
cwd: "/Users/me/Repositories/demo"
branch: "feat/x"
started: "2026-10-06T13:02:11Z"
ended: "2026-10-06T13:12:00Z"
model: "claude-opus-5-5"
turns: 4
user_turns: 2
title: "Fix flaky motion test"
summary: ""
tags: []
outcome: ""
decisions: []
summary_turns: 0
files: ["src/motion.ts"]
prs: ["https://github.com/me/demo/pull/7"]
parent: ""
raw: "raw/test-host/claude/2026/10/11111111-2222-3333-4444-555555555555.jsonl.gz"
---

## [1] user · 13:02

Fix the flaky test in tests/motion.spec.ts

## [2] assistant · 13:03

Let me run the tests.

- Bash `GITHUB_TOKEN=GHTOKEN npm test` → ERROR: FAIL tests/motion.spec.ts / Expected 3, received 2 / line3
- Edit `src/motion.ts` (+3 −2)
- Agent `Explore tests` → [subagent](2026-10-06_demo_11111111_sub-a1d0ec8d.md): Found 2 flaky tests.

Fixed: the timer was not awaited.

## [3] user · 13:10

/kb-retro last week

## [4] assistant · 13:11

Retro done.
""".replace("GHTOKEN", GH_TOKEN)


def _parse(tmp_path):
    units = claude.discover(make_claude_tree(tmp_path))
    assert len(units) == 1
    return units[0], claude.parse_unit(units[0])


def test_discover_groups_main_and_subagents(tmp_path):
    unit, _ = _parse(tmp_path)
    assert unit.agent == "claude" and unit.key.endswith(f"{SID}.jsonl")
    assert len(unit.paths) == 2 and unit.paths[1].endswith(f"agent-{AID}.jsonl")


def test_parse_main_session(tmp_path):
    _, s = _parse(tmp_path)
    assert s.id == SID and s.title == "Fix flaky motion test" and s.project == "demo"
    assert s.model == "claude-opus-5-5" and s.branch == "feat/x"
    assert [t.role for t in s.turns] == ["user", "assistant", "user", "assistant"]
    assert s.turns[2].text == "/kb-retro last week"
    assert s.turns[3].text == "Retro done."
    tools = [i for i in s.turns[1].items if isinstance(i, ToolCall)]
    assert [t.name for t in tools] == ["Bash", "Edit", "Agent"]
    assert tools[0].status == "error" and tools[1].diff == "+3 −2"
    assert tools[2].subagent_id == AID and tools[2].subagent_note == "Found 2 flaky tests."
    assert s.files == ["src/motion.ts"] and s.prs == ["https://github.com/me/demo/pull/7"]
    for key in ("brand-new-record-type", "<unparsable>", "attachment", "cost-state"):
        assert key in s.skipped
    assert "private thoughts" not in str(s.turns)


def test_parse_subagent(tmp_path):
    _, s = _parse(tmp_path)
    assert len(s.subagents) == 1
    sub = s.subagents[0]
    assert sub.id == AID and sub.parent == SID and sub.title == "Explore tests" and sub.project == "demo"
    assert [t.role for t in sub.turns] == ["user", "assistant"]
    assert sub.turns[0].text == "Find flaky tests"
    assert [i.name for i in sub.turns[1].items if isinstance(i, ToolCall)] == ["Grep"]


def test_golden_markdown(tmp_path):
    _, s = _parse(tmp_path)
    sub_name = md_rel("test-host", s.subagents[0], parent=s).rsplit("/", 1)[1]
    md = render_markdown(s, "test-host", sub_files={AID: sub_name}, raw=raw_rel("test-host", s))
    assert md == EXPECTED_MAIN
