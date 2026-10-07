"""Memories: the notes Claude Code and Codex keep between sessions, copied into memories/<host>/ by `kb sync`.

Sources (this machine only, like the sessions):
  Claude Code  <claude_dir>/<encoded cwd>/memory/**/*.md   → memories/<host>/claude/<encoded cwd>/<file>
  Codex        <codex_home>/memories/**/*.md               → memories/<host>/codex/<file>

A KB memory file is front matter (the same JSON-valued lines as a session file) and the memory's own text:
  kind: "memory"   agent, host, project, cwd   folder: the encoded cwd ("" for Codex)   file: the source file name
  name, description, type, origin_session, modified: from the memory's own front matter, when it has one
The source's YAML front matter is not copied: its fields are lifted into the JSON front matter.

A memory is a curated note, not a log: when it is deleted or renamed on this machine, the KB copy goes too. That holds
only while the source folder exists. A whole folder that is gone (a project moved or cleaned up) keeps its KB copies,
like a session whose transcript is gone. A file that cannot be read is never removed from the KB.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from kb.distill import dump_front_matter
from kb.redact import redact
from kb.util import atomic_write, main_checkout, project_from_cwd

MAX_BYTES = 256 * 1024          # a memory is a short note; a bigger file is reported and left out
CWD_SCAN_LINES = 200            # lines of a transcript read to learn the cwd of a project folder
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_YAML_KEY = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(?:\s+(.*?))?\s*$")
_LIFTED = {"name": "name", "description": "description", "type": "type", "originSessionId": "origin_session",
           "modified": "modified"}


@dataclass
class Source:
    agent: str
    path: Path          # the memory file on this machine
    folder: str         # Claude: the encoded cwd of its project folder; Codex: ""
    file: str           # path inside the memory folder, "/"-separated
    cwd: str
    project: str


def encode_cwd(cwd: str) -> str:
    """The name Claude Code gives the project folder of a cwd: every character that is not a letter or digit is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def kb_folder(host: str, agent: str, folder: str) -> str:
    base = f"memories/{host}/{agent}"
    return f"{base}/{_UNSAFE.sub('_', folder)}" if folder else base


def kb_rel(host: str, src: Source) -> str:
    parts = [_UNSAFE.sub("_", p) for p in src.file.split("/")]
    return f"{kb_folder(host, src.agent, src.folder)}/{'/'.join(parts)}"


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            out = json.loads(value)
            return out if isinstance(out, str) else value
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def split_yaml(text: str):
    """(fields, body) of a memory file. Its YAML front matter is read only as far as kb needs: `key: value` lines,
    nested ones (metadata:) flattened, a top-level key winning over a nested one. No front matter: ({}, text)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0 or text[end + 4: end + 5] not in ("", "\n"):
        return {}, text
    fields, top = {}, set()
    for line in text[4:end].split("\n"):
        m = _YAML_KEY.match(line)
        if not m or not m.group(3) or m.group(3)[0] in "|>":       # a section header or a block value: skipped
            continue
        key, nested = m.group(2), bool(m.group(1))
        if nested and key in top:
            continue
        fields[key] = _unquote(m.group(3))
        if not nested:
            top.add(key)
    body = text[end + 5:]
    return fields, body[1:] if body.startswith("\n") else body


def render(host: str, src: Source, text: str) -> str:
    fields, body = split_yaml(text)
    meta = {"kind": "memory", "agent": src.agent, "host": host, "project": src.project, "cwd": src.cwd,
            "folder": src.folder, "file": src.file}
    for key, out in _LIFTED.items():
        meta[out] = fields.get(key, "")
    meta["name"] = meta["name"] or src.file[:-3]
    return dump_front_matter(meta) + "\n" + body.strip("\n") + "\n"


def _owns(folder: str, cwd: str) -> str:
    """cwd, or the checkout of a worktree cwd, when Claude Code keeps its memory in this project folder; else ''."""
    for c in (cwd, main_checkout(cwd)):
        if c.startswith("/") and encode_cwd(c) == folder:
            return c
    return ""


def _cwd_in(proj: Path) -> str:
    """The cwd of a Claude project folder, read from its transcripts (newest first). '' when none names it.

    The first cwd of a transcript is not enough: a desktop session can start in a scratch folder and be moved to the
    project later (relocatedCwd). Only a cwd whose encoded name is the folder's own name counts."""
    try:
        mains = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    for main in mains:
        try:
            with open(main, encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(CWD_SCAN_LINES), fh):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    for key in ("cwd", "relocatedCwd") if isinstance(rec, dict) else ():
                        cwd = _owns(proj.name, rec.get(key) if isinstance(rec.get(key), str) else "")
                        if cwd:
                            return cwd
        except OSError:
            continue
    return ""


def known_cwds(idx, host: str) -> dict:
    """{encoded folder: cwd} from this host's Claude sessions in the index, worktrees mapped to their checkout too.
    It finds the cwd of a project folder whose transcripts Claude Code has already deleted."""
    out = {}
    if idx is None:
        return out
    for (cwd,) in idx.db.execute("SELECT DISTINCT cwd FROM sessions WHERE host=? AND agent='claude' AND cwd<>'' "
                                 "ORDER BY started", (host,)):
        for c in (cwd, main_checkout(cwd)):
            if c.startswith("/"):
                out[encode_cwd(c)] = c
    return out


def _files(folder: Path) -> list:
    """The .md files of a memory folder, as (path, "/"-separated name). Hidden files and symlinks are left out."""
    out = []
    for p in sorted(folder.rglob("*.md")):
        rel = p.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts) or p.is_symlink() or not p.is_file():
            continue
        out.append((p, rel.as_posix()))
    return out


def discover(cfg, idx=None) -> tuple:
    """(sources, folders): every memory file on this machine, and the KB folders whose source folder exists."""
    sources, folders = [], []
    cwds = None
    root = Path(cfg.claude_dir)
    for proj in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        mem = proj / "memory"
        if not mem.is_dir() or mem.is_symlink():
            continue
        folders.append(kb_folder(cfg.host, "claude", proj.name))
        files = _files(mem)
        if not files:
            continue
        cwd = _cwd_in(proj)
        if not cwd:
            cwds = known_cwds(idx, cfg.host) if cwds is None else cwds
            cwd = cwds.get(proj.name, "")
        project = project_from_cwd(cwd)
        sources += [Source("claude", p, proj.name, rel, cwd, project) for p, rel in files]
    mem = Path(cfg.codex_home) / "memories"
    if mem.is_dir() and not mem.is_symlink():
        folders.append(kb_folder(cfg.host, "codex", ""))
        sources += [Source("codex", p, "", rel, "", "") for p, rel in _files(mem)]      # global, no project
    return sources, folders


def sync_memories(cfg, idx, report, excluded, dry_run: bool = False) -> int:
    """Write this machine's memories under memories/<host>/ and remove the copies of deleted ones.

    excluded(cwd) -> True leaves a project folder's memories out (and removes their copies). Returns the number of
    KB files written or removed; errors go to report.errors, one per file, and never stop the run."""
    sources, folders = discover(cfg, idx)
    wanted, changed = set(), 0
    for src in sources:
        rel = kb_rel(cfg.host, src)
        if src.cwd and excluded(src.cwd):
            continue
        if rel in wanted:
            report.errors.append(f"memory {src.path}: another memory file has the same KB path {rel}; left out")
            continue
        wanted.add(rel)                                     # from here on a failure keeps the old copy
        try:
            data = src.path.read_bytes()
            if len(data) > MAX_BYTES:
                raise ValueError(f"{len(data)} bytes, more than {MAX_BYTES}")
            text, found = redact(render(cfg.host, src, data.decode("utf-8", errors="replace")))
        except (OSError, ValueError) as e:
            report.errors.append(f"memory {src.path}: {type(e).__name__}: {e}")
            continue
        report.redactions.update(found)
        target = cfg.root / rel
        if dry_run:
            changed += not target.exists() or target.read_bytes() != text.encode("utf-8", errors="replace")
        else:
            changed += atomic_write(target, text.encode("utf-8", errors="replace"))
    for folder in folders:
        base = cfg.root / folder
        for p in sorted(base.rglob("*.md")) if base.is_dir() else []:
            if p.relative_to(cfg.root).as_posix() in wanted:
                continue
            if not dry_run:
                p.unlink()
            changed += 1
    return changed
