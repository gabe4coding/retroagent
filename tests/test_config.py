import json

import pytest

from kb import config


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "missing.json"))
    cfg = config.load()
    assert cfg.root.name == ".retroagent-data"
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
    assert cfg.quiet_minutes == 15 and cfg.root.name == ".retroagent-data"
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
    assert err.startswith(f"retroagent: bad config {p}: ") and err.endswith("; using defaults\n")


@pytest.mark.parametrize("bad", ["abc", None, -5, True, False, [], {}, 1e999])
def test_bad_int_values_fall_back_to_default(tmp_path, monkeypatch, bad):
    cfg, _ = _load(tmp_path, monkeypatch, {"quiet_minutes": bad, "debounce_minutes": bad, "summary_cap_per_run": bad})
    assert (cfg.quiet_minutes, cfg.debounce_minutes, cfg.summary_cap_per_run) == (15, 10, 30)


def test_good_int_values_are_kept(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"quiet_minutes": 0, "debounce_minutes": "7", "summary_cap_per_run": 3})
    assert (cfg.quiet_minutes, cfg.debounce_minutes, cfg.summary_cap_per_run) == (0, 7, 3)


def test_raw_settle_hours_defaults_to_a_day_and_0_turns_the_wait_off(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, {})[0].raw_settle_hours == 24
    assert _load(tmp_path, monkeypatch, {"raw_settle_hours": 0})[0].raw_settle_hours == 0
    assert _load(tmp_path, monkeypatch, {"raw_settle_hours": "6"})[0].raw_settle_hours == 6


@pytest.mark.parametrize("bad", ["abc", None, -1, True, [], 1e999])
def test_unusable_raw_settle_hours_keep_the_default(tmp_path, monkeypatch, bad):
    assert _load(tmp_path, monkeypatch, {"raw_settle_hours": bad})[0].raw_settle_hours == 24


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


# ---- approval gate and new keys (auto_sync, skip_headless_single_prompt, gitleaks_path, require_gitleaks, branch)

def test_auto_sync_is_true_when_absent_and_follows_the_file(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, {})[0].auto_sync is True               # old configs keep syncing
    assert _load(tmp_path, monkeypatch, {"auto_sync": None})[0].auto_sync is True
    assert _load(tmp_path, monkeypatch, {"auto_sync": False})[0].auto_sync is False
    assert _load(tmp_path, monkeypatch, {"auto_sync": True})[0].auto_sync is True


@pytest.mark.parametrize("bad", ["maybe", "false", 0, 1, [], {}])
def test_auto_sync_with_an_unusable_value_fails_closed(tmp_path, monkeypatch, bad):
    assert _load(tmp_path, monkeypatch, {"auto_sync": bad})[0].auto_sync is False


def test_a_config_with_a_syntax_error_does_not_start_automatic_syncs(tmp_path, monkeypatch, capsys):
    cfg, _ = _load(tmp_path, monkeypatch, "{broken")
    assert cfg.auto_sync is False and capsys.readouterr().err.count("\n") == 1


def test_new_keys_have_defaults(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {})
    assert cfg.skip_headless_single_prompt is True and cfg.require_gitleaks is False
    assert cfg.gitleaks_path == "" and cfg.branch == "main"


def test_new_keys_are_read_from_the_file(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"skip_headless_single_prompt": False, "require_gitleaks": True,
                                           "gitleaks_path": "/opt/x/gitleaks", "branch": "trunk"})
    assert cfg.skip_headless_single_prompt is False and cfg.require_gitleaks is True
    assert cfg.gitleaks_path == "/opt/x/gitleaks" and cfg.branch == "trunk"


def test_unusable_values_of_new_keys_take_the_safe_side(tmp_path, monkeypatch):
    cfg, _ = _load(tmp_path, monkeypatch, {"skip_headless_single_prompt": "no", "require_gitleaks": "no",
                                           "gitleaks_path": 5, "branch": ""})
    assert cfg.skip_headless_single_prompt is True and cfg.require_gitleaks is True      # skip more, require more
    assert cfg.gitleaks_path == "" and cfg.branch == "main"


def test_set_key_rewrites_the_file_and_keeps_other_keys(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"root": "/x", "host": "box", "extra": [1, 2]}))
    monkeypatch.setenv("KB_CONFIG", str(p))
    assert config.set_key("auto_sync", True) == p
    assert json.loads(p.read_text()) == {"root": "/x", "host": "box", "extra": [1, 2], "auto_sync": True}
    assert p.read_text().endswith("\n")
    config.set_key("auto_sync", False)
    assert json.loads(p.read_text())["auto_sync"] is False


def test_set_key_creates_a_missing_file(tmp_path, monkeypatch):
    p = tmp_path / "new" / "dir" / "c.json"
    monkeypatch.setenv("KB_CONFIG", str(p))
    config.set_key("auto_sync", True)
    assert json.loads(p.read_text()) == {"auto_sync": True}


@pytest.mark.parametrize("content", ["{broken", "[1, 2]", "null"])
def test_set_key_refuses_to_overwrite_a_file_it_cannot_read(tmp_path, monkeypatch, content):
    p = tmp_path / "c.json"
    p.write_text(content)
    monkeypatch.setenv("KB_CONFIG", str(p))
    with pytest.raises(config.ConfigError) as e:
        config.set_key("auto_sync", True)
    assert "\n" not in str(e.value) and str(p) in str(e.value)
    assert p.read_text() == content


def test_embed_keys(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    monkeypatch.setenv("KB_CONFIG", str(p))
    cfg = config.load()
    assert (cfg.embed, cfg.embed_url, cfg.embed_sync_seconds) == (False, "", 60)
    p.write_text(json.dumps({"embed": True, "embed_url": "http://127.0.0.1:9/", "embed_sync_seconds": 5}))
    cfg = config.load()
    assert (cfg.embed, cfg.embed_url, cfg.embed_sync_seconds) == (True, "http://127.0.0.1:9", 5)
    p.write_text(json.dumps({"embed": "yes", "embed_url": 3, "embed_sync_seconds": -1}))
    cfg = config.load()
    assert (cfg.embed, cfg.embed_url, cfg.embed_sync_seconds) == (False, "", 60)    # bad values: off, defaults
