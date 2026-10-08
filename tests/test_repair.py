"""Incident of PR #1: another machine wrote summaries into this host's files; the next sync could not pull.

Covers: the pull conflict error points to `kb repair`, `kb repair` itself (and when it refuses), and the refusal to
write summaries for a host this machine does not own.
"""
import json
from pathlib import Path

import pytest
from fixtures import SID, clone, git
from test_sync import (FakeRunner, _fresh, _grow_claude, _head_files, _md, _no_gitleaks, _remote_files,  # noqa: F401
                       hosts)

from kb import gitops, repair as repair_mod
from kb import sync as sync_mod
from kb.catalog import write_catalog
from kb.cli import main
from kb.distill import split_front_matter, update_front_matter
from kb.index import Index
from kb.lock import Lock
from kb.paths import month_of
from kb.state import State
from kb.sync import Report, run_sync

pytestmark = pytest.mark.slow          # starts git and other processes; runs with scripts/test --all

VM_SUMMARY = {"summary": "Summary written on the vm.", "tags": ["vm"], "outcome": "done", "decisions": []}


def _meta(path):
    return split_front_matter(Path(path).read_text(encoding="utf-8"))[0]


def _remote(cfg):
    return cfg.root.parent / "remote.git"


def _rev(root, ref="HEAD"):
    return gitops.git(root, "rev-parse", ref).stdout.strip()


def _vm_writes_summaries(cfg, tmp_path):
    """What PR #1 did: a clone on another machine sets the summary fields of host-a's top-level sessions, rebuilds
    host-a's catalog and pushes to main. Returns the md paths it changed."""
    vm = clone(_remote(cfg), tmp_path / "vm-pr")
    changed = []
    for md in sorted((vm / "sessions" / cfg.host).rglob("*.md")):
        meta = _meta(md)
        if meta.get("parent"):
            continue
        update_front_matter(md, {**VM_SUMMARY, "summary_turns": meta["turns"]})
        changed.append(md.relative_to(vm).as_posix())
    assert changed
    write_catalog(vm, cfg.host, {month_of(p) for p in changed})
    git("add", "-A", cwd=vm)
    git("commit", "-q", "-m", "sync(host-a): 0 sessions, N summaries (#1)", cwd=vm)
    git("push", "-q", cwd=vm)
    return changed


def _incident(hosts, tmp_path):
    """host-a pushed without summaries; the vm pushed summaries for it; host-a re-rendered a grown session without
    them. The next sync of host-a commits, then its pull --rebase conflicts."""
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    changed = _vm_writes_summaries(a, tmp_path)
    _grow_claude(a)
    return a, changed


# ================================================================ the conflict and its error message

def test_the_incident_conflict_points_to_kb_repair_and_repeats_on_every_sync(hosts, tmp_path):
    a, _ = _incident(hosts, tmp_path)
    for _ in range(2):                                                    # every later sync fails the same way
        r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
        assert not r.pushed and r.errors
        assert r.errors[0].startswith("pull: ") and "kb repair" in r.errors[0]
        assert "kb repair" in r.line()                                    # the one-line log keeps the hint
        assert "kb repair" in State.load(a.kb_dir / "sync-state.json").last_error     # and so does kb status
        assert gitops.repair(a.root) == ""                                # the rebase was aborted cleanly
        assert gitops.current_branch(a.root) == "main"


def test_a_pull_refused_for_local_changes_does_not_point_to_kb_repair(hosts):
    a, _ = hosts
    (a.root / "README.md").write_text("local edit\n")
    r = run_sync(a, runner=FakeRunner(), summary_cap=0)
    assert any(e.startswith("pull: ") for e in r.errors) and not any("kb repair" in e for e in r.errors)


def test_a_conflict_from_a_host_name_clash_points_to_the_host_not_to_kb_repair(hosts):
    """b uses a's host name and had not pulled a's marker: its pull conflicts, but the fix is a unique host."""
    a, b = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    b.host = "host-a"
    r = run_sync(b, runner=FakeRunner(), summary_cap=0)
    assert not r.pushed and any(e.startswith("pull: ") for e in r.errors)
    assert "host 'host-a' belongs to another machine; set a unique host in the config" in r.errors
    assert not any("kb repair" in e for e in r.errors)


# ================================================================ gitops: a rebase conflict is a PullConflict

def _two_clones(tmp_path, remote):
    one, two = clone(remote, tmp_path / "one"), clone(remote, tmp_path / "two")
    for root, text in ((one, "one\n"), (two, "two\n")):
        (root / "README.md").write_text(text)
        git("commit", "-q", "-am", f"edit {text.strip()}", cwd=root)
    git("push", "-q", cwd=one)
    return one, two


def test_a_pull_that_conflicts_raises_pull_conflict_and_is_aborted(hosts, tmp_path):
    a, _ = hosts
    _, two = _two_clones(tmp_path, _remote(a))
    head = _rev(two)
    with pytest.raises(gitops.PullConflict) as e:
        gitops.pull(two)
    assert "README.md" in str(e.value)
    assert _rev(two) == head and gitops.repair(two) == "" and gitops.current_branch(two) == "main"


def test_a_pull_that_fails_without_a_conflict_is_a_plain_git_error(hosts, tmp_path):
    a, _ = hosts
    root = clone(_remote(a), tmp_path / "lost")
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=root)
    with pytest.raises(gitops.GitError) as e:
        gitops.pull(root)
    assert not isinstance(e.value, gitops.PullConflict)


def test_a_push_whose_retry_pull_conflicts_raises_pull_conflict(hosts, tmp_path):
    a, _ = hosts
    _, two = _two_clones(tmp_path, _remote(a))
    with pytest.raises(gitops.PullConflict) as e:
        gitops.push(two)
    assert str(e.value).startswith("push failed: ")


def test_a_push_that_ends_in_a_conflict_points_to_kb_repair(hosts, tmp_path, monkeypatch):
    """The remote moves between our pull and our push, and the retry's pull conflicts."""
    a, changed = _incident(hosts, tmp_path)
    real_pull, calls = gitops.pull, []

    def pull_once_late(root, keep=()):
        calls.append(root)
        if len(calls) == 1:
            return None                                                   # as if the remote moved after this pull
        return real_pull(root, keep=keep)

    monkeypatch.setattr(gitops, "pull", pull_once_late)
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert not r.pushed and len(calls) == 2
    assert "kb repair" in r.errors[0] and "push failed" in r.errors[0]
    assert "kb repair" in r.line()


# ================================================================ kb repair: the recovery

def test_kb_repair_recovers_and_the_next_sync_pushes_and_keeps_the_remote_summaries(hosts, tmp_path):
    a, changed = _incident(hosts, tmp_path)
    assert not run_sync(a, now=True, runner=FakeRunner(), summary_cap=0).pushed
    old_head, upstream = _rev(a.root), _rev(a.root, "@{u}")
    assert gitops.ahead(a.root) == 1

    lines = repair_mod.repair(a)

    text = "\n".join(lines)
    assert _rev(a.root) == upstream and gitops.ahead(a.root) == 0
    assert gitops.git(a.root, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert State.load(a.kb_dir / "sync-state.json").files == {}          # fingerprints cleared: every session again
    backups = gitops.git(a.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/kb/").stdout.split("\n")
    backup = [b.split() for b in backups if b]
    assert len(backup) == 1 and backup[0][1] == old_head                 # the dropped commit is kept locally
    assert backup[0][0] in text and "1 local commit" in text and "kb sync --now --no-summaries" in text
    assert "origin/main" in text

    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.committed and r.pushed and gitops.ahead(a.root) == 0
    sid_md = _md(a, "_55555555.md")
    # only real diffs are committed: the grown session (markdown, raw) and its month's catalog
    assert sorted(_head_files(a.root)) == sorted([sid_md, _meta(a.root / sid_md)["raw"], "catalog/host-a/2026-10.jsonl"])
    remote = _remote(a)
    for rel in changed:                                                   # every summary of the vm survives, on main
        meta = _meta_at(remote, rel)
        assert meta["summary"] == VM_SUMMARY["summary"] and meta["tags"] == ["vm"]
    grown = _meta_at(remote, sid_md)
    assert grown["turns"] > grown["summary_turns"]                        # the new turns are there, with the summary
    catalog = [json.loads(x) for x in _show(remote, "catalog/host-a/2026-10.jsonl").splitlines()]
    assert {c["summary"] for c in catalog if not c.get("parent")} == {VM_SUMMARY["summary"]}


def test_kb_repair_forgets_waiting_raw_copies_so_an_active_session_renders_again(hosts, tmp_path):
    """A raw copy that waits records that the session's markdown is written; the reset can drop that markdown."""
    a, _ = _incident(hosts, tmp_path)
    _fresh(a, SID)                                                        # the grown session is still active
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert not r.pushed and State.load(a.kb_dir / "sync-state.json").raw_pending
    repair_mod.repair(a)
    assert State.load(a.kb_dir / "sync-state.json").raw_pending == {}
    r = run_sync(a, now=True, runner=FakeRunner(), summary_cap=0)
    assert r.errors == [] and r.pushed and _md(a, "_55555555.md") in _head_files(a.root)    # rendered again, pushed


def _show(remote, rel, ref="main"):
    return git("show", f"{ref}:{rel}", cwd=remote).stdout


def _meta_at(remote, rel):
    return split_front_matter(_show(remote, rel))[0]


def test_kb_repair_discards_uncommitted_changes_inside_the_hosts_folders(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    mine = a.root / _md(a, "_55555555.md")
    before = mine.read_text()
    mine.write_text(before + "\nleft by an interrupted run\n")
    lines = repair_mod.repair(a)
    assert mine.read_text() == before
    assert any("1 file" in x for x in lines)
    assert gitops.git(a.root, "for-each-ref", "refs/kb/").stdout == ""   # no commit dropped: no backup ref


@pytest.mark.parametrize("files", [["notes.txt"], ["notes-1.txt", "notes-2.txt", "notes-3.txt", "notes-4.txt"]])
def test_kb_repair_refuses_local_commits_outside_the_hosts_folders(hosts, files):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    for name in files:
        (a.root / name).write_text("by hand\n")
    git("add", *files, cwd=a.root)
    git("commit", "-q", "-m", "by hand", cwd=a.root)
    _assert_refused(a, files[0])


def test_kb_repair_refuses_uncommitted_changes_outside_the_hosts_folders(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    (a.root / "README.md").write_text("by hand\n")
    _assert_refused(a, "README.md")
    assert (a.root / "README.md").read_text() == "by hand\n"


def test_kb_repair_refuses_another_branch(hosts):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    git("checkout", "-q", "-b", "other", cwd=a.root)
    _assert_refused(a, "on branch other, expected main")


def test_kb_repair_refuses_a_clone_without_an_upstream(tmp_path, hosts):
    a, _ = hosts
    root = tmp_path / "solo"
    git("init", "-q", "-b", "main", str(root))
    (root / "README.md").write_text("x\n")
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "x", cwd=root)
    a.root = root
    _assert_refused(a, "upstream")


def test_kb_repair_refuses_while_a_sync_runs(hosts):
    a, _ = hosts
    lock = Lock(a.kb_dir / "lock")
    assert lock.acquire()
    try:
        _assert_refused(a, "another sync is running")
    finally:
        lock.release()


def _assert_refused(cfg, *needles):
    head = gitops.git(cfg.root, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout
    state = cfg.kb_dir / "sync-state.json"
    State(path=state, files={"k": "fp"}).save()
    with pytest.raises(repair_mod.RepairRefused) as e:
        repair_mod.repair(cfg)
    for needle in needles:
        assert needle in str(e.value)
    assert "\n" not in str(e.value)
    assert gitops.git(cfg.root, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout == head
    assert State.load(state).files == {"k": "fp"}                        # nothing changed
    assert gitops.git(cfg.root, "for-each-ref", "refs/kb/", check=False).stdout == ""


# ================================================================ the CLI: kb repair

def _write_config(tmp_path, monkeypatch, cfg, **extra):
    p = tmp_path / "cli-config.json"
    data = {"root": str(cfg.root), "host": cfg.host, "claude_dir": str(cfg.claude_dir),
            "codex_dirs": [str(d) for d in cfg.codex_dirs], "codex_home": str(cfg.codex_home), **extra}
    p.write_text(json.dumps(data))
    monkeypatch.setenv("KB_CONFIG", str(p))


def test_cli_repair_prints_what_it_did(hosts, tmp_path, monkeypatch, capsys):
    a, _ = _incident(hosts, tmp_path)
    assert not run_sync(a, now=True, runner=FakeRunner(), summary_cap=0).pushed
    _write_config(tmp_path, monkeypatch, a)
    assert main(["repair"]) == 0
    out = capsys.readouterr().out
    assert "1 local commit" in out and "kb sync --now --no-summaries" in out


def test_cli_repair_refusal_is_one_line_and_exit_2(hosts, tmp_path, monkeypatch, capsys):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    (a.root / "README.md").write_text("by hand\n")
    _write_config(tmp_path, monkeypatch, a)
    assert main(["repair"]) == 2
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and out.startswith("kb repair: ") and "nothing changed" in out


# ================================================================ summaries only on the machine that owns the host

class FakeSummarize:
    def __init__(self):
        self.calls = 0

    def __call__(self, text, model, runner=None, cwd=None):
        self.calls += 1
        return dict(VM_SUMMARY)


def _vm(hosts, tmp_path, host="vm"):
    """A clone on another machine (its own .kb/machine-id), after host-a pushed its sessions without summaries."""
    a, b = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    gitops.pull(b.root)
    b.host = host
    return a, b


def _host_a_files(cfg):
    return {p.relative_to(cfg.root).as_posix(): p.read_bytes() for d in ("sessions", "catalog")
            for p in (cfg.root / d / "host-a").rglob("*") if p.is_file()}


def _summarize(cfg, **kw):
    idx = Index(cfg.kb_dir / "index.sqlite")
    idx.update(cfg.root)
    lock = Lock(cfg.kb_dir / "lock")
    try:
        state = State.load(cfg.kb_dir / "sync-state.json")
        return sync_mod.summarize_pending(cfg, idx, state, None, FakeRunner(), Report(), lock, **kw)
    finally:
        idx.close()


def test_summaries_for_another_host_are_refused_by_the_library(hosts, tmp_path, monkeypatch):
    _, vm = _vm(hosts, tmp_path)
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    before = _host_a_files(vm)
    with pytest.raises(sync_mod.ForeignHost) as e:
        _summarize(vm, host="host-a")
    assert "host-a" in str(e.value) and "kb backfill --summaries" in str(e.value)
    assert fake.calls == 0 and _host_a_files(vm) == before


def test_a_config_that_borrows_another_machines_host_is_refused_by_the_library(hosts, tmp_path, monkeypatch):
    _, vm = _vm(hosts, tmp_path, host="host-a")                           # the vm's config says host-a
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    before = _host_a_files(vm)
    with pytest.raises(sync_mod.ForeignHost) as e:
        _summarize(vm)
    assert "another machine" in str(e.value)
    assert fake.calls == 0 and _host_a_files(vm) == before


def test_force_host_lets_the_library_write_another_hosts_summaries(hosts, tmp_path, monkeypatch):
    _, vm = _vm(hosts, tmp_path)
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    done, months = _summarize(vm, host="host-a", force_host=True)
    assert done == fake.calls == 2 and months
    assert _meta(vm.root / _md_of(vm, "host-a", "_55555555.md"))["summary"] == VM_SUMMARY["summary"]


def test_the_owner_still_summarizes_its_own_host(hosts, monkeypatch):
    a, _ = hosts
    assert run_sync(a, runner=FakeRunner(), summary_cap=0).pushed
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    done, _ = _summarize(a)
    assert done == fake.calls == 2


def _md_of(cfg, host, suffix):
    return next(p.relative_to(cfg.root).as_posix() for p in (cfg.root / "sessions" / host).rglob("*.md")
                if p.name.endswith(suffix))


def test_cli_backfill_summaries_for_another_host_is_refused(hosts, tmp_path, monkeypatch, capsys):
    _, vm = _vm(hosts, tmp_path)
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    _write_config(tmp_path, monkeypatch, vm)
    before, head = _host_a_files(vm), _rev(vm.root)
    assert main(["backfill", "--summaries", "--host", "host-a"]) == 2
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1 and "host-a" in out and "--force-host" in out
    assert fake.calls == 0 and _host_a_files(vm) == before and _rev(vm.root) == head
    assert not (vm.root / "sessions" / "vm").exists()                     # no sync of the vm's own sessions either


def test_cli_backfill_with_a_borrowed_host_writes_no_summary(hosts, tmp_path, monkeypatch, capsys):
    _, vm = _vm(hosts, tmp_path, host="host-a")
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    _write_config(tmp_path, monkeypatch, vm)
    before = _host_a_files(vm)
    assert main(["backfill", "--summaries"]) == 1
    assert "belongs to another machine" in capsys.readouterr().out
    assert fake.calls == 0 and _host_a_files(vm) == before


def test_cli_backfill_force_host_writes_summaries_only_and_touches_no_git(hosts, tmp_path, monkeypatch, capsys):
    a, vm = _vm(hosts, tmp_path)
    fake = FakeSummarize()
    monkeypatch.setattr(sync_mod, "summarize", fake)
    _write_config(tmp_path, monkeypatch, vm)
    head, remote_before = _rev(vm.root), _remote_files(vm)
    assert main(["backfill", "--summaries", "--host", "host-a", "--force-host"]) == 0
    out = capsys.readouterr().out
    assert fake.calls == 2 and "2 summaries" in out and "not committed" in out and "kb repair" in out
    changed = gitops.git(vm.root, "status", "--porcelain").stdout
    assert changed and all(" sessions/host-a/" in x or " catalog/host-a/" in x for x in changed.splitlines())
    assert _rev(vm.root) == head and _remote_files(vm) == remote_before
    assert not (vm.root / "sessions" / "vm").exists()                     # the vm's own sessions were not synced


@pytest.mark.parametrize("argv", [["backfill", "--host", "host-a"], ["backfill", "--force-host"],
                                  ["backfill", "--summaries", "--force-host"]])
def test_cli_backfill_host_flags_need_summaries_and_a_foreign_host(hosts, tmp_path, monkeypatch, capsys, argv):
    _, vm = _vm(hosts, tmp_path)
    calls = []
    monkeypatch.setattr(sync_mod, "run_sync", lambda cfg, **kw: calls.append(kw) or Report())
    _write_config(tmp_path, monkeypatch, vm)
    assert main(argv) == 2
    assert len(capsys.readouterr().out.splitlines()) == 1 and calls == []


def test_cli_backfill_host_that_is_the_configured_one_is_a_plain_backfill(hosts, tmp_path, monkeypatch, capsys):
    a, _ = hosts
    calls = []
    monkeypatch.setattr(sync_mod, "run_sync", lambda cfg, **kw: calls.append(kw) or Report())
    _write_config(tmp_path, monkeypatch, a)
    assert main(["backfill", "--summaries", "--host", "host-a"]) == 0
    assert calls == [{"now": True, "summary_cap": None, "max_age_days": None, "push": True}]
