import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures import AID, SID, T1, T2, age, clone, git, init_remote, make_claude_tree, make_codex_tree, make_config

from kb import gitops
from kb import sync as sync_mod
from kb.cli import cmd_sync
from kb.index import Index
from kb.lock import Lock
from kb.state import State
from kb.sync import Report, run_sync

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
    a, _ = hosts
    run_sync(a, runner=FakeRunner())
    for d in ("sessions", "raw", "catalog", ".kb"):
        shutil.rmtree(a.root / d)
    r = run_sync(a, runner=FakeRunner())
    assert r.sessions == 3 and not r.committed
    assert gitops.git(a.root, "status", "--porcelain").stdout == ""


# ================================================================ helpers for the tests below

def _add_sessions(cfg, n):
    """n more Claude sessions (each with a subagent) in the host's source tree. Returns their ids."""
    sids = [f"{i + 1:08x}-2222-3333-4444-555555555555" for i in range(n)]
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
    mine = a.root / _md(a, "_11111111.md")                 # a crash left an own tracked file changed
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
    assert _tracked(a.root) >= {_md(a, "_11111111.md")}


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
    r = run_sync(a, now=True, runner=runner, clock=clock, summary_cap=None)
    assert len(runner.calls) == 2 and r.summarized == 2                        # t=0 and t=100 run; t=200 is over budget
    clock2 = FakeClock()
    runner = FakeRunner(tick=lambda: clock2.advance(100))
    run_sync(a, now=True, runner=runner, clock=clock2, summary_cap=None)       # the next run starts a fresh budget
    assert len(runner.calls) == 2


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
    gitleaks.flag("_11111111.md")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    flagged = _md(a, "_11111111.md")
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
    assert _md(a, "_11111111.md") in _tracked(a.root)


def test_a_changed_tracked_file_that_is_flagged_is_not_committed(hosts, gitleaks):
    """`git commit -- <dir>` takes unstaged tracked changes too; the quarantine must still hold it back."""
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    held, other = _md(a, "_11111111.md"), _md(a, f"_{T1[:8]}.md")
    for rel in (held, other):
        (a.root / rel).write_text((a.root / rel).read_text() + "\nmore\n")
    gitleaks.flag("_11111111.md")
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
    held = _md(a, "_11111111.md")
    (a.root / held).write_text((a.root / held).read_text() + "\nmore\n")
    gitleaks.flag("_11111111.md")
    head = gitops.git(a.root, "rev-parse", "HEAD").stdout
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert not r.committed and gitops.git(a.root, "rev-parse", "HEAD").stdout == head
    assert gitops.git(a.root, "diff", "--cached", "--name-only").stdout == "" and list(_quarantine(a)) == [held]


def test_quarantine_is_cleared_when_gitleaks_is_gone(hosts, gitleaks, monkeypatch, tmp_path):
    a, _ = hosts
    gitleaks.flag("_11111111.md")
    run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert _quarantine(a)
    _path(monkeypatch, tmp_path)                                               # gitleaks no longer installed
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert r.committed and r.quarantined == [] and _quarantine(a) == {}


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
