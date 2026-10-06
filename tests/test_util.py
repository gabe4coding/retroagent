import os

import pytest

from kb.util import (
    atomic_write, clean_user_text, first_line, head_lines, hhmm, iso_utc,
    project_from_cwd, project_from_git_url, rel_path, slug,
)


def test_iso_utc_normalizes_formats():
    assert iso_utc("2026-10-06T13:02:11.123Z") == "2026-10-06T13:02:11Z"
    assert iso_utc("2026-10-06T15:02:11+02:00") == "2026-10-06T13:02:11Z"
    assert iso_utc(1759755731) == "2025-10-06T13:02:11Z"
    assert iso_utc(1759755731000) == "2025-10-06T13:02:11Z"
    assert iso_utc("garbage") == ""
    assert iso_utc(None) == ""


def test_hhmm():
    assert hhmm("2026-10-06T13:02:11Z") == "13:02"
    assert hhmm("") == "--:--"


def test_first_line_and_head_lines():
    assert first_line("\n  hello world  \nsecond") == "hello world"
    assert first_line("x" * 200, 10) == "xxxxxxxxx…"
    assert head_lines("a\n\n  b \nc\nd", 3) == "a / b / c"


def test_slug():
    assert slug("Acme Agent Plugins!") == "acme-agent-plugins"
    assert slug("") == "unknown"


def test_rel_path():
    assert rel_path("/r/demo/src/a.ts", "/r/demo") == "src/a.ts"
    assert rel_path("/other/a.ts", "/r/demo") == "/other/a.ts"


def test_project_names():
    assert project_from_cwd("/Users/me/Repositories/demo-tool") == "demo-tool"
    assert project_from_cwd("/Users/me/Repositories/demo-tool/.claude/worktrees/x-1") == "demo-tool"
    assert project_from_cwd("/Users/me/code/app/.worktrees/feat") == "app"
    assert project_from_cwd("/Users/me/Library/Application Support/Claude/scratch-workspaces/a/b") == "scratch"
    assert project_from_cwd("") == "unknown"
    assert project_from_git_url("git@github.com:me/demo-repo.git") == "demo-repo"
    assert project_from_git_url("https://github.com/me/Demo/") == "demo"
    assert project_from_git_url("") == ""


def test_clean_user_text_strips_harness_wrappers():
    raw = "Fix it <system-reminder>hidden</system-reminder>\n\n\n\nplease"
    assert clean_user_text(raw) == "Fix it\n\nplease"
    cmd = ("<command-name>/kb-retro</command-name><command-message>kb-retro</command-message>"
           "<command-args>last week</command-args>")
    assert clean_user_text(cmd) == "/kb-retro last week"
    assert clean_user_text("<bash-input>ls -la</bash-input><bash-stdout>x</bash-stdout>") == "! ls -la"
    long = "y" * 5000
    out = clean_user_text(long)
    assert out.startswith("y" * 4000) and out.endswith("[… 1000 chars cut]")


def test_atomic_write_skips_identical(tmp_path):
    p = tmp_path / "a" / "b.txt"
    assert atomic_write(p, b"x") is True
    assert atomic_write(p, b"x") is False
    assert atomic_write(p, b"y") is True
    assert p.read_bytes() == b"y"


def test_atomic_write_failure_keeps_old_content_and_leaves_no_temp_file(tmp_path, monkeypatch):
    p = tmp_path / "d" / "f.txt"
    assert atomic_write(p, b"old") is True

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, b"new")
    monkeypatch.undo()
    assert p.read_bytes() == b"old"
    assert [x.name for x in p.parent.iterdir()] == ["f.txt"]


def test_atomic_write_cleans_up_on_keyboard_interrupt(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    p = work / "f.txt"

    def boom(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(KeyboardInterrupt):
        atomic_write(p, b"x")
    monkeypatch.undo()
    assert list(work.iterdir()) == []


def test_atomic_write_temp_name_is_unique_and_ends_with_tmp(tmp_path, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    atomic_write(tmp_path / "a.md", b"1")
    atomic_write(tmp_path / "a.md", b"2")
    assert all(n.startswith("a.md.") and n.endswith(".tmp") for n in seen) and len(set(seen)) == 2


def test_gitignore_ignores_tmp_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
        assert "*.tmp" in fh.read().split()


def test_slug_has_no_trailing_separator_after_the_length_cut():
    out = slug("a" * 39 + "-b")
    assert out == "a" * 39
    assert not out.endswith(("-", "."))
    assert slug("x" * 38 + "..y") == "x" * 38


def test_clean_user_text_drops_ci_monitor_events():
    raw = ('Check the build <ci-monitor-event pr="12" state="failed">\nlog line 1\nlog line 2\n</ci-monitor-event>'
           " now")
    assert clean_user_text(raw) == "Check the build  now"
    assert clean_user_text("<ci-monitor-event>x</ci-monitor-event>") == ""
