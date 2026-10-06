#!/bin/sh
# install.sh — set up sessions-kb on this machine. Safe to run again.
# Usage: ./install.sh [--root DIR] [--host NAME] [--no-sync] [--force-host]
#   --root DIR     the data clone the sync works in (default: $HOME/.sessions-kb). It is cloned from this repo's
#                  origin when it does not exist. Never edit it by hand; develop in a separate checkout.
#   --host NAME    this machine's name in the KB (default: the config's host, else the short hostname).
#                  It must be unique per machine.
#   --no-sync      do not start a background sync (only matters when auto_sync is already on).
#   --force-host   skip the check that sessions/<host> belongs to another machine (use it on the machine that
#                  already wrote those sessions, once, to claim them).
# Does: clone, config file, Claude plugin, Codex plugin, ~/.local/bin/kb, index. Automatic syncs stay off
# (auto_sync=false) until you run `kb enable`.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }
usage() { say "Usage: install.sh [--root DIR] [--host NAME] [--no-sync] [--force-host]"; }

ROOT="$HOME/.sessions-kb"
HOST=""
NO_SYNC=0
FORCE_HOST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || { usage >&2; die "--root needs a folder"; }; ROOT=$2; shift 2 ;;
    --root=*) ROOT=${1#--root=}; shift ;;
    --host) [ $# -ge 2 ] || { usage >&2; die "--host needs a name"; }; HOST=$2; shift 2 ;;
    --host=*) HOST=${1#--host=}; shift ;;
    --no-sync) NO_SYNC=1; shift ;;
    --force-host) FORCE_HOST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done
case "$ROOT" in /*) ;; *) ROOT="$PWD/$ROOT" ;; esac
while [ "${#ROOT}" -gt 1 ] && [ "${ROOT%/}" != "$ROOT" ]; do ROOT=${ROOT%/}; done

CFG_DIR="$HOME/.config/sessions-kb"
CFG="$CFG_DIR/config.json"

# 1. Tools: python3 >= 3.9 with SQLite FTS5, and git
command -v python3 >/dev/null 2>&1 || die "python3 not found; install Python 3.9 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
  || die "python3 3.9 or newer is needed (this one is $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown))"
python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' >/dev/null 2>&1 \
  || die "python3 has no SQLite FTS5 support, which kb needs for search; use another python3 (for example from Homebrew)"
command -v git >/dev/null 2>&1 || die "git not found"

# 2. This machine's host name: --host, else the config's, else the short hostname (same slug rule as kb)
slug() {
  s=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9._-][^a-z0-9._-]*/-/g' -e 's/^[-.]*//' -e 's/[-.]*$//' | cut -c1-40)
  printf '%s' "${s:-unknown}"
}
if [ -z "$HOST" ] && [ -f "$CFG" ]; then
  HOST=$(python3 -c 'import json, sys
try:
    h = json.load(open(sys.argv[1])).get("host")
except Exception:
    h = None
print(h if isinstance(h, str) else "")' "$CFG")
fi
if [ -z "$HOST" ]; then
  HOST=$(hostname -s 2>/dev/null || uname -n)
  HOST=${HOST%%.*}
  [ -n "$HOST" ] || HOST=host
fi
HOST=$(slug "$HOST")

# 3. The data clone: cloned from this repo's origin (else from this folder); an existing one must be the same repo
SELF_URL=$(git -C "$HERE" remote get-url origin 2>/dev/null || true)
if [ -n "$SELF_URL" ]; then
  URL=$SELF_URL
else
  URL=$HERE
  say "root: WARNING this repo has no origin remote; cloning from its folder $HERE (the sync cannot push there)"
fi
first_root_commit() { git -C "$1" rev-list --max-parents=0 HEAD 2>/dev/null | sort | head -n 1; }
if [ -e "$ROOT" ]; then
  TOP=$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -z "$TOP" ] || [ "$(cd "$TOP" && pwd -P)" != "$(cd "$ROOT" && pwd -P)" ]; then
    die "$ROOT exists but is not a git clone of sessions-kb; move it away or choose another folder with --root"
  fi
  MINE=$(first_root_commit "$HERE")
  THEIRS=$(first_root_commit "$ROOT")
  if [ -n "$MINE" ] && [ "$MINE" = "$THEIRS" ]; then
    :
  elif [ -n "$SELF_URL" ] && [ "$SELF_URL" = "$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)" ]; then
    :
  else
    die "$ROOT is a clone of another repo, not of sessions-kb; choose another folder with --root"
  fi
  say "root: using the existing clone $ROOT"
else
  mkdir -p "$(dirname "$ROOT")"
  git clone --quiet "$URL" "$ROOT" || die "could not clone $URL into $ROOT"
  say "root: cloned $URL into $ROOT"
fi
for f in plugin/bin/kb src/kb/cli.py scripts/codex_marketplace.py; do
  [ -f "$ROOT/$f" ] || die "$ROOT has no $f; push the commits of $HERE to its origin first, then run install.sh again"
done

# 4. Host guard: two machines with one host name write the same folders and block each other
HOSTDIR="$ROOT/sessions/$HOST"
LOCAL_ID="$ROOT/.kb/machine-id"
MARKER="$HOSTDIR/.machine-id"
squash() { tr -d ' \n\r\t' < "$1"; }
if [ -e "$HOSTDIR" ] && [ "$FORCE_HOST" != 1 ]; then
  if [ ! -s "$LOCAL_ID" ]; then
    die "sessions/$HOST already exists in $ROOT and this machine has no id yet (.kb/machine-id), so another machine may use the host '$HOST'. Choose a unique name with --host NAME. If this is the machine that wrote those sessions, run again with --force-host."
  fi
  if [ -s "$MARKER" ] && [ "$(squash "$MARKER")" != "$(squash "$LOCAL_ID")" ]; then
    die "host '$HOST' belongs to another machine (sessions/$HOST/.machine-id is not this machine's id). Choose a unique name with --host NAME."
  fi
fi

# 5. gitleaks: look in PATH, then the usual folders
GITLEAKS=$(command -v gitleaks 2>/dev/null || true)
case "$GITLEAKS" in /*) ;; *) GITLEAKS="" ;; esac
if [ -z "$GITLEAKS" ]; then
  OLD_IFS=$IFS
  IFS=:
  for d in ${KB_INSTALL_GITLEAKS_DIRS-/opt/homebrew/bin:/usr/local/bin}; do
    if [ -n "$d" ] && [ -x "$d/gitleaks" ]; then GITLEAKS="$d/gitleaks"; break; fi
  done
  IFS=$OLD_IFS
fi
if [ -z "$GITLEAKS" ]; then
  say "gitleaks: WARNING not found; install it (brew install gitleaks) and run install.sh again. Without it only the built-in redaction protects the pushed data, and gitleaks is not required yet."
fi

# 6. Config file (existing keys are kept)
mkdir -p "$CFG_DIR"
python3 - "$CFG" "$ROOT" "$HOST" "$GITLEAKS" <<'PY' || exit 1
import json
import os
import sys

path, root, host, gitleaks = sys.argv[1:5]
data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as e:
        sys.exit("install: %s is not valid JSON (%s); fix or remove it, then run install.sh again" % (path, e))
    if not isinstance(data, dict):
        sys.exit("install: %s is not a JSON object; fix or remove it, then run install.sh again" % path)
data["root"] = root
data["host"] = host
if not isinstance(data.get("auto_sync"), bool):
    data["auto_sync"] = False                       # the owner turns it on with `kb enable`
data.setdefault("skip_headless_single_prompt", True)
data.setdefault("branch", "main")
if gitleaks:
    data["gitleaks_path"] = gitleaks
    data["require_gitleaks"] = True
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print("config: wrote %s (host=%s, root=%s)" % (path, host, root))
PY
AUTO=$(python3 -c 'import json, sys; print("true" if json.load(open(sys.argv[1])).get("auto_sync") is True else "false")' "$CFG")

# 7. Claude Code plugin (from the data clone)
if command -v claude >/dev/null 2>&1; then
  if claude plugin marketplace add "$ROOT" >/dev/null 2>&1 || claude plugin marketplace update sessions-kb >/dev/null 2>&1; then
    say "claude: marketplace sessions-kb ready"
  else
    say "claude: WARNING could not add the marketplace; run: claude plugin marketplace add $ROOT (if sessions-kb points to another folder: claude plugin marketplace remove sessions-kb, then run install.sh again)"
  fi
  if claude plugin install sessions-kb@sessions-kb >/dev/null 2>&1 || claude plugin update sessions-kb@sessions-kb >/dev/null 2>&1; then
    say "claude: plugin sessions-kb@sessions-kb installed"
  else
    say "claude: WARNING could not install; run: claude plugin install sessions-kb@sessions-kb"
  fi
else
  say "claude: not found, skipped"
fi

# 8. Codex plugin (personal marketplace; keeps the marketplace's own name)
if command -v codex >/dev/null 2>&1; then
  if OUT=$(python3 "$ROOT/scripts/codex_marketplace.py" "$ROOT/plugin" 2>&1); then
    printf '%s\n' "$OUT" | sed '$d'
    MP=$(printf '%s\n' "$OUT" | sed -n 's/^marketplace: //p' | tail -n 1)
    if [ -n "$MP" ] && codex plugin add "sessions-kb@$MP" >/dev/null 2>&1; then
      say "codex: plugin sessions-kb@$MP installed"
    else
      say "codex: WARNING run 'codex plugin add sessions-kb@${MP:-<marketplace>}' to see why it failed"
    fi
    say "codex: open Codex once and trust the sessions-kb hook with /hooks"
  else
    printf '%s\n' "$OUT"
    say "codex: WARNING marketplace entry not written"
  fi
else
  say "codex: not found, skipped"
fi

# 9. kb on PATH for shells and Codex
mkdir -p "$HOME/.local/bin"
TARGET="$ROOT/plugin/bin/kb"
[ -e "$HOME/claude-tools/plugins/local-tools/bin/kb" ] && TARGET="$HOME/claude-tools/plugins/local-tools/bin/kb"
ln -sf "$TARGET" "$HOME/.local/bin/kb"
say "path: ~/.local/bin/kb -> $TARGET"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "path: add ~/.local/bin to your PATH" ;; esac

# 10. Index, and the sync only when the owner already turned automatic syncs on
mkdir -p "$ROOT/.kb"
KB_ROOT="$ROOT" "$ROOT/plugin/bin/kb" reindex
if [ "$AUTO" = true ] && [ "$NO_SYNC" != 1 ]; then
  KB_ROOT="$ROOT" nohup "$ROOT/plugin/bin/kb" sync </dev/null >>"$ROOT/.kb/sync.log" 2>&1 &
  say "sync: started in the background (log: $ROOT/.kb/sync.log)"
elif [ "$AUTO" = true ]; then
  say "sync: not started (--no-sync)"
else
  say "sync: not started (auto_sync is off)"
fi
if [ "$AUTO" != true ]; then
  say ""
  say "Next steps (host '$HOST'; nothing is committed or pushed until you run them):"
  say "  1. kb backfill                 process every session now and make the first data push"
  say "  2. kb backfill --summaries     write the summaries (slow; uses your Claude quota)"
  say "  3. kb enable                   let the SessionStart hook sync automatically"
fi
