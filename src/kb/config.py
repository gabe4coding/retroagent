"""Configuration: ~/.config/retroagent/config.json (every key optional). KB_CONFIG / KB_ROOT override.

An install from before the rename keeps working: when the new file does not exist, ~/.config/sessions-kb/config.json
is read (and changed by `kb enable` / `kb disable`) instead.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kb.util import atomic_write, expand, slug

CONFIG_PATH = "~/.config/retroagent/config.json"
LEGACY_CONFIG_PATH = "~/.config/sessions-kb/config.json"
DEFAULT_ROOT = "~/.retroagent-data"
CODE_ROOT = Path(__file__).resolve().parents[2]       # the retroagent checkout this code runs from
DEFAULT_EXCLUDES = ["/private/var/folders/*", "/var/folders/*", "/tmp/*", "/private/tmp/*"]


class ConfigError(ValueError):
    """The config file cannot be rewritten safely. One line, names the file."""


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
    auto_sync: bool = True                      # `kb sync --auto` (the hook) runs only when this is true
    skip_headless_single_prompt: bool = True    # skip `claude -p` / `codex exec` sessions with one prompt
    gitleaks_path: str = ""                     # optional: where gitleaks is, tried before PATH
    require_gitleaks: bool = False              # true: no scanner means no commit
    branch: str = "main"                        # the sync touches git only on this branch
    raw_settle_hours: int = 24                  # raw copy only once the session is idle this long (0: every sync)
    auto_update: bool = False                   # `kb sync --auto` pulls the code clone once a day (fast-forward only)
    embed: bool = False                         # semantic search: kb runs a local embedding model (`kb embed`)
    embed_url: str = ""                         # optional: an embedding server kb uses instead of its own
    embed_sync_seconds: int = 60                # longest a sync spends embedding new items
    cloud_import: bool = False                  # this machine imports the cloud sessions' inbox (`kb cloud import`)
    cloud_host: str = "cloud"                   # the host the imported cloud sessions are written under
    hints: bool = False                         # `kb hint`: a past fix when a tool call fails (kb.hint)
    hint_semantic_min: float = 0.78             # cosine a semantic hint needs (embeddinggemma scores are compressed)
    hint_keyword_min: int = 3                   # shared words a keyword hint needs
    brief: bool = False                         # SessionStart hook: `kb brief` (page pointer, accepted suggestions)

    @property
    def kb_dir(self) -> Path:
        return self.root / ".kb"


def _read(p: Path) -> dict:
    """The config object, or {} (unreadable). A file that is not JSON or not a JSON object warns once on stderr.

    Such a broken file also switches automatic syncs off: it is no approval to sync with default settings.
    """
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError as e:
        sys.stderr.write(f"retroagent: bad config {p}: {e}; using defaults\n")
        return {"auto_sync": False}
    if not isinstance(raw, dict):
        sys.stderr.write(f"retroagent: bad config {p}: not a JSON object; using defaults\n")
        return {"auto_sync": False}
    return raw


def _bool(raw: dict, key: str, default: bool, bad: bool) -> bool:
    """A bool from the config. Missing or null gives `default`; a value that is not a bool gives `bad` (the safe side)."""
    v = raw.get(key)
    if v is None:
        return default
    return v if isinstance(v, bool) else bad


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


def _float(raw: dict, key: str, default: float) -> float:
    """A number from 0 to 1 from the config; anything else gives the default."""
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v) if 0 <= v <= 1 else default


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


def config_path(path: str | None = None) -> Path:
    """The given path, else $KB_CONFIG, else the config file: the new one, or the old one when only that exists."""
    if path or os.environ.get("KB_CONFIG"):
        return expand(path or os.environ["KB_CONFIG"])
    new, old = expand(CONFIG_PATH), expand(LEGACY_CONFIG_PATH)
    return old if not new.exists() and old.exists() else new


def set_key(key: str, value, path: str | None = None) -> Path:
    """Set one key in the config file and keep every other key. A missing file is created.

    A file that is not a JSON object is never overwritten: ConfigError (one line) instead.
    """
    p = config_path(path)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ConfigError(f"cannot change {p}: not valid JSON ({' '.join(str(e).split())}); fix it first") from None
        if not isinstance(data, dict):
            raise ConfigError(f"cannot change {p}: it is not a JSON object; fix it first")
    data[key] = value
    atomic_write(p, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return p


def load(path: str | None = None) -> Config:
    p = config_path(path)
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
    cfg.auto_sync = _bool(raw, "auto_sync", True, bad=False)                       # absent: old configs keep syncing
    cfg.skip_headless_single_prompt = _bool(raw, "skip_headless_single_prompt", True, bad=True)
    cfg.require_gitleaks = _bool(raw, "require_gitleaks", False, bad=True)
    cfg.gitleaks_path = _text(raw, "gitleaks_path")
    cfg.branch = _text(raw, "branch") or cfg.branch
    cfg.raw_settle_hours = _int(raw, "raw_settle_hours", cfg.raw_settle_hours)
    cfg.auto_update = _bool(raw, "auto_update", False, bad=False)
    cfg.embed = _bool(raw, "embed", False, bad=False)
    cfg.embed_url = _text(raw, "embed_url").rstrip("/")
    cfg.embed_sync_seconds = _int(raw, "embed_sync_seconds", cfg.embed_sync_seconds)
    cfg.cloud_import = _bool(raw, "cloud_import", False, bad=False)
    cfg.cloud_host = slug(_text(raw, "cloud_host") or cfg.cloud_host)
    cfg.hints = _bool(raw, "hints", False, bad=False)
    cfg.hint_semantic_min = _float(raw, "hint_semantic_min", cfg.hint_semantic_min)
    cfg.hint_keyword_min = _int(raw, "hint_keyword_min", cfg.hint_keyword_min) or cfg.hint_keyword_min
    cfg.brief = _bool(raw, "brief", False, bad=False)
    return cfg
