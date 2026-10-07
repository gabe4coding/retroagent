"""Small helpers shared by all modules (stdlib only, Python 3.9)."""
from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from itertools import islice
from pathlib import Path

from kb.redact import redact

_FRAC = re.compile(r"\.\d+")


def iso_utc(ts) -> str:
    """Normalize an ISO string or epoch (s or ms) to 'YYYY-MM-DDTHH:MM:SSZ'. '' if unparsable."""
    if ts is None or ts == "":
        return ""
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            if v > 1e12:
                v /= 1000.0
            d = _dt.datetime.fromtimestamp(v, tz=_dt.timezone.utc)
        else:
            s = _FRAC.sub("", str(ts).strip())
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            d = _dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return ""


def short_id(sid: str) -> str:
    """The 8 characters used to name and show a session: the last 8 of the id without dashes.

    The head of an id is no good for this: a Codex id is a UUIDv7 and starts with a timestamp, so sessions started
    within a minute share their first 8 characters. The tail is random for UUIDv4, UUIDv7 and Claude agent ids.
    """
    return (sid or "").replace("-", "")[-8:]


def hhmm(iso: str) -> str:
    return iso[11:16] if len(iso or "") >= 16 else "--:--"


def first_line(text: str, limit: int = 160) -> str:
    """First non-empty line, redacted, then cut. Redaction comes first: a secret cut in half no longer matches."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            line = redact(line)[0]
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


_HEAD_SCAN = 50          # lines redacted together, so a secret that spans lines (a private key) is seen whole


def head_lines(text: str, n: int = 3, limit: int = 300) -> str:
    """First n non-empty lines joined with ' / ', redacted, then cut."""
    stripped = (l.strip() for l in (text or "").splitlines())
    head = list(islice((l for l in stripped if l), max(n, _HEAD_SCAN)))
    lines = [l.strip() for l in redact("\n".join(head))[0].splitlines() if l.strip()][:n]
    out = " / ".join(lines)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-.")
    return (s or "unknown")[:limit].strip("-.") or "unknown"[:limit]


def expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def rel_path(path: str, cwd: str) -> str:
    base = (cwd or "").rstrip("/")
    if base and path.startswith(base + "/"):
        return path[len(base) + 1:]
    return path


_TEMP_ROOTS = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")


def is_scratch_path(path: str) -> bool:
    """True for an absolute path in a temp folder or a scratchpad folder: noise in a session's list of edited files.

    A relative path (rel_path made it relative to the session cwd) is inside the project, so it never is.
    """
    return path.startswith("/") and (path.startswith(_TEMP_ROOTS) or "/scratchpad/" in path)


_WORKTREE = re.compile(r"^(.*?)/\.(?:claude/)?worktrees/[^/]+")


def project_from_cwd(cwd: str) -> str:
    if not cwd:
        return "unknown"
    if "/scratch-workspaces/" in cwd:
        return "scratch"
    m = _WORKTREE.match(cwd)
    base = (m.group(1) if m else cwd).rstrip("/")
    return slug(base.rsplit("/", 1)[-1])


def project_from_git_url(url: str) -> str:
    m = re.search(r"([^/:]+?)(?:\.git)?/?$", (url or "").strip())
    return slug(m.group(1)) if m and m.group(1) else ""


_DROP_BLOCKS = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-caveat|bash-stdout|bash-stderr"
    r"|command-message|agent-message|ci-monitor-event)\b[^>]*>.*?</\1>",
    re.S,
)
_CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BASH_IN = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
USER_TEXT_LIMIT = 4000


def clean_user_text(text: str) -> str:
    """Remove harness wrappers from a user prompt, redact secrets, and cap its length."""
    text = text or ""
    name = _CMD_NAME.search(text)
    if name:
        cmd = name.group(1).strip()
        args = _CMD_ARGS.search(text)
        if args and args.group(1).strip():
            cmd += " " + args.group(1).strip()
        text = cmd + "\n" + _CMD_ARGS.sub("", _CMD_NAME.sub("", text))
    text = _BASH_IN.sub(lambda m: "! " + m.group(1).strip(), text)
    text = _DROP_BLOCKS.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = redact(text)[0]                      # before the cap: a secret cut in half survives
    if len(text) > USER_TEXT_LIMIT:
        text = text[:USER_TEXT_LIMIT] + f"\n[… {len(text) - USER_TEXT_LIMIT} chars cut]"
    return text


def atomic_write(path, data: bytes) -> bool:
    """Write bytes only if they differ from the current content. Returns True if the file changed.

    The data goes to a unique temp file next to the target, is fsynced, then renamed over it. A failed or
    interrupted write leaves the old file untouched and no temp file behind.
    """
    path = Path(path)
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
