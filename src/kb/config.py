"""Configuration: ~/.config/sessions-kb/config.json (every key optional). KB_CONFIG / KB_ROOT override."""
from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kb.util import expand, slug

CONFIG_PATH = "~/.config/sessions-kb/config.json"
DEFAULT_ROOT = "~/Repositories/sessions-kb"
DEFAULT_EXCLUDES = ["/private/var/folders/*", "/var/folders/*", "/tmp/*", "/private/tmp/*"]


def default_host() -> str:
    return socket.gethostname().split(".")[0].lower() or "host"


@dataclass
class Config:
    root: Path
    host: str
    quiet_minutes: int = 15
    debounce_minutes: int = 10
    summary_model: str = "haiku"
    summary_cap_per_run: int = 30
    claude_dir: Path = field(default_factory=lambda: expand("~/.claude/projects"))
    codex_dirs: list = field(default_factory=lambda: [expand("~/.codex/sessions"), expand("~/.codex/archived_sessions")])
    codex_home: Path = field(default_factory=lambda: expand("~/.codex"))
    exclude_cwd_globs: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))

    @property
    def kb_dir(self) -> Path:
        return self.root / ".kb"


def _read(p: Path) -> dict:
    """The config object, or {} (unreadable, not JSON, not an object). A syntax error warns once on stderr."""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError as e:
        sys.stderr.write(f"sessions-kb: bad config {p}: {e}; using defaults\n")
        return {}
    return raw if isinstance(raw, dict) else {}


def _int(raw: dict, key: str, default: int) -> int:
    """A non-negative int from the config; anything else (missing, bool, text, null, negative) gives the default."""
    v = raw.get(key)
    if isinstance(v, bool) or v is None:
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n >= 0 else default


def _list(raw: dict, key: str, default: list) -> list:
    """A list of str from the config. A single str becomes a one-item list; a non-list gives the default."""
    v = raw.get(key)
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list):
        return list(default)
    return [str(i) for i in v]


def _text(raw: dict, key: str) -> str:
    v = raw.get(key)
    return v if isinstance(v, str) else ""


def _absolute(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(str(p)))


def load(path: str | None = None) -> Config:
    p = expand(path or os.environ.get("KB_CONFIG") or CONFIG_PATH)
    raw = _read(p)
    root = os.environ.get("KB_ROOT") or _text(raw, "root") or DEFAULT_ROOT
    host = slug(str(raw.get("host") or default_host()))
    cfg = Config(root=_absolute(expand(root)), host=host)
    cfg.quiet_minutes = _int(raw, "quiet_minutes", cfg.quiet_minutes)
    cfg.debounce_minutes = _int(raw, "debounce_minutes", cfg.debounce_minutes)
    cfg.summary_cap_per_run = _int(raw, "summary_cap_per_run", cfg.summary_cap_per_run)
    cfg.summary_model = _text(raw, "summary_model") or cfg.summary_model
    if _text(raw, "claude_dir"):
        cfg.claude_dir = expand(raw["claude_dir"])
    cfg.codex_dirs = [expand(d) for d in _list(raw, "codex_dirs", cfg.codex_dirs)]
    if _text(raw, "codex_home"):
        cfg.codex_home = expand(raw["codex_home"])
    cfg.exclude_cwd_globs = _list(raw, "exclude_cwd_globs", cfg.exclude_cwd_globs)
    return cfg
