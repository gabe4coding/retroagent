#!/bin/sh
# install.sh — set up sessions-kb on this machine. Safe to run again.
# Usage: ./install.sh [--no-sync]     (run from anywhere; the repo is the folder this script is in)
# Does: config file, Claude plugin, Codex plugin (personal marketplace), ~/.local/bin/kb, index, first sync.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
NO_SYNC=0
[ "${1:-}" = "--no-sync" ] && NO_SYNC=1
CFG_DIR="$HOME/.config/sessions-kb"
CFG="$CFG_DIR/config.json"
say() { printf '%s\n' "$*"; }

# 1. Config
mkdir -p "$CFG_DIR"
if [ -f "$CFG" ]; then
  say "config: kept $CFG"
else
  HOST=$(hostname -s | tr '[:upper:]' '[:lower:]')
  printf '{\n  "root": "%s",\n  "host": "%s"\n}\n' "$ROOT" "$HOST" > "$CFG"
  say "config: wrote $CFG (host=$HOST)"
fi

# 2. Claude Code plugin
if command -v claude >/dev/null 2>&1; then
  if claude plugin marketplace add "$ROOT" >/dev/null 2>&1 || claude plugin marketplace update sessions-kb >/dev/null 2>&1; then
    say "claude: marketplace sessions-kb ready"
  else
    say "claude: WARNING could not add marketplace; run: claude plugin marketplace add $ROOT"
  fi
  if claude plugin install sessions-kb@sessions-kb >/dev/null 2>&1 || claude plugin update sessions-kb@sessions-kb >/dev/null 2>&1; then
    say "claude: plugin sessions-kb@sessions-kb installed"
  else
    say "claude: WARNING could not install; run: claude plugin install sessions-kb@sessions-kb"
  fi
else
  say "claude: not found, skipped"
fi

# 3. Codex plugin
if command -v codex >/dev/null 2>&1; then
  python3 "$ROOT/scripts/codex_marketplace.py" "$ROOT/plugin" || say "codex: WARNING marketplace entry not written"
  if codex plugin add sessions-kb@personal >/dev/null 2>&1; then
    say "codex: plugin sessions-kb@personal installed"
  else
    say "codex: WARNING run 'codex plugin add sessions-kb@personal' to see why it failed"
  fi
  say "codex: open Codex once and trust the sessions-kb hook with /hooks"
else
  say "codex: not found, skipped"
fi

# 4. kb on PATH for shells and Codex
mkdir -p "$HOME/.local/bin"
TARGET="$ROOT/plugin/bin/kb"
[ -e "$HOME/claude-tools/plugins/local-tools/bin/kb" ] && TARGET="$HOME/claude-tools/plugins/local-tools/bin/kb"
ln -sf "$TARGET" "$HOME/.local/bin/kb"
say "path: ~/.local/bin/kb -> $TARGET"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "path: add ~/.local/bin to your PATH" ;; esac

# 5. Index and first sync
mkdir -p "$ROOT/.kb"
"$ROOT/plugin/bin/kb" reindex
if [ "$NO_SYNC" = 1 ]; then
  say "sync: skipped (--no-sync)"
else
  KB_ROOT="$ROOT" nohup "$ROOT/plugin/bin/kb" sync </dev/null >>"$ROOT/.kb/sync.log" 2>&1 &
  say "sync: started in the background (log: $ROOT/.kb/sync.log)"
fi
