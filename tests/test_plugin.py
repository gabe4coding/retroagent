import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"


def test_manifests_agree():
    claude = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    codex = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    assert claude["name"] == codex["name"] == "sessions-kb"
    assert claude["version"] == codex["version"]
    assert market["plugins"][0]["name"] == "sessions-kb" and market["plugins"][0]["source"] == "./plugin"
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())
    assert set(hooks) == {"hooks"}                      # Codex rejects unknown top-level keys
    entry = hooks["hooks"]["SessionStart"][0]
    assert entry["matcher"] == "startup|resume"
    assert entry["hooks"][0]["command"].endswith('/bin/kb-hook"')


def _hook_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(PLUGIN / "bin/kb-hook", bin_dir / "kb-hook")
    marker = tmp_path / "ran.txt"
    (bin_dir / "kb").write_text(f"#!/bin/sh\necho \"$@\" > '{marker}'\n")
    (bin_dir / "kb").chmod(0o755)
    root = tmp_path / "root"
    (root / "src/kb").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "KB_CHILD"}
    env.update({"KB_ROOT": str(root), "KB_CONFIG": str(tmp_path / "none.json")})
    return bin_dir / "kb-hook", marker, root, env


def _wait(path, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


def test_hook_starts_background_sync_and_prints_nothing(tmp_path):
    hook, marker, _, env = _hook_env(tmp_path)
    start = time.time()
    p = subprocess.run([str(hook)], env=env, input="{}", capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and p.stdout == "" and time.time() - start < 2
    assert _wait(marker) and marker.read_text().strip() == "sync"


def test_hook_respects_guard_and_debounce(tmp_path):
    hook, marker, root, env = _hook_env(tmp_path)
    p = subprocess.run([str(hook)], env={**env, "KB_CHILD": "1"}, input="{}", capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and not _wait(marker, 0.5)
    (root / ".kb").mkdir(exist_ok=True)
    (root / ".kb/last-ok").touch()
    subprocess.run([str(hook)], env=env, input="{}", capture_output=True, text=True, timeout=5)
    assert not _wait(marker, 1.0)


def test_kb_wrapper_runs_cli(tmp_path):
    env = {**os.environ, "KB_ROOT": str(REPO), "KB_CONFIG": str(tmp_path / "none.json")}
    p = subprocess.run([str(PLUGIN / "bin/kb"), "--help"], env=env, capture_output=True, text=True, timeout=20)
    assert p.returncode == 0 and "kb find" in p.stdout
