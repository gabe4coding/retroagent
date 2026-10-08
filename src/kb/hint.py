"""`kb hint --event error`: when a tool call fails, show one short hint from the past, or nothing.

Terms:
- bullet: one line of the "Errors seen → fixes" section of the current project's page (pages/projects/<project>.md).
  It reads "problem → fix (source session)". The problem is the text before "→". A hint shows one bullet.
- gate: a quick check that the start of the error text holds an error word (GATE).

Precision comes before recall, so most failures get no hint. The steps:
  1. Gate. So a grep that found nothing ("Exit code 1", no message) or plain output with exit code 1 gets no hint.
  2. Deny-list: no hint for a permission, classifier or safety denial, and no bullet about one. The fix in such a
     bullet is a way around the denial.
  3. Age: a bullet that the page's age rule calls stale is never a hint. kb.freshness calls a bullet stale when its
     date is more than `stale_days` older than the page's newest bullet. `kb pages finish` moves stale bullets to
     History, so this step only catches older pages.
  4. Semantic rule: if the embedding server already runs, take the bullet closest to the error by cosine, at least
     `hint_semantic_min`. This module never starts the server.
  5. Else the keyword rule: take the bullet whose problem shares the most words with the error, at least
     `hint_keyword_min`. Words are normalized as in `kb stats errors` (kb.stats.error_words).
  6. Once per session: a session gets each bullet once. If its best bullet was shown already, it gets nothing,
     because the second best bullet is noise.
The hint shows the date when a session last confirmed the bullet.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from kb import freshness
from kb.pages import parse_page, section
from kb.stats import error_words
from kb.util import atomic_write, head_lines, main_checkout, project_from_cwd, project_from_git_url

SECTION = "Errors seen"                 # the page section the hints come from ("## Errors seen → fixes")
DOC_TITLE = "Errors seen → fixes"       # the title the bullets are embedded with
PREFIX = "retroagent: seen before → "
MAX_CHARS = 299                         # the longest hint line, prefix and source included: format_hint cuts the text
GATE_CHARS = 200                        # the gate reads only this many characters from the start of the error
GATE = re.compile(r"error|fail|denied|blocked|not found|no such|cannot|can't|unable|traceback|exception|refus|invalid"
                  r"|not allowed|not permitted|does not", re.I)
# Denials by the agent's permission system, its auto mode classifier, a sandbox or a safety check. A hint here could
# only suggest a way around the denial.
DENY = re.compile(r"classifier|auto[ -]?mode|permission to use|permission prompt|permission request|requires? approval"
                  r"|approval|user (?:denied|declined|rejected)|doesn't want to proceed|does not want to proceed"
                  r"|denied by (?:the )?(?:user|policy|sandbox|harness)|sandbox|safety|unsafe", re.I)
_SOURCE = re.compile(r"\s*\(([^()]*)\)\.?\s*$")
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
    date: str = ""     # when a session last confirmed it (kb.freshness), "" when the page gives none


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
        out.append(Bullet(text, full, problem, source, hashlib.sha1(full.encode("utf-8")).hexdigest(),
                          freshness.bullet_date(full)))
    return out


def fresh(items: list, newest: str, days: int) -> list:
    """The bullets the page's age rule does not call stale. An undated bullet stays."""
    return [bullet for bullet in items if not freshness.is_stale(bullet.date, newest, days)]


def passes_gate(err: str) -> bool:
    return bool(GATE.search((err or "")[:GATE_CHARS]))


def denied(text: str) -> bool:
    return bool(DENY.search(text or ""))


def keyword_match(err: str, items: list, minimum: int):
    """(bullet, shared words) of the bullet whose problem shares the most words with err (at least minimum), or None.
    A tie goes to the bullet with the fewer words: more of it matched."""
    words = set(error_words(err))
    best_bullet, best_shared, best_rank = None, 0, None
    for bullet in items:
        bullet_words = set(error_words(bullet.problem))
        shared = len(words & bullet_words)
        rank = (shared, -len(bullet_words))         # more shared words first, then fewer words
        if shared >= max(minimum, 1) and (best_bullet is None or rank > best_rank):
            best_bullet, best_shared, best_rank = bullet, shared, rank
    return (best_bullet, best_shared) if best_bullet is not None else None


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))           # unit vectors (kb.embed.embed makes them)


def semantic_match(qvec, items: list, vectors: dict, minimum: float):
    """(bullet, cosine) of the closest bullet with a vector, at least minimum, or None."""
    best = None
    for bullet in items:
        v = vectors.get(bullet.key)
        if v is None:
            continue
        s = cosine(qvec, v)
        if s >= minimum and (best is None or s > best[1]):
            best = (bullet, s)
    return best


def format_hint(b: Bullet) -> str:
    tail = f" ({b.source}{' · ' + b.date if b.date else ''})" if b.source else ""
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
    """(project, bullets, the page's newest bullet date) of the first project page for cwd, or ("", [], "")."""
    for project in projects(cwd):
        try:
            text = (root / "pages" / "projects" / f"{project}.md").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        try:
            newest_bullet_date = freshness.newest(parse_page(text)[1])
        except ValueError:
            newest_bullet_date = ""
        return project, bullets(text), newest_bullet_date
    return "", [], ""


# ---- local state under <root>/.kb/hints/

class State:
    """The local state of hints, under <root>/.kb/hints/. Nothing here writes to the tracked files of the data clone.

    - seen/<session>.json: the bullets a session was shown, so it gets each bullet once.
    - vectors/<project>.json: the bullet vectors of a page, so once the page is known a hint embeds only the error.
    - log.jsonl: every hint shown (session, bullet, its date, score, method). A later report can then check whether
      hints helped and how old they were.
    """

    def __init__(self, kb_dir: Path, clock=time.time):
        self.dir = kb_dir / "hints"
        self.clock = clock

    def _session_file(self, session: str) -> Path:
        # Only safe characters of the session id, at most 100 of them, make the file name.
        return self.dir / "seen" / ((re.sub(r"[^A-Za-z0-9_.-]", "", session or "")[:100] or "unknown") + ".json")

    def seen(self, session: str) -> set:
        data = _read_json(self._session_file(session))
        return set(data) if isinstance(data, list) else set()

    def mark(self, session: str, key: str) -> None:
        p = self._session_file(session)
        _write_json(p, sorted(self.seen(session) | {key}))
        self._prune(p.parent)

    def _prune(self, folder: Path) -> None:
        cutoff = self.clock() - SEEN_DAYS * 86400           # 86400 seconds in a day
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
    # run() checks the error too, before it reads the page. find() checks again so it is also right when called alone.
    if not err or not passes_gate(err) or denied(err):
        return None
    items = [bullet for bullet in items if not denied(bullet.full)]
    if not items:
        return None
    if ep is not None:
        from kb import embed
        try:
            vecs = bullet_vectors(items, project, ep, state)
            error_vector = embed.embed([embed.query_text(err)], ep.url, ep.key, timeout=EMBED_TIMEOUT)[0]
        except embed.EmbedError:
            error_vector = None
        if error_vector is not None:
            match = semantic_match(error_vector, items, vecs, cfg.hint_semantic_min)
            return (match[0], round(match[1], 4), "semantic") if match else None
    match = keyword_match(err, items, cfg.hint_keyword_min)
    return (match[0], match[1], "keyword") if match else None


def run(cfg, event: dict) -> str:
    """The hint line for a hook event, or "". Records the hint so the session does not get the bullet again."""
    if not cfg.hints or not isinstance(event, dict):
        return ""
    err = error_text(event)
    if not err or not passes_gate(err) or denied(err):
        return ""
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    project, items, newest_bullet_date = page_bullets(cfg.root, cwd)
    items = fresh(items, newest_bullet_date, freshness.stale_settings(cfg.root)[0])
    session = str(event.get("session_id") or "")
    if not items:
        return ""
    state = State(cfg.kb_dir)
    try:
        embed_ep = endpoint(cfg)
    except Exception:  # noqa: BLE001 - no semantic match is no reason to give no hint
        embed_ep = None
    hit = find(cfg, err, items, project, embed_ep, state)
    if hit is None:
        return ""
    bullet, score, method = hit
    if bullet.key in state.seen(session):       # the same error again: the best bullet was shown, the next is noise
        return ""
    state.mark(session, bullet.key)
    state.log({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": session, "project": project,
               "tool": str(event.get("tool_name") or ""), "error": err, "bullet": bullet.full, "seen": bullet.date,
               "score": score, "method": method})
    return format_hint(bullet)
