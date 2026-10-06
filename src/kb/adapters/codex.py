"""Codex adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>[_<segment>].jsonl.

One thread can span several files. A later segment's session_meta.history_base cuts the earlier
history at end_ordinal_exclusive; records are then keyed by their 'ordinal'.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from kb.adapters.common import iter_records
from kb.model import Session, ToolCall, Unit
from kb.util import (clean_user_text, first_line, head_lines, iso_utc, project_from_cwd,
                     project_from_git_url, rel_path)

_NAME = re.compile(r"^rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)-([0-9a-f-]{36})(?:_([0-9a-f-]{36}))?\.jsonl$")
_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>",
                      "<recommended_plugins>", "<permissions instructions>", "<INSTRUCTIONS>")
_INJECTED_BLOCK = re.compile(r"^\s*<([A-Za-z_][\w -]*)>.*</\1>\s*$", re.S)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
_EXIT = re.compile(r"(?:Exit code:|exited with code)\s*(-?\d+)")
_EXEC_HEADER = re.compile(r"^(?:Exit code:|Wall time:|Chunk ID:|Process exited with code|Original token count:|Output:).*$",
                          re.M)
_EXEC_CMD = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def discover(dirs) -> list:
    groups = defaultdict(list)
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.rglob("rollout-*.jsonl"):
            m = _NAME.match(p.name)
            groups[m.group(2) if m else p.stem].append(p)
    units = []
    for thread, paths in sorted(groups.items()):
        paths.sort(key=lambda p: ((_NAME.match(p.name).group(1) if _NAME.match(p.name) else ""), p.name))
        units.append(Unit(key=f"codex:{thread}", agent="codex", paths=[str(p) for p in paths], main=str(paths[-1])))
    return units


def load_titles(codex_home) -> dict:
    """Thread id -> title. session_index.jsonl (last line wins) beats state_*.sqlite threads.title."""
    home = Path(codex_home)
    titles = {}
    for db in sorted(home.glob("state_*.sqlite"))[-1:]:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                for tid, title in con.execute("SELECT id, COALESCE(NULLIF(title, ''), NULLIF(name, '')) FROM threads"):
                    if title:
                        titles[tid] = title
            finally:
                con.close()
        except sqlite3.Error:
            pass
    index = home / "session_index.jsonl"
    if index.exists():
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("id") and o.get("thread_name"):
                titles[o["id"]] = o["thread_name"]
    return titles


def _load_records(paths, skipped: Counter) -> list:
    merged, unordered = {}, []
    for p in paths:
        recs = list(iter_records(p, skipped))
        if not recs:
            continue
        head = recs[0]
        hb = (head.get("payload") or {}).get("history_base") if head.get("type") == "session_meta" else None
        if isinstance(hb, dict) and hb.get("end_ordinal_exclusive") is not None:
            cut = int(hb["end_ordinal_exclusive"])
            merged = {k: v for k, v in merged.items() if k < cut}
        for r in recs:
            if isinstance(r.get("ordinal"), int):
                merged[r["ordinal"]] = r
            else:
                unordered.append(r)
    return [merged[k] for k in sorted(merged)] + unordered


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _user_text(content) -> str:
    texts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        tx = c.get("text") or ""
        if not tx.strip() or tx.lstrip().startswith(_INJECTED_PREFIXES) or _INJECTED_BLOCK.match(tx):
            continue
        texts.append(tx.strip())
    return clean_user_text("\n\n".join(texts))


def _function_call(name: str, args: dict) -> ToolCall:
    if name == "exec_command":
        arg = first_line(str(args.get("cmd", "")), 120)
    elif name == "spawn_agent":
        arg = first_line(str(args.get("task_name") or args.get("message") or ""), 100)
    else:
        arg = next((first_line(v, 80) for v in args.values() if isinstance(v, str) and v), "")
    return ToolCall(name=name, arg=arg)


def _custom_call(name: str, inp, cwd: str, files: list) -> ToolCall:
    text = inp if isinstance(inp, str) else json.dumps(inp)
    if name == "apply_patch":
        paths = [rel_path(p.strip(), cwd) for p in _PATCH_FILE.findall(text)]
        for p in paths:
            if p not in files:
                files.append(p)
        lines = text.splitlines()
        add = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        rem = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        return ToolCall(name="apply_patch", arg=", ".join(paths)[:120], diff=f"+{add} −{rem}")
    if name == "exec":
        m = _EXEC_CMD.search(text)
        cmd = text
        if m:
            try:
                cmd = json.loads('"' + m.group(1) + '"')
            except ValueError:
                cmd = m.group(1)
        return ToolCall(name="exec", arg=first_line(cmd, 120))
    return ToolCall(name=name, arg=first_line(text, 80))


def _output_text(out) -> str:
    if isinstance(out, list):
        return "\n".join(i.get("text", "") for i in out if isinstance(i, dict))
    if isinstance(out, str):
        try:
            j = json.loads(out)
            if isinstance(j, dict) and isinstance(j.get("output"), str):
                return j["output"]
        except ValueError:
            pass
        return out
    return ""


def _apply_output(tc, out) -> None:
    if tc is None:
        return
    text = _output_text(out)
    code = None
    if isinstance(out, str):
        try:
            j = json.loads(out)
            if isinstance(j, dict):
                code = (j.get("metadata") or {}).get("exit_code")
        except ValueError:
            pass
    if code is None:
        m = _EXIT.search(text[:2000])
        code = int(m.group(1)) if m else None
    if code not in (None, 0):
        tc.status = "error"
        tc.error_head = head_lines(_EXEC_HEADER.sub("", text))


def parse_unit(unit: Unit, titles=None):
    """Return a Session, or None for guardian/internal review threads."""
    skipped: Counter = Counter()
    recs = _load_records(unit.paths, skipped)
    meta = next(((r.get("payload") or {}) for r in recs if r.get("type") == "session_meta"), {})
    source = meta.get("source")
    sub_src = source.get("subagent") if isinstance(source, dict) else None
    if isinstance(sub_src, dict) and "other" in sub_src:
        return None
    parent = ((sub_src or {}).get("thread_spawn") or {}).get("parent_thread_id") if isinstance(sub_src, dict) else ""
    tid = meta.get("id") or unit.key.split(":", 1)[-1]
    s = Session(id=tid, agent="codex", source_paths=list(unit.paths), cwd=meta.get("cwd") or "",
                parent=parent or meta.get("parent_thread_id") or "")
    git = meta.get("git") or {}
    s.branch = git.get("branch") or ""
    stamps, calls, files = [], {}, []
    current = None
    for r in recs:
        ts = iso_utc(r.get("timestamp"))
        if ts:
            stamps.append(ts)
        kind, p = r.get("type"), r.get("payload") or {}
        if kind == "session_meta":
            continue
        if kind == "turn_context":
            s.model = s.model or p.get("model") or ""
            continue
        if kind != "response_item":
            if kind != "event_msg":
                skipped[str(kind)] += 1
            continue
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            if role == "user":
                text = _user_text(p.get("content"))
                if text:
                    s.add_turn("user", ts, [text])
                    current = None
            elif role == "assistant":
                text = "\n".join((c.get("text") or "") for c in p.get("content") or [] if isinstance(c, dict)).strip()
                if text:
                    current = current or s.add_turn("assistant", ts)
                    current.items.append(text)
        elif pt == "function_call":
            current = current or s.add_turn("assistant", ts)
            tc = _function_call(p.get("name", ""), _args(p.get("arguments")))
            current.items.append(tc)
            calls[p.get("call_id")] = tc
        elif pt == "custom_tool_call":
            current = current or s.add_turn("assistant", ts)
            tc = _custom_call(p.get("name", ""), p.get("input"), s.cwd, files)
            current.items.append(tc)
            calls[p.get("call_id")] = tc
        elif pt in ("function_call_output", "custom_tool_call_output"):
            _apply_output(calls.get(p.get("call_id")), p.get("output"))
        elif pt == "web_search_call":
            current = current or s.add_turn("assistant", ts)
            query = (p.get("action") or {}).get("query") or ""
            current.items.append(ToolCall(name="web_search", arg=first_line(str(query), 100)))
        elif pt in ("reasoning", "compaction"):
            continue
        else:
            skipped[f"response_item:{pt}"] += 1
    s.started = min(stamps) if stamps else ""
    s.ended = max(stamps) if stamps else ""
    s.project = project_from_git_url(git.get("repository_url") or "") or project_from_cwd(s.cwd)
    s.files = files
    s.title = (titles or {}).get(tid) or s.first_prompt() or "(untitled)"
    s.skipped = dict(skipped)
    return s
