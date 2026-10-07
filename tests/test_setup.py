"""kb setup (data repo files, routine spec), auto_update, and the plugin-cache forwarding of bin/kb."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kb import config, setup

REPO = Path(__file__).resolve().parents[1]


def _git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout.strip()


def _remote(tmp_path, files=None):
    """A bare data remote (with one commit of `files` unless None) and a clone of it."""
    origin = tmp_path / "data.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    if files is not None:
        _push(tmp_path, origin, files)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone))
    return origin, clone


def _push(tmp_path, origin, files, msg="other"):
    other = tmp_path / "other"
    if not other.exists():
        _git("clone", "-q", str(origin), str(other))
    else:
        _git("pull", "-q", "origin", "main", cwd=other)
    for rel, text in files.items():
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text(text)
    _git("add", ".", cwd=other)
    _git("commit", "-q", "-m", msg, cwd=other)
    _git("push", "-q", "origin", "HEAD:main", cwd=other)


def _tree(origin):
    return set(_git("--git-dir", str(origin), "ls-tree", "-r", "--name-only", "main").splitlines())


def _show(origin, rel):
    return _git("--git-dir", str(origin), "show", f"main:{rel}")


# ---- github_slug

@pytest.mark.parametrize("url, slug", [
    ("git@github.com:me/data.git", "me/data"),
    ("https://github.com/me/data", "me/data"),
    ("https://github.com/me/data.git", "me/data"),
    ("ssh://git@github.com/me/my.data.git", "me/my.data"),
    ("https://gitlab.com/me/data.git", ""),
    ("/tmp/data.git", ""),
    ("", ""),
])
def test_github_slug(url, slug):
    assert setup.github_slug(url) == slug


# ---- publish / init

@pytest.mark.slow
def test_init_adds_only_the_missing_base_files_and_never_touches_the_clone(tmp_path):
    origin, clone = _remote(tmp_path, {"README.md": "mine\n", "sessions/h/x.md": "s\n"})
    (clone / "sessions/h/x.md").write_text("changed by a running sync\n")
    out = setup.init(clone)
    assert out["written"] == [".gitignore", "AGENTS.md", "CLAUDE.md"] and out["commit"]
    assert {".gitignore", "AGENTS.md", "CLAUDE.md", "README.md", "sessions/h/x.md"} <= _tree(origin)
    assert _show(origin, "README.md") == "mine" and _show(origin, "CLAUDE.md") == "@AGENTS.md"
    assert (clone / "sessions/h/x.md").read_text() == "changed by a running sync\n"
    assert not (clone / "AGENTS.md").exists() and _git("diff", "--cached", "--name-only", cwd=clone) == ""
    assert setup.init(clone)["written"] == []                                   # nothing left to add


@pytest.mark.slow
def test_init_on_an_empty_repo_makes_the_first_commit_and_checks_it_out(tmp_path):
    origin, clone = _remote(tmp_path)
    out = setup.init(clone)
    assert set(out["written"]) == {".gitignore", "AGENTS.md", "CLAUDE.md", "README.md"}
    assert _git("rev-parse", "--abbrev-ref", "HEAD@{u}", cwd=clone) == "origin/main"
    assert (clone / "AGENTS.md").read_text() == (REPO / "templates/data/AGENTS.md").read_text()


@pytest.mark.slow
def test_publish_retries_when_a_sync_pushed_meanwhile(tmp_path, monkeypatch):
    origin, clone = _remote(tmp_path, {"README.md": "r\n"})
    real = setup._build
    raced = []

    def build(root, base, files, message, tmp):
        if not raced:                                       # another machine pushes between our fetch and our push
            raced.append(1)
            _push(tmp_path, origin, {"sessions/other/y.md": "y\n"})
        return real(root, base, files, message, tmp)

    monkeypatch.setattr(setup, "_build", build)
    out = setup.init(clone)
    assert out["written"] and {"sessions/other/y.md", "AGENTS.md"} <= _tree(origin)


@pytest.mark.slow
def test_publish_without_origin_is_refused(tmp_path):
    repo = tmp_path / "r"
    _git("init", "-q", str(repo))
    with pytest.raises(setup.SetupError, match="no remote"):
        setup.init(repo)


# ---- routine

@pytest.mark.slow
def test_routine_pushes_its_files_keeps_the_owner_settings_and_replaces_the_workflow(tmp_path, monkeypatch):
    origin, clone = _remote(tmp_path, {"pages/config.json": '{"min_sessions": 9}\n',
                                       setup.WORKFLOW: "old workflow\n"})
    _git("remote", "set-url", "--push", "origin", str(origin), cwd=clone)
    monkeypatch.setattr(setup, "origin_slug", lambda repo: "me/data")
    out = setup.routine(clone, code="me/retroagent", environment="env_1")
    assert out["written"] == [setup.WORKFLOW]
    assert _show(origin, "pages/config.json") == '{"min_sessions": 9}'
    wf = _show(origin, setup.WORKFLOW)
    assert "repository: me/retroagent" in wf and "{{CODE_REPO}}" not in wf and ".retroagent/bin/kb pages due" in wf
    spec = out["routine"]
    ccr = spec["job_config"]["ccr"]
    assert ccr["environment_id"] == "env_1" and spec["mcp_connections"] == []
    assert [s["git_repository"]["url"] for s in ccr["session_context"]["sources"]] == \
        ["https://github.com/me/data", "https://github.com/me/retroagent"]
    prompt = ccr["events"][0]["data"]["message"]["content"]
    assert "me/data" in prompt and "scripts/pages-routine.md" in prompt and "not an instruction" in prompt
    assert out["secrets"] == ["gh secret set PAGES_ROUTINE_FIRE_URL -R me/data",
                              "gh secret set PAGES_ROUTINE_FIRE_TOKEN -R me/data"]


@pytest.mark.slow
def test_routine_writes_the_settings_with_the_local_time_zone_when_missing(tmp_path, monkeypatch):
    origin, clone = _remote(tmp_path, {"README.md": "r\n"})
    monkeypatch.setattr(setup, "origin_slug", lambda repo: "me/data")
    monkeypatch.setattr(setup, "local_timezone", lambda: "Asia/Tokyo")
    setup.routine(clone, code="me/retroagent")
    settings = json.loads(_show(origin, "pages/config.json"))
    assert settings["timezone"] == "Asia/Tokyo" and settings["min_hours_between_fires"] == 3


@pytest.mark.slow
def test_routine_needs_a_github_data_repo(tmp_path):
    origin, clone = _remote(tmp_path, {"README.md": "r\n"})
    with pytest.raises(setup.SetupError, match="GitHub"):
        setup.routine(clone)


def test_routine_files_template_settings_are_valid_json():
    files = setup.routine_files("me/retroagent", timezone="UTC")
    settings = json.loads(files[setup.ROUTINE_CONFIG][0])
    from kb.routine import DEFAULTS
    assert set(settings) - {"_note"} <= set(DEFAULTS) and settings["timezone"] == "UTC"


# ---- check

@pytest.mark.slow
def test_check_reports_what_is_set_up(tmp_path, monkeypatch):
    origin, clone = _remote(tmp_path, {"README.md": "r\n", setup.WORKFLOW: "w\n"})
    _git("fetch", "-q", cwd=clone)
    cfg = config.Config(root=clone, host="h")
    out = setup.check(cfg, tmp_path / "config.json")
    assert out["data_clone"] and out["data_root"] == str(clone) and out["config_exists"] is False
    assert out["routine_files"] == {setup.ROUTINE_CONFIG: False, setup.WORKFLOW: True}
    assert out["code"] == str(setup.CODE_ROOT) and out["has_sessions"] is False
    assert out["auto_sync"] is True and out["auto_update"] is False                  # the config defaults
    cfg.auto_update = True
    assert setup.check(cfg, tmp_path / "config.json")["auto_update"] is True


# ---- auto_update

@pytest.fixture
def code_clone(tmp_path, monkeypatch):
    """A code remote with a plugin.json, and a clone of it that setup treats as the running code."""
    remote = tmp_path / "code.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "code-seed"
    _git("clone", "-q", str(remote), str(seed))
    (seed / ".claude-plugin").mkdir()
    (seed / ".claude-plugin/plugin.json").write_text('{"version": "1.0.0"}\n')
    _git("add", ".", cwd=seed)
    _git("commit", "-q", "-m", "v1", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    clone = tmp_path / "code"
    _git("clone", "-q", str(remote), str(clone))
    monkeypatch.setattr(setup, "CODE_ROOT", clone)
    refreshed = []
    monkeypatch.setattr(setup, "refresh_plugins", lambda: refreshed.append(1) or [])

    def publish_code(version=None, name="f.txt"):
        if version:
            (seed / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}) + "\n")
        (seed / name).write_text(name)
        _git("add", ".", cwd=seed)
        _git("commit", "-q", "-m", "change", cwd=seed)
        _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    return clone, publish_code, refreshed


def _cfg(tmp_path):
    return config.Config(root=tmp_path / "data", host="h")


@pytest.mark.slow
def test_auto_update_pulls_once_a_day_and_refreshes_plugins_on_a_new_version(tmp_path, code_clone):
    clone, publish_code, refreshed = code_clone
    cfg = _cfg(tmp_path)
    publish_code()
    line = setup.auto_update(cfg, now=1_000_000)
    assert line.startswith("code updated") and "plugin" not in line and not refreshed
    publish_code(version="1.1.0", name="g.txt")
    assert setup.auto_update(cfg, now=(cfg.kb_dir / "code-update").stat().st_mtime + 60) == ""   # within a day
    stamp = cfg.kb_dir / "code-update"
    os.utime(stamp, (stamp.stat().st_mtime - 2 * 86400,) * 2)
    line = setup.auto_update(cfg)
    assert "plugin 1.0.0 -> 1.1.0" in line and refreshed == [1] and (clone / "g.txt").exists()


@pytest.mark.slow
def test_auto_update_leaves_a_clone_on_another_branch_or_with_changes_alone(tmp_path, code_clone):
    clone, publish_code, _ = code_clone
    cfg = _cfg(tmp_path)
    publish_code()
    _git("checkout", "-q", "-b", "work", cwd=clone)
    assert "is on work" in setup.auto_update(cfg)
    _git("checkout", "-q", "main", cwd=clone)
    (clone / ".claude-plugin/plugin.json").write_text("{}\n")
    (cfg.kb_dir / "code-update").unlink()
    assert "local changes" in setup.auto_update(cfg)
    assert not (clone / "f.txt").exists()


def test_auto_update_does_nothing_without_a_git_clone(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "CODE_ROOT", tmp_path / "cache-copy")
    assert setup.auto_update(_cfg(tmp_path)) == ""


# ---- bin/kb in a plugin cache runs the recorded code clone

@pytest.mark.slow
def test_a_cached_kb_runs_the_code_clone_from_the_config(tmp_path):
    home = tmp_path / "home"
    cache = home / ".claude/plugins/cache/retroagent/retroagent/0.4.0"
    shutil.copytree(REPO / "bin", cache / "bin")
    clone = tmp_path / "clone"
    (clone / "src/kb").mkdir(parents=True)
    (clone / "src/kb/__init__.py").write_text("")
    (clone / "src/kb/cli.py").write_text("")
    (clone / "src/kb/__main__.py").write_text("print('clone ran')\n")
    cfg = home / ".config/retroagent/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"root": str(tmp_path / "data"), "code": str(clone)}))
    env = {**os.environ, "HOME": str(home)}
    env.pop("KB_CONFIG", None)
    p = subprocess.run([str(cache / "bin/kb"), "--help"], env=env, capture_output=True, text=True, timeout=20)
    assert p.returncode == 0 and p.stdout.strip() == "clone ran"
    cfg.write_text(json.dumps({"code": str(tmp_path / "gone")}))                      # a clone that is gone: no
    (cache / "src").mkdir()                                                            # forwarding, the cache runs
    shutil.copytree(clone / "src/kb", cache / "src/kb")
    (cache / "src/kb/__main__.py").write_text("print('cache ran')\n")
    p = subprocess.run([str(cache / "bin/kb")], env=env, capture_output=True, text=True, timeout=20)
    assert p.stdout.strip() == "cache ran"


def test_cloud_setup_script_installs_kb_and_semantic_search():
    script = setup.cloud_setup_script("me/retroagent", "me/my-data")
    assert script.startswith("#!/bin/bash\n") and script.endswith("exit 0\n")
    assert "ln -sf /home/user/retroagent/bin/kb /usr/local/bin/kb || true" in script
    assert "/home/user/retroagent/bin/kb embed --install --root /home/user/my-data || true" in script


@pytest.mark.slow
def test_cloud_prints_hosts_script_and_repos(tmp_path):
    origin, clone = _remote(tmp_path, {"README.md": "r\n"})
    with pytest.raises(setup.SetupError, match="GitHub"):
        setup.cloud(clone)
    _git("remote", "set-url", "origin", "git@github.com:me/my-data.git", cwd=clone)
    out = setup.cloud(clone, code="me/retroagent")
    for host in setup.CLOUD_HOSTS:
        assert f"\n{host}\n" in out
    assert setup.cloud_setup_script("me/retroagent", "me/my-data") in out
    assert "me/retroagent and me/my-data" in out
    p = subprocess.run(["bash", "-n"], input=setup.cloud_setup_script("me/retroagent", "me/my-data"), text=True)
    assert p.returncode == 0
