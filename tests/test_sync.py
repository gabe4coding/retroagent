import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fixtures import AID, SID, T1, T2, age, clone, git, init_remote, make_claude_tree, make_codex_tree, make_config

from kb import gitops
from kb import sync as sync_mod
from kb.cli import cmd_sync
from kb.index import Index
from kb.lock import Lock
from kb.state import State
from kb.sync import Report, pending_units, run_sync
from kb.util import short_id

SUMMARY = {"summary": "Did the thing.", "tags": ["demo"], "outcome": "done", "decisions": []}
GOOD = json.dumps({"type": "result", "is_error": False, "result": json.dumps(SUMMARY)})


class FakeRunner:
    """A fake `claude`. mode: ok | unusable (call works, answer is junk) | fail (exit 1) | raise (OSError) |
    timeout | error (envelope with is_error). `tick` runs on every call (a fake clock advances there)."""

    def __init__(self, mode="ok", tick=None):
        self.calls, self.mode, self.tick, self.cwd_seen = [], mode, tick, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        self.cwd_seen.append((kw.get("cwd"), os.path.isdir(kw.get("cwd") or ""), os.listdir(kw["cwd"])))
        if self.tick:
            self.tick()
        if self.mode == "raise":
            raise OSError("claude: not found")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        if self.mode == "fail":
            return subprocess.CompletedProcess(cmd, 1, stdout="boom", stderr="")
        if self.mode == "error":
            out = json.dumps({"type": "result", "is_error": True, "result": "Credit balance is too low"})
        elif self.mode == "unusable":
            out = json.dumps({"type": "result", "is_error": False, "result": "I cannot do that."})
        else:
            out = GOOD
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


FAKE_GITLEAKS = r"""#!/bin/sh
# Fake gitleaks, driven by files in $FAKE_GITLEAKS_DIR:
#   fail   -> exit 2 with a two-line message (a scan that could not run)
#   report -> its text is the JSON report of the `git --staged` scan
#   flag   -> report the staged files (`git`) or decompressed raw files (`dir`) whose path contains this text
mode="$1"; shift
report=""; prev=""; last=""
for a in "$@"; do
  if [ "$prev" = "--report-path" ]; then report="$a"; fi
  prev="$a"; last="$a"
done
d="$FAKE_GITLEAKS_DIR"
if [ -f "$d/fail" ]; then printf 'fatal: boom\nsecond line\n' >&2; exit 2; fi
if [ "$mode" = "git" ] && [ -f "$d/report" ]; then cp "$d/report" "$report"; exit 1; fi
flag=$(cat "$d/flag" 2>/dev/null)
[ -z "$flag" ] && exit 0
if [ "$mode" = "git" ]; then
  hits=$(git -C "$last" diff --cached --name-only --no-renames | grep -F -- "$flag")
else
  hits=$(find "$last" -type f | grep -F -- "$flag")
fi
[ -z "$hits" ] && exit 0
{ printf '['; sep=''; for h in $hits; do printf '%s{"File":"%s","RuleID":"fake-rule"}' "$sep" "$h"; sep=','; done; printf ']'; } > "$report"
exit 1
"""


class Gitleaks:
    def __init__(self, dir_):
        self.dir = dir_

    def flag(self, text=""):
        (self.dir / "flag").write_text(text)

    def report(self, text):
        (self.dir / "report").write_text(text)

    def fail(self, on=True):
        if on:
            (self.dir / "fail").write_text("x")
        else:
            (self.dir / "fail").unlink()


def _path(monkeypatch, tmp_path, *front):
    """PATH = the given dirs, then a dir holding only git, then the system dirs. No real gitleaks, no real claude."""
    real_git = shutil.which("git")
    only_git = tmp_path / "only-git"
    only_git.mkdir(exist_ok=True)
    if not (only_git / "git").exists():
        (only_git / "git").symlink_to(real_git)
    monkeypatch.setenv("PATH", ":".join([*map(str, front), str(only_git), "/usr/bin", "/bin"]))


@pytest.fixture(autouse=True)
def _no_gitleaks(monkeypatch, tmp_path):
    """Sync tests do not depend on a gitleaks installed on the machine."""
    _path(monkeypatch, tmp_path)


@pytest.fixture
def gitleaks(monkeypatch, tmp_path):
    """Put a fake gitleaks first on PATH. Returns a controller (flag / report / fail)."""
    bin_dir, data = tmp_path / "gl-bin", tmp_path / "gl-data"
    bin_dir.mkdir()
    data.mkdir()
    (bin_dir / "gitleaks").write_text(FAKE_GITLEAKS)
    (bin_dir / "gitleaks").chmod(0o755)
    monkeypatch.setenv("FAKE_GITLEAKS_DIR", str(data))
    _path(monkeypatch, tmp_path, bin_dir)
    return Gitleaks(data)


def _host(tmp_path, remote, name, sid, aid, t1, t2, t3):
    root = clone(remote, tmp_path / name)
    src = tmp_path / f"src-{name}"
    projects = make_claude_tree(src, sid=sid, aid=aid)
    sessions, home = make_codex_tree(src, t1=t1, t2=t2, t3=t3)
    return make_config(root, name, projects, sessions, home)


@pytest.fixture
def hosts(tmp_path):
    remote = init_remote(tmp_path)
    a = _host(tmp_path, remote, "host-a", SID, AID, T1, T2, "01a0c900-0000-7000-8000-0000000000aa")
    b = _host(tmp_path, remote, "host-b", "22222222-3333-4444-5555-666666666666", "b2b2b2b2b2b2b2b2b",
              "01a0d000-0000-7000-8000-00000000000b", "01a0d000-0000-7000-8000-00000000000c",
              "01a0d000-0000-7000-8000-00000000000d")
    return a, b


def test_two_hosts_sync_without_conflicts(hosts):
    a, b = hosts
    ra = run_sync(a, runner=FakeRunner())
    assert ra.errors == [] and ra.sessions == 3 and ra.summarized == 2 and ra.pushed
    rb = run_sync(b, runner=FakeRunner())
    assert rb.errors == [] and rb.sessions == 3 and rb.pushed
    ra2 = run_sync(a, runner=FakeRunner())
    assert ra2.sessions == 0 and ra2.summarized == 0 and not ra2.committed and ra2.errors == []
    idx = Index(a.kb_dir / "index.sqlite")
    hosts_seen = {r[0] for r in idx.db.execute("SELECT DISTINCT host FROM sessions")}
    idx.close()
    assert hosts_seen == {"host-a", "host-b"}
    assert (a.root / "catalog/host-a/2026-10.jsonl").exists() and (a.root / "catalog/host-b").is_dir()
    subjects = gitops.git(a.root, "log", "--format=%s").stdout.splitlines()
    assert subjects[0].startswith("sync(host-b)") and subjects[1].startswith("sync(host-a)")
    assert (a.kb_dir / "last-ok").exists()


def test_quiet_period_and_now(hosts):
    a, _ = hosts
    for p in list(Path(a.claude_dir).rglob("*.jsonl")) + list(Path(a.codex_dirs[0]).rglob("*.jsonl")):
        os.utime(p, None)
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).sessions == 0
    assert run_sync(a, now=True, runner=FakeRunner(), summary_cap=0).sessions == 3


def test_locked_out(hosts):
    a, _ = hosts
    lock = Lock(a.kb_dir / "lock")
    assert lock.acquire()
    r = run_sync(a, runner=FakeRunner())
    assert r.locked_out and r.sessions == 0
    lock.release()


def test_summary_failures_retry_then_stop(hosts):
    """Changed for item 2: a call that returns junk is an attempt keyed by id:turns (a failed call is not)."""
    a, _ = hosts
    for _ in range(4):
        run_sync(a, runner=FakeRunner("unusable"))
    attempts = State.load(a.kb_dir / "sync-state.json").summary_attempts
    assert attempts and set(attempts.values()) == {3} and all(":" in k for k in attempts)
    runner = FakeRunner()
    run_sync(a, runner=runner)
    assert runner.calls == []


def test_summary_call_uses_guard_env_and_temp_cwd(hosts):
    a, _ = hosts
    runner = FakeRunner()
    run_sync(a, runner=runner)
    cmd, kw = runner.calls[0]
    assert cmd[0] == "claude" and kw["env"]["KB_CHILD"] == "1"
    assert not str(kw["cwd"]).startswith(str(a.root))


def test_dry_run_with_sample(hosts):
    a, _ = hosts
    r = run_sync(a, dry_run=True, sample=1)
    assert r.sessions == 3 and r.sizes["md"] > 0 and r.redactions["github-token"] >= 1
    assert not (a.root / "sessions").exists() and not (a.kb_dir / "sync-state.json").exists()
    assert list((a.kb_dir / "dry-run" / "sessions").rglob("*.md"))


def test_excluded_cwd(hosts):
    a, _ = hosts
    a.exclude_cwd_globs = ["/Users/me/*"]
    r = run_sync(a, runner=FakeRunner())
    assert r.sessions == 0 and not r.committed


def test_gitleaks_finding_blocks_commit(hosts, tmp_path, monkeypatch):
    a, _ = hosts
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "gitleaks").write_text("#!/bin/sh\necho finding\nexit 1\n")
    (fake / "gitleaks").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and any(e.startswith("gitleaks") for e in r.errors)
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""


def test_cold_rebuild_is_identical(hosts):
    """Changed for item 5: .kb/machine-id says which machine owns the host, so a rebuild keeps it (the rest of .kb is
    derived data and goes). A wiped id looks like another machine."""
    a, _ = hosts
    run_sync(a, runner=FakeRunner())
    mid = (a.kb_dir / "machine-id").read_text()
    for d in ("sessions", "raw", "catalog", ".kb"):
        shutil.rmtree(a.root / d)
    a.kb_dir.mkdir()
    (a.kb_dir / "machine-id").write_text(mid)
    r = run_sync(a, runner=FakeRunner())
    assert r.sessions == 3 and not r.committed
    assert gitops.git(a.root, "status", "--porcelain").stdout == ""


# ================================================================ helpers for the tests below

def _add_sessions(cfg, n):
    """n more Claude sessions (each with a subagent) in the host's source tree. Returns their ids."""
    sids = [f"{i + 1:08x}-2222-3333-4444-{i + 1:012x}" for i in range(n)]
    for i, sid in enumerate(sids):
        make_claude_tree(Path(cfg.claude_dir).parent, sid=sid, aid=f"a{i + 1:016x}")
    return sids


def _grow_claude(cfg, sid=SID, pairs=1):
    """Append `pairs` user+assistant exchanges (2 turns each) to a Claude session; keep it past the quiet period."""
    f = next(Path(cfg.claude_dir).rglob(f"{sid}.jsonl"))
    n = len(f.read_text().splitlines())
    with open(f, "a", encoding="utf-8") as fh:
        for i in range(pairs):
            k = n + 2 * i
            base = {"sessionId": sid, "cwd": "/Users/me/Repositories/demo", "gitBranch": "feat/x"}
            fh.write(json.dumps({**base, "type": "user", "uuid": f"ug{k}", "timestamp": f"2026-10-07T10:{k % 60:02d}:00.000Z",
                                 "message": {"role": "user", "content": f"more question {k}"}}) + "\n")
            fh.write(json.dumps({**base, "type": "assistant", "uuid": f"ag{k}", "timestamp": f"2026-10-07T10:{k % 60:02d}:30.000Z",
                                 "message": {"id": f"mg{k}", "role": "assistant", "model": "claude-opus-5-5",
                                             "content": [{"type": "text", "text": f"answer {k}"}]}}) + "\n")
    age([f])


def _row(cfg, sid):
    idx = Index(cfg.kb_dir / "index.sqlite")
    try:
        return dict(idx.db.execute("SELECT id, turns, summary_turns, md_path FROM sessions WHERE id=?", (sid,)).fetchone())
    finally:
        idx.close()


def _head_files(root, rev="HEAD"):
    return gitops.git(root, "show", "--name-only", "--format=", rev).stdout.split()


def _tracked(root):
    return set(gitops.git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout.split())


def _md(cfg, suffix):
    return next(p.relative_to(cfg.root).as_posix() for p in (cfg.root / "sessions" / cfg.host).rglob("*.md")
                if p.name.endswith(suffix))


def _quarantine(cfg):
    return State.load(cfg.kb_dir / "sync-state.json").quarantine


# ================================================================ item 1: order, lock, repair

def test_git_steps_run_in_the_safe_order(hosts, monkeypatch):
    a, _ = hosts
    calls = []
    for name in ("repair", "stage", "secrets_check", "commit", "pull", "push"):
        real = getattr(gitops, name)
        monkeypatch.setattr(gitops, name, lambda *args, _n=name, _r=real, **kw: (calls.append(_n), _r(*args, **kw))[1])
    real_write = sync_mod.write_session
    monkeypatch.setattr(sync_mod, "write_session", lambda *args, **kw: (calls.append("write"), real_write(*args, **kw))[1])
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and r.pushed
    assert calls[0] == "repair" and "pull" not in calls[:calls.index("commit")]
    assert calls[-5:] == ["stage", "secrets_check", "commit", "pull", "push"]
    assert calls.index("repair") < calls.index("write") and calls.count("write") >= 3


def test_own_uncommitted_files_do_not_block_the_pull(hosts):
    a, b = hosts
    assert run_sync(a, runner=FakeRunner()).errors == []
    assert run_sync(b, runner=FakeRunner()).pushed
    mine = a.root / _md(a, "_55555555.md")                 # a crash left an own tracked file changed
    mine.write_text(mine.read_text() + "\nleft by an interrupted run\n")
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and r.committed and r.pushed
    assert gitops.git(a.root, "status", "--porcelain").stdout == ""
    idx = Index(a.kb_dir / "index.sqlite")
    assert {x[0] for x in idx.db.execute("SELECT DISTINCT host FROM sessions")} == {"host-a", "host-b"}
    idx.close()


def test_detached_head_skips_git_but_still_processes(hosts):
    a, _ = hosts
    git("checkout", "-q", "--detach", cwd=a.root)
    before = gitops.git(a.root, "rev-parse", "HEAD").stdout
    r = run_sync(a, runner=FakeRunner())
    assert r.sessions == 3 and any("detached" in e for e in r.errors)
    assert not r.committed and not r.pushed
    assert gitops.git(a.root, "rev-parse", "HEAD").stdout == before
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    assert (a.root / "sessions" / "host-a").is_dir()
    git("checkout", "-q", "main", cwd=a.root)
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and r.committed and r.pushed
    assert _tracked(a.root) >= {_md(a, "_55555555.md")}


def test_dry_run_runs_no_git_at_all(hosts, monkeypatch):
    a, _ = hosts

    class NoGit:
        def __getattr__(self, name):
            raise AssertionError(f"dry run called gitops.{name}")

    monkeypatch.setattr(sync_mod, "gitops", NoGit())
    assert run_sync(a, dry_run=True).sessions == 3


def test_state_save_failure_still_releases_the_lock(hosts, monkeypatch):
    a, _ = hosts

    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(State, "save", boom)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert any("state" in e and "disk full" in e for e in r.errors)
    lock = Lock(a.kb_dir / "lock")
    assert lock.acquire()
    lock.release()


def test_state_load_failure_still_releases_the_lock(hosts, monkeypatch):
    a, _ = hosts

    def boom(path):
        raise RuntimeError("cannot load")

    monkeypatch.setattr(State, "load", staticmethod(boom))
    with pytest.raises(RuntimeError):
        run_sync(a, runner=FakeRunner())
    lock = Lock(a.kb_dir / "lock")
    assert lock.acquire()
    lock.release()


def test_state_is_checkpointed_while_processing(hosts, monkeypatch):
    a, _ = hosts
    assert sync_mod.CHECKPOINT_EVERY == 50
    monkeypatch.setattr(sync_mod, "CHECKPOINT_EVERY", 1)
    seen, real = [], sync_mod.write_session

    def spy(*args, **kw):
        f = a.kb_dir / "sync-state.json"
        seen.append(len(json.loads(f.read_text())["files"]) if f.exists() else 0)
        return real(*args, **kw)

    monkeypatch.setattr(sync_mod, "write_session", spy)
    run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert len(seen) >= 3 and seen[0] == 0 and seen[1] >= 1 and seen == sorted(seen) and seen[-1] >= 2


def test_pull_failure_is_recorded_and_skips_the_push_until_it_works(hosts):
    a, _ = hosts
    readme = a.root / "README.md"
    readme.write_text("local edit\n")                      # a tracked file outside our folders: pull must refuse
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.committed and not r.pushed and any(e.startswith("pull:") for e in r.errors)
    assert gitops.ahead(a.root) == 1
    git("checkout", "--", "README.md", cwd=a.root)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.pushed and not r.committed and gitops.ahead(a.root) == 0


def test_first_push_into_an_empty_remote(tmp_path):
    remote = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    root = tmp_path / "root"
    git("clone", "-q", str(remote), str(root))
    src = tmp_path / "src"
    projects = make_claude_tree(src)
    sessions, home = make_codex_tree(src)
    cfg = make_config(root, "host-a", projects, sessions, home)
    r = run_sync(cfg, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed
    assert "sessions/host-a" in git("ls-tree", "-r", "--name-only", "main", cwd=remote).stdout


# ================================================================ item 2: summary circuit breaker and budget

@pytest.mark.parametrize("mode", ["fail", "raise", "timeout", "error"])
def test_summary_outage_stops_after_three_calls_and_records_no_attempts(hosts, mode):
    a, _ = hosts
    _add_sessions(a, 40)
    bad = FakeRunner(mode)
    r = run_sync(a, runner=bad, summary_cap=None)
    assert len(bad.calls) == 3 and r.summarized == 0
    assert State.load(a.kb_dir / "sync-state.json").summary_attempts == {}
    assert any("stopped" in e for e in r.errors)
    good = FakeRunner()
    r = run_sync(a, runner=good, summary_cap=None)
    assert r.summarized == 42 and len(good.calls) == 42 and r.errors == []
    assert State.load(a.kb_dir / "sync-state.json").summary_attempts == {}


def test_a_success_resets_the_consecutive_failure_count(hosts):
    a, _ = hosts
    _add_sessions(a, 7)                                                        # 9 sessions to summarize
    modes = iter(["fail", "fail", "ok", "fail", "fail", "ok", "fail", "fail", "ok"])

    class Flaky(FakeRunner):
        def __call__(self, cmd, **kw):
            self.mode = next(modes, "ok")
            return super().__call__(cmd, **kw)

    r = run_sync(a, runner=Flaky(), summary_cap=None)
    assert r.summarized == 3 and len([e for e in r.errors if "stopped" in e]) == 0


def test_unusable_output_three_times_skips_until_the_session_grows(hosts):
    a, _ = hosts
    for _ in range(3):
        run_sync(a, runner=FakeRunner("unusable"))
    attempts = State.load(a.kb_dir / "sync-state.json").summary_attempts
    old_turns = _row(a, SID)["turns"]
    assert attempts[f"{SID}:{old_turns}"] == 3
    idle = FakeRunner()
    assert run_sync(a, runner=idle).summarized == 0 and idle.calls == []      # skipped, not retried
    _grow_claude(a, pairs=1)
    r = run_sync(a, runner=FakeRunner())
    assert _row(a, SID)["turns"] > old_turns                                    # a new key: tried again
    assert r.summarized == 1
    assert not any(k.startswith(SID) for k in State.load(a.kb_dir / "sync-state.json").summary_attempts)


def test_old_plain_id_attempt_keys_are_ignored_and_dropped(hosts):
    a, _ = hosts
    run_sync(a, runner=FakeRunner(), summary_cap=0)
    state = State.load(a.kb_dir / "sync-state.json")
    state.summary_attempts = {SID: 3, T1: 3}                                   # the old format: plain session ids
    state.save()
    runner = FakeRunner()
    r = run_sync(a, runner=runner)
    assert r.summarized == 2 and len(runner.calls) == 2
    assert State.load(a.kb_dir / "sync-state.json").summary_attempts == {}


@pytest.mark.parametrize("mode", ["unusable", "fail"])
def test_cap_counts_every_call_not_only_successes(hosts, mode):
    a, _ = hosts
    _add_sessions(a, 10)
    runner = FakeRunner(mode)
    run_sync(a, runner=runner, summary_cap=2)
    assert len(runner.calls) == 2
    runner = FakeRunner("unusable")
    run_sync(a, runner=runner, summary_cap=5)
    assert len(runner.calls) == 5


def test_summary_budget_stops_the_pass(hosts, monkeypatch):
    a, _ = hosts
    _add_sessions(a, 6)
    assert sync_mod.SUMMARY_BUDGET_S == 20 * 60
    monkeypatch.setattr(sync_mod, "SUMMARY_BUDGET_S", 150)
    clock = FakeClock()
    runner = FakeRunner(tick=lambda: clock.advance(100))
    r = run_sync(a, now=True, runner=runner, clock=clock, summary_cap=10)      # a capped run keeps the wall-clock budget
    assert len(runner.calls) == 2 and r.summarized == 2                        # t=0 and t=100 run; t=200 is over budget
    clock2 = FakeClock()
    runner = FakeRunner(tick=lambda: clock2.advance(100))
    run_sync(a, now=True, runner=runner, clock=clock2, summary_cap=10)         # the next run starts a fresh budget
    assert len(runner.calls) == 2


def test_backfill_without_a_cap_ignores_the_wall_clock_budget(hosts, monkeypatch):
    """Item 2: `kb backfill --summaries` (summary_cap=None) must not stop after SUMMARY_BUDGET_S."""
    a, _ = hosts
    _add_sessions(a, 6)
    monkeypatch.setattr(sync_mod, "SUMMARY_BUDGET_S", 150)
    clock = FakeClock()
    runner = FakeRunner(tick=lambda: clock.advance(100))                       # 100 s per call: far past the budget
    r = run_sync(a, now=True, runner=runner, clock=clock, summary_cap=None)
    assert len(runner.calls) == r.summarized and r.summarized >= 6             # every session got its call
    idx = Index(a.kb_dir / "index.sqlite")
    try:
        assert sync_mod.needs_summary(idx, a.host) == []
    finally:
        idx.close()


def test_summaries_only_when_new_or_grown_by_four_turns(hosts):
    a, _ = hosts
    assert sync_mod.SUMMARY_REGROWTH == 4
    assert run_sync(a, runner=FakeRunner()).summarized == 2
    first = _row(a, SID)
    assert first["summary_turns"] == first["turns"]
    _grow_claude(a, pairs=1)                                                   # +2 turns: not enough
    runner = FakeRunner()
    assert run_sync(a, runner=runner).summarized == 0 and runner.calls == []
    assert _row(a, SID)["turns"] == first["turns"] + 2 and _row(a, SID)["summary_turns"] == first["turns"]
    _grow_claude(a, pairs=1)                                                   # +4 turns in all: summarize again
    assert run_sync(a, runner=runner).summarized == 1 and len(runner.calls) == 1
    again = _row(a, SID)
    assert again["summary_turns"] == again["turns"] == first["turns"] + 4


def test_summary_child_runs_in_a_fresh_removed_temp_dir(hosts):
    a, _ = hosts
    runner = FakeRunner()
    run_sync(a, runner=runner)
    assert len(runner.cwd_seen) == 2
    for cwd, existed, content in runner.cwd_seen:
        assert existed and content == [] and not str(cwd).startswith(str(a.root))
        assert not os.path.exists(cwd)
    assert runner.cwd_seen[0][0] != runner.cwd_seen[1][0]
    assert runner.calls[0][1]["encoding"] == "utf-8" and runner.calls[0][1]["errors"] == "replace"


def test_summary_temp_dir_is_removed_when_the_call_fails(hosts):
    a, _ = hosts
    runner = FakeRunner("raise")
    run_sync(a, runner=runner)
    assert runner.cwd_seen and not any(os.path.exists(c) for c, _, _ in runner.cwd_seen)


# ================================================================ item 4: gitleaks quarantine

def test_gitleaks_error_exit_commits_nothing(hosts, gitleaks):
    a, _ = hosts
    gitleaks.fail()
    head = gitops.git(a.root, "rev-parse", "HEAD").stdout
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and not r.pushed and any(e.startswith("gitleaks:") and "boom" in e for e in r.errors)
    assert gitops.git(a.root, "rev-parse", "HEAD").stdout == head
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    assert _quarantine(a) == {}
    gitleaks.fail(False)                                                       # nothing is lost: the next run commits
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


def test_unmappable_gitleaks_finding_fails_closed(hosts, gitleaks):
    a, _ = hosts
    gitleaks.report('[{"File": "(unknown file)"}]')
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and any(e.startswith("gitleaks:") for e in r.errors)
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    assert _quarantine(a) == {}


def test_flagged_file_is_quarantined_and_released_when_clean(hosts, gitleaks):
    a, _ = hosts
    gitleaks.flag("_55555555.md")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    flagged = _md(a, "_55555555.md")
    assert r.errors == [] and r.committed and r.pushed and r.quarantined == [flagged]
    assert flagged not in _tracked(a.root) and len(_tracked(a.root)) > 5
    assert (a.root / flagged).exists()                                         # stays on disk
    assert flagged not in git("ls-tree", "-r", "--name-only", "main", cwd=a.root.parent / "remote.git").stdout
    assert list(_quarantine(a)) == [flagged]
    state = State.load(a.kb_dir / "sync-state.json")                           # first-seen time is kept across runs
    state.quarantine[flagged] = "2020-01-01T00:00:00Z"
    state.save()
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and r.quarantined == [flagged] and not r.happened
    assert _quarantine(a) == {flagged: "2020-01-01T00:00:00Z"}
    gitleaks.flag("")                                                          # clean now: committed, leaves quarantine
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed and r.quarantined == []
    assert flagged in _tracked(a.root) and _quarantine(a) == {}


def test_flagged_raw_file_is_quarantined_too(hosts, gitleaks):
    a, _ = hosts
    gitleaks.flag("555555.jsonl")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    raw = f"raw/host-a/claude/2026/10/{SID}.jsonl.gz"
    assert r.errors == [] and r.committed and r.quarantined == [raw]
    assert raw not in _tracked(a.root) and (a.root / raw).exists()
    assert _md(a, "_55555555.md") in _tracked(a.root)


def test_a_changed_tracked_file_that_is_flagged_is_not_committed(hosts, gitleaks):
    """`git commit -- <dir>` takes unstaged tracked changes too; the quarantine must still hold it back."""
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    held, other = _md(a, "_55555555.md"), _md(a, f"_{short_id(T1)}.md")
    for rel in (held, other):
        (a.root / rel).write_text((a.root / rel).read_text() + "\nmore\n")
    gitleaks.flag("_55555555.md")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.committed and _head_files(a.root) == [other]
    assert gitops.git(a.root, "status", "--porcelain").stdout == f" M {held}\n"
    assert list(_quarantine(a)) == [held]
    gitleaks.flag("")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed and _head_files(a.root) == [held] and _quarantine(a) == {}


def test_nothing_is_committed_when_every_staged_file_is_flagged(hosts, gitleaks):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    held = _md(a, "_55555555.md")
    (a.root / held).write_text((a.root / held).read_text() + "\nmore\n")
    gitleaks.flag("_55555555.md")
    head = gitops.git(a.root, "rev-parse", "HEAD").stdout
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and gitops.git(a.root, "rev-parse", "HEAD").stdout == head
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == "" and list(_quarantine(a)) == [held]


def test_quarantine_is_kept_when_gitleaks_is_gone(hosts, gitleaks, monkeypatch, tmp_path):
    """Changed for item 3: a missing scanner no longer releases quarantined files; they stay held back (and on disk)."""
    a, _ = hosts
    gitleaks.flag("_55555555.md")
    run_sync(a, runner=FakeRunner(), summary_cap=0)
    held = _md(a, "_55555555.md")
    assert list(_quarantine(a)) == [held]
    _path(monkeypatch, tmp_path)                                               # gitleaks no longer installed
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and r.quarantined == [held] and list(_quarantine(a)) == [held]
    assert held not in _tracked(a.root) and (a.root / held).exists()
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    warnings = [e for e in r.errors if e.startswith("gitleaks:")]
    assert len(warnings) == 1 and "\n" not in warnings[0] and "quarantine" in warnings[0]


def test_other_files_are_committed_while_a_missing_scanner_keeps_the_quarantine(hosts, gitleaks, monkeypatch, tmp_path):
    a, _ = hosts
    gitleaks.flag("_55555555.md")
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    held, other = _md(a, "_55555555.md"), _md(a, f"_{short_id(T1)}.md")
    _path(monkeypatch, tmp_path)
    (a.root / other).write_text((a.root / other).read_text() + "\nmore\n")
    (a.root / held).write_text((a.root / held).read_text() + "\nmore\n")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.committed and _head_files(a.root) == [other] and list(_quarantine(a)) == [held]
    gitleaks_bin = tmp_path / "gl-bin"                                         # the scanner is back and finds nothing
    gitleaks.flag("")
    _path(monkeypatch, tmp_path, gitleaks_bin)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and _head_files(a.root) == [held] and _quarantine(a) == {}


def test_a_missing_scanner_without_quarantine_commits_quietly_as_before(hosts):
    a, _ = hosts
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


def test_require_gitleaks_without_a_scanner_commits_nothing(hosts):
    a, _ = hosts
    a.require_gitleaks = True
    head = gitops.git(a.root, "rev-parse", "HEAD").stdout
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and not r.pushed and "gitleaks: required but not found" in r.errors
    assert gitops.git(a.root, "rev-parse", "HEAD").stdout == head
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    assert (a.root / "sessions" / "host-a").is_dir() and _quarantine(a) == {}  # still processed locally
    assert State.load(a.kb_dir / "sync-state.json").last_error == "gitleaks: required but not found"


def test_require_gitleaks_with_a_scanner_commits(hosts, gitleaks):
    a, _ = hosts
    a.require_gitleaks = True
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


def test_gitleaks_path_from_the_config_is_used_when_path_has_none(hosts, tmp_path, monkeypatch):
    a, _ = hosts
    a.require_gitleaks = True
    away = tmp_path / "away"
    away.mkdir()
    (away / "gitleaks").write_text(FAKE_GITLEAKS)
    (away / "gitleaks").chmod(0o755)
    data = tmp_path / "gl-data2"
    data.mkdir()
    monkeypatch.setenv("FAKE_GITLEAKS_DIR", str(data))
    (data / "flag").write_text("_55555555.md")
    a.gitleaks_path = str(away / "gitleaks")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and len(r.quarantined) == 1          # it really ran: it flagged a file
    a.gitleaks_path = str(away / "missing")                                    # a wrong path and nothing on PATH
    (a.root / r.quarantined[0]).write_text("changed\n")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert "gitleaks: required but not found" in r.errors


def test_state_quarantine_round_trip_and_tolerant_load(tmp_path):
    p = tmp_path / "s.json"
    st = State.load(p)
    assert st.quarantine == {}
    st.quarantine["sessions/h/a.md"] = "2026-10-06T10:00:00Z"
    st.save()
    assert State.load(p).quarantine == {"sessions/h/a.md": "2026-10-06T10:00:00Z"}
    for bad in (["a"], "x", 5, None):
        p.write_text(json.dumps({"quarantine": bad}))
        assert State.load(p).quarantine == {}
    p.write_text(json.dumps({"quarantine": {"ok": "t", "odd": 5, "7": None}}))
    assert State.load(p).quarantine == {"ok": "t", "odd": "", "7": ""}
    p.write_text(json.dumps({"files": {}}))                                    # states written before this field
    assert State.load(p).quarantine == {}


# ================================================================ item 5: one log line per run

def test_report_line_is_one_line():
    r = Report(sessions=1, errors=["gitleaks: fatal: boom\nsecond line\n" + "x" * 500, "other"], quarantined=["a", "b"])
    line = r.line()
    assert "\n" not in line and "2 quarantined" in line and "boom second line" in line and len(line) < 400


def test_report_happened():
    assert not Report().happened
    assert Report(committed=True).happened and Report(newly_quarantined=1).happened
    assert not Report(quarantined=["still-held"]).happened


def test_sync_prints_one_line_when_something_happened_and_nothing_otherwise(hosts, gitleaks, capsys):
    a, _ = hosts
    args = argparse.Namespace(now=True, dry_run=False, sample=0, no_summaries=True)
    assert cmd_sync(args, a) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and out.startswith("20") and "host-a: 3 sessions" in out and out.rstrip().endswith("pushed")
    assert cmd_sync(args, a) == 0 and capsys.readouterr().out == ""
    gitleaks.fail()                                                            # a multi-line gitleaks failure: still one line
    _grow_claude(a)
    assert cmd_sync(args, a) == 1
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and "gitleaks" in out and "boom" in out
    assert "\n" not in State.load(a.kb_dir / "sync-state.json").last_error


# ================================================================ review fixes: short ids, collisions, moves, quarantine

def _squatter(cfg, sid="99999999-2222-3333-4444-555555555555", aid="c3c3c3c3c3c3c3c3c"):
    """A second Claude session whose id ends like SID's: the same short id, project and day, so the same md path."""
    make_claude_tree(Path(cfg.claude_dir).parent, sid=sid, aid=aid)
    return sid


def test_a_file_of_another_session_is_not_overwritten_and_the_error_is_reported(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).errors == []
    path = a.root / _md(a, "_55555555.md")
    before = path.read_bytes()
    squatter = _squatter(a)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert path.read_bytes() == before                                     # the existing file is untouched
    assert len(r.errors) == 1 and "PathCollision" in r.errors[0] and SID in r.errors[0] and squatter in r.errors[0]
    assert r.sessions == 0 and "PathCollision" in State.load(a.kb_dir / "sync-state.json").last_error
    assert not any(squatter in f.name for f in (a.root / "raw").rglob("*"))
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)           # it is retried (and reported) on every run
    assert len(r.errors) == 1 and "PathCollision" in r.errors[0] and path.read_bytes() == before


def test_sessions_with_the_same_first_eight_characters_are_all_kept_and_found(tmp_path):
    remote = init_remote(tmp_path)
    root = clone(remote, tmp_path / "root")
    src = tmp_path / "src"
    projects = make_claude_tree(src, sid="01a0d000-0000-7000-8000-5f3c9a1be7d2", aid="a111111111111111a")
    make_claude_tree(src, sid="01a0d000-0001-7123-9abc-0e4b7c2d91a6", aid="b222222222222222b")
    sessions, home = make_codex_tree(src)
    cfg = make_config(root, "host-a", projects, sessions, home)
    r = run_sync(cfg, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 4
    names = sorted(p.name for p in (root / "sessions/host-a/claude").rglob("*.md") if "_sub-" not in p.name)
    assert len(names) == 2 and names[0].endswith("_7c2d91a6.md") and names[1].endswith("_9a1be7d2.md")
    idx = Index(cfg.kb_dir / "index.sqlite")
    try:
        assert idx.get("9a1be7d2")["id"] == "01a0d000-0000-7000-8000-5f3c9a1be7d2"
        assert idx.get("7c2d91a6")["id"] == "01a0d000-0001-7123-9abc-0e4b7c2d91a6"
    finally:
        idx.close()


def _rewrite_month(cfg, sid, old="2026-10", new="2026-11"):
    for f in Path(cfg.claude_dir).rglob("*.jsonl"):
        if sid in str(f):
            f.write_text(f.read_text().replace(old, new))
            age([f])


def test_a_session_that_moves_to_another_month_is_fully_moved_by_the_sync(hosts):
    a, _ = hosts
    r = run_sync(a, runner=FakeRunner(), summary_cap=None)
    assert r.errors == [] and r.summarized == 2 and r.pushed
    old_md, old_raw = _md(a, "_55555555.md"), f"raw/host-a/claude/2026/10/{SID}.jsonl.gz"
    old_sub = _md(a, f"_55555555_sub-{short_id(AID)}.md")
    old_sub_raw = f"raw/host-a/claude/2026/10/{SID}__sub-{AID}.jsonl.gz"
    before = _row(a, SID)
    tracked = _tracked(a.root)
    assert {old_md, old_sub, old_raw, old_sub_raw, "catalog/host-a/2026-10.jsonl"} <= tracked
    _rewrite_month(a, SID)
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 1 and r.committed and r.pushed
    root, new_md = a.root, _md(a, "_55555555.md")
    assert "/2026/11/" in new_md and new_md != old_md
    for gone in (old_md, old_sub, old_raw, old_sub_raw, "catalog/host-a/2026-10.jsonl"):
        assert not (root / gone).exists() and gone not in _tracked(root), gone
    assert {new_md, f"raw/host-a/claude/2026/11/{SID}.jsonl.gz", "catalog/host-a/2026-11.jsonl"} <= _tracked(root)
    rows = [json.loads(l) for l in (root / "catalog/host-a/2026-11.jsonl").read_text().splitlines()]
    assert [x["id"] for x in rows] == [SID, AID] and rows[0]["summary"] == "Did the thing."
    after = _row(a, SID)
    assert after["md_path"] == new_md and after["summary_turns"] == before["summary_turns"] > 0
    assert gitops.git(root, "status", "--porcelain").stdout == ""


def test_a_quarantined_tracked_file_does_not_block_the_pull(hosts, gitleaks):
    a, b = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    held = _md(a, "_55555555.md")
    (a.root / held).write_text((a.root / held).read_text() + "\nlocal change\n")
    gitleaks.flag("_55555555.md")
    assert run_sync(b, runner=FakeRunner(), summary_cap=0).pushed           # a new commit on the remote
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and list(_quarantine(a)) == [held]
    assert (a.root / held).read_text().endswith("local change\n")           # the local file is as it was
    assert gitops.git(a.root, "status", "--porcelain").stdout == f" M {held}\n"
    assert gitops.git(a.root, "stash", "list").stdout == ""
    assert any((a.root / "sessions/host-b").rglob("*.md"))                   # the other host's work arrived
    idx = Index(a.kb_dir / "index.sqlite")
    assert {x[0] for x in idx.db.execute("SELECT DISTINCT host FROM sessions")} == {"host-a", "host-b"}
    idx.close()


# ================================================================ review fixes: lock message, agent messages, dry-run mix

@pytest.mark.parametrize("dry_run", [False, True])
def test_sync_says_so_and_exits_0_when_another_sync_runs(hosts, capsys, dry_run):
    a, _ = hosts
    lock = Lock(a.kb_dir / "lock")
    assert lock.acquire()
    try:
        args = argparse.Namespace(now=True, dry_run=dry_run, sample=0, no_summaries=True)
        assert cmd_sync(args, a) == 0
    finally:
        lock.release()
    assert capsys.readouterr().out == "another sync is running\n"


def test_agent_messages_do_not_count_as_prompts_or_trigger_summaries(tmp_path):
    from collections import Counter

    from fixtures import codex_msg, write_codex_unit

    from kb.adapters import codex
    from kb.index import run_sql
    from kb.stats import REPORTS
    from kb.store import write_session

    def report(author, text):
        return ("response_item", {"type": "agent_message", "author": author, "recipient": "/root",
                                  "content": [{"type": "input_text", "text": text}]})

    unit = write_codex_unit(tmp_path / "src", [
        codex_msg("user", "Audit the repo"),
        report("/root/finder", "Found 3 calls"),
        report("/root/mapper", "Mapped the modules"),
        codex_msg("assistant", "Done."),
    ])
    root = tmp_path / "kb"
    write_session(root, "h", codex.parse_unit(unit, {}), Counter())
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        idx.update(root)
        row = idx.db.execute("SELECT turns, user_turns FROM sessions").fetchone()
        assert (row["turns"], row["user_turns"]) == (4, 1)                     # four turns, one human prompt
        assert sync_mod.needs_summary(idx, "h") == []                          # one prompt is not enough for a summary
    finally:
        idx.close()
    cols, rows = run_sql(root / ".kb" / "index.sqlite", REPORTS["overview"])
    assert dict(zip(cols, rows[0]))["prompts"] == 1


def _dry_run_sample_ids(cfg):
    from kb.distill import split_front_matter
    base = cfg.kb_dir / "dry-run" / "sessions"
    return {split_front_matter(p.read_text(encoding="utf-8"))[0]["id"] for p in base.rglob("*.md")} if base.exists() else set()


def _mixed_host(tmp_path):
    """Claude: SID (newest of the two with subagents), SID2 (older, with subagents), four newer ones without.
    Codex: T1 (top level) and T2 (its subagent). The four plain Claude sessions are newer than everything else."""
    from fixtures import claude_main, write_jsonl
    src = tmp_path / "src"
    projects = make_claude_tree(src, sid=SID, aid=AID)
    sid2, aid2 = "22222222-3333-4444-5555-666666666666", "b2b2b2b2b2b2b2b2b"
    make_claude_tree(src, sid=sid2, aid=aid2)
    age([p for p in Path(projects).rglob("*.jsonl") if sid2 in str(p)], 3 * 86400)              # older than SID
    plain = []
    for i in range(4):
        sid = f"0000000{i}-2222-3333-4444-{i + 1:012x}"
        proj = next(Path(projects).iterdir())
        path = write_jsonl(proj / f"{sid}.jsonl", claude_main(sid, f"c{i:016x}"))
        age([path], 1000 + 100 * i)                                           # plain[0] is the newest of all
        plain.append(sid)
    sessions, home = make_codex_tree(src)
    return make_config(tmp_path / "kb", "h", projects, sessions, home), plain, sid2


def test_dry_run_sample_is_a_mix_of_kinds_not_just_the_newest(tmp_path):
    cfg, plain, sid2 = _mixed_host(tmp_path)
    report = run_sync(cfg, dry_run=True, sample=3)
    assert report.sessions == 8 and report.errors == []
    # the three kinds win over four newer plain Claude sessions: newest Claude with subagents (its subagent file goes
    # with it), Codex top level, Codex subagent
    assert _dry_run_sample_ids(cfg) == {SID, AID, T1, T2}


def test_dry_run_sample_fills_up_with_the_newest_others(tmp_path):
    cfg, plain, sid2 = _mixed_host(tmp_path)
    run_sync(cfg, dry_run=True, sample=5)
    assert _dry_run_sample_ids(cfg) == {SID, AID, T1, T2, plain[0], plain[1]}
    assert not (cfg.root / "sessions").exists()                                # still nothing outside .kb/


# ================================================================ item 2: headless one-prompt sessions are skipped

def _solo_host(tmp_path, claude_records=None, codex_items=None, codex_meta=None, **kw):
    """One host with a single hand-made session (Claude and/or Codex). Returns the config."""
    from fixtures import write_claude_session, write_codex_unit
    root = clone(init_remote(tmp_path), tmp_path / "root")
    src = tmp_path / "src"
    (src / "projects").mkdir(parents=True)
    paths = []
    if claude_records is not None:
        unit = write_claude_session(src, claude_records)
        paths += unit.paths
    (src / "codex" / "sessions").mkdir(parents=True)
    if codex_items is not None:
        paths += write_codex_unit(src / "codex", codex_items, meta=codex_meta).paths
    age(paths)
    return make_config(root, "host-a", src / "projects", src / "codex" / "sessions", src / "codex", **kw)


def _claude_prompts(n, entrypoint):
    from fixtures import claude_asst, claude_user
    extra = {"entrypoint": entrypoint} if entrypoint else {}
    out = []
    for i in range(n):
        out.append(claude_user(f"2026-10-06T10:{2 * i:02d}:00Z", f"question {i}", **extra))
        out.append(claude_asst(f"2026-10-06T10:{2 * i + 1:02d}:00Z", f"answer {i}", **extra))
    return out


def _codex_prompts(n):
    from fixtures import codex_msg
    out = []
    for i in range(n):
        out += [codex_msg("user", f"question {i}"), codex_msg("assistant", f"answer {i}")]
    return out


def _written(cfg):
    return sorted(p.name for p in (cfg.root / "sessions").rglob("*.md")) if (cfg.root / "sessions").exists() else []


def test_a_headless_claude_session_with_one_prompt_is_skipped_and_marked_done(tmp_path):
    cfg = _solo_host(tmp_path, claude_records=_claude_prompts(1, "sdk-cli"))
    r = run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 0 and not r.committed and _written(cfg) == []
    assert not (cfg.root / "raw").exists() and State.load(cfg.kb_dir / "sync-state.json").files   # done, not retried
    assert sync_mod.pending_units(cfg, State.load(cfg.kb_dir / "sync-state.json"), now=True) == []


def test_an_interactive_one_prompt_session_is_kept(tmp_path):
    cfg = _solo_host(tmp_path, claude_records=_claude_prompts(1, "cli"))
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 1


def test_a_headless_session_with_two_prompts_is_kept(tmp_path):
    cfg = _solo_host(tmp_path, claude_records=_claude_prompts(2, "sdk-cli"))
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 1


def test_the_skip_can_be_turned_off(tmp_path):
    cfg = _solo_host(tmp_path, claude_records=_claude_prompts(1, "sdk-cli"), skip_headless_single_prompt=False)
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 1


def test_a_skipped_headless_session_is_taken_once_it_grows_a_second_prompt(tmp_path):
    cfg = _solo_host(tmp_path, claude_records=_claude_prompts(1, "sdk-cli"))
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 0
    f = next(Path(cfg.claude_dir).rglob("*.jsonl"))
    more = _claude_prompts(2, "sdk-cli")[2:]
    with open(f, "a", encoding="utf-8") as fh:
        for rec in more:
            fh.write(json.dumps(rec) + "\n")
    age([f])
    r = run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 1 and len(_written(cfg)) == 1


def test_a_codex_exec_session_with_one_prompt_is_skipped(tmp_path):
    cfg = _solo_host(tmp_path, codex_items=_codex_prompts(1), codex_meta={"source": "exec"})
    r = run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 0 and _written(cfg) == []


def test_codex_exec_with_two_prompts_and_interactive_codex_are_kept(tmp_path):
    cfg = _solo_host(tmp_path, codex_items=_codex_prompts(2), codex_meta={"source": "exec"})
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 1
    cfg = _solo_host(tmp_path / "other", codex_items=_codex_prompts(1), codex_meta={"source": "vscode"})
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).sessions == 1


# ================================================================ item 4: branch guard and push guard

def _remote_files(cfg, ref="main"):
    return set(git("ls-tree", "-r", "--name-only", ref, cwd=cfg.root.parent / "remote.git").stdout.split())


def test_a_wrong_branch_skips_every_git_step_but_still_processes(hosts, monkeypatch):
    a, _ = hosts
    git("checkout", "-q", "-b", "feature", cwd=a.root)
    before = gitops.git(a.root, "rev-parse", "HEAD").stdout
    seen = []
    for name in ("repair", "stage", "secrets_check", "commit", "pull", "push"):
        real = getattr(gitops, name)
        monkeypatch.setattr(gitops, name, lambda *args, _n=name, _r=real, **kw: (seen.append(_n), _r(*args, **kw))[1])
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.sessions == 3 and r.errors == ["git: on branch feature, expected main; skipping git"]
    assert seen == [] and not r.committed and not r.pushed
    assert gitops.git(a.root, "rev-parse", "HEAD").stdout == before
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == ""
    assert (a.root / "sessions" / "host-a").is_dir()
    git("checkout", "-q", "main", cwd=a.root)                                   # back on main: the files are committed
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed and _md(a, "_55555555.md") in _tracked(a.root)


def test_the_configured_branch_is_the_one_that_counts(hosts):
    a, _ = hosts
    a.branch = "trunk"                                                          # the repo is on main
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == ["git: on branch main, expected trunk; skipping git"] and not r.committed
    git("checkout", "-q", "-b", "trunk", cwd=a.root)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed
    assert _remote_files(a, "trunk") >= {_md(a, "_55555555.md")}


def test_the_branch_guard_applies_without_a_remote_too(tmp_path):
    root = tmp_path / "solo"
    git("init", "-q", "-b", "other", str(root))
    src = tmp_path / "src"
    projects = make_claude_tree(src)
    sessions, home = make_codex_tree(src)
    cfg = make_config(root, "host-a", projects, sessions, home)
    r = run_sync(cfg, runner=FakeRunner(), summary_cap=0)
    assert r.sessions == 3 and "git: on branch other, expected main; skipping git" in r.errors and not r.committed


def _local_commit(cfg, files):
    """A commit by hand outside this host's folders (the sync must never push it)."""
    for name in files:
        (cfg.root / name).write_text(f"hand-made {name}\n")
    git("add", *files, cwd=cfg.root)
    git("commit", "-q", "-m", "by hand", cwd=cfg.root)


def test_unpushed_commits_outside_the_hosts_folders_block_the_push(hosts):
    a, _ = hosts
    remote_before = _remote_files(a)
    _local_commit(a, ["notes-1.txt", "notes-2.txt", "notes-3.txt", "notes-4.txt", "notes-5.txt"])
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.committed and not r.pushed
    msg = [e for e in r.errors if e.startswith("git: unpushed commits touch")]
    assert msg == ["git: unpushed commits touch notes-1.txt, notes-2.txt, notes-3.txt; push them by hand"]
    assert _remote_files(a) == remote_before                                    # nothing of ours went out either
    git("push", "-q", cwd=a.root)                                               # the owner pushes by hand
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and not r.committed and not r.pushed


def test_a_foreign_commit_that_was_already_pushed_does_not_block(hosts):
    a, _ = hosts
    _local_commit(a, ["notes-1.txt"])
    git("push", "-q", cwd=a.root)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


def test_unpushed_commits_inside_the_hosts_folders_are_pushed(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    mine = a.root / _md(a, "_55555555.md")
    mine.write_text(mine.read_text() + "\nmore\n")
    git("commit", "-q", "-am", "own change by hand", cwd=a.root)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.pushed and gitops.ahead(a.root) == 0


def test_the_first_push_without_an_upstream_is_not_blocked(tmp_path):
    remote = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    root = tmp_path / "root"
    git("clone", "-q", str(remote), str(root))
    src = tmp_path / "src"
    projects = make_claude_tree(src)
    sessions, home = make_codex_tree(src)
    cfg = make_config(root, "host-a", projects, sessions, home)
    r = run_sync(cfg, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.pushed


def test_the_pull_refusal_in_the_log_names_the_dirty_files(hosts):
    a, _ = hosts
    (a.root / "README.md").write_text("local edit\n")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert any(e.startswith("pull: ") and "not pulling: README.md" in e for e in r.errors)


# ================================================================ item 5: host marker (two machines, one host name)

HOST_TAKEN = "host 'host-a' belongs to another machine; set a unique host in the config"


def _local_id(cfg):
    return (cfg.kb_dir / "machine-id").read_text().strip()


def _marker(cfg, host="host-a"):
    return cfg.root / "sessions" / host / ".machine-id"


def test_the_machine_id_is_created_once_and_committed_as_a_marker_with_the_host_paths(hosts):
    a, _ = hosts
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    mid = _local_id(a)
    assert r.errors == [] and r.pushed and re.fullmatch(r"[0-9a-f]{32}", mid)
    assert _marker(a).read_text() == mid + "\n"
    rel = "sessions/host-a/.machine-id"
    assert rel in _tracked(a.root) and rel in _head_files(a.root) and rel in _remote_files(a)
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert _local_id(a) == mid and r.errors == [] and not r.committed        # nothing changed, nothing new


def test_the_marker_is_not_written_when_the_host_has_no_folder_yet(hosts):
    a, _ = hosts
    a.exclude_cwd_globs = ["/Users/me/*"]
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.sessions == 0 and not r.committed and not _marker(a).exists()
    assert _local_id(a)                                                       # the id itself is made at once


def test_a_dry_run_makes_no_machine_id_and_no_marker(hosts):
    a, _ = hosts
    run_sync(a, dry_run=True)
    assert not (a.kb_dir / "machine-id").exists() and not _marker(a).exists()


def test_a_missing_marker_of_existing_host_data_is_written_and_committed(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    git("rm", "-q", "sessions/host-a/.machine-id", cwd=a.root)                # data from before markers existed
    git("commit", "-q", "-m", "pre-marker state", cwd=a.root)
    git("push", "-q", cwd=a.root)
    assert not _marker(a).exists()
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed and _head_files(a.root) == ["sessions/host-a/.machine-id"]
    assert _marker(a).read_text() == _local_id(a) + "\n"


def _same_host_pulled(hosts):
    """a syncs and pushes; b (another machine, same host name) pulls that."""
    a, b = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    gitops.pull(b.root)
    b.host = "host-a"
    return a, b


def _host_listing(cfg):
    return sorted(p.relative_to(cfg.root).as_posix() for d in ("sessions", "raw", "catalog")
                  for p in (cfg.root / d / "host-a").rglob("*") if (cfg.root / d / "host-a").exists())


def test_a_second_machine_with_the_same_host_writes_nothing(hosts):
    a, b = _same_host_pulled(hosts)
    listing = _host_listing(b)
    head, remote_before = gitops.git(b.root, "rev-parse", "HEAD").stdout, _remote_files(b)
    r = run_sync(b, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [HOST_TAKEN] and r.sessions == 0 and r.summarized == 0 and not r.committed and not r.pushed
    assert _host_listing(b) == listing                                         # no write under the host's folders
    assert gitops.git(b.root, "rev-parse", "HEAD").stdout == head and _remote_files(b) == remote_before
    assert gitops.git(b.root, "diff", "--cached", "--name-only").stdout == ""
    assert not (b.kb_dir / "last-ok").exists() and State.load(b.kb_dir / "sync-state.json").files == {}
    assert State.load(b.kb_dir / "sync-state.json").last_error == HOST_TAKEN
    assert _marker(b).read_text() == _local_id(a) + "\n"                      # a's marker is untouched


def test_the_second_machine_works_once_it_has_its_own_host(hosts):
    a, b = _same_host_pulled(hosts)
    assert run_sync(b, runner=FakeRunner(), summary_cap=0).errors == [HOST_TAKEN]
    b.host = "host-b"
    r = run_sync(b, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 3 and r.committed and r.pushed
    assert (b.root / "sessions" / "host-b" / ".machine-id").read_text() == _local_id(b) + "\n"
    assert _local_id(a) != _local_id(b)


def test_a_marker_that_matches_the_local_id_is_fine(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    _grow_claude(a)
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


def test_a_clone_that_has_not_seen_the_other_marker_yet_finds_out_at_the_pull(hosts):
    """b was cloned before a pushed: its working tree has no marker, so the first run writes its own and the pull
    collides. The log says why, and from then on the fetched upstream marker stops every run."""
    a, b = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    b.host = "host-a"
    r = run_sync(b, runner=FakeRunner(), summary_cap=0)
    assert r.committed and not r.pushed
    assert any(e.startswith("pull:") for e in r.errors) and HOST_TAKEN in r.errors
    remote_before = _remote_files(b)
    r = run_sync(b, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [HOST_TAKEN] and not r.pushed and _remote_files(b) == remote_before
    assert gitops.ahead(b.root) == 1                                         # its stray commit stays local
    b.host = "host-b"                                                        # a unique host does not push the stray commit
    r = run_sync(b, now=True, runner=FakeRunner(), summary_cap=0)            # (it still collides with the remote)
    assert not r.pushed and _remote_files(b) == remote_before


def test_the_host_check_comes_before_the_git_gate_so_a_wrong_branch_is_still_blocked_locally(hosts):
    a, b = _same_host_pulled(hosts)
    git("checkout", "-q", "-b", "feature", cwd=b.root)
    r = run_sync(b, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [HOST_TAKEN] and r.sessions == 0                        # no writes under the host, no git talk


# ================================================================ item 6: a stale index.lock stops the git steps

def test_a_stale_index_lock_is_reported_and_skips_git_but_not_the_processing(hosts):
    import time as _time
    a, _ = hosts
    lock = a.root / ".git" / "index.lock"
    lock.write_text("")
    t = _time.time() - 3600
    os.utime(lock, (t, t))
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.sessions == 3 and not r.committed and not r.pushed
    assert any(e.startswith("git: ") and "index.lock" in e and "no git step this run" in e for e in r.errors)
    assert lock.exists()
    lock.unlink()
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed


# ================================================================ item 7: catalog skips are reported

def test_catalog_files_that_are_skipped_become_one_error_line_each(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).errors == []
    odd = a.root / "sessions/host-a/claude/2026/10/2026-10-06_demo_00000002.md"
    odd.write_text('---\nid: "odd-2"\nstarted: "2026-10-06T11:00:00Z"\nfiles: 5\n---\n\nbody\n')
    _grow_claude(a)                                                           # the month is rebuilt, the odd file is skipped
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    lines = [e for e in r.errors if e.startswith("catalog:")]
    assert len(lines) == 1 and lines[0].startswith("catalog: sessions/host-a/claude/2026/10/2026-10-06_demo_00000002.md: ")
    assert "\n" not in lines[0] and len(lines[0].split(": ", 2)[2]) > 0         # a reason follows the path
    assert r.committed                                                         # a skipped file is no reason to stop
    rows = [json.loads(l) for l in (a.root / "catalog/host-a/2026-10.jsonl").read_text().splitlines()]
    assert "odd-2" not in {x["id"] for x in rows} and SID in {x["id"] for x in rows}
    assert State.load(a.kb_dir / "sync-state.json").last_error.startswith("catalog:")


def test_a_clean_catalog_adds_no_error(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).errors == []


# ================================================================ a subagent copied under two sessions (resume / fork)

def _pair_host(tmp_path, name="root", **kw):
    """One host whose Claude folder holds SID and SID_B (a resume of SID) sharing the subagent AID."""
    from fixtures import make_shared_pair
    excl = kw.pop("exclude_cwd_globs", None)
    root = clone(init_remote(tmp_path / name), tmp_path / name / "root")
    src = tmp_path / name / "src"
    projects = make_shared_pair(src, **kw)
    (src / "codex" / "sessions").mkdir(parents=True)
    extra = {"exclude_cwd_globs": excl} if excl is not None else {}
    return make_config(root, "host-a", projects, src / "codex" / "sessions", src / "codex", **extra)


def _files(cfg):
    """Every file the sync writes, path -> bytes (the machine marker aside: it is random per clone)."""
    return {p.relative_to(cfg.root).as_posix(): p.read_bytes()
            for d in ("sessions", "raw", "catalog") for p in sorted((cfg.root / d).rglob("*"))
            if p.is_file() and not p.name.startswith(".")}


def _rerender(cfg, **kw):
    """A full re-render: forget what was processed, then sync."""
    (cfg.kb_dir / "sync-state.json").unlink()
    return run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0, **kw)


def _subs_of(cfg, aid):
    return sorted(n for n in (p.relative_to(cfg.root).as_posix() for p in cfg.root.rglob("*"))
                  if n.endswith(f"_sub-{short_id(aid)}.md") or n.endswith(f"__sub-{aid}.jsonl.gz"))


def _owned_by(cfg, sid, aid=AID):
    md = f"sessions/host-a/claude/2026/10/2026-10-06_demo_{short_id(sid)}_sub-{short_id(aid)}.md"
    return [md, f"raw/host-a/claude/2026/10/{sid}__sub-{aid}.jsonl.gz"]


def _links(cfg, sid):
    text = (cfg.root / _md(cfg, f"_{short_id(sid)}.md")).read_text(encoding="utf-8")
    return re.findall(r"\[subagent\]\(([^)]*)\)", text)


def test_a_shared_subagent_is_written_once_by_the_session_it_ran_under_and_both_link_to_it(tmp_path):
    from fixtures import AID_B, SID_B
    cfg = _pair_host(tmp_path)
    r = run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 2 and r.pushed
    assert _subs_of(cfg, AID) == sorted(_owned_by(cfg, SID))
    name = _owned_by(cfg, SID)[0].rsplit("/", 1)[1]
    assert _links(cfg, SID) == [name]
    assert _links(cfg, SID_B) == [name, f"2026-10-06_demo_{short_id(SID_B)}_sub-{short_id(AID_B)}.md"]
    assert _subs_of(cfg, AID_B) == sorted(_owned_by(cfg, SID_B, AID_B))
    idx = Index(cfg.kb_dir / "index.sqlite")
    rows = idx.db.execute("SELECT parent FROM sessions WHERE id=?", (AID,)).fetchall()
    dups = idx.db.execute("SELECT COUNT(*) FROM dups").fetchone()[0]
    idx.close()
    assert [x[0] for x in rows] == [SID] and dups == 0


def test_both_processing_orders_write_the_same_files(tmp_path, monkeypatch):
    seen, outputs = [], []
    real_pending, real_process = sync_mod.pending_units, sync_mod.process_unit
    monkeypatch.setattr(sync_mod, "process_unit", lambda cfg, st, rep, unit, *a, **k:
                        (seen.append(Path(unit.main).stem), real_process(cfg, st, rep, unit, *a, **k))[1])
    for name, flip in (("one", False), ("two", True)):
        monkeypatch.setattr(sync_mod, "pending_units",
                            lambda *a, flip=flip, **k: real_pending(*a, **k)[::-1] if flip else real_pending(*a, **k))
        cfg = _pair_host(tmp_path, name)
        for r in (run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0), _rerender(cfg)):
            assert r.errors == [] and r.sessions == 2
        outputs.append(_files(cfg))
    assert seen[:4] == seen[4:][::-1] and len(set(seen)) == 2
    assert outputs[0] == outputs[1]


def test_re_rendering_twice_is_byte_identical_and_deletes_nothing(tmp_path):
    cfg = _pair_host(tmp_path)
    run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    first = _files(cfg)
    for _ in range(2):
        r = _rerender(cfg)
        assert r.errors == [] and r.sessions == 2 and not r.committed
        assert _files(cfg) == first
        assert gitops.git(cfg.root, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("legacy", ["both copies", "only the resumed session's copy"])
def test_a_copy_an_older_sync_wrote_under_the_resumed_session_goes(tmp_path, monkeypatch, legacy):
    from fixtures import SID_B
    cfg = _pair_host(tmp_path)
    with monkeypatch.context() as m:                    # the old behaviour: every holder writes its copy
        m.setattr(sync_mod, "place_subagents", lambda *a: None)
        run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert _subs_of(cfg, AID) == sorted(_owned_by(cfg, SID) + _owned_by(cfg, SID_B))
    if legacy != "both copies":                          # what a later re-render of the old code left (see the PR)
        for rel in _owned_by(cfg, SID):
            git("rm", "-q", rel, cwd=cfg.root)
        git("commit", "-q", "-m", "old re-render", cwd=cfg.root)
    r = _rerender(cfg)
    assert r.errors == [] and r.committed
    assert _subs_of(cfg, AID) == sorted(_owned_by(cfg, SID))
    assert _links(cfg, SID_B)[0] == _owned_by(cfg, SID)[0].rsplit("/", 1)[1]
    after = _files(cfg)
    assert _rerender(cfg).committed is False and _files(cfg) == after


def test_an_owner_the_sync_skips_leaves_the_subagent_to_the_next_holder(tmp_path):
    from fixtures import SID_B
    cfg = _pair_host(tmp_path, cwd_b="/Users/me/Repositories/demo-b", exclude_cwd_globs=["/Users/me/Repositories/demo"])
    r = run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.sessions == 1
    md = f"sessions/host-a/claude/2026/10/2026-10-06_demo-b_{short_id(SID_B)}_sub-{short_id(AID)}.md"
    assert _subs_of(cfg, AID) == sorted([md, f"raw/host-a/claude/2026/10/{SID_B}__sub-{AID}.jsonl.gz"])


def test_a_subagent_file_whose_parent_transcript_is_gone_stays_where_it_is(tmp_path):
    from fixtures import SID_B
    cfg = _pair_host(tmp_path)
    run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0)
    before = _files(cfg)
    main = next(Path(cfg.claude_dir).rglob(f"{SID}.jsonl"))
    shutil.rmtree(main.with_suffix(""))                 # Claude Code's cleanup deletes the old session
    main.unlink()
    r = _rerender(cfg)
    assert r.errors == [] and r.sessions == 1 and not r.committed
    assert _files(cfg) == before
    assert _links(cfg, SID_B)[0] == _owned_by(cfg, SID)[0].rsplit("/", 1)[1]


@pytest.mark.parametrize("names, owner", [
    ("b b b b", "b"),                                   # it only ever ran under the resumed session
    ("x x x x", "a"),                                   # it names neither: the smallest session id
])
def test_the_owner_follows_the_session_the_records_name(tmp_path, names, owner):
    from fixtures import SID_B
    ids = {"a": SID, "b": SID_B, "x": "77777777-0000-0000-0000-000000000000"}
    cfg = _pair_host(tmp_path, names=[ids[n] for n in names.split()])
    assert run_sync(cfg, now=True, runner=FakeRunner(), summary_cap=0).errors == []
    assert _subs_of(cfg, AID) == sorted(_owned_by(cfg, ids[owner]))


# ================================================================ raw copies wait until the session settles

def _fresh(cfg, sid=""):
    """Make the host's source files (only those of `sid`, if given) 1 hour old: past the quiet period, not settled."""
    files = list(Path(cfg.claude_dir).rglob("*.jsonl")) + list(Path(cfg.codex_dirs[0]).rglob("*.jsonl"))
    age([f for f in files if sid in str(f)], 3600)


def _days_later(days=1.1):
    return FakeClock(time.time() + days * 86400)


def _raw(cfg, sid=SID):
    return cfg.root / f"raw/{cfg.host}/claude/2026/10/{sid}.jsonl.gz"


def _tree(root, folder):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / folder).rglob("*") if p.is_file()}


def _clean(cfg):
    return gitops.git(cfg.root, "status", "--porcelain").stdout == ""


def _wipe(cfg):
    """Remove everything the sync derives; keep .kb/machine-id (it says which machine owns the host)."""
    mid = (cfg.kb_dir / "machine-id").read_text()
    for d in ("sessions", "raw", "catalog", ".kb"):
        shutil.rmtree(cfg.root / d, ignore_errors=True)
    cfg.kb_dir.mkdir()
    (cfg.kb_dir / "machine-id").write_text(mid)


def test_an_active_session_gets_its_markdown_now_and_its_raw_copy_once_it_settles(hosts):
    a, _ = hosts
    assert a.raw_settle_hours == 24
    _fresh(a)
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and r.sessions == 3 and r.committed and r.pushed
    assert _md(a, "_55555555.md") in _tracked(a.root) and not (a.root / "raw").exists()
    waiting = State.load(a.kb_dir / "sync-state.json").raw_pending
    assert len(waiting) == 3
    assert pending_units(a, State.load(a.kb_dir / "sync-state.json"), now=True) == []      # kb status: 0 pending
    r = run_sync(a, now=True, runner=FakeRunner())          # waiting is no work, even with --now
    assert r.errors == [] and r.sessions == 0 and not r.committed and not (a.root / "raw").exists()
    md = _tree(a.root, "sessions")
    later = _days_later()
    assert len(pending_units(a, State.load(a.kb_dir / "sync-state.json"), clock=later)) == 3
    r = run_sync(a, runner=FakeRunner(), clock=later)
    assert r.errors == [] and r.sessions == 3 and r.committed and r.pushed
    assert _raw(a).exists() and set(_head_files(a.root)) == set(_tree(a.root, "raw"))     # the commit is the raw copies
    assert _tree(a.root, "sessions") == md                                                  # the markdown did not change
    st = State.load(a.kb_dir / "sync-state.json")
    assert st.raw_pending == {} and all(st.files.get(k) == fp for k, fp in waiting.items())
    r = run_sync(a, runner=FakeRunner(), clock=later)
    assert r.sessions == 0 and not r.committed


def test_a_growing_session_rewrites_its_markdown_but_not_its_raw_copy_until_it_settles_again(hosts):
    a, _ = hosts
    run_sync(a, runner=FakeRunner(), clock=_days_later())  # the fixture sessions are settled: all raw copies written
    raw = _raw(a)
    rel, before = raw.relative_to(a.root).as_posix(), raw.read_bytes()
    md = _md(a, "_55555555.md")
    for _ in range(3):                                      # the session goes on, with pauses past the quiet period
        _grow_claude(a)
        _fresh(a, SID)
        r = run_sync(a, runner=FakeRunner())
        assert r.errors == [] and r.sessions == 1 and r.committed
        assert md in _head_files(a.root) and rel not in _head_files(a.root) and raw.read_bytes() == before
    r = run_sync(a, runner=FakeRunner(), clock=_days_later())
    assert r.errors == [] and r.sessions == 1 and r.committed
    assert _head_files(a.root) == [rel] and raw.read_bytes() != before                     # one new version, once
    assert _clean(a)


def test_raw_settle_hours_0_writes_the_raw_copy_at_every_sync_as_before(hosts):
    a, _ = hosts
    a.raw_settle_hours = 0
    _fresh(a)
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and _raw(a).exists() and State.load(a.kb_dir / "sync-state.json").raw_pending == {}
    rel, before = _raw(a).relative_to(a.root).as_posix(), _raw(a).read_bytes()
    _grow_claude(a)
    _fresh(a, SID)
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and rel in _head_files(a.root) and _raw(a).read_bytes() != before


def test_cold_rebuild_is_identical_while_raw_copies_wait_and_after_they_settle(hosts):
    a, _ = hosts
    _fresh(a)
    run_sync(a, runner=FakeRunner())
    _wipe(a)
    r = run_sync(a, runner=FakeRunner())
    assert r.errors == [] and r.sessions == 3 and not r.committed and _clean(a) and not (a.root / "raw").exists()
    later = _days_later()
    assert run_sync(a, runner=FakeRunner(), clock=later).committed
    _wipe(a)
    r = run_sync(a, runner=FakeRunner(), clock=later)
    assert r.errors == [] and r.sessions == 3 and not r.committed and _clean(a) and _raw(a).exists()


def test_a_session_that_moves_while_its_raw_copy_waits_takes_the_copy_along(hosts):
    a, _ = hosts
    run_sync(a, runner=FakeRunner(), clock=_days_later())  # settled: raw copies in 2026/10
    _rewrite_month(a, SID)
    _fresh(a, SID)                                          # it now starts in 2026/11 and is active again
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed
    tracked = _tracked(a.root)
    for name in (f"{SID}.jsonl.gz", f"{SID}__sub-{AID}.jsonl.gz"):
        assert f"raw/host-a/claude/2026/11/{name}" in tracked and f"raw/host-a/claude/2026/10/{name}" not in tracked
        assert not (a.root / f"raw/host-a/claude/2026/10/{name}").exists()
    assert _clean(a)
