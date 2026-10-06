import json
import os
import subprocess
import sys
from pathlib import Path

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
