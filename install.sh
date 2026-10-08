#!/bin/sh
# install.sh — set up retroagent on this machine. Safe to run again. Run it from a clone of retroagent (the code).
# Usage: ./install.sh [--repo URL] [--root DIR] [--host NAME] [--no-sync] [--force-host]
#   --repo URL     your private data repo (a git URL). Needed on the first run; later runs reuse the clone in the
#                  config's root. A repo without the base files (README.md, AGENTS.md, …) gets them in one pushed
#                  commit; an empty repo gets its first commit that way.
#   --root DIR     where the data repo is cloned (default: the config's root, else $HOME/.retroagent-data). The sync
#                  works there; never edit it by hand.
#   --host NAME    this machine's name in the KB (default: the config's host, else the short hostname).
#                  It must be unique per machine.
#   --no-sync      do not start a background sync (only matters when auto_sync is already on).
#   --force-host   skip the check that sessions/<host> belongs to another machine, and take the id of its
#                  committed marker (use it on the machine that already wrote those sessions, once, to claim them:
#                  after a new clone or a lost .kb/machine-id).
# Does: data clone, config file, Claude plugin, Codex plugin, ~/.local/bin/kb, index. Automatic syncs stay off
# (auto_sync=false) until you run `kb enable`.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }
usage() { say "Usage: install.sh [--repo URL] [--root DIR] [--host NAME] [--no-sync] [--force-host]"; }

ROOT=""
REPO=""
HOST=""
NO_SYNC=0
FORCE_HOST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) [ $# -ge 2 ] || { usage >&2; die "--repo needs a git URL"; }; REPO=$2; shift 2 ;;
    --repo=*) REPO=${1#--repo=}; shift ;;
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
case "$HERE" in
  */.claude/plugins/cache/*|*/.codex/plugins/cache/*)
    die "run install.sh from a clone of retroagent, not from a plugin cache: git clone https://github.com/gabe4coding/retroagent ~/.retroagent && ~/.retroagent/install.sh --repo <your data repo URL>" ;;
esac

CFG_DIR="$HOME/.config/retroagent"
CFG="$CFG_DIR/config.json"
# sessions-kb is the old name of retroagent: old installs keep their config there.
LEGACY_CFG="$HOME/.config/sessions-kb/config.json"
cfg_get() {     # one string value of the config, or nothing
  [ -f "$CFG" ] || return 0
  python3 -c 'import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception:
    v = None
print(v if isinstance(v, str) else "")' "$CFG" "$1"
}

# 1. Tools: python3 >= 3.9 with SQLite FTS5, and git
command -v python3 >/dev/null 2>&1 || die "python3 not found; install Python 3.9 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
  || die "python3 3.9 or newer is needed (this one is $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown))"
python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' >/dev/null 2>&1 \
  || die "python3 has no SQLite FTS5 support, which kb needs for search; use another python3 (for example from Homebrew)"
command -v git >/dev/null 2>&1 || die "git not found"

# 2. The config: an old install (sessions-kb) keeps its settings
if [ ! -f "$CFG" ] && [ -f "$LEGACY_CFG" ]; then
  mkdir -p "$CFG_DIR"
  cp "$LEGACY_CFG" "$CFG"
  say "config: copied $LEGACY_CFG to $CFG"
fi
[ -n "$ROOT" ] || ROOT=$(cfg_get root)
[ -n "$ROOT" ] || ROOT="$HOME/.retroagent-data"
case "$ROOT" in "~"*) ROOT="$HOME${ROOT#\~}" ;; esac
case "$ROOT" in /*) ;; *) ROOT="$PWD/$ROOT" ;; esac
while [ "${#ROOT}" -gt 1 ] && [ "${ROOT%/}" != "$ROOT" ]; do ROOT=${ROOT%/}; done

# 3. This machine's host name: --host, else the config's, else the short hostname (same slug rule as kb)
slug() {
  s=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9._-][^a-z0-9._-]*/-/g' -e 's/^[-.]*//' -e 's/[-.]*$//' | cut -c1-40)
  printf '%s' "${s:-unknown}"
}
[ -n "$HOST" ] || HOST=$(cfg_get host)
if [ -z "$HOST" ]; then
  HOST=$(hostname -s 2>/dev/null || uname -n)
  HOST=${HOST%%.*}
  [ -n "$HOST" ] || HOST=host
fi
HOST=$(slug "$HOST")

# 4. The data clone: cloned from --repo; an existing one must be a clone of it
same_repo() {   # two git URLs name the same repo (ssh and https forms of GitHub, with or without .git)
  a=$(printf '%s' "$1" | sed -e 's#^git@github.com:#https://github.com/#' -e 's#^ssh://git@github.com/#https://github.com/#' -e 's#/*$##' -e 's#\.git$##')
  b=$(printf '%s' "$2" | sed -e 's#^git@github.com:#https://github.com/#' -e 's#^ssh://git@github.com/#https://github.com/#' -e 's#/*$##' -e 's#\.git$##')
  [ "$a" = "$b" ]
}
if [ -e "$ROOT" ]; then
  TOP=$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -z "$TOP" ] || [ "$(cd "$TOP" && pwd -P)" != "$(cd "$ROOT" && pwd -P)" ]; then
    die "$ROOT exists but is not a git clone; move it away or choose another folder with --root"
  fi
  [ "$(cd "$ROOT" && pwd -P)" != "$(cd "$HERE" && pwd -P)" ] \
    || die "$ROOT is the retroagent code; your data needs its own repo (--repo URL) and folder (--root DIR)"
  THEIRS=$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)
  if [ -n "$REPO" ] && ! same_repo "$THEIRS" "$REPO"; then
    die "$ROOT is a clone of ${THEIRS:-a repo without origin}, not of $REPO; choose another folder with --root"
  fi
  say "root: using the existing clone $ROOT"
else
  [ -n "$REPO" ] || die "no data repo yet: run again with --repo URL (a private git repo for your sessions; the retroagent:setup skill can create one)"
  mkdir -p "$(dirname "$ROOT")"
  git clone --quiet "$REPO" "$ROOT" 2>/dev/null || die "could not clone $REPO into $ROOT"
  say "root: cloned $REPO into $ROOT"
fi

# 5. Host guard: two machines with one host name write the same folders and block each other
HOSTDIR="$ROOT/sessions/$HOST"
LOCAL_ID="$ROOT/.kb/machine-id"
MARKER="$HOSTDIR/.machine-id"
squash() { tr -d ' \n\r\t' < "$1"; }
if [ -e "$HOSTDIR" ] && [ "$FORCE_HOST" != 1 ]; then
  if [ ! -s "$LOCAL_ID" ]; then
    die "sessions/$HOST already exists in $ROOT and this machine has no id yet (.kb/machine-id), so another machine may use the host '$HOST'. Choose a unique name with --host NAME. If this is the machine that wrote those sessions, run again with --force-host."
  fi
  if [ -s "$MARKER" ] && [ "$(squash "$MARKER")" != "$(squash "$LOCAL_ID")" ]; then
    die "host '$HOST' belongs to another machine (sessions/$HOST/.machine-id is not this machine's id). Choose a unique name with --host NAME. If this is the machine that wrote those sessions (a new clone, a lost .kb/machine-id), run again with --force-host."
  fi
fi
# --force-host on the owner after a new clone or a lost .kb/machine-id: take the committed id back, else every sync
# stops with "belongs to another machine"
if [ "$FORCE_HOST" = 1 ] && [ -s "$MARKER" ] && { [ ! -s "$LOCAL_ID" ] || [ "$(squash "$MARKER")" != "$(squash "$LOCAL_ID")" ]; }; then
  mkdir -p "$ROOT/.kb"
  cp "$MARKER" "$LOCAL_ID"
  say "host: this machine takes the id of sessions/$HOST/.machine-id (--force-host)"
fi

# 6. gitleaks: look in PATH, then the usual folders
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

# 7. Config file (existing keys are kept)
mkdir -p "$CFG_DIR"
python3 - "$CFG" "$ROOT" "$HOST" "$GITLEAKS" "$HERE" <<'PY' || exit 1
import json
import os
import sys

path, root, host, gitleaks, code = sys.argv[1:6]
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
data["code"] = code                                 # plugin caches run this clone (bin/kb)
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

# 8. The data repo's base files (README.md, AGENTS.md, …): only those it lacks, in one pushed commit
if OUT=$(KB_ROOT="$ROOT" "$HERE/bin/kb" setup init 2>&1); then
  WROTE=$(printf '%s\n' "$OUT" | python3 -c 'import json, sys
try:
    print(" ".join(json.load(sys.stdin).get("written") or []))
except Exception:
    print("")' 2>/dev/null || true)
  if [ -n "$WROTE" ]; then say "data: pushed $WROTE"; fi
else
  say "data: WARNING could not add the base files: $(printf '%s' "$OUT" | tail -n 1)"
  say "data: fix it, then run: kb setup init"
fi
git -C "$ROOT" rev-parse -q --verify HEAD >/dev/null 2>&1 \
  || die "the data repo has no commit yet, so the sync cannot work; fix the error above and run install.sh again"

# 9. Claude Code plugin (from this clone of the code)
if command -v claude >/dev/null 2>&1; then
  if claude plugin marketplace add "$HERE" >/dev/null 2>&1 || claude plugin marketplace update retroagent >/dev/null 2>&1; then
    say "claude: marketplace retroagent ready"
  else
    say "claude: WARNING could not add the marketplace; run: claude plugin marketplace add $HERE"
  fi
  if claude plugin install retroagent@retroagent >/dev/null 2>&1 || claude plugin update retroagent@retroagent >/dev/null 2>&1; then
    say "claude: plugin retroagent@retroagent installed"
  else
    say "claude: WARNING could not install; run: claude plugin install retroagent@retroagent"
  fi
else
  say "claude: not found, skipped"
fi

# 10. Codex plugin (personal marketplace; keeps the marketplace's own name)
if command -v codex >/dev/null 2>&1; then
  if OUT=$(python3 "$HERE/scripts/codex_marketplace.py" "$HERE" 2>&1); then
    printf '%s\n' "$OUT" | sed '$d'
    MP=$(printf '%s\n' "$OUT" | sed -n 's/^marketplace: //p' | tail -n 1)
    if [ -n "$MP" ] && codex plugin add "retroagent@$MP" >/dev/null 2>&1; then
      say "codex: plugin retroagent@$MP installed"
    else
      say "codex: WARNING run 'codex plugin add retroagent@${MP:-<marketplace>}' to see why it failed"
    fi
    say "codex: open Codex once and trust the retroagent hook with /hooks"
  else
    printf '%s\n' "$OUT"
    say "codex: WARNING marketplace entry not written"
  fi
else
  say "codex: not found, skipped"
fi

# 11. kb on PATH for shells and Codex (inside Claude Code the plugin puts it on PATH)
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/bin/kb" "$HOME/.local/bin/kb"
say "path: ~/.local/bin/kb -> $HERE/bin/kb"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "path: add ~/.local/bin to your PATH" ;; esac

# 12. Index, and the sync only when the owner already turned automatic syncs on
mkdir -p "$ROOT/.kb"
KB_ROOT="$ROOT" "$HERE/bin/kb" reindex
if [ "$AUTO" = true ] && [ "$NO_SYNC" != 1 ]; then
  KB_ROOT="$ROOT" nohup "$HERE/bin/kb" sync </dev/null >>"$ROOT/.kb/sync.log" 2>&1 &
  say "sync: started in the background (log: $ROOT/.kb/sync.log)"
elif [ "$AUTO" = true ]; then
  say "sync: not started (--no-sync)"
else
  say "sync: not started (auto_sync is off)"
fi
if [ ! -d "$HOSTDIR" ]; then
  say ""
  say "Next steps (host '$HOST'; nothing is committed or pushed until you run them):"
  say "  1. kb backfill                 process every session now and make the first data push"
  say "  2. kb backfill --summaries     write the summaries (slow; uses your Claude quota)"
  [ "$AUTO" = true ] || say "  3. kb enable                   let the SessionStart hook sync automatically"
elif [ "$AUTO" != true ]; then
  say ""
  say "Next step: kb enable            let the SessionStart hook sync automatically (host '$HOST' already has sessions)"
fi
