"""Thin git wrappers for the sync. Only this host's folders are ever staged."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(root, *args, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def has_remote(root) -> bool:
    return bool(git(root, "remote", check=False).stdout.strip())


def pull(root) -> None:
    git(root, "pull", "--rebase", "--autostash", "--quiet")


def stage(root, paths) -> bool:
    """Stage the given paths (deletions included). True if anything is staged."""
    existing = [p for p in paths if (Path(root) / p).exists()]
    tracked = [p for p in paths if p not in existing and git(root, "ls-files", "--", p, check=False).stdout.strip()]
    targets = existing + tracked
    if not targets:
        return False
    git(root, "add", "-A", "--", *targets)
    return git(root, "diff", "--cached", "--quiet", check=False).returncode == 1


def secrets_check(root) -> str:
    """'' if clean or gitleaks is not installed, else a short finding text."""
    exe = shutil.which("gitleaks")
    if not exe:
        return ""
    p = subprocess.run([exe, "git", "--staged", "--redact", "--no-banner", str(root)], capture_output=True, text=True)
    if p.returncode != 0 and "unknown command" in (p.stdout + p.stderr).lower():
        p = subprocess.run([exe, "protect", "--staged", "--redact", "--no-banner", "--source", str(root)],
                           capture_output=True, text=True)
    if p.returncode == 0:
        return ""
    return (p.stdout + p.stderr).strip()[-500:] or f"gitleaks exit {p.returncode}"


def commit(root, message: str) -> None:
    git(root, "commit", "--quiet", "--no-verify", "-m", message)


def ahead(root) -> int:
    p = git(root, "rev-list", "--count", "@{u}..HEAD", check=False)
    try:
        return int(p.stdout.strip()) if p.returncode == 0 else 0
    except ValueError:
        return 0


def push(root, tries: int = 3) -> None:
    last = ""
    for _ in range(tries):
        p = git(root, "push", "--quiet", check=False)
        if p.returncode == 0:
            return
        last = (p.stderr or p.stdout).strip()
        git(root, "pull", "--rebase", "--autostash", "--quiet", check=False)
    raise GitError(f"push failed after {tries} tries: {last}")
