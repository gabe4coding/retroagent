import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow          # starts git and other processes; runs with scripts/test --all

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO                        # the whole repo is the plugin


def test_manifests_agree():
    claude = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    codex = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    assert claude["name"] == codex["name"] == market["name"] == "retroagent"
    assert claude["version"] == codex["version"]
    assert market["plugins"][0]["name"] == "retroagent" and market["plugins"][0]["source"] == "./"
    mod = market["plugins"][1]                          # the mod: its own plugin, so Codex never reads its hooks
    manifest = json.loads((REPO / mod["source"] / ".claude-plugin/plugin.json").read_text())
    assert mod["name"] == manifest["name"] == "retroagent-decide"
    assert json.loads((REPO / mod["source"] / "hooks/hooks.json").read_text()) == {"modules": ["./register.tsx"]}
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())
    assert set(hooks) == {"hooks"}                      # Codex rejects unknown top-level keys
    entry = hooks["hooks"]["SessionStart"][0]
    assert entry["matcher"] == "startup|resume"
    assert entry["hooks"][0]["command"].endswith('/bin/kb-hook"')
    fail = hooks["hooks"]["PostToolUseFailure"][0]             # Claude Code; Codex has no such event and skips it
    assert "matcher" not in fail and fail["hooks"][0]["command"].endswith('/bin/kb-hint"')
    assert fail["hooks"][0]["timeout"] <= 5
    guard = hooks["hooks"]["PreToolUse"][0]
    assert guard["matcher"] == "Bash" and guard["hooks"][0]["command"].endswith('/bin/kb-guard"')


@pytest.mark.parametrize("command, denied", [
    ("kb decide accept s-1a2b3c", True),
    ("cd /x && ~/.local/bin/kb decide reject m-1a2b3c --yes", True),
    ("python3 -m kb decide accept s-1a2b3c", True),
    ("echo a; kb  decide   reject s-1a2b3c", True),
    ("x=$(kb decide accept s-1a2b3c)", True),
    ("ls\nkb decide accept s-1a2b3c", True),
    ("kb decide", False),
    ("kb decide later --days 2", False),
    ("kb decide --json", False),
    ('grep -rn "kb decide accept" docs', False),
    ("kb decide accepted", False),
])
def test_guard_refuses_an_agents_answer(command, denied):
    event = {"session_id": "x", "tool_name": "Bash", "tool_input": {"command": command.replace("\\n", "\n")}}
    p = subprocess.run([str(PLUGIN / "bin/kb-guard")], input=json.dumps(event), capture_output=True, text=True,
                       timeout=5)
    assert p.returncode == 0
    if denied:
        out = json.loads(p.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and "only the user" in out["permissionDecisionReason"]
    else:
        assert p.stdout == ""


def _hook_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(PLUGIN / "bin/kb-hook", bin_dir / "kb-hook")
    marker = tmp_path / "ran.txt"
    (bin_dir / "kb").write_text(f"#!/bin/sh\n[ \"$1\" = brief ] && exit 0\necho \"$@\" > '{marker}'\n")  # the sync only
    (bin_dir / "kb").chmod(0o755)
    root = tmp_path / "root"
    (root / ".git").mkdir(parents=True)                  # a data clone
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
    assert _wait(marker) and marker.read_text().strip() == "sync --auto"       # item 1: the gate is checked by kb itself


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


def test_hook_passes_auto_to_kb_even_when_the_config_turns_auto_sync_off(tmp_path):
    """The gate lives in `kb sync --auto`, not in the shell hook: the hook always hands over."""
    hook, marker, _, env = _hook_env(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_sync": False}))
    subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input="{}", capture_output=True, text=True, timeout=5)
    assert _wait(marker) and marker.read_text().strip() == "sync --auto"


def test_hook_starts_the_backfill_again_while_the_first_one_is_not_done(tmp_path):
    hook, marker, _, env = _hook_env(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_sync": False, "auto_sync_pending": True}))
    subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input="{}", capture_output=True, text=True, timeout=5)
    assert _wait(marker) and marker.read_text().strip() == "backfill"
    marker.unlink()
    cfg.write_text(json.dumps({"auto_sync": True, "auto_sync_pending": True}))      # the owner ran kb enable meanwhile
    subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input="{}", capture_output=True, text=True, timeout=5)
    assert _wait(marker) and marker.read_text().strip() == "sync --auto"


def test_hook_without_a_data_clone_only_asks_to_offer_the_setup(tmp_path):
    hook, marker, root, env = _hook_env(tmp_path)
    (root / ".git").rmdir()
    p = subprocess.run([str(hook)], env=env, input="{}", capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and not _wait(marker, 0.5) and not (root / ".kb").exists()
    ctx = json.loads(p.stdout)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart" and "/retroagent:setup" in ctx["additionalContext"]
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"brief": False}))
    p = subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input="{}", capture_output=True, text=True,
                       timeout=5)
    assert p.returncode == 0 and p.stdout == ""


def test_hook_falls_back_to_the_config_from_before_the_rename(tmp_path):
    hook, marker, root, env = _hook_env(tmp_path)
    home = tmp_path / "home"
    legacy = home / ".config/sessions-kb/config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"root": str(root)}))
    env = {k: v for k, v in env.items() if k not in ("KB_ROOT", "KB_CONFIG")}
    subprocess.run([str(hook)], env={**env, "HOME": str(home)}, input="{}", capture_output=True, text=True, timeout=5)
    assert _wait(marker) and (root / ".kb/sync.log").exists()


def test_kb_wrapper_finds_the_code_through_a_symlink(tmp_path):
    link = tmp_path / "bin/kb"
    link.parent.mkdir()
    link.symlink_to(PLUGIN / "bin/kb")
    env = {**os.environ, "KB_ROOT": str(tmp_path), "KB_CONFIG": str(tmp_path / "none.json")}
    p = subprocess.run([str(link), "--help"], env=env, capture_output=True, text=True, timeout=20, cwd="/")
    assert p.returncode == 0 and "kb setup" in p.stdout


def _hint_env(tmp_path, config):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(PLUGIN / "bin/kb-hint", bin_dir / "kb-hint")
    marker = tmp_path / "ran.txt"
    (bin_dir / "kb").write_text(f"#!/bin/sh\necho \"$@\" > '{marker}'\ncat >> '{marker}'\nexit 3\n")
    (bin_dir / "kb").chmod(0o755)
    cfg = tmp_path / "config.json"
    cfg.write_text(config)
    env = {k: v for k, v in os.environ.items() if k != "KB_CHILD"}
    env["KB_CONFIG"] = str(cfg)
    return bin_dir / "kb-hint", marker, env


def test_hint_hook_is_off_only_with_hints_false(tmp_path):
    hook, marker, env = _hint_env(tmp_path, '{"hints": false}')
    p = subprocess.run([str(hook)], env=env, input="{}", capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and p.stdout == "" and not marker.exists()


def test_hint_hook_hands_the_event_to_kb_and_always_exits_0(tmp_path):
    hook, marker, env = _hint_env(tmp_path, '{"root": "/data"}')            # on by default
    p = subprocess.run([str(hook)], env=env, input='{"error": "x"}', capture_output=True, text=True, timeout=5)
    assert p.returncode == 0                                    # kb exited 3
    assert marker.read_text().splitlines() == ["hint --event error --hook", '{"error": "x"}']
    marker.unlink()
    p = subprocess.run([str(hook)], env={**env, "KB_CHILD": "1"}, input="{}", capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and not marker.exists()


def test_hook_prints_the_brief_first_unless_it_is_off(tmp_path):
    hook, marker, root, env = _hook_env(tmp_path)
    kb = hook.parent / "kb"
    kb.write_text(f"#!/bin/sh\necho \"$@\" >> '{marker}'\n[ \"$1\" = brief ] && cat && echo BRIEF\nexit 0\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_sync": False}))                     # brief: on by default
    (root / ".kb").mkdir()
    (root / ".kb/last-ok").touch()                      # debounced: no sync, the brief still comes
    p = subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input='{"cwd": "/w/demo"}',
                       capture_output=True, text=True, timeout=5)
    assert p.returncode == 0 and p.stdout == '{"cwd": "/w/demo"}BRIEF\n'
    assert marker.read_text() == "brief --hook\n"
    cfg.write_text(json.dumps({"brief": False}))
    p = subprocess.run([str(hook)], env={**env, "KB_CONFIG": str(cfg)}, input="{}", capture_output=True, text=True,
                       timeout=5)
    assert p.stdout == "" and marker.read_text() == "brief --hook\n"
