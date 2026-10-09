"""Memory fixes: the pages routine proposes a change to one memory file, and the owner accepts or rejects it.

The routine reads every memory of every machine, so it sees what no single agent sees: two memories that disagree, a
summary line that the memory's own text contradicts, a memory kept in the folder of another project. It never edits
a memory (only the machine that owns a host writes memories/<host>). It writes a proposal instead, and
`kb pages finish` checks it (check()) and records it in pages/memory-edits.json:

  {"m-1a2b3c": {"ref": "demo/old-note", "path": "memories/<host>/claude/<folder>/old-note.md", "host": "<host>",
                "agent": "claude", "kind": "rewrite", "text": "...", "description": "...", "why": "...",
                "sources": ["a1b2c3d4", "memory demo/new-note"], "digest": "<sha256 of the KB copy, 16 hex>",
                "date": "YYYY-MM-DD"}}

Kinds:
- rewrite: the memory's text after its front matter becomes `text`; `description`, when given, replaces the summary
  line of its front matter. Refused for a KB copy that holds a redacted secret: the rewrite would lose it.
- delete: the memory file goes, and its line in the folder's MEMORY.md index.
- move: a Claude memory goes to the memory folder of another project (`folder`, an encoded cwd), with its index line.

The routine adds an entry under a key of its own (anything but an m- id); finish gives it its id and the fields that
it knows better (path, host, agent, digest, date). Entries are never changed or removed afterwards. A memory gets one
proposal per version (digest): when the owner rejects it, the routine does not ask again until the memory changes.

On the owner's machine `kb decide` lists the proposals of its host that have no answer yet and whose KB copy did not
change since (same digest). `kb decide accept` applies one to the local memory file (apply()), then records the
answer (kb.decide); the next sync copies the change to the data repo. A proposal whose memory changed is left out:
the routine proposes again from the new text if it still applies.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

from kb import freshness, ledger
from kb.distill import split_front_matter
from kb.memories import Source, _render_keeping_date, encode_cwd
from kb.redact import redact
from kb.util import atomic_write, main_checkout

REL = "pages/memory-edits.json"
KINDS = ("rewrite", "delete", "move")
MAX_NEW = 5                     # new proposals in one run
MAX_TEXT = 8000                 # characters of a rewritten memory
MAX_LINE = 300                  # characters of `why` and `description`
REDACTED = "[REDACTED"
_ID = re.compile(r"^m-[0-9a-f]{6}$")
_FOLDER = re.compile(r"^-[A-Za-z0-9-]+$")
_DESCRIPTION = re.compile(r"^description:.*$", re.M)


class ApplyError(ValueError):
    """One line: why the proposal was not applied (nothing was changed)."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def is_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def load(root) -> dict:
    """The recorded proposals {id: entry}; {} when there are none or the file is unreadable."""
    try:
        data = json.loads((Path(root) / REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if is_id(k) and isinstance(v, dict) and v.get("kind") in KINDS
            and isinstance(v.get("path"), str) and isinstance(v.get("host"), str)} if isinstance(data, dict) else {}


def save(root, data: dict) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write(Path(root) / REL, text.encode("utf-8"))


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _sources(v) -> list:
    items = v if isinstance(v, list) else [v] if isinstance(v, str) else []
    return [s.strip() for x in items if isinstance(x, str) for s in x.split(",") if s.strip()]


def _host_folders(idx, host: str) -> set:
    """The encoded project folders that host's Claude sessions and memories name: where a move may go."""
    out = set()
    for (cwd,) in idx.db.execute("SELECT DISTINCT cwd FROM sessions WHERE host=? AND agent='claude' AND cwd<>''",
                                 (host,)):
        out |= {encode_cwd(c) for c in (cwd, main_checkout(cwd)) if c.startswith("/")}
    for (path,) in idx.db.execute("SELECT path FROM memories WHERE host=? AND agent='claude'", (host,)):
        parts = path.split("/")
        if len(parts) > 4:
            out.add(parts[3])
    return out


def check(root, old, new, idx, today: str) -> tuple:
    """(problems, the proposals to write) when the routine changed pages/memory-edits.json from `old` (the committed
    JSON, None when there was none) to `new`."""
    from kb.index import AmbiguousId

    old = old if isinstance(old, dict) else {}
    if not isinstance(new, dict):
        return [f"{REL}: must be a JSON object"], old
    problems, out = [], {}
    lookup = freshness.index_lookup(idx)
    for key in sorted(old):
        if key not in new:
            problems.append(f"{REL}: {key}: removed; the routine never removes a proposal")
        elif new[key] != old[key]:
            problems.append(f"{REL}: {key}: changed; the routine never changes a proposal, it adds a new one")
        else:
            out[key] = old[key]
    added = [k for k in sorted(new) if k not in old]
    if len(added) > MAX_NEW:
        problems.append(f"{REL}: {len(added)} new proposals; at most {MAX_NEW} in one run")
    # one proposal per version of a memory: accepted, it changed the memory; rejected, the owner said no to it
    proposed = {(e.get("path"), e.get("digest")) for e in old.values() if isinstance(e, dict)}
    for key in added:
        where, p = f"{REL}: {key}", new[key]
        if is_id(key):
            problems.append(f"{where}: no such proposal; add a new one under a key of your own, like \"new-1\"")
            continue
        if not isinstance(p, dict):
            problems.append(f"{where}: must be an object with ref, kind, why and sources")
            continue
        kind, ref, why = p.get("kind"), _text(p.get("ref")), _text(p.get("why"))
        if kind not in KINDS:
            problems.append(f"{where}: kind must be one of {', '.join(KINDS)}")
            continue
        if not why or len(why) > MAX_LINE or "\n" in why:
            problems.append(f"{where}: give `why` in one line of at most {MAX_LINE} characters")
            continue
        try:
            row = idx.memory(ref) if ref else None
        except AmbiguousId as e:
            problems.append(f"{where}: {ref!r} names several memories ({e}); give the path as ref")
            continue
        if row is None or row["path"].endswith("/MEMORY.md"):
            problems.append(f"{where}: no memory {ref!r}; `kb memory` lists them")
            continue
        try:
            data = (Path(root) / row["path"]).read_bytes()
        except OSError:
            problems.append(f"{where}: {row['path']} is not in the data repo")
            continue
        sources = _sources(p.get("sources"))
        refs = [freshness.parse_tail(f"- x ({s})")[1] for s in sources]
        if not sources or any(r is None or not lookup(r) for r in refs):
            problems.append(f"{where}: give the sources that show it: session short ids or `memory <ref>` that are "
                            "in the index")
            continue
        entry = {"ref": row["ref"], "path": row["path"], "host": row["host"], "agent": row["agent"], "kind": kind,
                 "why": why, "sources": sources, "digest": digest(data), "date": today}
        if kind == "rewrite":
            text, description = _text(p.get("text")), _text(p.get("description"))
            body = split_front_matter(data.decode("utf-8", errors="replace"))[1]
            if REDACTED in body:
                problems.append(f"{where}: the memory holds a redacted secret; a rewrite would lose it")
                continue
            if not text or len(text) > MAX_TEXT or REDACTED in text:
                problems.append(f"{where}: give the new text of the memory (at most {MAX_TEXT} characters)")
                continue
            if len(description) > MAX_LINE or "\n" in description:
                problems.append(f"{where}: description must be one line of at most {MAX_LINE} characters")
                continue
            if redact(text + "\n" + description)[1]:
                problems.append(f"{where}: the new text looks like it holds a secret; describe it instead")
                continue
            entry["text"] = text
            if description:
                entry["description"] = description
        elif kind == "move":
            folder = _text(p.get("folder"))
            current = row["path"].split("/")[3] if row["agent"] == "claude" else ""
            if row["agent"] != "claude":
                problems.append(f"{where}: only a Claude Code memory has a project folder to move to")
                continue
            if not _FOLDER.match(folder) or folder == current or folder not in _host_folders(idx, row["host"]):
                problems.append(f"{where}: folder must be the encoded cwd of another project of host {row['host']} "
                                "(like -Users-me-src-demo)")
                continue
            entry["folder"] = folder
        if (entry["path"], entry["digest"]) in proposed:
            problems.append(f"{where}: {entry['path']} already has a proposal for this version; the owner answers it "
                            "or answered it")
            continue
        proposed.add((entry["path"], entry["digest"]))
        seed = json.dumps(entry, sort_keys=True)
        n = 0
        while True:
            eid = "m-" + hashlib.sha1(f"{seed}\n{n}".encode("utf-8")).hexdigest()[:6]
            if eid not in out and eid not in old:
                break
            n += 1
        out[eid] = entry
    return problems, out


def waiting(root, host: str) -> list:
    """(id, entry) of the proposals for `host` with no answer whose memory did not change since, oldest first."""
    answered, out = ledger.answers(root), []
    for eid, e in sorted(load(root).items(), key=lambda kv: (kv[1].get("date", ""), kv[0])):
        if e["host"] != host or eid in answered:
            continue
        try:
            if digest((Path(root) / e["path"]).read_bytes()) == e.get("digest"):
                out.append((eid, e))
        except OSError:
            continue
    return out


def title(e: dict) -> str:
    where = f" to {e['folder']}" if e["kind"] == "move" else ""
    return f"{e['kind']} memory {e['ref']}{where}: {e.get('why', '')}"


def detail(root, e: dict) -> str:
    """What the proposal changes, for a person: a diff for a rewrite, the text for a delete or a move."""
    try:
        meta, body = split_front_matter((Path(root) / e["path"]).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    if e["kind"] != "rewrite":
        return body.strip("\n")
    old = ([f"description: {meta.get('description', '')}"] if e.get("description") else []) + \
        body.strip("\n").splitlines()
    new = ([f"description: {e['description']}"] if e.get("description") else []) + e["text"].strip("\n").splitlines()
    return "\n".join(difflib.unified_diff(old, new, "now", "proposed", lineterm=""))


# ---------------------------------------------------------------- applying, on the owner's machine

def _memory_dir(cfg, meta: dict) -> Path:
    """The local memory folder of a KB copy: <claude_dir>/<folder>/memory, or <codex_home>/memories."""
    file = meta.get("file") or ""
    if not file or file.startswith("/") or ".." in file.split("/"):
        raise ApplyError("the KB copy names no safe file")
    if meta.get("agent") == "claude":
        folder = meta.get("folder") or ""
        if not _FOLDER.match(folder):
            raise ApplyError("the KB copy names no project folder")
        return Path(cfg.claude_dir) / folder / "memory"
    return Path(cfg.codex_home) / "memories"


def _rewrite(source: str, text: str, description: str) -> str:
    body = text.strip("\n") + "\n"
    if not source.startswith("---\n"):
        return body
    end = source.find("\n---", 3)
    if end < 0 or source[end + 4: end + 5] not in ("", "\n"):
        return body
    head = source[4:end]
    if description:
        line = "description: " + json.dumps(description, ensure_ascii=False)
        head = _DESCRIPTION.sub(lambda m: line, head, count=1) if _DESCRIPTION.search(head) else f"{head}\n{line}"
    return f"---\n{head}\n---\n\n{body}"


def _index_line(memory_dir: Path, file: str, remove: bool) -> str:
    """The line of memory_dir/MEMORY.md that links to file ("" when none); removed from the index when `remove`."""
    index = memory_dir / "MEMORY.md"
    try:
        lines = index.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return ""
    hit = [line for line in lines if f"]({file})" in line]
    if hit and remove:
        atomic_write(index, "".join(line for line in lines if line not in hit).encode("utf-8"))
    return hit[0].rstrip("\n") if hit else ""


def apply(cfg, eid: str, e: dict) -> str:
    """Make the change of proposal `eid` to this machine's memory file. Returns one line; ApplyError changes nothing."""
    if e["host"] != cfg.host:
        raise ApplyError(f"{eid} is for host {e['host']}; run it on that machine")
    try:
        kb_bytes = (Path(cfg.root) / e["path"]).read_bytes()
    except OSError:
        raise ApplyError(f"{e['path']} is gone; the memory was deleted") from None
    if digest(kb_bytes) != e.get("digest"):
        raise ApplyError("the memory changed since the proposal; the routine proposes again if it still applies")
    meta = split_front_matter(kb_bytes.decode("utf-8", errors="replace"))[0]
    memory_dir = _memory_dir(cfg, meta)
    file = meta["file"]
    path = memory_dir / file
    try:
        source = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        raise ApplyError(f"no local file {path}") from None
    src = Source(meta.get("agent", ""), path, meta.get("folder", "") or "", file, meta.get("cwd", "") or "",
                 meta.get("project", "") or "")
    if _render_keeping_date(cfg.host, src, source, kb_bytes)[0] != kb_bytes:
        raise ApplyError(f"{path} changed since the last sync; run `kb sync --now --no-summaries` first")
    if e["kind"] == "rewrite":
        atomic_write(path, _rewrite(source, e["text"], e.get("description", "")).encode("utf-8"))
        return f"rewrote {path}"
    if e["kind"] == "delete":
        path.unlink()
        _index_line(memory_dir, file, remove=True)
        return f"deleted {path}"
    project = Path(cfg.claude_dir) / e["folder"]
    if not project.is_dir():
        raise ApplyError(f"no project folder {project} on this machine")
    target = project / "memory" / file
    if target.exists():
        raise ApplyError(f"{target} exists already")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, path.read_bytes())
    path.unlink()
    line = _index_line(memory_dir, file, remove=True)
    if not line:
        name = meta.get("name") or Path(file).stem
        line = f"- [{name}]({file})" + (f" — {meta['description']}" if meta.get("description") else "")
    index = project / "memory" / "MEMORY.md"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        text = ""
    atomic_write(index, (text + ("" if not text or text.endswith("\n") else "\n") + line + "\n").encode("utf-8"))
    return f"moved {path} to {target}"
