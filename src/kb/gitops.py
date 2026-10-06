"""Thin git wrappers for the sync. Only this host's folders are ever staged or committed.

Every git and gitleaks process runs non-interactively (no prompts, no editor, no stdin, ssh BatchMode) and
under a timeout, in its own process group so a timeout also stops helpers such as ssh.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 60
PULL_TIMEOUT = 120
PUSH_TIMEOUT = 30 * 60        # the first data push can be hundreds of MB
GITLEAKS_TIMEOUT = 300


class GitError(Exception):
    pass


@dataclass
class SecretsResult:
    ran: bool = False                              # False if gitleaks is not installed
    error: str = ""                                # gitleaks could not run properly (not a finding)
    files: list = field(default_factory=list)      # repo-relative paths with findings


def _ssh_command(root) -> str:
    """The user's ssh command (GIT_SSH_COMMAND, else core.sshCommand, else plain ssh) that never asks questions."""
    cmd = os.environ.get("GIT_SSH_COMMAND", "").strip()
    if not cmd and root is not None:
        try:
            p = subprocess.run(["git", "-C", str(root), "config", "--get", "core.sshCommand"], capture_output=True,
                               text=True, stdin=subprocess.DEVNULL, timeout=10)
            cmd = p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            cmd = ""
    return (cmd or "ssh") + " -o BatchMode=yes"


def _env(root=None) -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_EDITOR"] = "true"
    env["GIT_SSH_COMMAND"] = _ssh_command(root)
    return env


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001 - best effort; a helper outside the group may still hold the pipes
        pass


def _run(cmd, timeout, label, root=None, env=None) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", env=env if env is not None else _env(root),
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise GitError(f"{label}: timed out after {timeout}s") from None
    except BaseException:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def git(root, *args, check: bool = True, timeout: float = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    p = _run(["git", "-C", str(root), *args], timeout, f"git {' '.join(args)}", root=root)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def has_remote(root) -> bool:
    return bool(git(root, "remote", check=False).stdout.strip())


def _dirty_tracked(root) -> set:
    """Paths of tracked files with local changes (staged or not). Untracked files are not listed."""
    entries = git(root, "status", "--porcelain", "-z", "--untracked-files=no").stdout.split("\0")
    paths, i = set(), 0
    while i < len(entries):
        entry, i = entries[i], i + 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":      # a rename or copy lists the original path next
            if i < len(entries) and entries[i]:
                paths.add(entries[i])
            i += 1
    return paths


def _put_back(root, paths: set, pull_error) -> None:
    """Restore the stash made by pull(). If that fails, leave the stash for the user and say so."""
    p = git(root, "stash", "pop", "--quiet", check=False)
    if p.returncode == 0:
        return
    # a conflict leaves markers in the files and unmerged entries in the index: put the files back to HEAD so
    # nothing half-merged is staged or committed later; the stash entry itself stays
    pathspecs = [f":(literal){x}" for x in sorted(paths)]
    git(root, "reset", "--quiet", "--", *pathspecs, check=False)
    git(root, "checkout", "--", *pathspecs, check=False)
    note = f"git stash pop: {(p.stderr or p.stdout).strip()}; your local changes to {len(paths)} held-back file(s) " \
           f"are still in the stash (see `git stash list`; restore with `git stash pop`)"
    if pull_error is not None:
        note += f"; the pull itself failed too: {pull_error}"
    raise GitError(note)


def pull(root, keep=()) -> None:
    """Rebase onto the upstream. Refuses when tracked files have local changes; a failed rebase is aborted.

    `keep` lists files (exact repo-relative paths) that may stay modified, such as quarantined files that were held
    back from a commit. If they are the only tracked files with changes, they are stashed for the pull and restored
    after it, whether or not it worked. If the stash cannot be restored, GitError says so and the stash is left.
    """
    dirty = _dirty_tracked(root)
    if dirty - set(keep):
        raise GitError("local changes in tracked files; not pulling")
    stashed = False
    if dirty:
        before = git(root, "rev-parse", "-q", "--verify", "refs/stash", check=False).stdout.strip()
        git(root, "stash", "push", "--quiet", "-m", "kb sync: held-back files during pull",
            "--", *[f":(literal){x}" for x in sorted(dirty)])
        stashed = git(root, "rev-parse", "-q", "--verify", "refs/stash", check=False).stdout.strip() != before
    error = None
    try:
        p = git(root, "pull", "--rebase", "--quiet", check=False, timeout=PULL_TIMEOUT)
        if p.returncode != 0:
            try:
                git(root, "rebase", "--abort", check=False)
            except GitError:
                pass
            error = GitError(f"git pull --rebase: {(p.stderr or p.stdout).strip()}")
    except GitError as e:                  # a timeout
        error = e
    if stashed:
        _put_back(root, dirty, error)
    if error is not None:
        raise error


def repair(root) -> str:
    """Abort a rebase left half-done by an earlier run. Returns an error text if the repo is still unfit to sync."""
    try:
        stuck = [Path(root) / git(root, "rev-parse", "--git-path", name).stdout.strip()
                 for name in ("rebase-merge", "rebase-apply")]
        if any(p.exists() for p in stuck):
            git(root, "rebase", "--abort", check=False)
            if any(p.exists() for p in stuck):
                return "a rebase is stuck and could not be aborted"
        if git(root, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0:
            return "HEAD is detached (not on a branch)"
    except GitError as e:
        return str(e)
    return ""


def _known(root, path: str) -> bool:
    """True if git has something at `path`: a tracked file or a staged change (a staged deletion too)."""
    return bool(git(root, "ls-files", "--", path, check=False).stdout.strip()
                or git(root, "diff", "--cached", "--name-only", "--", path, check=False).stdout.strip())


def stage(root, paths) -> bool:
    """Stage the given paths (deletions included). True if those paths now have staged changes."""
    existing = [p for p in paths if (Path(root) / p).exists()]
    tracked = [p for p in paths if p not in existing and git(root, "ls-files", "--", p, check=False).stdout.strip()]
    targets = existing + tracked
    if not targets:
        return False
    git(root, "add", "-A", "--", *targets)
    rc = git(root, "diff", "--cached", "--quiet", "--", *targets, check=False).returncode
    if rc not in (0, 1):
        raise GitError(f"git diff --cached --quiet: exit {rc}")
    return rc == 1


def unstage(root, paths) -> None:
    """Remove the given paths from the index (the working tree is left alone)."""
    paths = list(paths)
    if paths:
        git(root, "reset", "--quiet", "--", *paths)


def commit(root, message: str, paths) -> None:
    """Commit only `paths`, whatever else is staged."""
    targets = [p for p in paths if _known(root, p)]
    if not targets:
        raise GitError("nothing to commit in the given paths")
    git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify", "-m", message, "--", *targets)


def ahead(root) -> int:
    p = git(root, "rev-list", "--count", "@{u}..HEAD", check=False)
    try:
        return int(p.stdout.strip()) if p.returncode == 0 else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------- secrets

def _report_files(report: Path):
    """File names of the findings in a gitleaks JSON report, or None if there is no usable report."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [str(f.get("File") or "(unknown file)") if isinstance(f, dict) else "(unknown file)" for f in data]


def _gitleaks(exe, args, fallback, unknown, report: Path):
    """Run gitleaks with a JSON report. `fallback` is used only if this gitleaks does not know the command.

    Returns (files with findings, error text). An exit without findings is an error: not a clean scan.
    """
    p = None
    for cmd in (args, fallback):
        if report.exists():
            report.unlink()
        p = _run([exe, *cmd], GITLEAKS_TIMEOUT, "gitleaks")
        if p.returncode == 0 or unknown not in p.stdout + p.stderr:
            break
    files = _report_files(report) or []
    if files:
        return files, ""
    if p.returncode == 0:
        return [], ""
    return [], (p.stdout + p.stderr).strip()[-500:] or f"gitleaks exit {p.returncode}"


def _staged_raw(root) -> list:
    out = git(root, "diff", "--cached", "-z", "--name-only", "--diff-filter=AM").stdout
    return [p for p in out.split("\0") if p.endswith(".jsonl.gz")]


def _map_raw(found: str, tmp: str, rels: dict) -> str:
    """Map a path reported for the decompressed copy back to the staged .gz path."""
    f = found
    for base in (tmp, os.path.realpath(tmp)):
        prefix = base.rstrip("/") + "/"
        if f.startswith(prefix):
            f = f[len(prefix):]
            break
    if f in rels:
        return rels[f]
    best = max((r for r in rels if f.endswith("/" + r)), key=len, default=None)
    return rels[best] if best else found


def _scan_raw(root, exe, raw_paths, tmpdir: Path):
    """Decompress staged raw files into a temp tree and scan it. Returns (files with findings, errors)."""
    tree, rels, errors = tmpdir / "tree", {}, []
    for rel in raw_paths:
        dest = tree / rel[:-3]
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(str(Path(root) / rel), "rb") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        except (OSError, EOFError, ValueError) as e:
            errors.append(f"cannot read {rel}: {type(e).__name__}: {e}")
            continue
        rels[rel[:-3]] = rel
    if not rels:
        return [], errors
    cfg = Path(root) / ".gitleaks.toml"
    extra = ["--config", str(cfg)] if cfg.exists() else []
    flags = ["--redact", "--no-banner", "--report-format", "json", "--report-path", str(tmpdir / "raw-report.json"), *extra]
    found, err = _gitleaks(exe, ["dir", *flags, str(tree)], ["detect", "--no-git", *flags, "--source", str(tree)],
                           'unknown command "dir"', tmpdir / "raw-report.json")
    if err:
        errors.append(err)
    return [_map_raw(f, str(tree), rels) for f in found], errors


def secrets_check(root) -> SecretsResult:
    """Scan what is staged (and the decompressed staged raw files) with gitleaks, if it is installed."""
    exe = shutil.which("gitleaks")
    if not exe:
        return SecretsResult(ran=False)
    result = SecretsResult(ran=True)
    errors = []
    try:
        with tempfile.TemporaryDirectory(prefix="kb-gitleaks-") as td:
            tmpdir = Path(td)
            report = tmpdir / "staged-report.json"
            flags = ["--redact", "--no-banner", "--report-format", "json", "--report-path", str(report)]
            files, err = _gitleaks(exe, ["git", "--staged", *flags, str(root)],
                                   ["protect", "--staged", *flags, "--source", str(root)],
                                   'unknown command "git"', report)
            result.files.extend(files)
            if err:
                errors.append(err)
            raw_paths = _staged_raw(root)
            if raw_paths:
                files, errs = _scan_raw(root, exe, raw_paths, tmpdir)
                result.files.extend(files)
                errors.extend(errs)
    except GitError as e:
        errors.append(str(e))
    result.files = sorted(set(result.files))
    result.error = "; ".join(errors)
    return result


_RETRYABLE = ("[rejected]", "fetch first", "non-fast-forward")


def push(root, tries: int = 3, keep=()) -> None:
    """Push without hooks. Sets the upstream on first use. Rebases and retries only when the remote moved;
    any other failure (auth, server hook, network) raises at once with every message collected."""
    has_upstream = git(root, "rev-parse", "--abbrev-ref", "@{u}", check=False).returncode == 0
    args = ["push", "--quiet", "--no-verify"] + ([] if has_upstream else ["-u", "origin", "HEAD"])
    notes = []
    for _ in range(tries):
        p = git(root, *args, check=False, timeout=PUSH_TIMEOUT)
        if p.returncode == 0:
            return
        err = (p.stderr or p.stdout).strip()
        notes.append(f"push: {err}")
        if not any(marker in err for marker in _RETRYABLE):
            break
        try:
            pull(root, keep=keep)
        except GitError as e:
            notes.append(str(e))
            break
    raise GitError("push failed: " + " | ".join(notes))
