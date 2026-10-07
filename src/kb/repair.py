"""kb repair: put the data clone back on its upstream when this host's commits no longer rebase onto it.

When: the remote changed this host's files (for example summaries made on another machine and merged), so the
sync's `pull --rebase` conflicts on every run. Safe because all that this host commits can be made again from the
local transcripts: the reset drops this host's local-only commits and uncommitted changes, and clearing the
fingerprints makes the next sync render every session again on top of the upstream files. That sync keeps the
summary fields it finds in those files and commits only real differences.

It refuses, and changes nothing, unless: no sync runs, the clone is on the configured branch, has an upstream and
can fetch it, and every local-only commit and every uncommitted change of a tracked file is inside this host's
folders. A half-done rebase is aborted first, as every sync does. The old HEAD is kept as refs/kb/repair/<time>
when commits are dropped. The quarantine is kept: files gitleaks holds back stay held back.
"""
from __future__ import annotations

import datetime as dt

from kb import gitops
from kb.lock import Lock
from kb.state import State
from kb.sync import _within, own_paths

NEXT_STEP = "next: kb sync --now --no-summaries"


class RepairRefused(Exception):
    """One line: why nothing was changed."""


def _refuse(why: str):
    raise RepairRefused(f"{why}; nothing changed")


def _listed(paths) -> str:
    more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
    return ", ".join(paths[:3]) + more


def _count(root, revs: str) -> int:
    return int(gitops.git(root, "rev-list", "--count", revs).stdout.strip() or 0)


def repair(cfg) -> list:
    """Reset the clone to its upstream and clear the fingerprints. Returns the lines that say what was done.

    Raises RepairRefused (one line) when it is not safe; then nothing was changed.
    """
    lock = Lock(cfg.kb_dir / "lock")
    if not lock.acquire():
        _refuse("another sync is running; try again later")
    try:
        return _repair(cfg)
    finally:
        lock.release()


def _check_branch(cfg) -> None:
    branch = gitops.current_branch(cfg.root)
    if branch != cfg.branch:
        _refuse(f"on branch {branch or '(detached HEAD)'}, expected {cfg.branch}")


def _repair(cfg) -> list:
    root, own = cfg.root, own_paths(cfg)
    if gitops.current_branch(root) not in ("", cfg.branch):    # "" can be a half-done rebase: repaired below
        _check_branch(cfg)
    problem = gitops.repair(root)
    if problem:
        _refuse(f"git: {problem}")
    _check_branch(cfg)
    p = gitops.git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if p.returncode != 0:
        _refuse(f"branch {cfg.branch} has no upstream to reset to")
    upstream = p.stdout.strip()
    try:
        gitops.git(root, "fetch", "--quiet", timeout=gitops.PULL_TIMEOUT)
    except gitops.GitError as e:
        _refuse(" ".join(str(e).split()))
    foreign = [x for x in gitops.unpushed_paths(root) if not _within(x, own)]
    if foreign:
        _refuse(f"local commits that {upstream} does not have touch {_listed(foreign)}, outside this host's folders; "
                f"push or remove them by hand")
    dirty = sorted(gitops.dirty_tracked(root))
    stray = [x for x in dirty if not _within(x, own)]
    if stray:
        _refuse(f"uncommitted changes in {_listed(stray)}, outside this host's folders; commit or undo them first")

    head = gitops.git(root, "rev-parse", "HEAD").stdout.strip()
    target = gitops.git(root, "rev-parse", "@{u}").stdout.strip()
    ahead, behind = _count(root, "@{u}..HEAD"), _count(root, "HEAD..@{u}")
    state = State.load(cfg.kb_dir / "sync-state.json")
    cleared = len(state.files)
    state.files = {}
    state.raw_pending = {}             # it says "markdown written": the reset may drop that markdown
    state.save()                       # first: a failed save stops here, and an early clear only costs a re-render
    lines = [f"reset {cfg.branch} to {upstream} ({target[:12]}); it was {ahead} commit(s) ahead and {behind} behind"]
    if ahead:
        ref = "refs/kb/repair/" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        gitops.git(root, "update-ref", ref, head)
        lines.append(f"dropped {ahead} local commit(s) that touch only this host's folders; "
                     f"the old HEAD {head[:12]} is kept as {ref}")
    gitops.git(root, "reset", "--quiet", "--hard", target)
    if dirty:
        lines.append(f"discarded uncommitted changes in {len(dirty)} file(s) of this host's folders")
    lines.append(f"cleared {cleared} fingerprints: the next sync renders every session again, keeps the summaries "
                 f"in the files and commits only real changes")
    lines.append(NEXT_STEP)
    return lines
