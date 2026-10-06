import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_codex_marketplace_upsert_keeps_other_entries(tmp_path):
    home = tmp_path.resolve()
    mp = home / ".agents/plugins/marketplace.json"
    mp.parent.mkdir(parents=True)
    mp.write_text(json.dumps({"name": "personal", "interface": {"displayName": "Personal"},
                              "plugins": [{"name": "other", "source": {"source": "local", "path": "./plugins/other"}}]}))
    plugin = home / "Repositories/sessions-kb/plugin"
    plugin.mkdir(parents=True)
    env = {**os.environ, "HOME": str(home)}
    for _ in range(2):
        p = subprocess.run([sys.executable, str(REPO / "scripts/codex_marketplace.py"), str(plugin)],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(mp.read_text())
    assert [x["name"] for x in data["plugins"]] == ["other", "sessions-kb"]
    assert data["plugins"][1]["source"] == {"source": "local", "path": "./Repositories/sessions-kb/plugin"}
    assert data["interface"] == {"displayName": "Personal"}
    assert (mp.parent / "marketplace.json.bak-sessions-kb").exists()


def test_codex_marketplace_rejects_plugin_outside_home(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    p = subprocess.run([sys.executable, str(REPO / "scripts/codex_marketplace.py"), "/opt/elsewhere"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 1 and "not under" in p.stdout


def test_install_script_syntax():
    assert subprocess.run(["sh", "-n", str(REPO / "install.sh")]).returncode == 0


# ---- item 9: scripts/codex_marketplace.py keeps the marketplace name and refuses bad files

def _marketplace(tmp_path, content=None):
    home = tmp_path.resolve() / "home-mp"
    mp = home / ".agents/plugins/marketplace.json"
    plugin = home / ".sessions-kb/plugin"
    plugin.mkdir(parents=True)
    if content is not None:
        mp.parent.mkdir(parents=True)
        mp.write_text(content if isinstance(content, str) else json.dumps(content))
    return home, mp, plugin


def _run_marketplace(home, plugin):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "scripts/codex_marketplace.py"), str(plugin)],
                          capture_output=True, text=True, env=env)


def test_codex_marketplace_keeps_an_existing_name_and_prints_it(tmp_path):
    home, mp, plugin = _marketplace(tmp_path, {"name": "team-tools", "interface": {"displayName": "Team"},
                                               "plugins": [{"name": "other", "source": {"source": "local", "path": "./x"}}]})
    p = _run_marketplace(home, plugin)
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(mp.read_text())
    assert data["name"] == "team-tools" and data["interface"] == {"displayName": "Team"}
    assert [x["name"] for x in data["plugins"]] == ["other", "sessions-kb"]
    assert p.stdout.splitlines()[-1] == "marketplace: team-tools"
    assert data["plugins"][1]["source"] == {"source": "local", "path": "./.sessions-kb/plugin"}


def test_codex_marketplace_new_file_is_named_personal_and_the_name_is_printed(tmp_path):
    home, mp, plugin = _marketplace(tmp_path)
    p = _run_marketplace(home, plugin)
    assert p.returncode == 0 and p.stdout.splitlines()[-1] == "marketplace: personal"
    assert json.loads(mp.read_text())["name"] == "personal"
    assert not (mp.parent / "marketplace.json.bak-sessions-kb").exists()          # nothing to back up


def test_codex_marketplace_without_a_usable_name_gets_personal(tmp_path):
    for bad in ({"plugins": []}, {"name": "", "plugins": []}, {"name": 5, "plugins": []}):
        home, mp, plugin = _marketplace(tmp_path / str(len(str(bad))), bad)
        p = _run_marketplace(home, plugin)
        assert p.returncode == 0 and json.loads(mp.read_text())["name"] == "personal", bad
        assert p.stdout.splitlines()[-1] == "marketplace: personal"


@pytest.mark.parametrize("content", ["{broken", "", "[1, 2]", "null", '{"name": "x", "plugins": {"a": 1}}'])
def test_codex_marketplace_refuses_a_malformed_file_and_leaves_it_alone(tmp_path, content):
    home, mp, plugin = _marketplace(tmp_path, content)
    p = _run_marketplace(home, plugin)
    assert p.returncode == 1 and len(p.stdout.splitlines()) == 1 and "Traceback" not in p.stderr
    assert str(mp) in p.stdout and "marketplace:" not in p.stdout
    assert mp.read_text() == content
    assert not (mp.parent / "marketplace.json.bak-sessions-kb").exists()


def test_codex_marketplace_keeps_entries_it_does_not_understand(tmp_path):
    home, mp, plugin = _marketplace(tmp_path, {"name": "m", "plugins": ["odd", {"name": "sessions-kb", "x": 1}, {"name": "b"}]})
    assert _run_marketplace(home, plugin).returncode == 0
    plugins = json.loads(mp.read_text())["plugins"]
    assert plugins[0] == "odd" and plugins[1] == {"name": "b"} and plugins[2]["name"] == "sessions-kb" and len(plugins) == 3


# ---- item 8: install.sh (HOME is a temp folder, claude/codex/gitleaks/kb are fakes, the remote is a local bare repo)

FAKE_KB = '#!/bin/sh\nprintf \'kb %s\\n\' "$*" >> "$FAKE_LOG"\n'
FAKE_TOOL = '#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$FAKE_LOG"\nexit "${FAKE_EXIT:-0}"\n'
SYSTEM_PATH = "/usr/bin:/bin"


def _git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout.strip()


class Machine:
    """A fake machine: its own HOME, a bin dir of fake tools first on PATH, a log of the calls the fakes saw."""

    def __init__(self, tmp_path, fake_kb=True, tools=("claude", "codex"), gitleaks=False):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        for name in tools:
            self.tool(name, FAKE_TOOL)
        if gitleaks:
            self.tool("gitleaks", FAKE_TOOL)
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "dev-checkout"
        self.fake_kb = fake_kb
        self._make_repo()
        self.root = self.home / ".sessions-kb"

    def tool(self, name, body):
        (self.bin / name).write_text(body)
        (self.bin / name).chmod(0o755)

    def _make_repo(self):
        _git("init", "-q", "--bare", "-b", "main", str(self.origin))
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".kb")
        self.repo.mkdir()
        for name in ("install.sh", "scripts", "plugin", "src", ".claude-plugin"):
            src = REPO / name
            if src.is_dir():
                shutil.copytree(src, self.repo / name, ignore=ignore)
            else:
                shutil.copy2(src, self.repo / name)
        (self.repo / ".gitignore").write_text(".kb/\n")
        if self.fake_kb:
            (self.repo / "plugin/bin/kb").write_text(FAKE_KB)
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("add", ".", cwd=self.repo)
        _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", cwd=self.repo)
        _git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        _git("push", "-q", "-u", "origin", "main", cwd=self.repo)

    def push_file(self, rel, text):
        """Put a file on the remote (what another machine already pushed)."""
        other = self.tmp / "other-machine"
        if not other.exists():
            _git("clone", "-q", str(self.origin), str(other))
        path = other / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git("add", ".", cwd=other)
        _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "other", cwd=other)
        _git("push", "-q", cwd=other)

    def env(self, **extra):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:{SYSTEM_PATH}", "FAKE_LOG": str(self.log),
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "KB_INSTALL_GITLEAKS_DIRS": "",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com", "LANG": "C"}
        env.update(extra)
        return env

    def install(self, *args, **env):
        shell = os.environ.get("KB_TEST_SHELL", "sh")             # KB_TEST_SHELL=/bin/dash checks strict POSIX sh
        return subprocess.run([shell, str(self.repo / "install.sh"), *args], capture_output=True, text=True,
                              env=self.env(**env), timeout=120)

    @property
    def config(self):
        return json.loads((self.home / ".config/sessions-kb/config.json").read_text())

    def calls(self):
        return self.log.read_text().splitlines()


def _out(p):
    return p.stdout + p.stderr


def test_fresh_install_clones_writes_the_config_and_installs_both_plugins(tmp_path):
    m = Machine(tmp_path, gitleaks=True)
    p = m.install("--host", "laptop-1")
    assert p.returncode == 0, _out(p)
    assert (m.root / ".git").is_dir() and (m.root / "plugin/bin/kb").exists()
    assert _git("remote", "get-url", "origin", cwd=m.root) == str(m.origin)
    cfg = m.config
    assert cfg["root"] == str(m.root) and cfg["host"] == "laptop-1"
    assert cfg["auto_sync"] is False and cfg["skip_headless_single_prompt"] is True and cfg["branch"] == "main"
    assert cfg["gitleaks_path"] == str(m.bin / "gitleaks") and cfg["require_gitleaks"] is True
    calls = m.calls()
    assert f"claude plugin marketplace add {m.root}" in calls and "claude plugin install sessions-kb@sessions-kb" in calls
    assert "codex plugin add sessions-kb@personal" in calls
    assert "kb reindex" in calls and not any(c.startswith("kb sync") for c in calls)    # auto_sync is off: no sync
    mp = json.loads((m.home / ".agents/plugins/marketplace.json").read_text())
    assert mp["plugins"][0]["source"]["path"] == "./.sessions-kb/plugin"
    link = m.home / ".local/bin/kb"
    assert link.is_symlink() and os.readlink(link) == str(m.root / "plugin/bin/kb")
    out = p.stdout
    assert "kb backfill" in out and "kb backfill --summaries" in out and "kb enable" in out
    assert out.index("kb backfill") < out.index("kb backfill --summaries") < out.index("kb enable")
    assert not (m.root / ".kb/sync.log").exists()


def test_install_is_idempotent(tmp_path):
    m = Machine(tmp_path, gitleaks=True)
    first = m.install("--host", "laptop-1")
    cfg_text = (m.home / ".config/sessions-kb/config.json").read_text()
    head = _git("rev-parse", "HEAD", cwd=m.root)
    second = m.install("--host", "laptop-1")
    assert first.returncode == 0 and second.returncode == 0, _out(second)
    assert (m.home / ".config/sessions-kb/config.json").read_text() == cfg_text
    assert _git("rev-parse", "HEAD", cwd=m.root) == head and _git("status", "--porcelain", cwd=m.root) == ""
    assert os.readlink(m.home / ".local/bin/kb") == str(m.root / "plugin/bin/kb")
    mp = json.loads((m.home / ".agents/plugins/marketplace.json").read_text())
    assert [x["name"] for x in mp["plugins"]] == ["sessions-kb"]


def test_install_never_touches_anything_outside_its_fake_home(tmp_path):
    m = Machine(tmp_path)
    assert m.install("--host", "h").returncode == 0
    inside = {p.relative_to(m.home).parts[0] for p in m.home.iterdir()} - {"Library"}     # Library: Apple's python caches
    assert inside == {".sessions-kb", ".config", ".agents", ".local"}


def test_host_defaults_to_the_slug_of_the_short_hostname_and_is_slugged_when_given(tmp_path):
    m = Machine(tmp_path)
    m.tool("hostname", '#!/bin/sh\n[ "$1" = "-s" ] && echo "Some Box_2" || echo "Some Box_2.local"\n')
    assert m.install().returncode == 0
    assert m.config["host"] == "some-box_2"
    assert m.install("--host", "Other Box!").returncode == 0
    assert m.config["host"] == "other-box"


def test_an_existing_config_keeps_its_other_keys_and_its_host(tmp_path):
    m = Machine(tmp_path)
    cfg = m.home / ".config/sessions-kb/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"root": "/old/dev-checkout", "host": "kept-host", "quiet_minutes": 3, "extra": [1]}))
    p = m.install()
    assert p.returncode == 0, _out(p)
    assert m.config["root"] == str(m.root) and m.config["host"] == "kept-host"       # the root moves, the host stays
    assert m.config["quiet_minutes"] == 3 and m.config["extra"] == [1] and m.config["auto_sync"] is False


def test_an_explicit_auto_sync_choice_survives_a_second_install_and_starts_the_sync(tmp_path):
    m = Machine(tmp_path)
    assert m.install("--host", "h").returncode == 0
    cfg = m.home / ".config/sessions-kb/config.json"
    data = json.loads(cfg.read_text())
    data["auto_sync"] = True                                                          # the owner ran `kb enable`
    cfg.write_text(json.dumps(data))
    p = m.install("--host", "h")
    assert p.returncode == 0 and m.config["auto_sync"] is True
    deadline = time.time() + 5
    while time.time() < deadline and "kb sync" not in m.calls():
        time.sleep(0.05)
    assert "kb sync" in m.calls() and "kb enable" not in p.stdout
    m.log.write_text("")
    p = m.install("--host", "h", "--no-sync")
    time.sleep(0.3)
    assert p.returncode == 0 and "kb sync" not in m.calls() and "--no-sync" in p.stdout


def test_gitleaks_found_in_a_usual_folder_is_recorded_and_required(tmp_path):
    m = Machine(tmp_path)
    usual = tmp_path / "usr-local-bin"
    usual.mkdir()
    (usual / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
    (usual / "gitleaks").chmod(0o755)
    assert m.install("--host", "h", KB_INSTALL_GITLEAKS_DIRS=f"{tmp_path / 'nowhere'}:{usual}").returncode == 0
    assert m.config["gitleaks_path"] == str(usual / "gitleaks") and m.config["require_gitleaks"] is True


def test_without_gitleaks_install_warns_and_does_not_require_it(tmp_path):
    m = Machine(tmp_path)
    p = m.install("--host", "h")
    assert p.returncode == 0
    warning = [l for l in _out(p).splitlines() if "gitleaks" in l.lower() and "WARNING" in l]
    assert len(warning) == 1 and "brew install gitleaks" in warning[0]
    assert "gitleaks_path" not in m.config and m.config.get("require_gitleaks") is not True


def test_missing_claude_and_codex_are_skipped(tmp_path):
    m = Machine(tmp_path, tools=())
    p = m.install("--host", "h")
    assert p.returncode == 0 and "claude: not found, skipped" in p.stdout and "codex: not found, skipped" in p.stdout
    assert m.calls() == ["kb reindex"]


def test_failing_plugin_commands_warn_but_do_not_stop_the_install(tmp_path):
    m = Machine(tmp_path)
    p = m.install("--host", "h", FAKE_EXIT="1")
    assert p.returncode == 0 and p.stdout.count("WARNING") >= 3
    assert m.config["host"] == "h"


def test_the_codex_plugin_is_added_to_the_marketplace_that_already_exists(tmp_path):
    m = Machine(tmp_path)
    mp = m.home / ".agents/plugins/marketplace.json"
    mp.parent.mkdir(parents=True)
    mp.write_text(json.dumps({"name": "team-tools", "plugins": []}))
    assert m.install("--host", "h").returncode == 0
    assert "codex plugin add sessions-kb@team-tools" in m.calls()


def test_a_broken_codex_marketplace_file_is_reported_and_left_alone(tmp_path):
    m = Machine(tmp_path)
    mp = m.home / ".agents/plugins/marketplace.json"
    mp.parent.mkdir(parents=True)
    mp.write_text("{broken")
    p = m.install("--host", "h")
    assert p.returncode == 0 and mp.read_text() == "{broken" and "codex: WARNING" in p.stdout
    assert not any(c.startswith("codex plugin add") for c in m.calls())


def test_the_kb_link_points_to_the_local_tools_copy_when_there_is_one(tmp_path):
    m = Machine(tmp_path)
    tool = m.home / "claude-tools/plugins/local-tools/bin/kb"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    assert m.install("--host", "h").returncode == 0
    assert os.readlink(m.home / ".local/bin/kb") == str(tool)


# ---- the root folder

def test_a_root_that_is_not_a_git_repo_stops_the_install(tmp_path):
    m = Machine(tmp_path)
    m.root.mkdir()
    (m.root / "notes.txt").write_text("mine")
    p = m.install("--host", "h")
    assert p.returncode == 1 and str(m.root) in _out(p) and "not a git clone" in _out(p)
    assert not (m.home / ".config/sessions-kb/config.json").exists() and m.calls() == []


def test_a_folder_inside_another_repo_is_not_accepted_as_the_root(tmp_path):
    m = Machine(tmp_path)
    m.root.mkdir()
    _git("init", "-q", "-b", "main", cwd=m.root)
    sub = m.root / "inner"
    sub.mkdir()
    p = m.install("--host", "h", "--root", str(sub))
    assert p.returncode == 1 and "not a git clone" in _out(p)


def test_a_clone_of_another_repo_is_refused(tmp_path):
    m = Machine(tmp_path)
    other = tmp_path / "other.git"
    _git("init", "-q", "--bare", "-b", "main", str(other))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(other), str(seed))
    (seed / "a.txt").write_text("a")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x", cwd=seed)
    _git("push", "-q", "-u", "origin", "main", cwd=seed)
    _git("clone", "-q", str(other), str(m.root))
    p = m.install("--host", "h")
    assert p.returncode == 1 and "another repo" in _out(p) and not (m.home / ".config").exists()


def test_a_clone_of_the_same_repo_is_accepted_and_a_custom_root_is_used(tmp_path):
    m = Machine(tmp_path)
    custom = tmp_path / "somewhere" / "kb-data"
    _git("clone", "-q", str(m.origin), str(custom))
    p = m.install("--host", "h", "--root", str(custom))
    assert p.returncode == 0, _out(p)
    assert m.config["root"] == str(custom) and f"claude plugin marketplace add {custom}" in m.calls()


def test_a_relative_root_becomes_absolute(tmp_path):
    m = Machine(tmp_path)
    p = subprocess.run(["sh", str(m.repo / "install.sh"), "--host", "h", "--root", "rel-kb"], capture_output=True,
                       text=True, env=m.env(), cwd=str(tmp_path), timeout=120)
    assert p.returncode == 0, _out(p)
    assert m.config["root"] == str(tmp_path / "rel-kb") and (tmp_path / "rel-kb/.git").is_dir()


def test_a_repo_without_origin_is_cloned_from_its_folder(tmp_path):
    m = Machine(tmp_path)
    _git("remote", "remove", "origin", cwd=m.repo)
    p = m.install("--host", "h")
    assert p.returncode == 0, _out(p)
    assert _git("remote", "get-url", "origin", cwd=m.root) == str(m.repo)
    assert any("no origin" in l and "WARNING" in l for l in p.stdout.splitlines())


def test_a_clone_that_lacks_the_new_files_stops_the_install_with_a_hint(tmp_path):
    m = Machine(tmp_path)
    _git("rm", "-q", "-r", "scripts", cwd=m.repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "drop", cwd=m.repo)
    _git("push", "-q", cwd=m.repo)
    p = m.install("--host", "h")
    assert p.returncode == 1 and "push" in _out(p) and not (m.home / ".config").exists()


# ---- the host guard

def _seed_host_folder(m, host="laptop-1", marker=None):
    m.push_file(f"sessions/{host}/claude/2026/10/x.md", "session\n")
    if marker is not None:
        m.push_file(f"sessions/{host}/.machine-id", marker + "\n")


def test_an_existing_host_folder_without_a_local_id_is_refused(tmp_path):
    m = Machine(tmp_path)
    _seed_host_folder(m)
    p = m.install("--host", "laptop-1")
    assert p.returncode == 1
    out = _out(p)
    assert "sessions/laptop-1" in out and "--host" in out and "--force-host" in out
    assert not (m.home / ".config").exists() and not any(c.startswith(("claude", "codex")) for c in m.calls())


def test_force_host_lets_the_owner_of_existing_data_in(tmp_path):
    m = Machine(tmp_path)
    _seed_host_folder(m)
    p = m.install("--host", "laptop-1", "--force-host")
    assert p.returncode == 0, _out(p)
    assert m.config["host"] == "laptop-1"


def test_a_marker_that_differs_from_the_local_id_is_refused(tmp_path):
    m = Machine(tmp_path)
    _seed_host_folder(m, marker="b" * 32)
    m.root.parent.mkdir(exist_ok=True)
    _git("clone", "-q", str(m.origin), str(m.root))
    (m.root / ".kb").mkdir()
    (m.root / ".kb/machine-id").write_text("a" * 32 + "\n")
    p = m.install("--host", "laptop-1")
    assert p.returncode == 1 and "another machine" in _out(p) and not (m.home / ".config").exists()
    assert m.install("--host", "laptop-1", "--force-host").returncode == 0


def test_a_marker_that_matches_the_local_id_and_a_missing_marker_pass(tmp_path):
    m = Machine(tmp_path)
    _seed_host_folder(m, marker="c" * 32)
    _git("clone", "-q", str(m.origin), str(m.root))
    (m.root / ".kb").mkdir()
    (m.root / ".kb/machine-id").write_text("c" * 32 + "\n")
    assert m.install("--host", "laptop-1").returncode == 0                              # same machine, second run
    _git("rm", "-q", "sessions/laptop-1/.machine-id", cwd=m.root)                       # data from before markers
    assert m.install("--host", "laptop-1").returncode == 0


def test_a_host_without_a_folder_is_free(tmp_path):
    m = Machine(tmp_path)
    _seed_host_folder(m, host="other-mac")
    assert m.install("--host", "laptop-1").returncode == 0


# ---- python

def test_python_older_than_3_9_stops_the_install(tmp_path):
    m = Machine(tmp_path)
    m.tool("python3", '#!/bin/sh\ncase "$2" in *version_info*) exit 1;; esac\necho 3.8\n')
    p = m.install("--host", "h")
    assert p.returncode == 1 and "3.9" in _out(p) and not (m.root).exists()


def test_python_without_fts5_stops_the_install(tmp_path):
    m = Machine(tmp_path)
    m.tool("python3", '#!/bin/sh\ncase "$2" in *version_info*) exit 0;; *fts5*) exit 1;; esac\nexit 0\n')
    p = m.install("--host", "h")
    assert p.returncode == 1 and "FTS5" in _out(p) and not (m.root).exists()


def test_unknown_options_are_refused(tmp_path):
    m = Machine(tmp_path)
    p = m.install("--bogus")
    assert p.returncode == 1 and "--bogus" in _out(p) and "Usage" in _out(p)
    assert m.install("--root").returncode == 1                                         # a missing value


def test_the_real_kb_builds_the_index_during_the_install(tmp_path):
    m = Machine(tmp_path, fake_kb=False)
    p = m.install("--host", "h")
    assert p.returncode == 0, _out(p)
    assert (m.root / ".kb/index.sqlite").exists() and "indexed 0 sessions" in p.stdout
