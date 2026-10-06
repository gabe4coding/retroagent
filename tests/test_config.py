import json

import pytest

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


def _load(tmp_path, monkeypatch, content, name="c.json"):
    p = tmp_path / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setenv("KB_CONFIG", str(p))
    return config.load(), p


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', "42", "null", "true"])
def test_non_object_json_gives_defaults_silently(tmp_path, monkeypatch, capsys, content):
    cfg, _ = _load(tmp_path, monkeypatch, content)
    assert cfg.quiet_minutes == 15 and cfg.root.name == "sessions-kb"
    assert capsys.readouterr().err == ""


def test_unreadable_file_gives_defaults(tmp_path, monkeypatch):
    d = tmp_path / "dir.json"
    d.mkdir()  # reading a directory raises OSError
    monkeypatch.setenv("KB_CONFIG", str(d))
    assert config.load().quiet_minutes == 15


def test_syntax_error_gives_defaults_and_one_warning(tmp_path, monkeypatch, capsys):
    cfg, p = _load(tmp_path, monkeypatch, "{broken")
    assert cfg.quiet_minutes == 15
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith(f"sessions-kb: bad config {p}: ") and err.endswith("; using defaults\n")


@pytest.mark.parametrize("bad", ["abc", None, -5, True, False, [], {}, 1e999])
def test_bad_int_values_fall_back_to_default(tmp_path, monkeypatch, bad):
    cfg, _ = _load(tmp_path, monkeypatch, {"quiet_minutes": bad, "debounce_minutes": bad, "summary_cap_per_run": bad})
    assert (cfg.quiet_minutes, cfg.debounce_minutes, cfg.summary_cap_per_run) == (15, 10, 30)


def test_good_int_values_are_kept(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"quiet_minutes": 0, "debounce_minutes": "7", "summary_cap_per_run": 3})
    assert (cfg.quiet_minutes, cfg.debounce_minutes, cfg.summary_cap_per_run) == (0, 7, 3)


def test_string_instead_of_list_becomes_one_item_list(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"codex_dirs": str(tmp_path / "cx"), "exclude_cwd_globs": "/work/*"})
    assert cfg.codex_dirs == [tmp_path / "cx"]
    assert cfg.exclude_cwd_globs == ["/work/*"]


def test_non_list_values_fall_back_to_default_lists(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"codex_dirs": 5, "exclude_cwd_globs": {"a": 1}})
    assert [d.name for d in cfg.codex_dirs] == ["sessions", "archived_sessions"]
    assert cfg.exclude_cwd_globs == config.DEFAULT_EXCLUDES


def test_list_items_are_coerced_to_str(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"exclude_cwd_globs": ["/a/*", 7]})
    assert cfg.exclude_cwd_globs == ["/a/*", "7"]


def test_default_lists_are_not_shared_between_loads(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {})
    cfg.exclude_cwd_globs.append("x")
    assert "x" not in _load(tmp_path, monkeypatch, {})[0].exclude_cwd_globs


def test_host_is_slugged(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"host": "A/B C"})
    assert cfg.host == "a-b-c"


def test_relative_root_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg, _ = _load(tmp_path, monkeypatch, {"root": "rel/kb"})
    assert cfg.root.is_absolute() and cfg.root == (tmp_path / "rel" / "kb").resolve()
    monkeypatch.setenv("KB_ROOT", "other")
    assert config.load().root == (tmp_path / "other").resolve()
