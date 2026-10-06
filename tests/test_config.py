import json

from kb import config


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "missing.json"))
    cfg = config.load()
    assert cfg.root.name == "sessions-kb"
    assert cfg.host
    assert cfg.quiet_minutes == 15 and cfg.debounce_minutes == 10
    assert cfg.summary_model == "haiku" and cfg.summary_cap_per_run == 30
    assert "/tmp/*" in cfg.exclude_cwd_globs
    assert cfg.kb_dir == cfg.root / ".kb"


def test_values_from_file_and_env(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"root": str(tmp_path / "kb"), "host": "box", "quiet_minutes": 1,
                             "codex_dirs": [str(tmp_path / "cx")], "exclude_cwd_globs": []}))
    monkeypatch.setenv("KB_CONFIG", str(p))
    cfg = config.load()
    assert cfg.root == tmp_path / "kb" and cfg.host == "box" and cfg.quiet_minutes == 1
    assert cfg.codex_dirs == [tmp_path / "cx"] and cfg.exclude_cwd_globs == []
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "other"))
    assert config.load().root == tmp_path / "other"
