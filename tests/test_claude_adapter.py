from pathlib import Path

import pytest
from fixtures import (AID, AID_B, CWD, SID, SID_B, claude_asst, claude_result, claude_tool, claude_user, make_claude_tree,
                      make_shared_pair, write_claude_session)

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

- Bash `GITHUB_TOKEN=[REDACTED:github-token] npm test` → ERROR: FAIL tests/motion.spec.ts / Expected 3, received 2 / line3
- Edit `src/motion.ts` (+3 −2)
- Agent `Explore tests` → [subagent](2026-10-06_demo_55555555_sub-e94ad30f.md): Found 2 flaky tests.

Fixed: the timer was not awaited.

## [3] user · 13:10

/kb-retro last week

## [4] assistant · 13:11

Retro done.
"""


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


# ---------------------------------------------------------------- per-record robustness (C1)

def T(minute):
    return f"2026-10-06T13:{minute:02d}:00.000Z"


def _session(tmp_path, records, subs=None):
    return claude.parse_unit(write_claude_session(tmp_path, records, subs=subs))


def _tools(turn):
    return [i for i in turn.items if isinstance(i, ToolCall)]


def test_odd_records_do_not_lose_the_session(tmp_path):
    records = [
        claude_user(T(1), "first prompt"),
        {"type": "user", "timestamp": T(2), "message": "just a string"},
        {"type": "assistant", "timestamp": T(3), "message": 5},
        claude_asst(T(4), [
            {"type": "text", "text": None}, {"type": "text", "text": 5},
            {"type": "tool_use", "id": ["x"], "name": None, "input": {"command": "ls"}}]),
        claude_user(T(5), [{"type": "text", "text": None}, {"type": "text", "text": "second prompt"}]),
        claude_user(T(6), [{"type": "tool_result", "tool_use_id": ["x"], "content": None}]),
        claude_asst(T(7), "done"),
    ]
    s = _session(tmp_path, records)
    assert [t.role for t in s.turns] == ["user", "assistant", "user", "assistant"]
    assert s.turns[0].text == "first prompt" and s.turns[2].text == "second prompt"
    assert s.turns[1].text == "5" and s.turns[3].text == "done"
    tools = _tools(s.turns[1])
    assert [(t.name, t.arg) for t in tools] == [("", "ls")]
    assert s.skipped["<error:AttributeError>"] == 2


def test_record_that_still_raises_is_counted_and_skipped(tmp_path, monkeypatch):
    def boom(name, inp, cwd):
        raise RuntimeError("boom")
    monkeypatch.setattr(claude, "short_arg", boom)
    records = [claude_user(T(1), "hi"), claude_asst(T(2), [claude_tool("t1", "Bash", command="ls")]),
               claude_asst(T(3), "still here")]
    s = _session(tmp_path, records)
    assert s.skipped == {"<error:RuntimeError>": 1}
    assert [t.text for t in s.turns] == ["hi", "still here"]


def test_non_string_scalars_do_not_break_the_session(tmp_path):
    records = [
        claude_user(T(1), "hi", cwd=["not", "a", "string"], gitBranch=5),
        {"type": "custom-title", "customTitle": ["x"]},
        {"type": "pr-link", "prUrl": 7},
        {"type": "relocated", "relocatedCwd": ["y"]},
        claude_asst(T(2), "ok", cwd=CWD),
    ]
    s = _session(tmp_path, records)
    assert s.cwd == CWD and s.title == "hi" and s.branch == "" and s.prs == []
    assert [t.text for t in s.turns] == ["hi", "ok"]


def test_text_of_and_tool_name_coerce_odd_values():
    assert claude.text_of([{"type": "text", "text": None}, {"type": "text", "text": 5}, "x"]) == "\n5"
    assert claude.tool_name(None) == "" and claude.tool_name(5) == "5"
    assert claude.tool_name("mcp__plugin_x__search") == "mcp:search"


# ---------------------------------------------------------------- harness records (C2, C3, C7)

@pytest.mark.parametrize("extra", [
    {"promptSource": "system"},
    {"turnOrigin": "peer"},
    {"origin": {"kind": "peer"}},
    {"turnOrigin": "task_notification"},
    {"origin": {"kind": "task-notification"}},
    {"isMeta": True},
])
def test_harness_prompts_are_not_user_turns(tmp_path, extra):
    s = _session(tmp_path, [claude_user(T(1), "real prompt"), claude_user(T(2), "injected text", **extra),
                            claude_asst(T(3), "answer")])
    assert [(t.role, t.text) for t in s.turns] == [("user", "real prompt"), ("assistant", "answer")]


@pytest.mark.parametrize("extra,content", [
    ({"isCompactSummary": True}, "This session is being continued from a previous conversation. Summary: ..."),
    ({"isVisibleInTranscriptOnly": True}, "visible only in the transcript"),
    ({}, "[Request interrupted by user]"),
    ({}, "[Request interrupted by user for tool use]"),
    ({}, [{"type": "text", "text": "[Request interrupted by user]"}]),
])
def test_compact_summary_and_interrupt_markers_are_not_prompts(tmp_path, extra, content):
    s = _session(tmp_path, [claude_user(T(1), "real prompt"), claude_asst(T(2), "answer"),
                            claude_user(T(3), content, **extra), claude_asst(T(4), "more")])
    assert [t.role for t in s.turns] == ["user", "assistant"] and s.turns[1].items == ["answer", "more"]
    assert s.user_turns == 1 and s.first_prompt() == "real prompt"
    assert "interrupted" not in str(s.turns) and "continued from" not in str(s.turns)


def test_a_prompt_that_merely_mentions_an_interrupt_is_kept(tmp_path):
    s = _session(tmp_path, [claude_user(T(1), "why does it print [Request interrupted by user]?")])
    assert s.user_turns == 1


def test_skipped_notification_or_peer_record_ends_the_assistant_turn(tmp_path):
    records = [
        claude_user(T(1), "go"),
        claude_asst(T(2), "started"),
        claude_user(T(3), "<task-notification>done</task-notification>", turnOrigin="task_notification"),
        claude_asst(T(4), "background job finished"),
        claude_user(T(5), "peer says hi", origin={"kind": "peer"}),
        claude_asst(T(6), "replied to peer"),
    ]
    s = _session(tmp_path, records)
    assert [(t.role, t.text, t.ts) for t in s.turns] == [
        ("user", "go", "2026-10-06T13:01:00Z"), ("assistant", "started", "2026-10-06T13:02:00Z"),
        ("assistant", "background job finished", "2026-10-06T13:04:00Z"),
        ("assistant", "replied to peer", "2026-10-06T13:06:00Z")]


def test_skipped_meta_record_does_not_end_the_assistant_turn(tmp_path):
    records = [claude_user(T(1), "go"), claude_asst(T(2), "one"), claude_user(T(3), "skill body", isMeta=True),
               claude_asst(T(4), "two")]
    s = _session(tmp_path, records)
    assert [t.role for t in s.turns] == ["user", "assistant"]
    assert s.turns[1].items == ["one", "two"]


def test_image_only_prompt_is_a_user_turn(tmp_path):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    records = [claude_user(T(1), [image]), claude_asst(T(2), "I see it"),
               claude_user(T(3), [{"type": "text", "text": "and this?"}, image])]
    s = _session(tmp_path, records)
    assert [(t.role, t.text) for t in s.turns] == [("user", "[image]"), ("assistant", "I see it"),
                                                    ("user", "and this?")]


# ---------------------------------------------------------------- session metadata (C2)

def test_relocated_sets_cwd_and_project(tmp_path):
    s = _session(tmp_path, [claude_user(T(1), "hi"), {"type": "relocated", "relocatedCwd": "/Users/me/Repos/moved"}])
    assert s.cwd == "/Users/me/Repos/moved" and s.project == "moved"


@pytest.mark.parametrize("titles,expected", [
    (["custom-title", "ai-title", "agent-name"], "From custom"),
    (["ai-title", "agent-name"], "From ai"),
    (["agent-name"], "From agent"),
    ([], "hi there"),
])
def test_title_precedence(tmp_path, titles, expected):
    rec = {"custom-title": {"type": "custom-title", "customTitle": "From custom"},
           "ai-title": {"type": "ai-title", "aiTitle": "From ai"},
           "agent-name": {"type": "agent-name", "agentName": "From agent"}}
    s = _session(tmp_path, [claude_user(T(1), "hi there")] + [rec[t] for t in titles])
    assert s.title == expected


def test_detached_head_branch_is_ignored(tmp_path):
    s = _session(tmp_path, [claude_user(T(1), "a", gitBranch="HEAD")])
    assert s.branch == ""
    s = _session(tmp_path / "two",
                 [claude_user(T(1), "a", gitBranch="feat/x"), claude_user(T(2), "b", gitBranch="HEAD")])
    assert s.branch == "feat/x"


def test_mcp_tool_names_are_shortened(tmp_path):
    s = _session(tmp_path, [claude_asst(T(1), [claude_tool("t1", "mcp__plugin_jira_jira__getIssue", id="X-1"),
                                               claude_tool("t2", "Read", file_path=CWD + "/a.py")])])
    assert [t.name for t in _tools(s.turns[0])] == ["mcp:getIssue", "Read"]


# ---------------------------------------------------------------- tool results and files (C5)

def test_files_lists_only_successful_edits(tmp_path):
    records = [
        claude_asst(T(1), [
            claude_tool("e1", "Edit", file_path=CWD + "/failed.py", old_string="a", new_string="b"),
            claude_tool("e2", "Write", file_path=CWD + "/written.py", content="x"),
            claude_tool("e3", "Edit", file_path=CWD + "/pending.py", old_string="a", new_string="b"),
            claude_tool("e4", "Edit", file_path=CWD + "/twice.py", old_string="a", new_string="b"),
            claude_tool("e5", "Edit", file_path=CWD + "/twice.py", old_string="b", new_string="c")]),
        claude_user(T(2), [claude_result("e1", "File has not been read yet", is_error=True),
                           claude_result("e2"), claude_result("e4", "no match", is_error=True),
                           claude_result("e5")]),
    ]
    s = _session(tmp_path, records)
    assert s.files == ["written.py", "pending.py", "twice.py"]


SCRATCHPAD = "/private/tmp/claude-501/-Users-me-Repositories-demo/" + SID + "/scratchpad"


def test_files_skip_temp_and_scratchpad_paths(tmp_path):
    records = [
        claude_asst(T(1), [
            claude_tool("e1", "Write", file_path=SCRATCHPAD + "/pr-body.md", content="x"),
            claude_tool("e2", "Edit", file_path=CWD + "/src/a.py", old_string="a", new_string="b"),
            claude_tool("e3", "Write", file_path="/tmp/notes.md", content="x"),
            claude_tool("e4", "Write", file_path="/var/folders/xy/abc/T/t.py", content="x"),
            claude_tool("e5", "MultiEdit", file_path="/private/var/folders/xy/abc/T/m.py", edits=[]),
            claude_tool("e6", "NotebookEdit", notebook_path="/private/tmp/n.ipynb", new_source="x"),
            claude_tool("e7", "Write", file_path="/Users/me/.claude/scratchpad/plan.md", content="x"),
            claude_tool("e8", "Edit", file_path="/Users/me/Repositories/other/b.py", old_string="a", new_string="b"),
            claude_tool("e9", "Write", file_path=CWD + "/notes/scratchpad/keep.md", content="x")]),
        claude_user(T(2), [claude_result(f"e{i}") for i in range(1, 10)]),
    ]
    s = _session(tmp_path, records)
    assert s.files == ["src/a.py", "/Users/me/Repositories/other/b.py", "notes/scratchpad/keep.md"]


def test_files_keep_project_paths_when_the_project_is_under_tmp(tmp_path):
    cwd = "/private/tmp/demo"
    records = [
        claude_user(T(0), "go", cwd=cwd),
        claude_asst(T(1), [claude_tool("e1", "Write", file_path=cwd + "/src/a.py", content="x"),
                           claude_tool("e2", "Write", file_path="/private/tmp/other/b.py", content="x")], cwd=cwd),
        claude_user(T(2), [claude_result("e1"), claude_result("e2")], cwd=cwd),
    ]
    s = _session(tmp_path, records)
    assert s.cwd == cwd and s.files == ["src/a.py"]


# ---------------------------------------------------------------- subagent links (C4, C6)

def _sub_records(answer, cwd=CWD):
    return [claude_user(T(10), "task", cwd=cwd), claude_asst(T(11), answer, cwd=cwd)]


def test_agent_id_links_only_agent_or_task_calls_with_one_result(tmp_path):
    records = [
        claude_asst(T(1), [claude_tool("b1", "Bash", command="ls"), claude_tool("g1", "Task", description="d1"),
                           claude_tool("g2", "Agent", description="d2"), claude_tool("g3", "Agent", description="d3")]),
        claude_user(T(2), [claude_result("b1")], toolUseResult={"agentId": "zzz"}),
        claude_user(T(3), [claude_result("g1", "finished 1")], toolUseResult={"agentId": "aaa"}),
        claude_user(T(4), [claude_result("g2"), claude_result("g3")], toolUseResult={"agentId": "yyy"}),
    ]
    tools = _tools(_session(tmp_path, records).turns[0])
    assert [(t.name, t.subagent_id) for t in tools] == [("Bash", ""), ("Task", "aaa"), ("Agent", ""), ("Agent", "")]
    assert tools[1].subagent_note == "finished 1"


def test_subagent_is_linked_through_its_meta_tool_use_id(tmp_path):
    records = [
        claude_asst(T(1), [claude_tool("w1", "Agent", description="in a workflow"),
                           claude_tool("l1", "Agent", description="linked by id"),
                           claude_tool("n1", "Agent", description="nobody")]),
        claude_user(T(2), [claude_result("l1", "ok")], toolUseResult={"agentId": "linked"}),
    ]
    subs = [
        ("workflows/wf_1/agent-wfagent.jsonl", _sub_records("Workflow answer.\nMore."), {"toolUseId": "w1"}),
        ("agent-linked.jsonl", _sub_records("Linked answer."), {"toolUseId": "l1"}),
        # Same call as above, but that call is linked already: it must keep its agentId link.
        ("agent-other.jsonl", _sub_records("Other answer."), {"toolUseId": "l1"}),
        ("agent-nometa.jsonl", _sub_records("No meta."), None),
        ("agent-badmeta.jsonl", _sub_records("Bad meta."), {"toolUseId": ["x"]}),
    ]
    unit = write_claude_session(tmp_path, records, subs=subs)
    assert any(p.endswith("workflows/wf_1/agent-wfagent.jsonl") for p in unit.paths)
    s = claude.parse_unit(unit)
    tools = _tools(s.turns[0])
    assert [(t.subagent_id, t.subagent_note) for t in tools] == [
        ("wfagent", "Workflow answer."), ("linked", "Linked answer."), ("", "")]
    assert {sub.id for sub in s.subagents} == {"wfagent", "linked", "other", "nometa", "badmeta"}


def test_subagent_project_comes_from_its_own_cwd_when_the_parent_has_none(tmp_path):
    subs = [("agent-abc.jsonl", _sub_records("hi", cwd="/Users/me/Repos/other-proj"), None)]
    s = _session(tmp_path, [], subs=subs)
    assert s.cwd == "" and s.subagents[0].cwd == "/Users/me/Repos/other-proj"
    assert s.subagents[0].project == "other-proj"


def test_subagent_uses_the_parent_project_when_the_parent_has_a_cwd(tmp_path):
    subs = [("agent-abc.jsonl", _sub_records("hi", cwd=CWD + "/sub"), None)]
    s = _session(tmp_path, [claude_user(T(1), "hi")], subs=subs)
    assert s.project == "demo" and s.subagents[0].project == "demo"


# ---- item 2: headless sessions (entrypoint "sdk-cli", what `claude -p` writes)

def _headless(tmp_path, entrypoints, sub_entrypoint=None):
    ts = "2026-10-06T10:00:0%dZ"
    recs = []
    for i, ep in enumerate(entrypoints):
        extra = {"entrypoint": ep} if ep is not None else {}
        recs.append(claude_user(ts % (2 * i), f"question {i}", **extra))
        recs.append(claude_asst(ts % (2 * i + 1), f"answer {i}", **extra))
    subs = []
    if sub_entrypoint:
        subs = [("agent-s1.jsonl", [claude_user("2026-10-06T10:00:05Z", "sub task", entrypoint=sub_entrypoint),
                                    claude_asst("2026-10-06T10:00:06Z", "sub done", entrypoint=sub_entrypoint)], None)]
    return claude.parse_unit(write_claude_session(tmp_path, recs, subs=subs))


def test_an_sdk_cli_transcript_is_headless(tmp_path):
    s = _headless(tmp_path, ["sdk-cli"])
    assert s.headless is True and s.user_turns == 1


@pytest.mark.parametrize("entrypoint", ["cli", "claude-desktop", "", None])
def test_other_entrypoints_are_not_headless(tmp_path, entrypoint):
    assert _headless(tmp_path, [entrypoint]).headless is False


def test_the_fixture_session_is_not_headless(tmp_path):
    assert _parse(tmp_path)[1].headless is False


def test_only_the_main_transcript_decides_headless(tmp_path):
    assert _headless(tmp_path, ["cli"], sub_entrypoint="sdk-cli").headless is False
    assert _headless(tmp_path, ["sdk-cli"], sub_entrypoint="cli").headless is True


# ---------------------------------------------------------------- a subagent copied under two sessions (resume / fork)

def _pair(tmp_path, **kw):
    units = claude.discover(make_shared_pair(tmp_path, **kw))
    return {Path(u.main).stem: u for u in units}


def test_discover_tells_each_holder_of_a_copied_subagent_about_the_others(tmp_path):
    units = _pair(tmp_path)
    a, b = units[SID], units[SID_B]
    copy_a = next(p for p in a.paths if p.endswith(f"agent-{AID}.jsonl"))
    copy_b = next(p for p in b.paths if p.endswith(f"agent-{AID}.jsonl"))
    assert a.shared == b.shared == {AID: {a.main: copy_a, b.main: copy_b}}       # AID_B is b's alone
    assert a.related == [b.main, copy_b] and b.related == [a.main, copy_a]


def test_a_change_to_the_other_holder_changes_the_fingerprint(tmp_path):
    units = _pair(tmp_path)
    before = units[SID].fingerprint()
    with open(units[SID_B].main, "a", encoding="utf-8") as fh:
        fh.write("{}\n")
    assert units[SID].fingerprint() != before


def test_a_subagent_held_by_one_session_has_no_shared_entry(tmp_path):
    unit = claude.discover(make_claude_tree(tmp_path))[0]
    assert unit.shared == {} and unit.related == []


@pytest.mark.parametrize("names, first", [
    (["A", "A", "B", "B"], SID),            # started under a, kept running under the resumed b
    (["B", "B", "A", "A"], SID_B),
    (["X", "B", "A", "A"], SID_B),          # X: a session that no longer exists names nobody here
    (["X", "X", "X", "X"], SID),            # nobody named: smallest id
    ([None, None, None, None], SID),
])
def test_owner_order_puts_the_session_the_subagent_ran_under_first(tmp_path, names, first):
    ids = {"A": SID, "B": SID_B, "X": "77777777-0000-0000-0000-000000000000", None: None}
    units = _pair(tmp_path, names=[ids[n] for n in names])
    order = claude.owner_order(units[SID].shared[AID])
    assert [Path(m).stem for m in order] == [first, SID_B if first == SID else SID]
    assert order == claude.owner_order(units[SID_B].shared[AID])               # both holders agree


def test_copies_that_name_their_own_holder_at_the_same_time_go_by_session_id(tmp_path):
    units = _pair(tmp_path, names=[SID] * 4)
    copies = units[SID].shared[AID]
    copy_b = copies[units[SID_B].main]
    Path(copy_b).write_text(Path(copy_b).read_text().replace(SID, SID_B))    # a copy that rewrote the sessionId
    assert [Path(m).stem for m in claude.owner_order(copies)] == [SID, SID_B]


def test_owner_order_skips_a_copy_that_is_gone(tmp_path):
    units = _pair(tmp_path)
    copies = units[SID].shared[AID]
    Path(copies[units[SID].main]).unlink()
    assert [Path(m).stem for m in claude.owner_order(copies)] == [SID, SID_B]  # the other copy still names a first
