"""Configuration: ~/.config/sessions-kb/config.json (every key optional). KB_CONFIG / KB_ROOT override."""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from kb.util import expand

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


def load(path: str | None = None) -> Config:
    p = expand(path or os.environ.get("KB_CONFIG") or CONFIG_PATH)
    raw = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
    root = os.environ.get("KB_ROOT") or raw.get("root") or DEFAULT_ROOT
    cfg = Config(root=expand(root), host=str(raw.get("host") or default_host()))
    for key in ("quiet_minutes", "debounce_minutes", "summary_cap_per_run"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    if raw.get("summary_model"):
        cfg.summary_model = str(raw["summary_model"])
    if raw.get("claude_dir"):
        cfg.claude_dir = expand(raw["claude_dir"])
    if "codex_dirs" in raw:
        cfg.codex_dirs = [expand(d) for d in raw["codex_dirs"]]
    if raw.get("codex_home"):
        cfg.codex_home = expand(raw["codex_home"])
    if "exclude_cwd_globs" in raw:
        cfg.exclude_cwd_globs = list(raw["exclude_cwd_globs"])
    return cfg
