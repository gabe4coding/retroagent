"""Claude Code adapter: ~/.claude/projects/<encoded-cwd>/<session>.jsonl (+ <session>/subagents/**/agent-*.jsonl)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from kb.adapters.common import iter_records
from kb.model import Session, ToolCall, Unit
from kb.util import (clean_user_text, first_line, head_lines, is_scratch_path, iso_utc, project_from_cwd,
                     rel_path)

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SUBAGENT_TOOLS = ("Agent", "Task")
INTERRUPT_MARK = "[Request interrupted by user"


def discover(claude_dir) -> list:
    root = Path(claude_dir)
    units = []
    if not root.is_dir():
        return units
    for proj in sorted(p for p in root.iterdir() if p.is_dir()):
        for main in sorted(proj.glob("*.jsonl")):
            sub_dir = proj / main.stem / "subagents"
            subs = sorted(sub_dir.rglob("agent-*.jsonl")) if sub_dir.is_dir() else []
            units.append(Unit(key=str(main), agent="claude", paths=[str(main)] + [str(s) for s in subs],
                              main=str(main)))
    _mark_shared(units)
    return units


def agent_id(path) -> str:
    stem = Path(path).stem
    return stem[len("agent-"):] if stem.startswith("agent-") else stem


def _mark_shared(units) -> None:
    """Mark the subagents that several sessions hold a copy of.

    A resumed or forked session copies its parent's subagents. So one agent-<id>.jsonl can sit under several
    sessions. For each such subagent, every holder gets all the copies in unit.shared. Every holder also gets the
    other holders' main transcripts and copies in unit.related, so a change to any of them changes its fingerprint.
    """
    holders = defaultdict(dict)                     # agent id -> {main transcript: copy}
    for u in units:
        for p in u.paths[1:]:
            holders[agent_id(p)].setdefault(u.main, p)
    for u in units:
        for aid in dict.fromkeys(agent_id(p) for p in u.paths[1:]):
            copies = holders[aid]
            if len(copies) > 1:
                u.shared[aid] = dict(sorted(copies.items()))
                for main, copy in sorted(copies.items()):
                    if main != u.main:
                        u.related += [main, copy]
        u.related = list(dict.fromkeys(u.related))


def owner_order(copies: dict) -> list:
    """The sessions that hold a copy of one subagent ({main transcript: copy}), best owner first.

    A subagent's records carry the sessionId of the session it ran under. The session named by the earliest record,
    in any copy, is the one that started it; a resumed or forked session is named later or never. Sessions that no
    record names come last. Equal times go by session id. Every holder reads the same files, so all agree.
    """
    main_by_session_id = {Path(main).stem: main for main in copies}
    first_seen = {}                                 # session id -> earliest timestamp of a record that names it
    for copy in copies.values():
        try:
            for rec in iter_records(copy, Counter()):
                sid, ts = rec.get("sessionId"), rec.get("timestamp")
                # "~" sorts after every ISO timestamp, so the first timestamp seen for a session always wins.
                if (isinstance(sid, str) and sid in main_by_session_id and isinstance(ts, str) and ts
                        and ts < first_seen.get(sid, "~")):
                    first_seen[sid] = ts
        except OSError:                             # gone since discover: it names nobody
            continue
    # Named sessions first (False sorts before True), earliest first; then by session id.
    order = sorted(main_by_session_id, key=lambda k: (k not in first_seen, first_seen.get(k, ""), k))
    return [main_by_session_id[k] for k in order]


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def tool_name(name) -> str:
    name = str(name or "")
    return "mcp:" + name.split("__")[-1] if name.startswith("mcp__") else name


def short_arg(name: str, inp, cwd: str) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return first_line(str(inp.get("command", "")), 120)
    for key in ("file_path", "notebook_path"):
        if inp.get(key):
            return rel_path(str(inp[key]), cwd)
    if name in ("Grep", "Glob"):
        return first_line(str(inp.get("pattern", "")), 100)
    if name in ("Agent", "Task"):
        return first_line(str(inp.get("description", "")), 100)
    for key in ("url", "query", "skill", "description", "prompt", "command"):
        if isinstance(inp.get(key), str) and inp[key]:
            return first_line(inp[key], 100)
    for value in inp.values():
        if isinstance(value, str) and value:
            return first_line(value, 80)
    return ""


def diff_stat(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return ""
    lines = lambda v: len(str(v or "").splitlines())
    if name == "Edit":
        return f"+{lines(inp.get('new_string'))} −{lines(inp.get('old_string'))}"
    if name == "Write":
        return f"+{lines(inp.get('content'))}"
    if name == "MultiEdit":
        edits = [e for e in inp.get("edits") or [] if isinstance(e, dict)]
        return f"+{sum(lines(e.get('new_string')) for e in edits)} −{sum(lines(e.get('old_string')) for e in edits)}"
    return ""


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _is_external_event(rec: dict) -> bool:
    """A task notification or a message from a peer session: the harness wrote it, the user did not."""
    if rec.get("turnOrigin") in ("task_notification", "peer"):
        return True
    origin = rec.get("origin")
    return isinstance(origin, dict) and origin.get("kind") in ("task-notification", "peer")


def _is_human_prompt(rec: dict) -> bool:
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
        return False
    return rec.get("promptSource") != "system" and not _is_external_event(rec)


def _has_image(content) -> bool:
    return isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image" for p in content)


def _apply_result(tc, part: dict, tool_use_result, single: bool) -> None:
    if tc is None:
        return
    text = text_of(part.get("content"))
    if part.get("is_error"):
        tc.status = "error"
        tc.error_head = head_lines(text)
    # toolUseResult describes the whole record: it says nothing about one call when the record holds several results.
    if single and tc.name in SUBAGENT_TOOLS and isinstance(tool_use_result, dict) and tool_use_result.get("agentId"):
        tc.subagent_id = str(tool_use_result["agentId"])
        tc.subagent_note = first_line(text)


def _read_user_record(rec: dict, s: Session, ts: str, current, calls: dict):
    """Add one user record: tool results go to their calls, a human prompt becomes a user turn.

    Returns the open assistant turn, or None when the next assistant text starts a new turn.
    """
    content = (rec.get("message") or {}).get("content")
    results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] \
        if isinstance(content, list) else []
    if results:
        for part in results:
            tid = part.get("tool_use_id")
            _apply_result(calls.get(tid) if isinstance(tid, str) else None, part,
                          rec.get("toolUseResult"), len(results) == 1)
        return current
    if not _is_human_prompt(rec):
        if _is_external_event(rec) and not rec.get("isMeta"):
            return None                 # what the assistant says next answers the event, not the last prompt
        return current
    raw = text_of(content)
    if raw.lstrip().startswith(INTERRUPT_MARK):
        return current
    text = clean_user_text(raw)
    if not text and _has_image(content):
        text = "[image]"
    if text:
        s.add_turn("user", ts, [text])
        return None
    return current


def _read_assistant_record(rec: dict, s: Session, ts: str, current, calls: dict, edits: list, skipped: Counter):
    """Add the text and tool calls of one assistant record to the open assistant turn. Returns that turn.

    An error stops the record, as in _parse, but the turn it opened stays open: the next record adds to it.
    """
    msg = rec.get("message") or {}
    model = _str(msg.get("model"))
    if model == "<synthetic>":
        return current
    if model and not s.model:
        s.model = model
    try:
        for part in msg.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    current = current or s.add_turn("assistant", ts)
                    current.items.append(text)
            elif part.get("type") == "tool_use":
                current = current or s.add_turn("assistant", ts)
                name, inp = str(part.get("name") or ""), part.get("input") or {}
                tc = ToolCall(name=tool_name(name), arg=short_arg(name, inp, s.cwd), diff=diff_stat(name, inp))
                current.items.append(tc)
                tid = part.get("id")
                if isinstance(tid, str) and tid:
                    calls[tid] = tc
                path_in = inp.get("file_path") or inp.get("notebook_path") if isinstance(inp, dict) else None
                if name in EDIT_TOOLS and path_in:
                    edits.append((rel_path(str(path_in), s.cwd), tc))
    except Exception as e:                      # the same count as the except in _parse
        skipped[f"<error:{type(e).__name__}>"] += 1
    return current


def _read_title_record(rec: dict, kind: str, s: Session, titles: dict) -> bool:
    """Read a title or PR link record. Returns False for a record kind it does not know."""
    if kind == "custom-title":
        titles["custom"] = _str(rec.get("customTitle"))
    elif kind == "ai-title":
        titles["ai"] = _str(rec.get("aiTitle"))
    elif kind == "agent-name":
        titles["agent"] = _str(rec.get("agentName"))
    elif kind == "pr-link":
        url = rec.get("prUrl")
        if isinstance(url, str) and url and url not in s.prs:
            s.prs.append(url)
    else:
        return False
    return True


def _finish(s: Session, relocated: str, titles: dict, stamps: list, edits: list, skipped: Counter) -> None:
    """Set the fields that need the whole transcript: cwd, times, project, edited files, title, skipped counts."""
    if relocated:
        s.cwd = relocated
    s.started = min(stamps) if stamps else ""
    s.ended = max(stamps) if stamps else ""
    s.project = project_from_cwd(s.cwd)
    # A failed edit changed nothing; a call that never got a result still counts.
    s.files = list(dict.fromkeys(rel for rel, tc in edits if tc.status != "error" and not is_scratch_path(rel)))
    s.title = titles.get("custom") or titles.get("ai") or titles.get("agent") or s.first_prompt() or "(untitled)"
    s.skipped = dict(skipped)


def _parse(path, session_id: str, parent: str = ""):
    """Parse one transcript. Returns (Session, {tool_use_id: ToolCall})."""
    skipped: Counter = Counter()
    s = Session(id=session_id, agent="claude", source_paths=[str(path)], parent=parent)
    titles, stamps, calls, edits = {}, [], {}, []
    relocated = ""
    current = None                              # the open assistant turn: new assistant items go there
    for rec in iter_records(path, skipped):
        try:
            if rec.get("entrypoint") == "sdk-cli":      # what `claude -p` and the Agent SDK write
                s.headless = True
            kind = rec.get("type")
            ts = iso_utc(rec.get("timestamp"))
            if ts:
                stamps.append(ts)
            if _str(rec.get("cwd")) and not s.cwd:
                s.cwd = rec["cwd"]
            if _str(rec.get("gitBranch")) and rec["gitBranch"] != "HEAD":
                s.branch = rec["gitBranch"]
            if kind == "user":
                current = _read_user_record(rec, s, ts, current, calls)
            elif kind == "assistant":
                current = _read_assistant_record(rec, s, ts, current, calls, edits, skipped)
            elif kind == "relocated":
                relocated = _str(rec.get("relocatedCwd")) or relocated
            elif not _read_title_record(rec, kind, s, titles):
                skipped[str(kind)] += 1
        except Exception as e:                  # one odd record must not lose the whole session
            skipped[f"<error:{type(e).__name__}>"] += 1
    _finish(s, relocated, titles, stamps, edits, skipped)
    return s, calls


def parse_file(path, session_id: str, parent: str = "") -> Session:
    return _parse(path, session_id, parent)[0]


def _read_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_unit(unit: Unit) -> Session:
    main = Path(unit.main)
    s, calls = _parse(main, session_id=main.stem)
    by_id = {}
    for p in unit.paths[1:]:
        p, aid = Path(p), agent_id(p)
        sub, _ = _parse(p, session_id=aid, parent=s.id)
        meta = _read_meta(p.with_name(p.stem + ".meta.json"))
        sub.title = _str(meta.get("description")) or sub.title
        sub.cwd = sub.cwd or s.cwd
        sub.project = s.project if s.cwd else project_from_cwd(sub.cwd)
        sub.branch = sub.branch or s.branch
        s.subagents.append(sub)
        by_id[aid] = sub
        # A subagent the parent's tool result did not name (a workflow's) is found through its meta file.
        tool_use_id = meta.get("toolUseId")
        call = calls.get(tool_use_id) if isinstance(tool_use_id, str) else None
        if call is not None and not call.subagent_id:
            call.subagent_id = aid
    for turn in s.turns:
        for item in turn.items:
            if isinstance(item, ToolCall) and item.subagent_id in by_id:
                answers = [t for t in by_id[item.subagent_id].turns if t.role == "assistant" and t.text]
                if answers:
                    # The final answer is the last text item, not the first (earlier items are progress notes).
                    last_text = [i for i in answers[-1].items if isinstance(i, str)][-1]
                    item.subagent_note = first_line(last_text)
    return s
