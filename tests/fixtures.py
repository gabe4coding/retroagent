"""Synthetic Claude Code and Codex transcripts. Never put real session data in tests."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

SID = "11111111-2222-3333-4444-555555555555"
AID = "a1d0ec8d8e94ad30f"
CWD = "/Users/me/Repositories/demo"
T1 = "01a0c87d-996a-7962-9b1b-67ac92987a24"
S3 = "01a0cac7-c522-7e82-b83a-b0b0b0b0b0b0"
T2 = "01a0c900-0000-7000-8000-000000000002"
T3 = "01a0c900-0000-7000-8000-000000000003"
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    return path


def age(paths, seconds=3600):
    t = time.time() - seconds
    for p in paths:
        os.utime(p, (t, t))


# ---------------------------------------------------------------- Claude Code

def claude_main(sid=SID, aid=AID, cwd=CWD):
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": "feat/x", "version": "2.1.289",
            "userType": "external", "isSidechain": False}

    def user(uuid, ts, content, **extra):
        return {**base, "type": "user", "uuid": uuid, "timestamp": ts,
                "message": {"role": "user", "content": content}, **extra}

    def asst(uuid, ts, mid, content, model="claude-opus-5-5"):
        return {**base, "type": "assistant", "uuid": uuid, "timestamp": ts,
                "message": {"id": mid, "role": "assistant", "model": model, "content": content}}

    return [
        user("u1", "2026-10-06T13:02:11.000Z",
             "Fix the flaky test in tests/motion.spec.ts <system-reminder>secret context</system-reminder>"),
        {**base, "type": "attachment", "uuid": "att1", "timestamp": "2026-10-06T13:02:12.000Z",
         "attachment": {"type": "hook_success", "content": "big hook output"}},
        asst("a1", "2026-10-06T13:03:00.000Z", "m1", [
            {"type": "thinking", "thinking": "private thoughts", "signature": "x"},
            {"type": "text", "text": "Let me run the tests."},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": f"GITHUB_TOKEN={GH_TOKEN} npm test\n--watch=false", "description": "run"}}]),
        user("u2", "2026-10-06T13:03:30.000Z",
             [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
               "content": "FAIL tests/motion.spec.ts\nExpected 3, received 2\nline3\nline4"}],
             toolUseResult={"stdout": "", "stderr": ""}),
        asst("a2", "2026-10-06T13:04:00.000Z", "m2", [
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": cwd + "/src/motion.ts", "old_string": "a\nb", "new_string": "a\nb\nc"}}]),
        user("u3", "2026-10-06T13:04:10.000Z", [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]),
        asst("a3", "2026-10-06T13:05:00.000Z", "m3", [
            {"type": "tool_use", "id": "t3", "name": "Agent",
             "input": {"description": "Explore tests", "prompt": "Find flaky tests", "subagent_type": "Explore"}}]),
        user("u4", "2026-10-06T13:06:00.000Z",
             [{"type": "tool_result", "tool_use_id": "t3",
               "content": [{"type": "text", "text": "Found 2 flaky tests.\nDetails..."}]}],
             toolUseResult={"agentId": aid, "status": "completed"}),
        asst("a4", "2026-10-06T13:07:00.000Z", "m4", [{"type": "text", "text": "Fixed: the timer was not awaited."}]),
        user("u5", "2026-10-06T13:08:00.000Z", "meta skill body", isMeta=True),
        user("u6", "2026-10-06T13:09:00.000Z", "<task-notification>done</task-notification>",
             origin={"kind": "task-notification"}, turnOrigin="task_notification"),
        user("u7", "2026-10-06T13:10:00.000Z",
             "<command-name>/kb-retro</command-name><command-message>kb-retro</command-message>"
             "<command-args>last week</command-args>"),
        asst("a5", "2026-10-06T13:11:00.000Z", "m5", [{"type": "text", "text": "Retro done."}]),
        asst("a6", "2026-10-06T13:11:30.000Z", "m6", [{"type": "text", "text": "No response requested."}],
             model="<synthetic>"),
        {"type": "ai-title", "aiTitle": "Fix flaky motion test", "sessionId": sid},
        {"type": "pr-link", "sessionId": sid, "prNumber": 7, "prUrl": "https://github.com/me/demo/pull/7",
         "prRepository": "me/demo", "timestamp": "2026-10-06T13:12:00.000Z"},
        {"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.0},
        {"type": "brand-new-record-type", "sessionId": sid, "foo": 1},
        "this line is not json",
    ]


def claude_sub(sid=SID, aid=AID, cwd=CWD):
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": "feat/x", "isSidechain": True, "agentId": aid}
    return [
        {**base, "type": "user", "uuid": "s1", "timestamp": "2026-10-06T13:05:10.000Z",
         "message": {"role": "user", "content": "Find flaky tests"}},
        {**base, "type": "assistant", "uuid": "s2", "timestamp": "2026-10-06T13:05:20.000Z",
         "message": {"id": "sm1", "role": "assistant", "model": "claude-haiku-4-5", "content": [
             {"type": "text", "text": "Searching."},
             {"type": "tool_use", "id": "st1", "name": "Grep", "input": {"pattern": "setTimeout", "path": "tests"}}]}},
        {**base, "type": "user", "uuid": "s3", "timestamp": "2026-10-06T13:05:30.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "st1", "content": "tests/motion.spec.ts:12"}]}},
        {**base, "type": "assistant", "uuid": "s4", "timestamp": "2026-10-06T13:05:50.000Z",
         "message": {"id": "sm2", "role": "assistant", "model": "claude-haiku-4-5",
                     "content": [{"type": "text", "text": "Found 2 flaky tests.\nDetails..."}]}},
    ]


def make_claude_tree(root, sid=SID, aid=AID, cwd=CWD):
    """Create <root>/projects/<encoded cwd>/<sid>.jsonl plus one subagent. Returns the projects dir."""
    projects = Path(root) / "projects"
    proj = projects / ("-" + cwd.strip("/").replace("/", "-"))
    main = write_jsonl(proj / f"{sid}.jsonl", claude_main(sid, aid, cwd))
    sub_dir = proj / sid / "subagents"
    sub = write_jsonl(sub_dir / f"agent-{aid}.jsonl", claude_sub(sid, aid, cwd))
    (sub_dir / f"agent-{aid}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "Explore tests", "toolUseId": "t3"}))
    age([main, sub])
    return projects


# ---------------------------------------------------------------- Codex

def _cx(ts, typ, payload, ordinal):
    return {"timestamp": ts, "type": typ, "ordinal": ordinal, "payload": payload}


def _msg(role, kind, text):
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def codex_t1_seg1(t1=T1, cwd=CWD):
    items = [
        ("session_meta", {"id": t1, "session_id": t1, "timestamp": "2026-09-22T09:41:00.000Z", "cwd": cwd,
                          "originator": "Codex Desktop", "source": "vscode",
                          "git": {"commit_hash": "abc", "branch": "main",
                                  "repository_url": "git@github.com:me/demo-repo.git"},
                          "base_instructions": {"text": "very long system prompt"}}),
        ("response_item", _msg("developer", "input_text", "developer rules")),
        ("response_item", _msg("user", "input_text", "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>")),
        ("response_item", _msg("user", "input_text", "Add a retry to the fetch client")),
        ("turn_context", {"turn_id": "turn1", "cwd": cwd, "model": "gpt-5.5-codex"}),
        ("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "ENCRYPTED"}),
        ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "c1",
                           "arguments": json.dumps({"cmd": "npm test", "workdir": cwd})}),
        ("response_item", {"type": "function_call_output", "call_id": "c1",
                           "output": "Chunk ID: 1\nWall time: 1.0 seconds\nProcess exited with code 1\nOutput:\n"
                                     "TypeError: x is undefined\n    at foo"}),
        ("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "status": "completed",
                           "input": f"*** Begin Patch\n*** Update File: {cwd}/src/client.ts\n@@\n-old\n+new\n+new2\n"
                                    "*** End Patch"}),
        ("response_item", {"type": "custom_tool_call_output", "call_id": "c2",
                           "output": [{"type": "input_text", "text": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess"}]}),
        ("response_item", _msg("assistant", "output_text", "Added retry with backoff.")),
        ("event_msg", {"type": "token_count"}),
        ("response_item", _msg("user", "input_text", "thanks, also log the attempts")),
    ]
    return [_cx(f"2026-09-22T09:41:{i:02d}.000Z", t, p, i) for i, (t, p) in enumerate(items)]


def codex_t1_seg3(t1=T1, prev=T1, cwd=CWD):
    meta = {"id": t1, "session_id": t1, "timestamp": "2026-09-22T20:21:24.000Z", "cwd": cwd, "source": "vscode",
            "history_base": {"thread_id": prev, "end_ordinal_exclusive": 12, "end_byte_offset": 999}}
    return [
        _cx("2026-09-22T20:21:24.000Z", "session_meta", meta, 12),
        _cx("2026-09-22T20:21:30.000Z", "response_item", _msg("user", "input_text", "Please log the attempts too"), 13),
        _cx("2026-09-22T20:22:00.000Z", "response_item", _msg("assistant", "output_text", "Logging added."), 14),
    ]


def codex_t2(t2=T2, t1=T1, cwd=CWD):
    meta = {"id": t2, "session_id": t1, "timestamp": "2026-09-22T09:50:00.000Z", "cwd": cwd,
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": t1, "depth": 1, "agent_nickname": "scout"}}}}
    return [
        _cx("2026-09-22T09:50:00.000Z", "session_meta", meta, 0),
        _cx("2026-09-22T09:50:01.000Z", "response_item", _msg("user", "input_text", "Find all fetch calls"), 1),
        _cx("2026-09-22T09:50:30.000Z", "response_item", _msg("assistant", "output_text", "Found 3 calls."), 2),
    ]


def codex_t3(t3=T3, t1=T1, cwd=CWD):
    meta = {"id": t3, "session_id": t1, "timestamp": "2026-09-22T09:55:00.000Z", "cwd": cwd,
            "source": {"subagent": {"other": "guardian"}}}
    return [
        _cx("2026-09-22T09:55:00.000Z", "session_meta", meta, 0),
        _cx("2026-09-22T09:55:01.000Z", "response_item", _msg("user", "input_text", ">>> APPROVAL REQUEST START"), 1),
    ]


def make_codex_tree(root, t1=T1, t2=T2, t3=T3, cwd=CWD):
    """Create a Codex home with 2 segments of T1, subagent T2, guardian T3. Returns (sessions_dir, codex_home)."""
    home = Path(root) / "codex"
    day = home / "sessions" / "2026" / "09" / "22"
    files = [
        write_jsonl(day / f"rollout-2026-09-22T11-41-09-{t1}.jsonl", codex_t1_seg1(t1, cwd)),
        write_jsonl(day / f"rollout-2026-09-22T22-21-24-{t1}_{S3}.jsonl", codex_t1_seg3(t1, t1, cwd)),
        write_jsonl(day / f"rollout-2026-09-22T11-50-00-{t2}.jsonl", codex_t2(t2, t1, cwd)),
        write_jsonl(day / f"rollout-2026-09-22T11-55-00-{t3}.jsonl", codex_t3(t3, t1, cwd)),
    ]
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": t1, "thread_name": "Old name", "updated_at": "x"}) + "\n"
        + json.dumps({"id": t1, "thread_name": "Fetch retry", "updated_at": "y"}) + "\n")
    con = sqlite3.connect(str(home / "state_5.sqlite"))
    con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
    con.execute("INSERT INTO threads VALUES (?,?,?)", (t2, "Explore fetch calls", ""))
    con.commit()
    con.close()
    age(files)
    return home / "sessions", home


# ---------------------------------------------------------------- git + config helpers

def git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} (cwd={cwd}) failed with exit {p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()}")
    return p


def init_remote(tmp):
    """A bare remote with one commit on main (README + .gitignore)."""
    tmp = Path(tmp)
    remote = tmp / "remote.git"
    git("init", "--bare", "-q", "-b", "main", str(remote))
    seed = tmp / "seed"
    git("clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("kb\n")
    (seed / ".gitignore").write_text(".kb/\n")
    git("add", ".", cwd=seed)
    git("commit", "-q", "-m", "init", cwd=seed)
    git("push", "-q", "-u", "origin", "main", cwd=seed)
    return remote


def clone(remote, dest):
    git("clone", "-q", str(remote), str(dest))
    return Path(dest)


def make_config(root, host, claude_dir, codex_sessions, codex_home, **kw):
    from kb.config import Config
    return Config(root=Path(root), host=host, claude_dir=Path(claude_dir), codex_dirs=[Path(codex_sessions)],
                  codex_home=Path(codex_home), **kw)
