"""Distilled markdown: JSON-valued front matter + '## [N] role · HH:MM' turns."""
from __future__ import annotations

import json
import re
from pathlib import Path

from kb.model import ToolCall
from kb.util import atomic_write, hhmm

FIELD_ORDER = ["id", "agent", "host", "project", "cwd", "branch", "started", "ended", "model", "turns",
               "user_turns", "title", "summary", "tags", "outcome", "decisions", "summary_turns", "files",
               "prs", "parent", "raw"]
SUMMARY_FIELDS = ("summary", "tags", "outcome", "decisions", "summary_turns")
HEADER_RE = re.compile(r"^## \[(\d+)\] (user|assistant) · (\S+)$", re.M)


def dump_front_matter(meta: dict) -> str:
    keys = [k for k in FIELD_ORDER if k in meta] + sorted(k for k in meta if k not in FIELD_ORDER)
    lines = ["---"] + [f"{k}: {json.dumps(meta[k], ensure_ascii=False)}" for k in keys] + ["---", ""]
    return "\n".join(lines)


def split_front_matter(text: str):
    """Return (meta, body). Body has the single blank separator line removed."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].split("\n"):
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        try:
            meta[key] = json.loads(value)
        except ValueError:
            meta[key] = value
    body = text[end + 5:]
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


def session_meta(s, host: str, keep: dict, raw: str) -> dict:
    meta = {"id": s.id, "agent": s.agent, "host": host, "project": s.project, "cwd": s.cwd,
            "branch": s.branch, "started": s.started, "ended": s.ended, "model": s.model,
            "turns": len(s.turns), "user_turns": s.user_turns, "title": s.title, "summary": "",
            "tags": [], "outcome": "", "decisions": [], "summary_turns": 0, "files": s.files[:50],
            "prs": s.prs, "parent": s.parent, "raw": raw}
    for k in SUMMARY_FIELDS:
        if k in keep:
            meta[k] = keep[k]
    return meta


def _tool_line(tc: ToolCall, sub_files: dict) -> str:
    arg = tc.arg.replace("`", "'")
    line = f"- {tc.name}" + (f" `{arg}`" if arg else "")
    if tc.diff:
        line += f" ({tc.diff})"
    if tc.status == "error":
        line += " → ERROR" + (f": {tc.error_head}" if tc.error_head else "")
    if tc.subagent_id:
        fname = sub_files.get(tc.subagent_id)
        link = f"[subagent]({fname})" if fname else f"subagent {tc.subagent_id[:8]}"
        line += f" → {link}" + (f": {tc.subagent_note}" if tc.subagent_note else "")
    return line


def _escape(text: str) -> str:
    return re.sub(r"^(## \[\d+\] (?:user|assistant) · )", r"\\\1", text, flags=re.M)


def render_body(s, sub_files: dict) -> str:
    out = []
    for t in s.turns:
        out += [f"## [{t.n}] {t.role} · {hhmm(t.ts)}", ""]
        tools = []
        for item in t.items:
            if isinstance(item, str):
                if tools:
                    out += tools + [""]
                    tools = []
                out += [_escape(item.strip()), ""]
            else:
                tools.append(_tool_line(item, sub_files))
        if tools:
            out += tools + [""]
    return "\n".join(out).rstrip() + "\n"


def render_markdown(s, host: str, keep=None, sub_files=None, raw: str = "") -> str:
    return dump_front_matter(session_meta(s, host, keep or {}, raw)) + "\n" + render_body(s, sub_files or {})


def parse_markdown(text: str):
    """Return (meta, turns) where turns = [{'n', 'role', 'time', 'text'}]."""
    meta, body = split_front_matter(text)
    turns = []
    matches = list(HEADER_RE.finditer(body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        turns.append({"n": int(m.group(1)), "role": m.group(2), "time": m.group(3),
                      "text": body[m.end():end].strip()})
    return meta, turns


def update_front_matter(path, fields: dict) -> None:
    path = Path(path)
    meta, body = split_front_matter(path.read_text(encoding="utf-8"))
    meta.update(fields)
    atomic_write(path, (dump_front_matter(meta) + "\n" + body).encode("utf-8"))
