import os

from fixtures import clone, init_remote

from kb import gitops


def _add(repo, host, text):
    d = repo / "sessions" / host
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text(text)


def test_stage_commit_push(tmp_path):
    a = clone(init_remote(tmp_path), tmp_path / "a")
    assert gitops.has_remote(a)
    _add(a, "h", "x")
    assert gitops.stage(a, ["sessions/h", "raw/h"]) is True
    gitops.commit(a, "m")
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
        gitops.commit(repo, host)
    gitops.push(a)
    gitops.push(b)
    gitops.pull(a)
    assert (a / "sessions/hb/x.md").read_text() == "hb"


def test_secrets_check_with_fake_gitleaks(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "gitleaks"
    script.write_text("#!/bin/sh\necho 'leak found: Finding 1'\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    assert "leak found" in gitops.secrets_check(tmp_path)
