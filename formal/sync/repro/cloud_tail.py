"""Reproduce the CloudInbox.tla counterexample (MC_code_rounds.cfg): a cloud session loses its last turn.

`kb cloud push` holds .kb/cloud-push.lock for up to PUSH_ROUNDS pushes. A hook that finds the lock busy returns
"another push is running" and pushes nothing. When the session ends during the last round, the Stop hook of the last
turn and the SessionEnd hook both give up, and no push sends the last turn. The container is then deleted.

The git wrapper adds one turn to the transcript during each push, as a fast session does. During the third push it
starts the two last hooks. Every kb step runs unchanged.

Run: python3 formal/sync/repro/cloud_tail.py
     exit 1: reproduced, exit 0: not reproduced, exit 2: a hook did not finish
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time

from _sandbox import REPO, sandbox, install_wrapper

tmp = sandbox("cloud")
from fixtures import SID, clone, init_remote, make_claude_tree, make_config  # noqa: E402
from kb import cloud  # noqa: E402

remote = init_remote(tmp)
box = clone(remote, tmp / "box")
subprocess.run([os.environ["REAL_GIT"], "-C", str(box), "checkout", "-q", "--detach"], check=True)
projects = make_claude_tree(tmp / "cloud-src")
transcript = next(projects.glob("*/*.jsonl"))
cfg = make_config(box, "box", projects, tmp / "none", tmp / "none")
branch = cloud.inbox_branch(str(transcript))

hook = tmp / "hook.py"
hook.write_text(f"""import sys
sys.path[:0] = [{str(REPO / 'src')!r}, {str(REPO / 'tests')!r}]
from fixtures import make_config
from kb import cloud
cfg = make_config({str(box)!r}, "box", {str(projects)!r}, {str(tmp / 'none')!r}, {str(tmp / 'none')!r})
print(sys.argv[1] + ": " + cloud.push(cfg, {str(transcript)!r}), flush=True)
""")


def turn(n: int) -> str:
    rec = {"type": "user", "sessionId": SID, "timestamp": f"2026-10-06T14:0{n}:00.000Z",
           "message": {"role": "user", "content": f"TURN-{n}-MARKER"}}
    return json.dumps(rec)


os.environ.update(BOX=str(box), TRANSCRIPT=str(transcript), HOOK=str(hook), PY=sys.executable,
                  T2=turn(2), T3=turn(3), T4=turn(4))
install_wrapper(tmp, r'''
if [ "$1" = "-C" ] && [ "$2" = "$BOX" ] && [ "$3" = "push" ]; then
  n=$(( $(cat "$REPRO_STATE/pushes" 2>/dev/null || echo 0) + 1 )); echo $n > "$REPRO_STATE/pushes"
  case $n in
    1) echo "$T2" >> "$TRANSCRIPT" ;;
    2) echo "$T3" >> "$TRANSCRIPT" ;;
    3) echo "$T4" >> "$TRANSCRIPT"
       # Record each hook's pid and exit. A traceback is one process failing, not two finished hooks.
       (
         "$PY" "$HOOK" "Stop hook of turn 4" > "$REPRO_STATE/stop.out" 2> "$REPRO_STATE/stop.err"
         echo $? > "$REPRO_STATE/stop.rc"
       ) &
       echo $! > "$REPRO_STATE/stop.pid"
       (
         "$PY" "$HOOK" "SessionEnd hook" > "$REPRO_STATE/end.out" 2> "$REPRO_STATE/end.err"
         echo $? > "$REPRO_STATE/end.rc"
       ) &
       echo $! > "$REPRO_STATE/end.pid"
       ;;
  esac
fi''')

print("Stop hook of turn 1: " + cloud.push(cfg, str(transcript)))


def finish_hook(hook_id: str, label: str) -> None:
    """Wait until this hook process has exited 0. A timeout or a failure is an error, not a lost turn."""
    pid_path, rc_path = tmp / f"{hook_id}.pid", tmp / f"{hook_id}.rc"
    out_path, err_path = tmp / f"{hook_id}.out", tmp / f"{hook_id}.err"
    if not pid_path.is_file():
        print(f"ERROR: {label} was not started")
        sys.exit(2)
    pid = pid_path.read_text().strip()
    deadline = time.time() + 60
    while time.time() < deadline and not rc_path.is_file():
        time.sleep(0.05)
    out = out_path.read_text(encoding="utf-8", errors="replace") if out_path.is_file() else ""
    err = err_path.read_text(encoding="utf-8", errors="replace") if err_path.is_file() else ""
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip())
    if not rc_path.is_file():
        print(f"ERROR: {label} timed out (pid {pid})")
        sys.exit(2)
    rc = int(rc_path.read_text().strip())
    if rc != 0:
        print(f"ERROR: {label} exited {rc}")
        sys.exit(2)


finish_hook("stop", "Stop hook of turn 4")
finish_hook("end", "SessionEnd hook")

show = subprocess.run([os.environ["REAL_GIT"], "-C", str(remote), "show",
                       f"{branch}:inbox/claude/{transcript.parent.name}/{transcript.name}.gz"],
                      capture_output=True)
if show.returncode != 0:
    print(f"ERROR: cannot read the pushed transcript on {branch}: "
          f"{show.stderr.decode('utf-8', 'replace').strip()}")
    sys.exit(2)
blob = show.stdout
pushed = gzip.decompress(blob).decode("utf-8")
local = transcript.read_text(encoding="utf-8")
for n in (2, 3, 4):
    print(f"TURN-{n}-MARKER  in transcript: {f'TURN-{n}-MARKER' in local}  on {branch}: {f'TURN-{n}-MARKER' in pushed}")
lost = "TURN-4-MARKER" in local and "TURN-4-MARKER" not in pushed
print("REPRODUCED: no hook pushed turn 4; it is lost when the container is deleted" if lost else "NOT REPRODUCED")
sys.exit(1 if lost else 0)
