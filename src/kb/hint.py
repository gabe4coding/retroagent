"""`kb hint --event error`: when a tool call fails, one short hint from the past, or nothing.

The hint is a bullet of the "Errors seen → fixes" section of the current project's page (pages/projects/<project>.md)
with its source session. Precision comes before recall, so most failures get no hint:
  1. gate: the start of the error must look like an error. A grep that found nothing ("Exit code 1", no message) or
     plain command output with exit code 1 never gets a hint.
  2. deny-list: never a hint for a permission, classifier or safety denial, and never a bullet about one (the fix
     such a bullet gives is a way around the denial).
  3. semantic: when the embedding server already runs (it is never started here), the bullet closest to the error by
     cosine, at least `hint_semantic_min`. Bullet vectors are kept in <root>/.kb/hints/, so a hint embeds only the
     error once the page is known.
  4. else keyword: the bullet whose problem side (the text before "→") shares the most words with the error, at least
     `hint_keyword_min`. Words are normalized like `kb stats errors` (kb.stats.error_words).
A session gets each bullet once; when its best bullet was shown already, nothing (the second best is noise). Every
hint shown is logged to <root>/.kb/hints/log.jsonl (session, bullet, score, method), so a later report can check
whether hints helped. Nothing here writes to the tracked files of the data clone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from kb.pages import parse_page, section
from kb.stats import error_words
from kb.util import atomic_write, head_lines, main_checkout, project_from_cwd, project_from_git_url

SECTION = "Errors seen"                 # the page section the hints come from ("## Errors seen → fixes")
DOC_TITLE = "Errors seen → fixes"       # the title the bullets are embedded with
PREFIX = "retroagent: seen before → "
MAX_CHARS = 299                         # the whole hint line
GATE_CHARS = 200
GATE = re.compile(r"error|fail|denied|blocked|not found|no such|cannot|can't|unable|traceback|exception|refus|invalid"
                  r"|not allowed|not permitted|does not", re.I)
# Denials by the agent's permission system, its auto mode classifier, a sandbox or a safety check. A hint here could
# only suggest a way around the denial.
DENY = re.compile(r"classifier|auto[ -]?mode|permission to use|permission prompt|permission request|requires? approval"
                  r"|approval|user (?:denied|declined|rejected)|doesn't want to proceed|does not want to proceed"
                  r"|denied by (?:the )?(?:user|policy|sandbox|harness)|sandbox|safety|unsafe", re.I)
_SOURCE = re.compile(r"\s*\(([^()]*)\)\s*$")
_SHORT = re.compile(r"\b[0-9a-f]{8}\b")
_MEMORY = re.compile(r"^memory\s+(\S+)")
_EXIT = re.compile(r"^\s*Exit code:?\s*(-?\d+)")
EMBED_TIMEOUT = 1.5                     # per call, 2 calls at most: well under the hook timeout of 5 s
SEEN_DAYS = 7                           # per-session state older than this is removed


@dataclass
class Bullet:
    text: str          # the bullet without "- " and without its source
    full: str          # the bullet as on the page, without "- ": what is embedded
    problem: str       # the text before "→"
    source: str        # "kb summary 1a2b3c4d", "kb memory <ref>" or ""
    key: str           # sha1 of full: one hint per bullet per session, and the vector cache key


def bullets(page_text: str) -> list:
    """The bullets of a page's "Errors seen → fixes" section that give a fix ("problem → fix")."""
    try:
        _, body = parse_page(page_text)
    except ValueError:
        return []
    part = section(body, SECTION) or ""
    out = []
    for line in part.splitlines()[1:]:
        if not line.startswith("- "):
            continue
        full = " ".join(line[2:].split())
        if "→" not in full:
            continue
        text, source = full, ""
        m = _SOURCE.search(full)
        if m:
            ref = m.group(1).strip()
            mem, shorts = _MEMORY.match(ref), _SHORT.findall(ref)
            if mem:
                source = f"kb memory {mem.group(1)}"
            elif shorts:
                source = f"kb summary {shorts[0]}"
            if source:
                text = full[: m.start()].rstrip()
        problem = full.split("→", 1)[0].strip()
        out.append(Bullet(text, full, problem, source, hashlib.sha1(full.encode("utf-8")).hexdigest()))
    return out


def passes_gate(err: str) -> bool:
    return bool(GATE.search((err or "")[:GATE_CHARS]))


def denied(text: str) -> bool:
    return bool(DENY.search(text or ""))


def keyword_match(err: str, items: list, minimum: int):
    """(bullet, shared words) of the bullet whose problem shares the most words with err (at least minimum), or None.
    A tie goes to the bullet with the fewer words: more of it matched."""
    words = set(error_words(err))
    best = None
    for b in items:
        theirs = set(error_words(b.problem))
        shared = len(words & theirs)
        if shared >= max(minimum, 1) and (best is None or (shared, -len(theirs)) > best[2]):
            best = (b, shared, (shared, -len(theirs)))
    return (best[0], best[1]) if best else None


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))           # unit vectors (kb.embed.embed makes them)


def semantic_match(qvec, items: list, vectors: dict, minimum: float):
    """(bullet, cosine) of the closest bullet with a vector, at least minimum, or None."""
    best = None
    for b in items:
        v = vectors.get(b.key)
        if v is None:
            continue
        s = cosine(qvec, v)
        if s >= minimum and (best is None or s > best[1]):
            best = (b, s)
    return best


def format_hint(b: Bullet) -> str:
    tail = f" ({b.source})" if b.source else ""
    room = MAX_CHARS - len(PREFIX) - len(tail)
    text = b.text if len(b.text) <= room else b.text[: room - 1].rstrip() + "…"
    return PREFIX + text + tail


# ---- the hook input

def error_text(event: dict) -> str:
    """The error of a failed tool call, as the index keeps it (first 3 lines, redacted, 300 chars), or "" when the
    event is not a failure. Claude Code sends PostToolUseFailure with `error`. A PostToolUse is a failure only when its
    response says so: `is_error`/`isError` true, `success` false, a non-zero `exit_code`, or text that starts with
    "Exit code: N" (Codex's shell format). Plain output is never read as an error."""
    err = event.get("error")
    if isinstance(err, str) and err.strip():
        return head_lines(err)
    resp = event.get("tool_response")
    if isinstance(resp, str):
        m = _EXIT.match(resp)
        return head_lines(resp) if m and m.group(1) != "0" else ""
    if not isinstance(resp, dict):
        return ""
    code = resp.get("exit_code", resp.get("exitCode"))
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    if not (resp.get("is_error") is True or resp.get("isError") is True or resp.get("success") is False or code):
        return ""
    parts = [f"Exit code {code}" if code else ""]
    for k in ("error", "stderr", "output", "stdout", "content"):
        v = resp.get(k)
        if isinstance(v, list):                     # MCP content: [{"type": "text", "text": "..."}]
            v = "\n".join(i.get("text", "") for i in v if isinstance(i, dict) and isinstance(i.get("text"), str))
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return head_lines("\n".join(p for p in parts if p))


def projects(cwd: str) -> list:
    """The project names a cwd can have: its folder (as kb names sessions), then its git remote (as Codex does)."""
    out = [project_from_cwd(cwd)] if cwd else []
    try:
        cfg = Path(main_checkout(cwd)) / ".git" / "config"
        m = re.search(r'^\s*url\s*=\s*(\S+)', cfg.read_text(encoding="utf-8", errors="replace"), re.M)
        name = project_from_git_url(m.group(1)) if m else ""
        if name and name not in out:
            out.append(name)
    except (OSError, ValueError):
        pass
    return out


def page_bullets(root: Path, cwd: str):
    """(project, bullets) of the first project page for cwd, or ("", [])."""
    for project in projects(cwd):
        try:
            text = (root / "pages" / "projects" / f"{project}.md").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        return project, bullets(text)
    return "", []


# ---- local state under <root>/.kb/hints/

class State:
    def __init__(self, kb_dir: Path, clock=time.time):
        self.dir = kb_dir / "hints"
        self.clock = clock

    def _session_file(self, session: str) -> Path:
        return self.dir / "seen" / ((re.sub(r"[^A-Za-z0-9_.-]", "", session or "")[:100] or "unknown") + ".json")

    def seen(self, session: str) -> set:
        data = _read_json(self._session_file(session))
        return set(data) if isinstance(data, list) else set()

    def mark(self, session: str, key: str) -> None:
        p = self._session_file(session)
        _write_json(p, sorted(self.seen(session) | {key}))
        self._prune(p.parent)

    def _prune(self, folder: Path) -> None:
        cutoff = self.clock() - SEEN_DAYS * 86400
        try:
            for f in folder.iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink()
        except OSError:
            pass

    def vectors(self, project: str, model: str) -> dict:
        data = _read_json(self.dir / "vectors" / f"{project}.json")
        if not isinstance(data, dict) or data.get("model") != model or not isinstance(data.get("vectors"), dict):
            return {}
        return data["vectors"]

    def save_vectors(self, project: str, model: str, vectors: dict) -> None:
        _write_json(self.dir / "vectors" / f"{project}.json", {"model": model, "vectors": vectors})

    def log(self, row: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / "log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, data) -> None:
    try:
        atomic_write(p, json.dumps(data).encode("utf-8"))
    except OSError:
        pass


# ---- one hint

def endpoint(cfg):
    """The embedding endpoint when one already answers, else None. Never starts or installs anything."""
    from kb import embed_runtime
    if cfg.embed_url:
        return embed_runtime.Endpoint(cfg.embed_url, "", "url:" + cfg.embed_url)
    srv = embed_runtime.Server()
    return srv.endpoint() if srv.alive() else None


def bullet_vectors(items: list, project: str, ep, state) -> dict:
    """key -> vector of every bullet, from the cache; the missing ones are embedded and the cache rewritten with the
    page's current bullets only. Raises embed.EmbedError."""
    from kb import embed
    model = embed.model_key(ep.model)
    have = state.vectors(project, model) if state else {}
    missing = [b for b in items if b.key not in have]
    if missing:
        vecs = embed.embed([embed.doc_text(DOC_TITLE, b.full) for b in missing], ep.url, ep.key, timeout=EMBED_TIMEOUT)
        have.update({b.key: v for b, v in zip(missing, vecs)})
        if state:
            state.save_vectors(project, model, {b.key: have[b.key] for b in items})
    return have


def find(cfg, err: str, items: list, project: str = "", ep=None, state=None):
    """(bullet, score, method) for an error and the bullets of its project, or None. ep: an embedding endpoint (then
    the semantic rule), or None (the keyword rule)."""
    if not err or not passes_gate(err) or denied(err):
        return None
    items = [b for b in items if not denied(b.full)]
    if not items:
        return None
    if ep is not None:
        from kb import embed
        try:
            vecs = bullet_vectors(items, project, ep, state)
            q = embed.embed([embed.query_text(err)], ep.url, ep.key, timeout=EMBED_TIMEOUT)[0]
        except embed.EmbedError:
            q = None
        if q is not None:
            m = semantic_match(q, items, vecs, cfg.hint_semantic_min)
            return (m[0], round(m[1], 4), "semantic") if m else None
    m = keyword_match(err, items, cfg.hint_keyword_min)
    return (m[0], m[1], "keyword") if m else None


def run(cfg, event: dict) -> str:
    """The hint line for a hook event, or "". Records the hint so the session does not get the bullet again."""
    if not cfg.hints or not isinstance(event, dict):
        return ""
    err = error_text(event)
    if not err or not passes_gate(err) or denied(err):
        return ""
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    project, items = page_bullets(cfg.root, cwd)
    session = str(event.get("session_id") or "")
    if not items:
        return ""
    state = State(cfg.kb_dir)
    try:
        ep = endpoint(cfg)
    except Exception:  # noqa: BLE001 - no semantic match is no reason to give no hint
        ep = None
    hit = find(cfg, err, items, project, ep, state)
    if hit is None:
        return ""
    b, score, method = hit
    if b.key in state.seen(session):            # the same error again: the best bullet was shown, the next is noise
        return ""
    state.mark(session, b.key)
    state.log({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": session, "project": project,
               "tool": str(event.get("tool_name") or ""), "error": err, "bullet": b.full, "score": score,
               "method": method})
    return format_hint(b)
