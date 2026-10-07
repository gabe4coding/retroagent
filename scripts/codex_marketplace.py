#!/usr/bin/env python3
"""Add or update the retroagent entry in the Codex personal marketplace (~/.agents/plugins/marketplace.json).

Usage: scripts/codex_marketplace.py <absolute path to the plugin folder>
The path must be under $HOME (Codex resolves personal-marketplace paths from $HOME).
The marketplace keeps the name it already has ("personal" only for a new file). The last line printed is
`marketplace: <name>`, so a caller can run `codex plugin add retroagent@<name>`.
A marketplace file that is not valid JSON, or not the expected shape, is never changed: one line, exit 1.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

DEFAULT_NAME = "personal"


def load(mp: Path):
    """The marketplace object, or (None, one-line problem)."""
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, f"codex: {mp} is not valid JSON ({' '.join(str(e).split())}); fix it, then run this again"
    if not isinstance(data, dict) or not isinstance(data.get("plugins", []), list):
        return None, f"codex: {mp} is not a marketplace file (an object with a plugins list); fix it, then run this again"
    return data, ""


def main(plugin_dir: str) -> int:
    home = Path.home().resolve()
    plugin = Path(plugin_dir).resolve()
    try:
        rel = "./" + plugin.relative_to(home).as_posix()
    except ValueError:
        print(f"codex: plugin folder {plugin} is not under {home}; the personal marketplace needs a path under HOME")
        return 1
    mp = home / ".agents" / "plugins" / "marketplace.json"
    data = {"name": DEFAULT_NAME, "interface": {"displayName": "Personal"}, "plugins": []}
    if mp.exists():
        data, problem = load(mp)
        if data is None:
            print(problem)
            return 1
        shutil.copy2(mp, mp.with_name("marketplace.json.bak-retroagent"))
    name = data.get("name")
    data["name"] = name = name if isinstance(name, str) and name.strip() else DEFAULT_NAME
    entry = {"name": "retroagent", "source": {"source": "local", "path": rel},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}
    data["plugins"] = [p for p in data.get("plugins", []) if not (isinstance(p, dict) and p.get("name") == "retroagent")] + [entry]
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"codex: marketplace entry retroagent -> {rel}")
    print(f"marketplace: {name}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: codex_marketplace.py <absolute path to the plugin folder>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
