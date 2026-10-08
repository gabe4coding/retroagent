import gzip
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fixtures import clone, init_remote
from fixtures import git as fgit

from kb import gitops

pytestmark = pytest.mark.slow          # starts git and other processes; runs with scripts/test --all


def _add(repo, host, text, name="x.md"):
    d = repo / "sessions" / host
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def _staged(repo):
    return sorted(gitops.git(repo, "diff", "--cached", "--name-only").stdout.split())


def _no_proxy(monkeypatch):
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def _diverge(tmp_path):
    """Two clones that changed README.md differently; `a` pushed first, so `b` conflicts when it pulls."""
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    for repo, text in ((a, "from a\n"), (b, "from b\n")):
        (repo / "README.md").write_text(text)
        fgit("commit", "-q", "-am", text.strip(), cwd=repo)
    gitops.push(a)
    return a, b


# ---------------------------------------------------------------- test isolation

def test_tests_never_see_the_user_home_or_git_config(tmp_path):
    assert os.environ["HOME"] == str(tmp_path / "home") and (tmp_path / "home").is_dir()
    assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null" and os.environ["GIT_CONFIG_NOSYSTEM"] == "1"


def test_fixture_git_failure_names_the_command_and_stderr(tmp_path):
    with pytest.raises(RuntimeError) as e:
        fgit("no-such-subcommand", cwd=tmp_path)
    assert "no-such-subcommand" in str(e.value) and "is not a git command" in str(e.value)


# ---------------------------------------------------------------- basic flow

def test_stage_commit_push(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    assert gitops.has_remote(a)
    _add(a, "h", "x")
    paths = ["sessions/h", "raw/h"]
    assert gitops.stage(a, paths) is True
    gitops.commit(a, "m", paths)
    assert gitops.ahead(a) == 1
    gitops.push(a)
    assert gitops.ahead(a) == 0
    assert gitops.stage(a, ["sessions/h"]) is False


def test_push_rebases_when_remote_moved(tmp_path):
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    for repo, host in ((a, "ha"), (b, "hb")):
        _add(repo, host, host)
        gitops.stage(repo, [f"sessions/{host}"])
        gitops.commit(repo, host, [f"sessions/{host}"])
    gitops.push(a)
    gitops.push(b)
    gitops.pull(a)
    assert (a / "sessions/hb/x.md").read_text() == "hb"


# ---------------------------------------------------------------- non-interactive git

def test_git_runs_with_an_editor_that_does_nothing(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.setenv("EDITOR", "vi")
    monkeypatch.setenv("VISUAL", "vi")
    assert gitops.git(a, "var", "GIT_EDITOR").stdout.strip() == "true"


def _fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "sshbin"
    bindir.mkdir()
    log = tmp_path / "ssh.log"
    script = bindir / "ssh"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 255\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return log


def _ssh_args_seen(a, log):
    gitops.git(a, "ls-remote", "ssh://ssh-host.invalid/x.git", check=False)
    return log.read_text()


def test_ssh_never_asks_questions(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    log = _fake_ssh(tmp_path, monkeypatch)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    assert "-o BatchMode=yes" in _ssh_args_seen(a, log)


def test_ssh_keeps_the_users_ssh_command(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    log = _fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /my/key")
    seen = _ssh_args_seen(a, log)
    assert "-i /my/key" in seen and "-o BatchMode=yes" in seen


def test_ssh_keeps_core_ssh_command_from_git_config(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    log = _fake_ssh(tmp_path, monkeypatch)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    fgit("config", "core.sshCommand", "ssh -F /my/ssh_config", cwd=a)
    seen = _ssh_args_seen(a, log)
    assert "-F /my/ssh_config" in seen and "-o BatchMode=yes" in seen


def test_git_gives_up_on_a_server_that_never_answers(tmp_path, monkeypatch):
    _no_proxy(monkeypatch)
    a = clone(init_remote(tmp_path), tmp_path / "a")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
    start = time.time()
    try:
        with pytest.raises(gitops.GitError, match="timed out"):
            gitops.git(a, "ls-remote", f"http://127.0.0.1:{srv.getsockname()[1]}/x.git", timeout=2)
    finally:
        srv.close()
        for conn, _ in held:
            conn.close()
    assert time.time() - start < 20


def test_pull_from_an_unreachable_http_remote_fails_fast(tmp_path, monkeypatch):
    _no_proxy(monkeypatch)
    a = clone(init_remote(tmp_path), tmp_path / "a")
    fgit("remote", "set-url", "origin", "http://127.0.0.1:9/x.git", cwd=a)
    start = time.time()
    with pytest.raises(gitops.GitError):
        gitops.pull(a)
    assert time.time() - start < 30


# ---------------------------------------------------------------- stage / commit / unstage

def test_commit_includes_only_the_given_paths_even_if_other_files_are_staged(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "mine")
    (a / "other.txt").write_text("not mine")
    fgit("add", "other.txt", cwd=a)
    paths = ["sessions/h", "raw/h", "catalog/h"]
    assert gitops.stage(a, paths) is True
    gitops.commit(a, "sync", paths)
    assert fgit("show", "--name-only", "--format=", "HEAD", cwd=a).stdout.split() == ["sessions/h/x.md"]
    assert _staged(a) == ["other.txt"]


def test_commit_ignores_empty_and_missing_paths(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "mine")
    (a / "raw" / "h" / "empty").mkdir(parents=True)  # exists, but git knows nothing in it
    paths = ["sessions/h", "raw/h", "catalog/h"]
    assert gitops.stage(a, paths) is True
    gitops.commit(a, "sync", paths)
    assert gitops.ahead(a) == 1


def test_commit_records_deletions(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "mine")
    gitops.stage(a, ["sessions/h"])
    gitops.commit(a, "one", ["sessions/h"])
    (a / "sessions/h/x.md").unlink()
    assert gitops.stage(a, ["sessions/h"]) is True
    gitops.commit(a, "two", ["sessions/h"])
    assert fgit("show", "--name-status", "--format=", "HEAD", cwd=a).stdout.split() == ["D", "sessions/h/x.md"]


def test_commit_without_changes_in_the_paths_raises_and_leaves_other_staged_files(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "mine")
    gitops.stage(a, ["sessions/h"])
    gitops.commit(a, "one", ["sessions/h"])
    (a / "other.txt").write_text("x")
    fgit("add", "other.txt", cwd=a)
    with pytest.raises(gitops.GitError):
        gitops.commit(a, "nothing", ["sessions/h"])
    with pytest.raises(gitops.GitError):
        gitops.commit(a, "no paths", [])
    assert _staged(a) == ["other.txt"] and gitops.ahead(a) == 1


def test_commit_does_not_sign(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    fgit("config", "commit.gpgsign", "true", cwd=a)
    fgit("config", "gpg.program", "/nonexistent/gpg", cwd=a)
    _add(a, "h", "mine")
    gitops.stage(a, ["sessions/h"])
    gitops.commit(a, "m", ["sessions/h"])
    assert gitops.ahead(a) == 1


def test_stage_is_false_when_only_an_unrelated_file_is_staged(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    (a / "other.txt").write_text("x")
    fgit("add", "other.txt", cwd=a)
    _add(a, "h", "mine")
    assert gitops.stage(a, ["sessions/h"]) is True
    fgit("reset", "-q", "sessions/h", cwd=a)
    (a / "sessions/h/x.md").unlink()
    assert gitops.stage(a, ["sessions/h", "raw/h"]) is False
    assert _staged(a) == ["other.txt"]


def test_unstage_resets_only_the_given_paths(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    (a / "other.txt").write_text("x")
    fgit("add", "other.txt", cwd=a)
    _add(a, "h", "mine")
    gitops.stage(a, ["sessions/h"])
    assert _staged(a) == ["other.txt", "sessions/h/x.md"]
    gitops.unstage(a, ["sessions/h", "raw/h"])
    assert _staged(a) == ["other.txt"]
    gitops.unstage(a, [])
    assert _staged(a) == ["other.txt"]


# ---------------------------------------------------------------- pull / repair

def test_pull_refuses_when_a_tracked_file_has_local_changes(tmp_path):
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    _add(b, "hb", "b")
    gitops.stage(b, ["sessions/hb"])
    gitops.commit(b, "b", ["sessions/hb"])
    gitops.push(b)
    (a / "README.md").write_text("edited locally\n")
    with pytest.raises(gitops.GitError, match="local changes in tracked files; not pulling"):
        gitops.pull(a)
    assert (a / "README.md").read_text() == "edited locally\n" and not (a / "sessions/hb").exists()


def test_pull_works_with_untracked_files_present(tmp_path):
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    _add(b, "hb", "b")
    gitops.stage(b, ["sessions/hb"])
    gitops.commit(b, "b", ["sessions/hb"])
    gitops.push(b)
    (a / "scratch.txt").write_text("untracked")
    gitops.pull(a)
    assert (a / "sessions/hb/x.md").read_text() == "b"


def test_pull_rebase_conflict_is_aborted_and_raises(tmp_path):
    _, b = _diverge(tmp_path)
    with pytest.raises(gitops.GitError):
        gitops.pull(b)
    assert not (b / ".git" / "rebase-merge").exists() and not (b / ".git" / "rebase-apply").exists()
    assert (b / "README.md").read_text() == "from b\n"
    assert fgit("symbolic-ref", "-q", "HEAD", cwd=b).stdout.strip() == "refs/heads/main"


def _stuck_rebase(tmp_path):
    _, b = _diverge(tmp_path)
    p = subprocess.run(["git", "-C", str(b), "pull", "--rebase", "--quiet"], capture_output=True, text=True)
    assert p.returncode != 0 and (b / ".git" / "rebase-merge").exists()
    return b


def test_repair_aborts_a_stuck_rebase(tmp_path):
    b = _stuck_rebase(tmp_path)
    assert gitops.repair(b) == ""
    assert not (b / ".git" / "rebase-merge").exists()
    assert (b / "README.md").read_text() == "from b\n"


def test_repair_is_quiet_on_a_healthy_repo_and_reports_a_detached_head(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    assert gitops.repair(a) == ""
    fgit("checkout", "-q", "--detach", cwd=a)
    msg = gitops.repair(a)
    assert msg and "detached" in msg


def test_repair_reports_a_path_that_is_not_a_repo(tmp_path):
    assert gitops.repair(tmp_path / "nowhere")


# ---------------------------------------------------------------- secrets check

def _fake_gitleaks(tmp_path, monkeypatch, body):
    """A gitleaks stand-in. The script gets $cmd, $report (--report-path) and $src (last argument)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gitleaks"
    script.write_text(
        "#!/bin/sh\n"
        'cmd="$1"; report=""; prev=""; src=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--report-path" ]; then report="$a"; fi\n'
        '  if [ "$prev" = "--source" ]; then src="$a"; fi\n'
        '  prev="$a"; last="$a"\n'
        "done\n"
        '[ -z "$src" ] && src="$last"\n' + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return script


def _stage_raw(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(text)
    fgit("add", rel, cwd=repo)


def test_secrets_check_reports_the_file_of_a_finding(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    _add(a, "h", "y", name="y.md")
    gitops.stage(a, ["sessions/h"])
    _fake_gitleaks(tmp_path, monkeypatch, (
        'if [ "$cmd" = git ]; then\n'
        '  echo \'[{"RuleID":"r","File":"sessions/h/x.md"}]\' > "$report"; echo leaks found >&2; exit 1\n'
        "fi\n"
        'echo "[]" > "$report"; exit 0\n'))
    r = gitops.secrets_check(a)
    assert r.ran is True and r.error == "" and r.files == ["sessions/h/x.md"]


def test_secrets_check_clean(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    _fake_gitleaks(tmp_path, monkeypatch, 'echo "[]" > "$report"; exit 0\n')
    r = gitops.secrets_check(a)
    assert r.ran is True and r.error == "" and r.files == []


def test_secrets_check_finds_a_secret_inside_a_staged_raw_file(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "clean")
    _stage_raw(a, "raw/h/2026-10/leaky.jsonl.gz", '{"text": "SECRETVALUE"}\n')
    _stage_raw(a, "raw/h/2026-10/fine.jsonl.gz", '{"text": "nothing"}\n')
    gitops.stage(a, ["sessions/h", "raw/h"])
    _fake_gitleaks(tmp_path, monkeypatch, (
        'if [ "$cmd" = dir ]; then\n'
        '  hit=$(grep -rl SECRETVALUE "$src")\n'
        '  if [ -n "$hit" ]; then echo "[{\\"File\\":\\"$hit\\"}]" > "$report"; exit 1; fi\n'
        "fi\n"
        'echo "[]" > "$report"; exit 0\n'))
    r = gitops.secrets_check(a)
    assert r.ran is True and r.error == "" and r.files == ["raw/h/2026-10/leaky.jsonl.gz"]


def test_secrets_check_maps_relative_and_resolved_raw_paths(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _stage_raw(a, "raw/h/2026-10/leaky.jsonl.gz", "SECRETVALUE\n")
    gitops.stage(a, ["raw/h"])
    _fake_gitleaks(tmp_path, monkeypatch, (
        'if [ "$cmd" = dir ]; then\n'
        '  real=$(cd "$src" && pwd -P)\n'
        '  echo "[{\\"File\\":\\"$real/raw/h/2026-10/leaky.jsonl\\"}]" > "$report"; exit 1\n'
        "fi\n"
        'echo "[]" > "$report"; exit 0\n'))
    assert gitops.secrets_check(a).files == ["raw/h/2026-10/leaky.jsonl.gz"]
    _fake_gitleaks(tmp_path, monkeypatch, (
        'if [ "$cmd" = dir ]; then\n'
        '  echo \'[{"File":"raw/h/2026-10/leaky.jsonl"}]\' > "$report"; exit 1\n'
        "fi\n"
        'echo "[]" > "$report"; exit 0\n'))
    assert gitops.secrets_check(a).files == ["raw/h/2026-10/leaky.jsonl.gz"]


def test_secrets_check_merges_findings_of_both_scans(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    _stage_raw(a, "raw/h/2026-10/leaky.jsonl.gz", "SECRETVALUE\n")
    gitops.stage(a, ["sessions/h", "raw/h"])
    _fake_gitleaks(tmp_path, monkeypatch, (
        'if [ "$cmd" = dir ]; then\n'
        '  echo \'[{"File":"raw/h/2026-10/leaky.jsonl"}]\' > "$report"; exit 1\n'
        "fi\n"
        'echo \'[{"File":"sessions/h/x.md"}]\' > "$report"; exit 1\n'))
    assert gitops.secrets_check(a).files == ["raw/h/2026-10/leaky.jsonl.gz", "sessions/h/x.md"]


def test_secrets_check_error_without_a_report_is_not_a_finding(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    _fake_gitleaks(tmp_path, monkeypatch, 'echo "Error: failed to load config: bad toml" >&2; exit 1\n')
    r = gitops.secrets_check(a)
    assert r.ran is True and r.files == [] and "bad toml" in r.error


def test_secrets_check_error_with_an_unreadable_report(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    _fake_gitleaks(tmp_path, monkeypatch, 'echo "not json" > "$report"; echo boom >&2; exit 1\n')
    r = gitops.secrets_check(a)
    assert r.ran is True and r.files == [] and "boom" in r.error


def test_secrets_check_without_gitleaks_installed(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    r = gitops.secrets_check(tmp_path)
    assert r.ran is False and r.error == "" and r.files == []


def test_secrets_check_falls_back_to_the_old_gitleaks_commands(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    _stage_raw(a, "raw/h/2026-10/leaky.jsonl.gz", "SECRETVALUE\n")
    gitops.stage(a, ["sessions/h", "raw/h"])
    _fake_gitleaks(tmp_path, monkeypatch, (
        'case "$cmd" in\n'
        '  git|dir) echo "Error: unknown command \\"$cmd\\" for \\"gitleaks\\"" >&2; exit 1;;\n'
        '  protect) echo \'[{"File":"sessions/h/x.md"}]\' > "$report"; exit 1;;\n'
        '  detect) echo \'[{"File":"raw/h/2026-10/leaky.jsonl"}]\' > "$report"; exit 1;;\n'
        "esac\n"
        "exit 2\n"))
    r = gitops.secrets_check(a)
    assert r.error == "" and r.files == ["raw/h/2026-10/leaky.jsonl.gz", "sessions/h/x.md"]


def test_secrets_check_runs_gitleaks_non_interactively(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    seen = tmp_path / "seen.txt"
    _fake_gitleaks(tmp_path, monkeypatch, (
        f'echo "$GIT_TERMINAL_PROMPT $GIT_EDITOR $GIT_SSH_COMMAND" > "{seen}"\n'
        'echo "[]" > "$report"; exit 0\n'))
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    gitops.secrets_check(a)
    assert seen.read_text().strip() == "0 true ssh -o BatchMode=yes"


def test_secrets_check_with_an_unreadable_staged_raw_file_is_an_error(tmp_path, monkeypatch):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    bad = a / "raw/h/2026-10/bad.jsonl.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not gzip")
    fgit("add", "raw/h", cwd=a)
    _fake_gitleaks(tmp_path, monkeypatch, 'echo "[]" > "$report"; exit 0\n')
    r = gitops.secrets_check(a)
    assert r.ran is True and r.files == [] and "bad.jsonl.gz" in r.error


def test_push_sets_the_upstream_when_missing(tmp_path):
    remote = tmp_path / "remote.git"
    fgit("init", "--bare", "-q", "-b", "main", str(remote))
    repo = tmp_path / "repo"
    fgit("init", "-q", "-b", "main", str(repo))
    _add(repo, "h", "x")
    fgit("add", ".", cwd=repo)
    fgit("commit", "-q", "-m", "first", cwd=repo)
    fgit("remote", "add", "origin", str(remote), cwd=repo)
    gitops.push(repo)
    assert gitops.git(repo, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip() == "origin/main"
    assert gitops.ahead(repo) == 0


def test_push_does_not_retry_when_a_server_hook_declines(tmp_path):
    remote = init_remote(tmp_path)
    log = tmp_path / "hook.log"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho call >> '{log}'\necho 'denied by policy' >&2\nexit 1\n")
    hook.chmod(0o755)
    a = clone(remote, tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    gitops.commit(a, "m", ["sessions/h"])
    with pytest.raises(gitops.GitError) as e:
        gitops.push(a)
    assert "denied by policy" in str(e.value)
    assert log.read_text().splitlines() == ["call"]


# ---------------------------------------------------------------- pull with quarantined (kept) files

def _tracked_pair(tmp_path):
    """Two clones of a remote that holds sessions/ha/x.md. Returns (a, b)."""
    remote = init_remote(tmp_path)
    a = clone(remote, tmp_path / "a")
    _add(a, "ha", "committed\n")
    gitops.stage(a, ["sessions/ha"])
    gitops.commit(a, "a", ["sessions/ha"])
    gitops.push(a)
    return a, clone(remote, tmp_path / "b")


def _remote_commit(repo, host="hb", text="b"):
    _add(repo, host, text)
    gitops.stage(repo, [f"sessions/{host}"])
    gitops.commit(repo, host, [f"sessions/{host}"])
    gitops.push(repo)


KEPT = "sessions/ha/x.md"


def test_pull_with_only_kept_files_dirty_stashes_pulls_and_restores_them(tmp_path):
    a, b = _tracked_pair(tmp_path)
    _remote_commit(b)
    (a / KEPT).write_text("quarantined local edit\n")
    gitops.pull(a, keep=(KEPT,))
    assert (a / "sessions/hb/x.md").read_text() == "b"                      # the pull happened
    assert (a / KEPT).read_text() == "quarantined local edit\n"             # and the local edit is still there
    assert fgit("status", "--porcelain", cwd=a).stdout == f" M {KEPT}\n"
    assert fgit("stash", "list", cwd=a).stdout == ""


def test_pull_still_refuses_when_another_tracked_file_is_dirty(tmp_path):
    a, b = _tracked_pair(tmp_path)
    _remote_commit(b)
    (a / KEPT).write_text("quarantined local edit\n")
    (a / "README.md").write_text("edited locally\n")
    with pytest.raises(gitops.GitError, match="local changes in tracked files; not pulling"):
        gitops.pull(a, keep=(KEPT,))
    assert (a / KEPT).read_text() == "quarantined local edit\n" and (a / "README.md").read_text() == "edited locally\n"
    assert not (a / "sessions/hb").exists() and fgit("stash", "list", cwd=a).stdout == ""


def test_kept_files_are_exact_paths_not_folders_or_patterns(tmp_path):
    a, b = _tracked_pair(tmp_path)
    _remote_commit(b)
    (a / KEPT).write_text("local edit\n")
    for keep in ((), ("sessions/ha",), ("sessions/ha/*.md",), ("sessions/ha/x.md/",), ("x.md",)):
        with pytest.raises(gitops.GitError, match="local changes in tracked files; not pulling"):
            gitops.pull(a, keep=keep)
    assert (a / KEPT).read_text() == "local edit\n" and fgit("stash", "list", cwd=a).stdout == ""


def test_pull_with_kept_files_does_not_touch_a_stash_the_user_already_has(tmp_path):
    a, b = _tracked_pair(tmp_path)
    (a / "README.md").write_text("user work in progress\n")
    fgit("stash", "push", "-q", "-m", "mine", cwd=a)
    _remote_commit(b)
    gitops.pull(a, keep=(KEPT,))                                          # clean tree: nothing to stash, nothing to pop
    assert "mine" in fgit("stash", "list", cwd=a).stdout and len(fgit("stash", "list", cwd=a).stdout.splitlines()) == 1
    (a / KEPT).write_text("local edit\n")
    _remote_commit(b, "hc", "c")
    gitops.pull(a, keep=(KEPT,))
    assert (a / KEPT).read_text() == "local edit\n" and (a / "sessions/hc/x.md").exists()
    assert len(fgit("stash", "list", cwd=a).stdout.splitlines()) == 1 and "mine" in fgit("stash", "list", cwd=a).stdout


def test_a_failed_pull_gives_the_kept_files_back(tmp_path):
    a, b = _tracked_pair(tmp_path)
    (b / "README.md").write_text("from b\n")
    fgit("commit", "-q", "-am", "b readme", cwd=b)
    gitops.push(b)
    (a / "README.md").write_text("from a\n")
    fgit("commit", "-q", "-am", "a readme", cwd=a)                         # a diverging commit: the rebase conflicts
    (a / KEPT).write_text("local edit\n")
    with pytest.raises(gitops.GitError, match="git pull --rebase"):
        gitops.pull(a, keep=(KEPT,))
    assert (a / KEPT).read_text() == "local edit\n" and fgit("stash", "list", cwd=a).stdout == ""
    assert not (a / ".git" / "rebase-merge").exists() and not (a / ".git" / "rebase-apply").exists()


def test_a_stash_that_cannot_be_restored_is_left_for_the_user(tmp_path):
    a, b = _tracked_pair(tmp_path)
    (b / KEPT).write_text("remote edit\n")                                 # the remote changes the same file
    fgit("commit", "-q", "-am", "b edit", cwd=b)
    gitops.push(b)
    (a / KEPT).write_text("local edit\n")
    with pytest.raises(gitops.GitError, match="stash") as e:
        gitops.pull(a, keep=(KEPT,))
    assert "git stash" in str(e.value)
    assert len(fgit("stash", "list", cwd=a).stdout.splitlines()) == 1        # nothing lost: it is still in the stash
    assert fgit("stash", "show", "-p", "stash@{0}", cwd=a).stdout.count("local edit") == 1
    assert fgit("status", "--porcelain", cwd=a).stdout == ""                 # and no conflict markers are left to commit
    assert (a / KEPT).read_text() == "remote edit\n"


def test_push_retry_keeps_quarantined_files_out_of_the_way(tmp_path):
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    _add(b, "hb", "v1", name="q.md")
    gitops.stage(b, ["sessions/hb"])
    gitops.commit(b, "q", ["sessions/hb"])
    gitops.push(b)
    gitops.pull(a)
    _add(a, "ha", "a")
    gitops.stage(a, ["sessions/ha"])
    gitops.commit(a, "a", ["sessions/ha"])
    gitops.push(a)
    (b / "sessions/hb/q.md").write_text("held back")            # quarantined: tracked and modified
    _add(b, "hb", "y", name="y.md")
    gitops.stage(b, ["sessions/hb/y.md"])
    gitops.commit(b, "y", ["sessions/hb/y.md"])
    gitops.push(b, keep=["sessions/hb/q.md"])                   # rejected first, then pull(keep) + push
    assert (b / "sessions/hb/q.md").read_text() == "held back"
    assert gitops.git(remote, "show", "main:sessions/hb/q.md").stdout == "v1"
    assert gitops.git(remote, "show", "main:sessions/hb/y.md").stdout == "y"


def test_push_has_a_long_timeout():
    assert gitops.PUSH_TIMEOUT >= 30 * 60


# ---------------------------------------------------------------- item 3: finding gitleaks

def _gitleaks_script(directory, name="gitleaks", body='echo "[]" > "$report"; exit 0\n'):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text('#!/bin/sh\nreport=""; prev=""\nfor a in "$@"; do\n  if [ "$prev" = "--report-path" ]; then report="$a"; fi\n'
                      '  prev="$a"\ndone\n' + body)
    script.chmod(0o755)
    return script


def _path_with_only_git(tmp_path, monkeypatch):
    """PATH holds git and nothing else: no gitleaks, but the scan can still run git."""
    only_git = tmp_path / "only-git"
    only_git.mkdir(exist_ok=True)
    if not (only_git / "git").exists():
        (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))


def _staged_repo(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    _add(a, "h", "x")
    gitops.stage(a, ["sessions/h"])
    return a


def test_find_gitleaks_order_is_given_path_then_path_then_fallbacks(tmp_path, monkeypatch):
    given = _gitleaks_script(tmp_path / "given")
    on_path = _gitleaks_script(tmp_path / "onpath")
    fallback = _gitleaks_script(tmp_path / "fallback")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setattr(gitops, "GITLEAKS_FALLBACKS", (str(tmp_path / "missing" / "gitleaks"), str(fallback)))
    assert gitops.find_gitleaks(str(given)) == str(given)
    assert gitops.find_gitleaks(None) == str(on_path)
    assert gitops.find_gitleaks("") == str(on_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert gitops.find_gitleaks(None) == str(fallback)
    monkeypatch.setattr(gitops, "GITLEAKS_FALLBACKS", ())
    assert gitops.find_gitleaks(None) is None
    from kb.gitleaks_fetch import binary_path
    downloaded = _gitleaks_script(binary_path().parent)       # last: the one `kb setup gitleaks` downloaded
    assert gitops.find_gitleaks(None) == str(downloaded)


def test_a_given_path_that_is_not_usable_falls_through(tmp_path, monkeypatch):
    on_path = _gitleaks_script(tmp_path / "onpath")
    monkeypatch.setenv("PATH", str(on_path.parent))
    plain = tmp_path / "not-executable"
    plain.write_text("x")
    for bad in (str(tmp_path / "nowhere" / "gitleaks"), str(plain), str(tmp_path)):      # missing, not executable, a folder
        assert gitops.find_gitleaks(bad) == str(on_path)


def test_the_default_fallbacks_are_the_homebrew_and_usr_local_locations():
    import importlib
    fresh = importlib.reload(gitops)           # conftest empties the tuple; the shipped value is what counts
    try:
        assert fresh.GITLEAKS_FALLBACKS == ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks")
    finally:
        fresh.GITLEAKS_FALLBACKS = ()


def test_secrets_check_uses_the_given_path_with_an_empty_path(tmp_path, monkeypatch):
    a = _staged_repo(tmp_path)
    _path_with_only_git(tmp_path, monkeypatch)
    hit = _gitleaks_script(tmp_path / "elsewhere", body='echo \'[{"File":"sessions/h/x.md"}]\' > "$report"; exit 1\n')
    r = gitops.secrets_check(a, exe=str(hit))
    assert r.ran is True and r.files == ["sessions/h/x.md"]
    assert gitops.secrets_check(a).ran is False                                   # without the path: no scanner


def test_secrets_check_prefers_the_given_path_over_path(tmp_path, monkeypatch):
    a = _staged_repo(tmp_path)
    on_path = _gitleaks_script(tmp_path / "onpath", body='echo path-version >&2; echo \'[]\' > "$report"; exit 2\n')
    monkeypatch.setenv("PATH", f"{on_path.parent}:{os.environ['PATH']}")
    given = _gitleaks_script(tmp_path / "given")
    r = gitops.secrets_check(a, exe=str(given))
    assert r.ran is True and r.error == "" and r.files == []


def test_secrets_check_finds_gitleaks_in_a_fallback_location(tmp_path, monkeypatch):
    a = _staged_repo(tmp_path)
    fallback = _gitleaks_script(tmp_path / "usr-local-bin")
    _path_with_only_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gitops, "GITLEAKS_FALLBACKS", (str(fallback),))
    r = gitops.secrets_check(a)
    assert r.ran is True and r.error == "" and r.files == []


# ---------------------------------------------------------------- item 4: branch, unpushed paths, pull message

def test_current_branch(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    assert gitops.current_branch(a) == "main"
    fgit("checkout", "-q", "-b", "feature/x", cwd=a)
    assert gitops.current_branch(a) == "feature/x"
    fgit("checkout", "-q", "--detach", cwd=a)
    assert gitops.current_branch(a) == ""                                      # detached: no branch name


def test_unpushed_paths_is_empty_without_an_upstream(tmp_path):
    repo = tmp_path / "solo"
    fgit("init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("a")
    fgit("add", ".", cwd=repo)
    fgit("commit", "-q", "-m", "a", cwd=repo)
    assert gitops.unpushed_paths(repo) == []
    assert gitops.unpushed_paths(tmp_path / "nowhere") == []


def test_unpushed_paths_lists_files_of_commits_not_on_the_upstream(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    assert gitops.unpushed_paths(a) == []
    _add(a, "h", "x")
    _add(a, "h", "y", name="odd name.md")
    gitops.stage(a, ["sessions/h"])
    gitops.commit(a, "own", ["sessions/h"])
    (a / "README.md").write_text("edited\n")
    fgit("commit", "-q", "-am", "readme", cwd=a)
    (a / "README.md").write_text("edited again\n")
    fgit("commit", "-q", "-am", "readme 2", cwd=a)                              # the same path twice: listed once
    assert gitops.unpushed_paths(a) == ["README.md", "sessions/h/odd name.md", "sessions/h/x.md"]
    gitops.push(a)
    assert gitops.unpushed_paths(a) == []


def test_unpushed_paths_includes_deleted_files_and_ignores_what_upstream_has_that_we_lack(tmp_path):
    remote = init_remote(tmp_path)
    a, b = clone(remote, tmp_path / "a"), clone(remote, tmp_path / "b")
    _add(b, "hb", "b")
    gitops.stage(b, ["sessions/hb"])
    gitops.commit(b, "b", ["sessions/hb"])
    gitops.push(b)                                                              # upstream moved; a has not pulled
    fgit("rm", "-q", ".gitignore", cwd=a)
    fgit("commit", "-q", "-m", "drop it", cwd=a)
    fgit("fetch", "-q", cwd=a)
    assert gitops.unpushed_paths(a) == [".gitignore"]                           # not sessions/hb/x.md from the remote


def test_pull_refusal_names_up_to_three_dirty_files(tmp_path):
    remote = init_remote(tmp_path)
    a = clone(remote, tmp_path / "a")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (a / name).write_text("v1")
    fgit("add", ".", cwd=a)
    fgit("commit", "-q", "-m", "files", cwd=a)
    gitops.push(a)
    for name in ("e.txt", "d.txt", "c.txt", "b.txt"):
        (a / name).write_text("v2")
    with pytest.raises(gitops.GitError) as e:
        gitops.pull(a)
    msg = str(e.value)
    assert "local changes in tracked files; not pulling" in msg and "\n" not in msg
    assert "b.txt, c.txt, d.txt" in msg and "e.txt" not in msg and "+1 more" in msg


def test_pull_refusal_with_one_dirty_file_names_it_without_a_count(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    (a / "README.md").write_text("edited locally\n")
    with pytest.raises(gitops.GitError) as e:
        gitops.pull(a)
    assert str(e.value).endswith("not pulling: README.md")


# ---------------------------------------------------------------- item 6: gentle timeouts, stale index lock

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, seconds: float = 3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_a_timeout_sends_sigterm_first_so_the_process_can_clean_up(tmp_path):
    note, helper = tmp_path / "note", tmp_path / "helper.pid"
    script = (f'sleep 60 & echo $! > "{helper}"; trap \'echo cleaned > "{note}"; exit 0\' TERM; wait')
    start = time.time()
    with pytest.raises(gitops.GitError, match="timed out after 1s"):
        gitops._run(["sh", "-c", script], 1, "fake")
    assert note.read_text().strip() == "cleaned"                                # it saw SIGTERM, not SIGKILL
    assert time.time() - start < 4                                              # and nobody waited for the grace period
    assert _wait_gone(int(helper.read_text()))                                  # the helper in the group stopped too


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "KILL_GRACE", 1)
    pidfile = tmp_path / "pid"
    script = f'echo $$ > "{pidfile}"; trap "" TERM; while :; do sleep 1; done'
    start = time.time()
    with pytest.raises(gitops.GitError, match="timed out"):
        gitops._run(["sh", "-c", script], 1, "fake")
    elapsed = time.time() - start
    assert 1.8 <= elapsed < 6, elapsed                                          # timeout (1 s) + grace (1 s)
    assert _wait_gone(int(pidfile.read_text()))


def test_the_default_grace_period_is_five_seconds():
    assert gitops.KILL_GRACE == 5


def _stale(path, minutes):
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


def test_repair_reports_a_stale_index_lock_and_leaves_it(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    lock = a / ".git" / "index.lock"
    lock.write_text("")
    _stale(lock, 11)
    msg = gitops.repair(a)
    assert "index.lock" in msg and "stale" in msg and "10 minutes" in msg and "\n" not in msg
    assert lock.exists()                                                        # never deleted by us


def test_repair_ignores_a_young_index_lock(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    lock = a / ".git" / "index.lock"
    lock.write_text("")
    _stale(lock, 2)                                                             # another git may be running right now
    assert gitops.repair(a) == "" and lock.exists()


def test_repair_reports_the_lock_even_when_a_rebase_is_stuck(tmp_path):
    b = _stuck_rebase(tmp_path)
    lock = b / ".git" / "index.lock"
    lock.write_text("")
    _stale(lock, 30)
    msg = gitops.repair(b)                                                      # the lock is why the abort fails
    assert "a rebase is stuck and could not be aborted" in msg and "stale" in msg and "index.lock" in msg
    assert "\n" not in msg and lock.exists() and (b / ".git" / "rebase-merge").exists()
    lock.unlink()
    assert gitops.repair(b) == "" and not (b / ".git" / "rebase-merge").exists()     # the owner removed it: repaired


def test_repair_lists_every_problem_it_finds(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    fgit("checkout", "-q", "--detach", cwd=a)
    lock = a / ".git" / "index.lock"
    lock.write_text("")
    _stale(lock, 30)
    msg = gitops.repair(a)
    assert "detached" in msg and "index.lock" in msg and "; " in msg
