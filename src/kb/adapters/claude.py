"""Claude Code adapter: ~/.claude/projects/<encoded-cwd>/<session>.jsonl (+ <session>/subagents/**/agent-*.jsonl)."""
from __future__ import annotations

import json
from collections import Counter
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
    return units


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


def _parse(path, session_id: str, parent: str = ""):
    """Parse one transcript. Returns (Session, {tool_use_id: ToolCall})."""
    skipped: Counter = Counter()
    s = Session(id=session_id, agent="claude", source_paths=[str(path)], parent=parent)
    titles, stamps, calls, edits = {}, [], {}, []
    relocated = ""
    current = None
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
                content = (rec.get("message") or {}).get("content")
                results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] \
                    if isinstance(content, list) else []
                if results:
                    for part in results:
                        tid = part.get("tool_use_id")
                        _apply_result(calls.get(tid) if isinstance(tid, str) else None, part,
                                      rec.get("toolUseResult"), len(results) == 1)
                    continue
                if not _is_human_prompt(rec):
                    if _is_external_event(rec) and not rec.get("isMeta"):
                        current = None          # what the assistant says next answers the event, not the last prompt
                    continue
                raw = text_of(content)
                if raw.lstrip().startswith(INTERRUPT_MARK):
                    continue
                text = clean_user_text(raw)
                if not text and _has_image(content):
                    text = "[image]"
                if text:
                    s.add_turn("user", ts, [text])
                    current = None
            elif kind == "assistant":
                msg = rec.get("message") or {}
                model = _str(msg.get("model"))
                if model == "<synthetic>":
                    continue
                if model and not s.model:
                    s.model = model
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
            elif kind == "custom-title":
                titles["custom"] = _str(rec.get("customTitle"))
            elif kind == "ai-title":
                titles["ai"] = _str(rec.get("aiTitle"))
            elif kind == "agent-name":
                titles["agent"] = _str(rec.get("agentName"))
            elif kind == "pr-link":
                url = rec.get("prUrl")
                if isinstance(url, str) and url and url not in s.prs:
                    s.prs.append(url)
            elif kind == "relocated":
                relocated = _str(rec.get("relocatedCwd")) or relocated
            else:
                skipped[str(kind)] += 1
        except Exception as e:                  # one odd record must not lose the whole session
            skipped[f"<error:{type(e).__name__}>"] += 1
    if relocated:
        s.cwd = relocated
    s.started = min(stamps) if stamps else ""
    s.ended = max(stamps) if stamps else ""
    s.project = project_from_cwd(s.cwd)
    # A failed edit changed nothing; a call that never got a result still counts.
    s.files = list(dict.fromkeys(rel for rel, tc in edits if tc.status != "error" and not is_scratch_path(rel)))
    s.title = titles.get("custom") or titles.get("ai") or titles.get("agent") or s.first_prompt() or "(untitled)"
    s.skipped = dict(skipped)
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
        p = Path(p)
        agent_id = p.stem[len("agent-"):] if p.stem.startswith("agent-") else p.stem
        sub, _ = _parse(p, session_id=agent_id, parent=s.id)
        meta = _read_meta(p.with_name(p.stem + ".meta.json"))
        sub.title = _str(meta.get("description")) or sub.title
        sub.cwd = sub.cwd or s.cwd
        sub.project = s.project if s.cwd else project_from_cwd(sub.cwd)
        sub.branch = sub.branch or s.branch
        s.subagents.append(sub)
        by_id[agent_id] = sub
        # A subagent the parent's tool result did not name (a workflow's) is found through its meta file.
        tool_use_id = meta.get("toolUseId")
        call = calls.get(tool_use_id) if isinstance(tool_use_id, str) else None
        if call is not None and not call.subagent_id:
            call.subagent_id = agent_id
    for turn in s.turns:
        for item in turn.items:
            if isinstance(item, ToolCall) and item.subagent_id in by_id:
                answers = [t for t in by_id[item.subagent_id].turns if t.role == "assistant" and t.text]
                if answers:
                    # The final answer is the last text item, not the first (earlier items are progress notes).
                    last_text = [i for i in answers[-1].items if isinstance(i, str)][-1]
                    item.subagent_note = first_line(last_text)
    return s
