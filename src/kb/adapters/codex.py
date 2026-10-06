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
# Context Codex injects as a user message. Only these tags: a real prompt can start with any other tag (<div>).
_INJECTED_TAGS = ("environment_context", "user_instructions", "recommended_plugins", "permissions instructions",
                  "INSTRUCTIONS", "subagent_notification", "turn_aborted", "skill", "user_shell_command")
_INJECTED = re.compile(r"^\s*(?:<(?:" + "|".join(re.escape(t) for t in _INJECTED_TAGS) + r")>"
                       r"|# AGENTS\.md instructions for \S+)")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$", re.M)
_EXIT = re.compile(r"^(?:Exit code:|Process exited with code)\s*(-?\d+)\s*$", re.M)
_OUTPUT_MARK = re.compile(r"^Output:\r?$", re.M)
_EXEC_HEADER = re.compile(r"^(?:Exit code:|Wall time:|Chunk ID:|Process exited with code|Original token count:|Output:).*$",
                          re.M)
_STATE_DB = re.compile(r"^state_(\d+)\.sqlite$")
_EXEC_CMD = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _rank(path: Path):
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns, str(path))
    except OSError:
        return (-1, -1, str(path))


def _segment_order(path: Path):
    m = _NAME.match(path.name)
    return (m.group(1) if m else "", path.name)


_FROM_PREFIX = re.compile(r"^\[from [^\]]*\]\s*")


def discover(dirs) -> list:
    groups = defaultdict(dict)                  # thread -> {file name: path}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.rglob("rollout-*.jsonl"):
            m = _NAME.match(p.name)
            copies = groups[m.group(2) if m else p.stem]
            # The same rollout can sit in sessions/ and archived_sessions/: keep the larger, then the newer copy.
            if p.name not in copies or _rank(p) > _rank(copies[p.name]):
                copies[p.name] = p
    units = []
    for thread, copies in sorted(groups.items()):
        paths = sorted(copies.values(), key=_segment_order)
        units.append(Unit(key=f"codex:{thread}", agent="codex", paths=[str(p) for p in paths], main=str(paths[-1])))
    return units


def _state_db(home: Path):
    """The state_<N>.sqlite with the highest N (state_10 is newer than state_5)."""
    best, best_n = None, -1
    for db in home.glob("state_*.sqlite"):
        m = _STATE_DB.match(db.name)
        if m and int(m.group(1)) > best_n:
            best, best_n = db, int(m.group(1))
    return best


def _db_titles(db: Path) -> dict:
    # immutable=1: a WAL-mode database without -wal/-shm files cannot be opened read-only the normal way.
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
        if not {"id", "title"} <= columns:
            return {}
        select = "id, title" + (", name" if "name" in columns else "")
        titles = {}
        for tid, *candidates in con.execute(f"SELECT {select} FROM threads"):
            for value in candidates:
                title = first_line(value, 120) if isinstance(value, str) else ""
                if isinstance(tid, str) and title:
                    titles[tid] = title
                    break
        return titles
    finally:
        con.close()


def load_titles(codex_home) -> dict:
    """Thread id -> title. session_index.jsonl (last line wins) beats state_*.sqlite threads.title."""
    home = Path(codex_home)
    titles = {}
    db = _state_db(home)
    if db is not None:
        try:
            titles.update(_db_titles(db))
        except (sqlite3.Error, OSError, ValueError):
            pass
    try:
        lines = (home / "session_index.jsonl").read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        lines = []
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"]:
            name = o.get("thread_name")
            title = first_line(name, 120) if isinstance(name, str) else ""
            if title:
                titles[o["id"]] = title
    return titles


def _payload(rec: dict) -> dict:
    p = rec.get("payload")
    return p if isinstance(p, dict) else {}


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _load_records(paths, skipped: Counter) -> list:
    merged, unordered = {}, []
    for p in paths:
        recs = list(iter_records(p, skipped))
        if not recs:
            continue
        try:
            head = recs[0]
            hb = _payload(head).get("history_base") if head.get("type") == "session_meta" else None
            if isinstance(hb, dict) and hb.get("end_ordinal_exclusive") is not None:
                cut = int(hb["end_ordinal_exclusive"])
                merged = {k: v for k, v in merged.items() if k < cut}
        except Exception:                       # an unreadable cut: keep the earlier history whole
            skipped["<bad-record>"] += 1
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
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        tx = str(c.get("text") or "")
        if not tx.strip() or _INJECTED.match(tx):
            continue
        texts.append(tx.strip())
    return clean_user_text("\n\n".join(texts))


def _command_text(value) -> str:
    """A command is a string, or an argv list (legacy shell): join the list with spaces."""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _function_call(name: str, args: dict) -> ToolCall:
    if name == "exec_command":
        arg = first_line(_command_text(args.get("cmd")), 120)
    elif name == "shell":
        arg = first_line(_command_text(args.get("command")), 120)
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
        # apply_patch has no ---/+++ headers: every line that starts with + or - is a changed line.
        lines = text.splitlines()
        add = sum(1 for l in lines if l.startswith("+"))
        rem = sum(1 for l in lines if l.startswith("-"))
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
        return "\n".join(str(i.get("text") or "") for i in out if isinstance(i, dict))
    if isinstance(out, str):
        try:
            j = json.loads(out)
            if isinstance(j, dict) and isinstance(j.get("output"), str):
                return j["output"]
        except ValueError:
            pass
        return out
    return ""


def _exit_code(out, text: str):
    """The exit code of a tool run, or None. Metadata first, then the header lines before 'Output:'.

    The body is the program's own output: a log line there that says 'exited with code 1' is not the exit code.
    """
    if isinstance(out, str):
        try:
            j = json.loads(out)
        except ValueError:
            j = None
        meta = j.get("metadata") if isinstance(j, dict) else None
        raw = meta.get("exit_code") if isinstance(meta, dict) else None
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    mark = _OUTPUT_MARK.search(text)
    m = _EXIT.search(text[:mark.start()] if mark else text)
    return int(m.group(1)) if m else None


def _apply_output(tc, out) -> None:
    if tc is None:
        return
    text = _output_text(out)
    code = _exit_code(out, text)
    if code not in (None, 0):
        tc.status = "error"
        tc.error_head = head_lines(_EXEC_HEADER.sub("", text))


def parse_unit(unit: Unit, titles=None):
    """Return a Session, or None for guardian/internal review threads."""
    skipped: Counter = Counter()
    recs = _load_records(unit.paths, skipped)
    meta = next((_payload(r) for r in recs if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict)),
                {})
    source = meta.get("source")
    sub_src = source.get("subagent") if isinstance(source, dict) else None
    if isinstance(sub_src, dict) and "other" in sub_src:
        return None
    spawn = sub_src.get("thread_spawn") if isinstance(sub_src, dict) else None
    parent = _str(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""
    tid = _str(meta.get("id")) or unit.key.split(":", 1)[-1]
    s = Session(id=tid, agent="codex", source_paths=list(unit.paths), cwd=_str(meta.get("cwd")),
                parent=parent or _str(meta.get("parent_thread_id")))
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    s.branch = _str(git.get("branch"))
    me = {_str(meta.get("agent_path")), _str(meta.get("agent_nickname"))}
    if isinstance(spawn, dict):
        me |= {_str(spawn.get("agent_path")), _str(spawn.get("agent_nickname"))}
    me.discard("")
    if not s.parent:
        me.add("/root")
    stamps, calls, files = [], {}, []
    current = None
    for r in recs:
        try:
            ts = iso_utc(r.get("timestamp"))
            if ts:
                stamps.append(ts)
            kind, p = r.get("type"), _payload(r)
            if kind == "session_meta":
                continue
            if kind == "turn_context":
                s.model = s.model or _str(p.get("model"))
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
                    content = p.get("content")
                    text = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict)).strip() \
                        if isinstance(content, list) else ""
                    if text:
                        current = current or s.add_turn("assistant", ts)
                        current.items.append(text)
            elif pt == "function_call":
                current = current or s.add_turn("assistant", ts)
                tc = _function_call(str(p.get("name") or ""), _args(p.get("arguments")))
                current.items.append(tc)
                _register(calls, p, tc)
            elif pt == "custom_tool_call":
                current = current or s.add_turn("assistant", ts)
                tc = _custom_call(str(p.get("name") or ""), p.get("input"), s.cwd, files)
                current.items.append(tc)
                _register(calls, p, tc)
            elif pt in ("function_call_output", "custom_tool_call_output"):
                call_id = p.get("call_id")
                _apply_output(calls.get(call_id) if isinstance(call_id, str) else None, p.get("output"))
            elif pt == "web_search_call":
                current = current or s.add_turn("assistant", ts)
                action = p.get("action")
                query = action.get("query") if isinstance(action, dict) else ""
                current.items.append(ToolCall(name="web_search", arg=first_line(str(query or ""), 100)))
            elif pt == "agent_message":
                # Messages between agents. Incoming ones (a task from the parent, a report from a child) are
                # turns; outgoing ones are already visible as the send/spawn tool call.
                if _str(p.get("author")) in me:
                    continue
                content = p.get("content")
                text = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict)).strip() \
                    if isinstance(content, list) else _str(content).strip()
                if text:
                    s.add_turn("user", ts, [clean_user_text(f"[from {_str(p.get('author')) or 'agent'}] {text}")])
                    current = None
            elif pt in ("reasoning", "compaction"):
                continue
            else:
                skipped[f"response_item:{pt}"] += 1
        except Exception:                       # one bad record must not lose the whole session
            skipped["<bad-record>"] += 1
    s.started = min(stamps) if stamps else ""
    s.ended = max(stamps) if stamps else ""
    s.project = project_from_git_url(_str(git.get("repository_url"))) or project_from_cwd(s.cwd)
    s.files = files
    s.title = (titles or {}).get(tid) or _FROM_PREFIX.sub("", s.first_prompt()) or "(untitled)"
    s.skipped = dict(skipped)
    return s


def _register(calls: dict, payload: dict, tc: ToolCall) -> None:
    """Remember a call by its call_id so its output can find it. A call without an id cannot be matched."""
    call_id = payload.get("call_id")
    if isinstance(call_id, str) and call_id:
        calls[call_id] = tc
