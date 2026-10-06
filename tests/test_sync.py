import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures import AID, SID, T1, T2, clone, init_remote, make_claude_tree, make_codex_tree, make_config

from kb import gitops
from kb.index import Index
from kb.lock import Lock
from kb.state import State
from kb.sync import run_sync

SUMMARY = {"summary": "Did the thing.", "tags": ["demo"], "outcome": "done", "decisions": []}


class FakeRunner:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = json.dumps({"type": "result", "is_error": False, "result": json.dumps(SUMMARY)}) if self.ok else "boom"
        return subprocess.CompletedProcess(cmd, 0 if self.ok else 1, stdout=out, stderr="")


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
    a, _ = hosts
    for _ in range(4):
        run_sync(a, runner=FakeRunner(ok=False))
    assert set(State.load(a.kb_dir / "sync-state.json").summary_attempts.values()) == {3}
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
