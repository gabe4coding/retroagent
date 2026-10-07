"""Cloud sessions: a Claude Code cloud container is deleted after its session, so its transcripts reach the KB through
the data repo.

In a cloud session (a Stop hook that `kb cloud install` puts in ~/.claude/settings.json; the cloud setup script runs it):
  kb cloud push   slim and redact the session's transcript and its subagents (kb.slimraw) and commit them with git
                  plumbing as inbox/claude/<encoded cwd>/<id>.jsonl.gz (+ <id>/subagents/…) on the branch of the data
                  clone: the session's own claude/… branch, never the sync branch. The working tree is not touched,
                  and the cloud owns no host.

On the one machine that imports them (cloud_import on, `kb cloud import --on`), each sync:
  fetches, copies the inbox files of every remote branch into .kb/cloud/claude/ (laid out like ~/.claude/projects),
  and processes them as the sessions of host `cloud` (cloud_host): markdown, raw copies, summaries, catalog and
  vectors, as for its own sessions. This machine then owns sessions/cloud (kb.machine), so a second importer stops.
  After a clean push it deletes each branch whose only change since the sync branch is inbox/ and whose inbox files
  are all imported.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from kb import gitops, machine, setup
from kb.adapters import claude
from kb.config import CODE_ROOT
from kb.lock import Lock
from kb.model import Unit
from kb.slimraw import slim
from kb.util import atomic_write, expand, short_id

INBOX = "inbox/claude"
PUSH_ROUNDS = 3                 # pushes in one run while the transcript keeps growing
HOOK_EVENTS = ("Stop", "SessionEnd")
SETTINGS = "~/.claude/settings.json"


class CloudError(RuntimeError):
    """One line: what failed and what to do."""


# ---------------------------------------------------------------- in the cloud session

def is_cloud() -> bool:
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


def session_files(transcript) -> dict:
    """{inbox path: source file} of one Claude Code session: the transcript and its subagents."""
    main = Path(transcript)
    proj = main.parent
    files = {f"{INBOX}/{proj.name}/{main.name}.gz": main}
    sub_dir = proj / main.stem / "subagents"
    for sub in sorted(sub_dir.rglob("agent-*.jsonl")) if sub_dir.is_dir() else []:
        files[f"{INBOX}/{proj.name}/{main.stem}/subagents/{sub.relative_to(sub_dir).as_posix()}.gz"] = sub
    return files


def _fingerprint(files: dict) -> str:
    parts = []
    for rel, src in sorted(files.items()):
        st = os.stat(src)
        parts.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


def _headless(cfg, main: Path, files: dict) -> bool:
    """A `claude -p` run with one prompt, such as the pages routine: the sync skips it, so it is not pushed (on the
    routine's bootstrap branch it would also reach main with the pages)."""
    unit = Unit(key=str(main), agent="claude", paths=[str(main)] + [str(p) for p in files.values() if p != main],
                main=str(main))
    s = claude.parse_unit(unit)
    return s is not None and cfg.skip_headless_single_prompt and s.headless and s.user_turns <= 1


def newest_transcript(claude_dir) -> str:
    mains = [p for proj in Path(claude_dir).glob("*") if proj.is_dir() for p in proj.glob("*.jsonl")]
    return str(max(mains, key=lambda p: p.stat().st_mtime)) if mains else ""


def _bootstrap_branch(root) -> str:
    from kb import routine
    try:
        return routine.load_settings(root)["bootstrap_branch"]
    except routine.PagesError:          # a broken pages/config.json is the routine's to report
        return routine.DEFAULTS["bootstrap_branch"]


def push(cfg, transcript: str) -> str:
    """Commit the session's slim transcript files on the data clone's branch and push. Pushes again while the
    transcript grew during the push (up to PUSH_ROUNDS). Returns one line."""
    root = cfg.root
    if not (root / ".git").exists():
        raise CloudError(f"no data clone at {root}; start the session with the data repo too")
    branch = gitops.current_branch(root)
    if not branch or branch == cfg.branch:
        raise CloudError(f"the data clone is on {branch or '(detached HEAD)'}; cloud sessions push only to their own "
                         f"branch, never to {cfg.branch}")
    if branch == _bootstrap_branch(root):
        raise CloudError(f"the data clone is on {branch}, the pages routine's branch: it reaches {cfg.branch} with the "
                         f"pages, so nothing is pushed there")
    lock = Lock(cfg.kb_dir / "cloud-push.lock")
    if not lock.acquire():
        return "another push is running"
    done_file = cfg.kb_dir / "cloud-pushed.json"
    try:
        try:
            done = json.loads(done_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        line = "nothing new"
        for _ in range(PUSH_ROUNDS):
            files = session_files(transcript)
            fp = _fingerprint(files)
            if done.get(transcript) == fp:
                break
            if _headless(cfg, Path(transcript), files):
                return "skipped: a headless run with one prompt (the sync would skip it too)"
            data = {rel: (slim("claude", [src])[0], True) for rel, src in files.items()}
            res = setup.publish(root, data, f"inbox: cloud session {short_id(Path(transcript).stem)}", branch,
                                start=cfg.branch)
            done[transcript] = fp
            atomic_write(done_file, json.dumps(done, indent=1).encode("utf-8"))
            line = f"pushed {len(res['written'])} file(s) to {branch}" if res["written"] else "nothing new"
        return line
    finally:
        lock.release()


def hook(cfg, stdin) -> None:
    """The Stop / SessionEnd hook: start `kb cloud push` in the background and return at once. Only in a cloud
    session (CLAUDE_CODE_REMOTE=true), never in our own headless runs (KB_CHILD=1)."""
    if not is_cloud() or os.environ.get("KB_CHILD") == "1":
        return
    try:
        event = json.loads(stdin.read() or "{}")
    except ValueError:
        event = {}
    transcript = event.get("transcript_path") if isinstance(event, dict) else ""
    if not isinstance(transcript, str) or not transcript.endswith(".jsonl"):
        return
    cfg.kb_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.kb_dir / "cloud.log", "ab") as log:
        subprocess.Popen([str(CODE_ROOT / "bin" / "kb"), "cloud", "push", "--transcript", transcript],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def install(kb_exe: str, settings=SETTINGS) -> str:
    """Add the hook to Claude Code's user settings (kept: every other key and hook). Returns the settings path."""
    path = expand(settings)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CloudError(f"cannot change {path}: not valid JSON ({' '.join(str(e).split())})") from None
        if not isinstance(data, dict):
            raise CloudError(f"cannot change {path}: it is not a JSON object")
    command = f"{kb_exe} cloud hook"
    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not any(h.get("command") == command for g in groups if isinstance(g, dict)
                   for h in g.get("hooks", []) if isinstance(h, dict)):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return str(path)


# ---------------------------------------------------------------- on the importing machine

def lane(cfg):
    """The Config of host `cloud_host` (its transcripts in .kb/cloud/claude/, no Codex), or None when import is off."""
    if not cfg.cloud_import or cfg.cloud_host == cfg.host:
        return None
    return replace(cfg, host=cfg.cloud_host, claude_dir=cfg.kb_dir / "cloud" / "claude", codex_dirs=[])


def _branches(root, sync_branch: str) -> list:
    """(name, tip) of every remote branch but the sync branch."""
    out = gitops.git(root, "for-each-ref", "--format=%(refname:strip=3) %(objectname)", "refs/remotes/origin/").stdout
    pairs = [line.split(" ", 1) for line in out.splitlines() if " " in line]
    return [(name, sha) for name, sha in pairs if name not in ("HEAD", sync_branch)]


def _inbox_blobs(root, tip: str) -> dict:
    """{inbox path: (blob, size)} on one commit."""
    out = gitops.git(root, "ls-tree", "-r", "-l", tip, "--", INBOX).stdout
    found = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob" and path.endswith(".jsonl.gz"):
            found[path] = (parts[2], int(parts[3]) if parts[3].isdigit() else 0)
    return found


def _only_inbox(root, tip: str, sync_branch: str) -> bool:
    base = gitops.git(root, "merge-base", tip, f"refs/remotes/origin/{sync_branch}", check=False)
    if base.returncode != 0:
        return False
    changed = gitops.git(root, "diff", "--name-only", base.stdout.strip(), tip).stdout.split()
    return bool(changed) and all(p.startswith(INBOX + "/") for p in changed)


def _blob(root, blob: str) -> bytes:
    p = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=gitops.DEFAULT_TIMEOUT)
    if p.returncode != 0:
        raise gitops.GitError(f"git cat-file blob {blob}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def import_inbox(cfg, ccfg) -> tuple:
    """Fetch, then copy every branch's inbox files into ccfg.claude_dir (the largest copy of a file wins: transcripts
    only grow). Returns (files written, errors, branches to delete after a clean push: [(name, tip)])."""
    root = cfg.root
    gitops.git(root, "fetch", "--quiet", "--prune", "origin", timeout=gitops.PULL_TIMEOUT)
    seen_file = cfg.kb_dir / "cloud-inbox.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    best, per_branch = {}, []
    for name, tip in _branches(root, cfg.branch):
        blobs = _inbox_blobs(root, tip)
        if blobs:
            per_branch.append((name, tip, blobs))
        for path, (blob, size) in blobs.items():
            if path not in best or size > best[path][1]:
                best[path] = (blob, size, tip)
    written, errors, failed = 0, [], set()
    for path, (blob, _, tip) in sorted(best.items()):
        if seen.get(path) == blob:
            continue
        try:
            target = ccfg.claude_dir / path[len(INBOX) + 1:-len(".gz")]
            if atomic_write(target, gzip.decompress(_blob(root, blob))):
                written += 1
                # the push's time, not now: the sync's quiet period then counts from the session's last activity
                when = int(gitops.git(root, "show", "-s", "--format=%ct", tip).stdout.strip() or 0)
                if when:
                    os.utime(target, (when, when))
        except (OSError, EOFError, ValueError, gitops.GitError) as e:     # one bad file must not stop the others
            errors.append(f"cloud: {path}: {type(e).__name__}: {' '.join(str(e).split())[:200]}")
            failed.add(path)
            continue
        seen[path] = blob
    atomic_write(seen_file, (json.dumps(seen, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    done = [(name, tip) for name, tip, blobs in per_branch
            if not failed.intersection(blobs) and _only_inbox(root, tip, cfg.branch)]
    return written, errors, done


def delete_branches(root, branches) -> list:
    """Delete the imported inbox branches on the remote, each only if it did not move since the fetch. Returns errors."""
    errors = []
    for name, tip in branches:
        p = gitops.git(root, "push", "--quiet", "--no-verify", f"--force-with-lease=refs/heads/{name}:{tip}",
                       "origin", f":refs/heads/{name}", check=False, timeout=gitops.PUSH_TIMEOUT)
        if p.returncode != 0:
            errors.append(f"cloud: cannot delete branch {name}: {' '.join((p.stderr or p.stdout).split())[:200]}")
    return errors


def taken(cfg, ccfg) -> str:
    """Why this machine must not import (another machine owns the cloud host), else ''."""
    if machine.owned_by_another(cfg.root, ccfg.host, machine.local_id(cfg.kb_dir), cfg.branch):
        return (f"cloud: host '{ccfg.host}' belongs to another machine; only one machine imports cloud sessions "
                f"(turn it off here: kb cloud import --off)")
    return ""
