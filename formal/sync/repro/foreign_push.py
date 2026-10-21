"""Reproduce the Sync.tla counterexample (SyncMC_code.cfg): `kb sync` pushes another host's commit.

publish() checks the unpushed commits for paths outside this host's folders once, before gitops.push(). When that
push is rejected because another machine pushed first, push() pulls and pushes again without a new check. A commit
that a person made in the data clone during that window goes out with the sync.

Run: python3 formal/sync/repro/foreign_push.py
     exit 1: reproduced, exit 0: not reproduced, exit 2: the race did not run
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _sandbox import sandbox, install_wrapper

tmp = sandbox("foreign")
from fixtures import clone, init_remote, make_claude_tree, make_config  # noqa: E402
from kb.sync import run_sync  # noqa: E402

FOREIGN = "sessions/host-b/hand-fix.md"


def git(*args, cwd):
    return subprocess.run([os.environ["REAL_GIT"], *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def die(msg: str) -> None:
    """The race did not run. Exit 2, not a reproduced / not-reproduced verdict."""
    print(f"ERROR: {msg}")
    sys.exit(2)


def refused(errors) -> bool:
    return any("hand-fix.md" in e and "unpushed commits touch" in e for e in errors)


remote = init_remote(tmp)
a, b = clone(remote, tmp / "a"), clone(remote, tmp / "b")
cfg_a = make_config(a, "host-a", make_claude_tree(tmp / "a-src"), tmp / "none", tmp / "none")
cfg_b = make_config(b, "host-b", make_claude_tree(tmp / "b-src"), tmp / "none", tmp / "none")

rep = run_sync(cfg_b, now=True, summary_cap=0)
print(f"machine B first sync: pushed={rep.pushed} errors={rep.errors}")
if not rep.pushed or rep.errors:
    die(f"setup: machine B's sync did not finish: pushed={rep.pushed} errors={rep.errors}")

# Control: a hand commit of host-b's files that exists before the sync is refused.
os.environ.update(A_ROOT=str(a), B_ROOT=str(b))
(a / "sessions" / "host-b").mkdir(parents=True)
(a / FOREIGN).write_text("hand edit\n")
git("add", FOREIGN, cwd=a)
git("commit", "-q", "-m", "hand: fix a host-b summary", cwd=a)
rep = run_sync(cfg_a, now=True, summary_cap=0)
print(f"control (hand commit before the sync): pushed={rep.pushed} errors={rep.errors}")
control_remote = git("log", "--format=%s", "--name-only", "main", "--", FOREIGN, cwd=remote).split()
if rep.pushed or FOREIGN in control_remote or not refused(rep.errors):
    die("setup: the hand commit that existed before the sync was not refused: "
        f"pushed={rep.pushed} errors={rep.errors}")
git("reset", "-q", "--hard", "origin/main", cwd=a)
git("clean", "-qfd", "sessions/host-b", cwd=a)
for f in (cfg_a.kb_dir / "sync-state.json",):
    f.unlink(missing_ok=True)

# The race: B pushes while A's first push runs; a person commits host-b's files in A before A's retry pull.
install_wrapper(tmp, r'''
if [ "$1" = "-C" ] && [ "$2" = "$A_ROOT" ]; then
  if [ "$3" = "push" ] && [ ! -e "$REPRO_STATE/push1" ]; then
    touch "$REPRO_STATE/push1"
    echo "  [event] machine B pushes while A's push runs" >&2
    if "$REAL_GIT" -C "$B_ROOT" commit -q --allow-empty -m "sync(host-b): meanwhile" \
        && "$REAL_GIT" -C "$B_ROOT" push -q; then
      touch "$REPRO_STATE/push1.ok"
    else
      echo "  [event] machine B's push failed" >&2
    fi
  fi
  if [ "$3" = "pull" ] && [ -e "$REPRO_STATE/push1" ] && [ ! -e "$REPRO_STATE/hand" ]; then
    touch "$REPRO_STATE/hand"
    echo "  [event] a person commits host-b's files in A's data clone" >&2
    if mkdir -p "$A_ROOT/sessions/host-b" \
        && echo "hand edit" > "$A_ROOT/sessions/host-b/hand-fix.md" \
        && "$REAL_GIT" -C "$A_ROOT" add sessions/host-b/hand-fix.md \
        && "$REAL_GIT" -C "$A_ROOT" commit -q -m "hand: fix a host-b summary"; then
      touch "$REPRO_STATE/hand.ok"
    else
      echo "  [event] the hand commit failed" >&2
    fi
  fi
fi''')
try:
    rep = run_sync(cfg_a, now=True, summary_cap=0)
except Exception as exc:  # noqa: BLE001 - any failure here is a broken run, not a verdict
    die(f"race sync raised {type(exc).__name__}: {exc}")
print(f"race: pushed={rep.pushed} errors={rep.errors}")
pushed_ok = (tmp / "push1.ok").is_file()
hand_ok = (tmp / "hand.ok").is_file()
if not pushed_ok or not hand_ok:
    die(f"the race did not run: B push injected={pushed_ok} hand commit injected={hand_ok}")
on_remote = git("log", "--format=%s", "--name-only", "main", "--", FOREIGN, cwd=remote).split()
leaked = FOREIGN in on_remote
if not leaked and not rep.pushed and not refused(rep.errors):
    die(f"race sync did not complete a push: pushed={rep.pushed} errors={rep.errors}")
if not leaked and rep.errors and not refused(rep.errors):
    die(f"race sync failed: {rep.errors}")
print(f"remote main has {FOREIGN}: {leaked}")
print(git("log", "--format=%h %s", "-4", "main", cwd=remote).rstrip())
print("REPRODUCED: the sync of host-a pushed host-b's file" if leaked else "NOT REPRODUCED")
sys.exit(1 if leaked else 0)
