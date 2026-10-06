#!/usr/bin/env python3
"""Add or update the sessions-kb entry in the Codex personal marketplace (~/.agents/plugins/marketplace.json).

Usage: scripts/codex_marketplace.py <absolute path to the plugin folder>
The path must be under $HOME (Codex resolves personal-marketplace paths from $HOME).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def main(plugin_dir: str) -> int:
    home = Path.home().resolve()
    plugin = Path(plugin_dir).resolve()
    try:
        rel = "./" + plugin.relative_to(home).as_posix()
    except ValueError:
        print(f"codex: plugin folder {plugin} is not under {home}; the personal marketplace needs a path under HOME")
        return 1
    mp = home / ".agents" / "plugins" / "marketplace.json"
    data = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    if mp.exists():
        shutil.copy2(mp, mp.with_name("marketplace.json.bak-sessions-kb"))
        data = json.loads(mp.read_text(encoding="utf-8"))
    entry = {"name": "sessions-kb", "source": {"source": "local", "path": rel},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}
    data["plugins"] = [p for p in data.get("plugins", []) if p.get("name") != "sessions-kb"] + [entry]
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"codex: marketplace entry sessions-kb -> {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
