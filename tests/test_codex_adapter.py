import json
import os
import sqlite3

import pytest
from fixtures import (CWD, S3, T1, T2, T3, codex_call, codex_msg, codex_output, codex_patch, codex_t1_seg1,
                      codex_t1_seg3, make_codex_tree, write_codex_unit, write_jsonl)

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


def _parse(tmp_path, items, **kw):
    return codex.parse_unit(write_codex_unit(tmp_path, items, **kw))


def _tools(s):
    return [i for t in s.turns for i in t.items if isinstance(i, ToolCall)]


def _only_tool(tmp_path, items, **kw):
    tools = _tools(_parse(tmp_path, items, **kw))
    assert len(tools) == 1
    return tools[0]


# ---------------------------------------------------------------- per-record robustness (X1)

def test_odd_records_do_not_lose_the_session(tmp_path):
    items = [
        codex_msg("user", "first prompt"),
        ("response_item", [1, 2]),
        ("event_msg", "text"),
        ("response_item", {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": 5}, {"type": "output_text", "text": None},
                                       "plain", {"type": "output_text", "text": "fine"}]}),
        codex_call("exec_command", "c1", {"cmd": "ls"}),
        codex_output("c1", [{"type": "input_text", "text": None},
                            {"type": "input_text", "text": "Exit code: 2\nOutput:\nboom"}]),
        codex_call("exec_command", "c2", {"cmd": "pwd"}),
        codex_output("c2", json.dumps({"output": "fine", "metadata": "weird"})),
        ("response_item", {"type": "web_search_call", "action": "search"}),
        ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": 7}]}),
        codex_msg("user", "second prompt"),
        ("turn_context", "not a dict"),
    ]
    s = _parse(tmp_path, items, meta={"git": "not-a-dict", "source": {"subagent": {"thread_spawn": "x"}}})
    assert s.branch == "" and s.project == "demo" and s.parent == ""
    assert [t.role for t in s.turns] == ["user", "assistant", "user", "user"]
    assert [s.turns[0].text, s.turns[1].text, s.turns[2].text, s.turns[3].text] == [
        "first prompt", "5\n\nfine", "7", "second prompt"]
    ls, pwd, search = _tools(s)
    assert (ls.status, ls.error_head) == ("error", "boom") and pwd.status == "ok"
    assert (search.name, search.arg) == ("web_search", "")


def test_session_meta_with_a_bad_payload_is_not_the_session_meta(tmp_path):
    s = _parse(tmp_path, [codex_msg("user", "hello")], leading=[("session_meta", "oops")])
    assert s.id == T1 and s.cwd == CWD and s.turns[0].text == "hello"


def test_non_numeric_history_cut_is_ignored(tmp_path):
    day = tmp_path / "sessions" / "2026" / "09" / "22"
    seg3 = codex_t1_seg3()
    seg3[0]["payload"]["history_base"]["end_ordinal_exclusive"] = "abc"
    write_jsonl(day / f"rollout-2026-09-22T11-41-09-{T1}.jsonl", codex_t1_seg1())
    write_jsonl(day / f"rollout-2026-09-22T22-21-24-{T1}_{S3}.jsonl", seg3)
    (unit,) = codex.discover([tmp_path / "sessions"])
    s = codex.parse_unit(unit)
    assert s.skipped["<bad-record>"] == 1
    assert s.turns[0].text == "Add a retry to the fetch client" and s.turns[-1].text == "Logging added."


def test_record_that_still_raises_is_counted_and_skipped(tmp_path, monkeypatch):
    def boom(name, args):
        raise RuntimeError("boom")
    monkeypatch.setattr(codex, "_function_call", boom)
    s = _parse(tmp_path, [codex_msg("user", "hi"), codex_call("exec_command", "c1", {"cmd": "ls"}),
                          codex_msg("assistant", "still here")])
    assert s.skipped == {"<bad-record>": 1}
    assert [t.text for t in s.turns] == ["hi", "still here"]


# ---------------------------------------------------------------- titles (X2)

def _db(path, columns="id TEXT PRIMARY KEY, title TEXT, name TEXT", rows=(), wal=False):
    con = sqlite3.connect(str(path))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"CREATE TABLE threads({columns})")
    for row in rows:
        con.execute(f"INSERT INTO threads VALUES ({','.join('?' * len(row))})", row)
    con.commit()
    con.close()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(str(path) + suffix):
            os.remove(str(path) + suffix)


def test_titles_load_from_a_wal_mode_db_without_side_files(tmp_path):
    _db(tmp_path / "state_5.sqlite", rows=[("t1", "From the db", "")], wal=True)
    assert not (tmp_path / "state_5.sqlite-wal").exists() and not (tmp_path / "state_5.sqlite-shm").exists()
    assert codex.load_titles(tmp_path) == {"t1": "From the db"}


def test_titles_load_when_the_threads_table_has_no_name_column(tmp_path):
    _db(tmp_path / "state_5.sqlite", columns="id TEXT PRIMARY KEY, title TEXT", rows=[("t1", "Only title")])
    assert codex.load_titles(tmp_path) == {"t1": "Only title"}


def test_titles_fall_back_to_name_and_ignore_odd_values(tmp_path):
    _db(tmp_path / "state_5.sqlite", columns="id TEXT PRIMARY KEY, title, name", rows=[
        ("t1", "", "Named"), ("t2", 42, "Name beats a number"), ("t3", "  \n ", "Blank title"),
        ("t4", b"bytes", None), ("t5", None, None)])
    assert codex.load_titles(tmp_path) == {"t1": "Named", "t2": "Name beats a number", "t3": "Blank title"}


def test_titles_need_a_title_column(tmp_path):
    _db(tmp_path / "state_5.sqlite", columns="id TEXT PRIMARY KEY, name TEXT", rows=[("t1", "Named")])
    assert codex.load_titles(tmp_path) == {}


def test_titles_use_the_database_with_the_highest_number(tmp_path):
    _db(tmp_path / "state_5.sqlite", rows=[("t1", "OLD", ""), ("only5", "Only in 5", "")])
    _db(tmp_path / "state_10.sqlite", rows=[("t1", "NEW", "")])
    assert codex.load_titles(tmp_path) == {"t1": "NEW"}


def test_long_multi_line_titles_are_cut_to_one_line(tmp_path):
    _db(tmp_path / "state_5.sqlite", rows=[("t1", "First line\n" + "x" * 5000, "")])
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": "t2", "thread_name": "\n  Indexed\n" + "y" * 5000}) + "\n"
        + json.dumps({"id": "t3", "thread_name": 5}) + "\n" + json.dumps({"id": ["t4"], "thread_name": "x"}) + "\n")
    titles = codex.load_titles(tmp_path)
    assert titles["t1"] == "First line"
    assert titles["t2"] == "Indexed" and set(titles) == {"t1", "t2"}
    _db(tmp_path / "state_6.sqlite", rows=[("t5", "z" * 5000, "")])
    assert len(codex.load_titles(tmp_path)["t5"]) == 120


def test_an_unreadable_session_index_is_ignored(tmp_path):
    _db(tmp_path / "state_5.sqlite", rows=[("t1", "From the db", "")])
    (tmp_path / "session_index.jsonl").mkdir()          # reading it raises an OSError
    assert codex.load_titles(tmp_path) == {"t1": "From the db"}


def test_a_corrupt_database_is_ignored(tmp_path):
    (tmp_path / "state_5.sqlite").write_text("this is not a database")
    assert codex.load_titles(tmp_path) == {}


# ---------------------------------------------------------------- exit codes (X3)

EXEC_RUNNING = ("Chunk ID: 1\nWall time: 1.0 seconds\nProcess running with session ID 7\n"
                "Original token count: 5\nOutput:\n")


@pytest.mark.parametrize("output,status", [
    # a running process whose own log says it exited
    (EXEC_RUNNING + "worker exited with code 1", "ok"),
    (EXEC_RUNNING + "Process exited with code 1", "ok"),
    (EXEC_RUNNING + "Exit code: 3", "ok"),
    ("Chunk ID: 1\nWall time: 1.0 seconds\nProcess exited with code 0\nOutput:\nExit code: 1", "ok"),
    ("Chunk ID: 1\nWall time: 1.0 seconds\nProcess exited with code 2\nOutput:\nboom", "error"),
    ("Exit code: -1\nWall time: 0.1 seconds\nOutput:\nboom", "error"),
    ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\nok", "ok"),
    # no Output: marker: only a line that is the exit code itself counts
    ("the docs say Exit code: 2 means usage error", "ok"),
    ("It exited with code 1 yesterday", "ok"),
])
def test_exit_code_is_read_from_the_header_only(tmp_path, output, status):
    items = [codex_call("exec_command", "c1", {"cmd": "run"}), codex_output("c1", output)]
    assert _only_tool(tmp_path, items).status == status


def test_exit_code_in_the_body_of_a_non_exec_tool_is_not_an_error(tmp_path):
    body = "Notes\nOutput:\nExit code: 2\nProcess exited with code 1\n"
    items = [codex_call("read_notes", "c1", {"path": "notes.md"}), codex_output("c1", body)]
    assert _only_tool(tmp_path, items).status == "ok"


@pytest.mark.parametrize("code,status", [("0", "ok"), (0, "ok"), ("3", "error"), (1, "error"), ("x", "ok"),
                                         (None, "ok")])
def test_metadata_exit_code_is_coerced(tmp_path, code, status):
    out = json.dumps({"output": "text", "metadata": {"exit_code": code, "duration_seconds": 0.1}})
    items = [codex_call("shell", "c1", {"command": "x"}), codex_output("c1", out)]
    assert _only_tool(tmp_path, items).status == status


# ---------------------------------------------------------------- discover (X4)

def test_discover_keeps_one_copy_of_a_rollout_found_in_two_directories(tmp_path):
    name = f"rollout-2026-09-22T11-41-09-{T1}.jsonl"
    live = write_jsonl(tmp_path / "sessions" / "2026" / "09" / "22" / name, codex_t1_seg1())
    archived = write_jsonl(tmp_path / "archived_sessions" / name, codex_t1_seg1())
    seg3 = write_jsonl(tmp_path / "sessions" / "2026" / "09" / "22" / f"rollout-2026-09-22T22-21-24-{T1}_{S3}.jsonl",
                       codex_t1_seg3())
    dirs = [tmp_path / "sessions", tmp_path / "archived_sessions"]

    os.utime(live, (1_000, 1_000))
    os.utime(archived, (2_000, 2_000))                      # same size: the newer copy wins
    (unit,) = codex.discover(dirs)
    assert unit.paths == [str(archived), str(seg3)]
    (unit,) = codex.discover(dirs[::-1])
    assert unit.paths == [str(archived), str(seg3)]

    with open(live, "a", encoding="utf-8") as fh:           # a larger copy wins, even when it is older
        fh.write("\n")
    os.utime(live, (500, 500))
    (unit,) = codex.discover(dirs)
    assert unit.paths == [str(live), str(seg3)]


# ---------------------------------------------------------------- injected context (X5)

TAGS = ["environment_context", "user_instructions", "recommended_plugins", "permissions instructions",
        "INSTRUCTIONS", "subagent_notification", "turn_aborted", "skill", "user_shell_command"]


@pytest.mark.parametrize("tag", TAGS)
def test_known_injected_blocks_are_dropped(tmp_path, tag):
    s = _parse(tmp_path, [codex_msg("user", f"<{tag}>\ninjected body\n</{tag}>"), codex_msg("user", "real prompt")])
    assert [t.text for t in s.turns] == ["real prompt"]


def test_agents_md_injection_is_dropped(tmp_path):
    text = "# AGENTS.md instructions for /Users/me/repo\n\n<INSTRUCTIONS>\nbe nice\n</INSTRUCTIONS>"
    s = _parse(tmp_path, [codex_msg("user", text), codex_msg("user", "real prompt")])
    assert [t.text for t in s.turns] == ["real prompt"]


@pytest.mark.parametrize("text", [
    "<div>Fix this layout</div>",
    "<Button>Save</Button> is misaligned",
    "# AGENTS.md instructions are wrong, fix them",
    "<skills> is a folder name</skills>",
])
def test_real_prompts_that_look_like_markup_are_kept(tmp_path, text):
    s = _parse(tmp_path, [codex_msg("user", text)])
    assert [t.text for t in s.turns] == [text]


# ---------------------------------------------------------------- apply_patch and tool arguments (X6, X7)

def test_apply_patch_counts_every_added_and_removed_line(tmp_path):
    patch = (f"*** Begin Patch\n*** Update File: {CWD}/a.py\n@@\n context\n++i;\n+++plus\n----\n-- sql\n"
             "*** End Patch")
    tc = _only_tool(tmp_path, [codex_patch(patch)])
    assert tc.diff == "+2 −2" and tc.arg == "a.py"


def test_apply_patch_move_adds_both_paths(tmp_path):
    patch = (f"*** Begin Patch\n*** Update File: {CWD}/old.py\n*** Move to: {CWD}/pkg/new.py\n@@\n-a\n+b\n"
             f"*** Add File: {CWD}/added.py\n+x\n+y\n*** Delete File: {CWD}/gone.py\n*** End Patch")
    s = _parse(tmp_path, [codex_patch(patch)])
    assert s.files == ["old.py", "pkg/new.py", "added.py", "gone.py"]
    assert _tools(s)[0].arg == "old.py, pkg/new.py, added.py, gone.py" and _tools(s)[0].diff == "+3 −1"


def test_calls_without_a_call_id_are_not_registered(tmp_path):
    items = [codex_call("exec_command", None, {"cmd": "first"}), codex_call("exec_command", None, {"cmd": "second"}),
             codex_output(None, "Chunk ID: 1\nProcess exited with code 1\nOutput:\nboom"),
             codex_output(["x"], "Process exited with code 1\nOutput:\nboom")]
    assert [(t.arg, t.status) for t in _tools(_parse(tmp_path, items))] == [("first", "ok"), ("second", "ok")]


def test_list_commands_are_joined_with_spaces(tmp_path):
    items = [codex_call("exec_command", "c1", {"cmd": ["npm", "run", "test"]}),
             codex_call("shell", "c2", {"command": ["bash", "-lc", "git status"], "workdir": CWD}),
             codex_call("exec_command", "c3", {"cmd": "ls -la\nsecond line"})]
    assert [t.arg for t in _tools(_parse(tmp_path, items))] == ["npm run test", "bash -lc git status", "ls -la"]


def _agent_msg(author, recipient, text):
    return ("response_item", {"type": "agent_message", "author": author, "recipient": recipient,
                              "content": [{"type": "input_text", "text": text}]})


def test_subagent_task_from_parent_becomes_the_first_user_turn(tmp_path):
    from fixtures import T2, codex_msg, write_codex_unit
    spawn = {"parent_thread_id": T1, "agent_path": "/root/finder", "agent_nickname": "Maxwell"}
    unit = write_codex_unit(tmp_path, [
        codex_msg("developer", "rules for subagents"),
        _agent_msg("/root", "/root/finder", "Find all fetch calls\nin src/"),
        codex_msg("assistant", "Found 3 calls."),
    ], tid=T2, meta={"source": {"subagent": {"thread_spawn": spawn}}, "agent_path": "/root/finder"})
    s = codex.parse_unit(unit, {})
    assert [t.role for t in s.turns] == ["user", "assistant"]
    assert s.turns[0].text == "[from /root] Find all fetch calls\nin src/"
    assert s.title == "Find all fetch calls"
    assert "response_item:agent_message" not in s.skipped


def test_top_level_thread_keeps_reports_from_children_and_ignores_its_own_messages(tmp_path):
    from fixtures import codex_msg, write_codex_unit
    unit = write_codex_unit(tmp_path, [
        codex_msg("user", "Audit the repo"),
        _agent_msg("/root", "/root/finder", "Find all fetch calls"),
        _agent_msg("/root/finder", "/root", "Found 3 calls in src/api.ts"),
        codex_msg("assistant", "Done."),
    ])
    s = codex.parse_unit(unit, {})
    assert [t.role for t in s.turns] == ["user", "user", "assistant"]
    assert s.turns[1].text == "[from /root/finder] Found 3 calls in src/api.ts"
    assert s.title == "Audit the repo"
