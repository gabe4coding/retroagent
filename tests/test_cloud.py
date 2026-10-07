"""Cloud sessions: the cloud pushes a session to the inbox on its branch; one machine's sync imports it as host cloud."""
import gzip
import io
import json
import os
from pathlib import Path

import pytest
from fixtures import GH_TOKEN, SID, clone, git, init_remote, make_claude_tree, make_codex_tree, make_config

from kb import cloud
from kb.index import Index
from kb.sync import own_paths, run_sync
from test_sync import FakeRunner

pytestmark = pytest.mark.slow          # starts git

BRANCH = "claude/demo-x1y2"


def _remote_files(remote, ref) -> list:
    out = git("ls-tree", "-r", "--name-only", ref, cwd=remote).stdout
    return out.split()


def _has_branch(remote, name) -> bool:
    return bool(git("branch", "--list", name, cwd=remote).stdout.strip())


@pytest.fixture
def setup_(tmp_path):
    remote = init_remote(tmp_path)
    box = clone(remote, tmp_path / "box")                    # the cloud session's data clone, on its own branch
    git("checkout", "-q", "-b", BRANCH, cwd=box)
    projects = make_claude_tree(tmp_path / "cloud-src")
    transcript = next(projects.glob("*/*.jsonl"))
    cloud_cfg = make_config(box, "box", projects, tmp_path / "none", tmp_path / "none")
    home = clone(remote, tmp_path / "home")
    src = tmp_path / "home-src"
    sessions, codex_home = make_codex_tree(src)
    home_cfg = make_config(home, "host-a", src / "projects", sessions, codex_home, cloud_import=True)
    return remote, cloud_cfg, transcript, home_cfg


def test_push_commits_the_slim_transcript_on_the_sessions_branch(setup_):
    remote, cfg, transcript, _ = setup_
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "sessionId": SID, "timestamp": "2026-10-06T14:00:00.000Z",
                             "message": {"role": "user", "content": f"token {GH_TOKEN}"}}) + "\n")
    assert cloud.push(cfg, str(transcript)).startswith("pushed 2 file(s)")
    files = _remote_files(remote, BRANCH)
    proj = transcript.parent.name
    assert f"inbox/claude/{proj}/{SID}.jsonl.gz" in files
    assert any(f.startswith(f"inbox/claude/{proj}/{SID}/subagents/agent-") for f in files)
    raw = cloud._blob(remote, git("rev-parse", f"{BRANCH}:inbox/claude/{proj}/{SID}.jsonl.gz", cwd=remote)
                      .stdout.strip())
    assert GH_TOKEN not in gzip.decompress(raw).decode("utf-8")              # slim and redacted
    assert git("status", "--porcelain", cwd=cfg.root).stdout == ""         # the working tree is not touched
    assert cloud.push(cfg, str(transcript)) == "nothing new"
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "sessionId": SID, "timestamp": "2026-10-06T14:01:00.000Z",
                             "message": {"role": "user", "content": "one more"}}) + "\n")
    assert cloud.push(cfg, str(transcript)).startswith("pushed 1 file(s)")


def test_push_refuses_the_sync_branch(setup_):
    _, cfg, transcript, _ = setup_
    git("checkout", "-q", "main", cwd=cfg.root)
    with pytest.raises(cloud.CloudError, match="never to main"):
        cloud.push(cfg, str(transcript))


def test_one_machine_imports_cloud_sessions_as_host_cloud(setup_):
    remote, cfg, transcript, home = setup_
    cloud.push(cfg, str(transcript))
    assert "inbox/claude" not in own_paths(make_config(home.root, "x", "p", "s", "h"))
    assert "sessions/cloud" in own_paths(home)
    rep = run_sync(home, now=True, runner=FakeRunner())
    assert rep.errors == [] and rep.pushed, rep.errors
    idx = Index(home.kb_dir / "index.sqlite")
    rows = idx.db.execute("SELECT host, summary FROM sessions WHERE id=?", (SID,)).fetchall()
    idx.close()
    assert [r[0] for r in rows] == ["cloud"] and rows[0][1]                 # host cloud, summarized
    head = git("ls-tree", "-r", "--name-only", "main", cwd=remote).stdout
    assert "sessions/cloud/.machine-id" in head and "catalog/cloud/" in head and "inbox/" not in head
    assert not _has_branch(remote, BRANCH)                                   # imported, so the branch is deleted
    # the cloud session goes on: its next push starts the branch again from main
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "sessionId": SID, "timestamp": "2026-10-06T14:02:00.000Z",
                             "message": {"role": "user", "content": "later"}}) + "\n")
    assert cloud.push(cfg, str(transcript)).startswith("pushed")
    assert git("merge-base", "--is-ancestor", "main", BRANCH, cwd=remote).returncode == 0
    rep = run_sync(home, now=True, runner=FakeRunner())
    assert rep.errors == [] and rep.sessions == 1 and not _has_branch(remote, BRANCH)


def test_imported_files_keep_the_pushs_time(setup_):
    _, cfg, transcript, home = setup_
    cloud.push(cfg, str(transcript))
    git("fetch", "-q", "origin", cwd=home.root)
    tip = git("rev-parse", f"origin/{BRANCH}", cwd=home.root).stdout.strip()
    pushed = int(git("show", "-s", "--format=%ct", tip, cwd=home.root).stdout.strip())
    ccfg = cloud.lane(home)
    written, errors, done = cloud.import_inbox(home, ccfg)
    assert errors == [] and written == 2 and done == [(BRANCH, tip)]
    main = next(Path(ccfg.claude_dir).glob("*/*.jsonl"))
    assert int(os.stat(main).st_mtime) == pushed
    assert cloud.import_inbox(home, ccfg)[0] == 0                            # nothing new: nothing written


def test_a_branch_with_other_changes_is_kept(setup_):
    remote, cfg, transcript, home = setup_
    (cfg.root / "notes.md").write_text("work\n")
    git("add", "notes.md", cwd=cfg.root)
    git("commit", "-q", "-m", "notes", cwd=cfg.root)
    git("push", "-q", "origin", BRANCH, cwd=cfg.root)
    cloud.push(cfg, str(transcript))
    rep = run_sync(home, now=True, runner=FakeRunner())
    assert rep.errors == [] and _has_branch(remote, BRANCH)
    idx = Index(home.kb_dir / "index.sqlite")
    assert idx.db.execute("SELECT host FROM sessions WHERE id=?", (SID,)).fetchone()[0] == "cloud"
    idx.close()


def _records(path, n):
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(1, n + 1):
            fh.write(json.dumps({"type": "user", "sessionId": "late-session", "timestamp": f"2026-10-06T14:00:{i:02d}Z",
                                 "message": {"role": "user", "content": "Continue working on the same task."}}) + "\n")


def test_an_older_copy_of_the_same_size_never_replaces_a_newer_one(setup_, monkeypatch):
    remote, cfg, transcript, home = setup_
    late = transcript.parent / "99999999-0000-0000-0000-000000000099.jsonl"
    (cfg.root / "notes.md").write_text("work\n")
    git("add", "notes.md", cwd=cfg.root)
    git("commit", "-q", "-m", "notes", cwd=cfg.root)
    git("push", "-q", "origin", BRANCH, cwd=cfg.root)
    _records(late, 34)
    cloud.push(cfg, str(late))                                               # the old copy, on a kept branch
    _records(late, 35)
    git("checkout", "-q", "-b", "claude/inbox", "origin/main", cwd=cfg.root)
    cloud.push(cfg, str(late))                                               # the newer copy, on another branch
    real = cloud._inbox_blobs                    # two gzip copies can have the same size: the size tells nothing
    monkeypatch.setattr(cloud, "_inbox_blobs", lambda root, tip: {p: (b, 0) for p, (b, _) in real(root, tip).items()})
    ccfg = cloud.lane(home)
    local = Path(ccfg.claude_dir) / late.parent.name / late.name
    written, errors, done = cloud.import_inbox(home, ccfg)
    assert errors == [] and "claude/inbox" in [name for name, _ in done]
    assert len(local.read_text().splitlines()) == 35                         # both branches there: the newer wins
    assert cloud.delete_branches(home.root, done) == []
    assert _has_branch(remote, BRANCH) and not _has_branch(remote, "claude/inbox")
    written, errors, _ = cloud.import_inbox(home, ccfg)
    assert errors == [] and written == 0 and len(local.read_text().splitlines()) == 35
    local.write_text("another history\n" * 400)                              # the kept copy no longer fits it
    written, errors, done = cloud.import_inbox(home, ccfg)
    assert written == 0 and "does not extend the one imported before" in errors[0] and done == []
    assert local.read_text() == "another history\n" * 400


def test_a_second_importer_stops_with_a_note(setup_, tmp_path):
    remote, cfg, transcript, home = setup_
    cloud.push(cfg, str(transcript))
    assert run_sync(home, now=True, runner=FakeRunner()).errors == []
    other = clone(remote, tmp_path / "other")
    src = tmp_path / "other-src"
    projects = make_claude_tree(src, sid="33333333-4444-5555-6666-777777777777", aid="c3c3c3c3c3c3c3c3c")
    sessions, codex_home = make_codex_tree(src, t1="01a0e000-0000-7000-8000-00000000000e",
                                           t2="01a0e000-0000-7000-8000-00000000000f",
                                           t3="01a0e000-0000-7000-8000-000000000010")
    ocfg = make_config(other, "host-b", projects, sessions, codex_home, cloud_import=True)
    rep = run_sync(ocfg, now=True, runner=FakeRunner(), summary_cap=0)
    assert rep.errors == [] and rep.pushed
    assert any("belongs to another machine" in n for n in rep.notes)
    assert not (ocfg.kb_dir / "cloud").exists()


def test_install_adds_the_hooks_once_and_keeps_other_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                   "command": "echo hi"}]}]}}))
    cloud.install("/code/bin/kb", settings=str(settings))
    cloud.install("/code/bin/kb", settings=str(settings))
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert commands == ["echo hi", "/code/bin/kb cloud hook"]
    assert [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]] == ["/code/bin/kb cloud hook"]


def test_hook_does_nothing_outside_a_cloud_session(setup_, monkeypatch):
    _, cfg, transcript, _ = setup_
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    cloud.hook(cfg, io.StringIO(json.dumps({"transcript_path": str(transcript)})))
    assert not (cfg.kb_dir / "cloud.log").exists()


def test_push_refuses_the_routines_bootstrap_branch(setup_):
    _, cfg, transcript, _ = setup_
    git("checkout", "-q", "-b", "claude/pages-bootstrap", cwd=cfg.root)
    with pytest.raises(cloud.CloudError, match="pages routine"):
        cloud.push(cfg, str(transcript))


def test_push_skips_a_headless_run_with_one_prompt(setup_, tmp_path):
    remote, cfg, _, _ = setup_
    rec = {"type": "user", "sessionId": SID, "entrypoint": "sdk-cli", "cwd": "/home/user/data",
           "timestamp": "2026-10-06T14:00:00.000Z", "message": {"role": "user", "content": "write the pages"}}
    proj = tmp_path / "headless" / "-home-user-data"
    proj.mkdir(parents=True)
    transcript = proj / f"{SID}.jsonl"
    transcript.write_text(json.dumps(rec) + "\n")
    assert cloud.push(cfg, str(transcript)).startswith("skipped")
    assert not _has_branch(remote, BRANCH)
