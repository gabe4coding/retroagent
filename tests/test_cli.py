import json
import sqlite3

import pytest
from fixtures import SID, T1
from test_index import OLD_SCHEMA, build_kb, put

from kb.cli import main
from kb.state import State
from kb.stats import REPORTS


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    root = build_kb(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"root": str(root), "host": "h", "claude_dir": str(tmp_path / "none"),
                               "codex_dirs": [], "codex_home": str(tmp_path / "none")}))
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    return root


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_find(kb_env, capsys):
    code, out = run(capsys, "find", "flaky", "motion")
    first = out.splitlines()[0]
    assert code == 0 and first.startswith("11111111 2026-10-06 claude")
    assert "Fix flaky motion test" in first and "«" in first
    code, out = run(capsys, "find", "nothing-matches-this")
    assert code == 1 and out.strip() == "no matches"
    code, out = run(capsys, "find", "retry", "--agent", "codex", "--json")
    assert json.loads(out)[0]["id"] == T1


def test_summary(kb_env, capsys):
    code, out = run(capsys, "summary", SID[:8])
    assert code == 0 and "title: Fix flaky motion test" in out
    assert "subagent: a1d0ec8d Explore tests" in out and "pr: https://github.com/me/demo/pull/7" in out
    assert "files: src/motion.ts" in out
    code, out = run(capsys, "summary", "01a0c")
    assert code == 1 and out.startswith("ambiguous id")
    code, out = run(capsys, "summary", "ffffffff")
    assert code == 1 and "no session" in out


def test_show(kb_env, capsys):
    _, out = run(capsys, "show", SID[:8])
    assert "## [1] user" in out and "## [4] assistant" in out and "## [2]" not in out
    _, out = run(capsys, "show", SID[:8], "--turn", "2")
    assert "## [2] assistant" in out and "## [1]" not in out
    _, out = run(capsys, "show", SID[:8], "--grep", "timer")
    assert "## [2] assistant" in out and "## [3]" not in out
    _, out = run(capsys, "show", SID[:8], "--max-chars", "50")
    assert "cut at 50 chars" in out


def test_recent_stats_sql(kb_env, capsys):
    _, out = run(capsys, "recent")
    assert [l[:8] for l in out.splitlines()] == ["11111111", T1[:8]]
    for name in REPORTS:
        assert run(capsys, "stats", name)[0] == 0
    _, out = run(capsys, "stats", "overview")
    assert "claude" in out and "codex" in out
    code, out = run(capsys, "stats", "nope")
    assert code == 2 and "overview" in out
    _, out = run(capsys, "sql", "SELECT COUNT(*) AS n FROM sessions")
    assert out.splitlines()[1].strip() == "4"
    code, out = run(capsys, "sql", "DELETE FROM sessions")
    assert code == 2 and "only SELECT" in out


def test_reindex_and_status(kb_env, capsys):
    code, out = run(capsys, "reindex")
    assert code == 0 and out.strip() == "indexed 4 sessions"
    _, out = run(capsys, "status")
    assert "last sync: never" in out and "pending sessions: 0" in out and "summary backlog: 2" in out


def test_help(capsys):
    assert main([]) == 0
    assert "kb find" in capsys.readouterr().out


def test_find_rebuilds_an_index_written_by_an_older_schema(kb_env, capsys):
    path = kb_env / ".kb" / "index.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.close()
    code, out = run(capsys, "find", "flaky", "motion")
    assert code == 0 and out.startswith("11111111 2026-10-06 claude")


def test_find_fts_reports_a_bad_query(kb_env, capsys):
    code, out = run(capsys, "find", "--fts", '"unterminated')
    assert code == 2 and out.startswith("bad FTS query")
    code, out = run(capsys, "find", "--fts", "title:flaky", "--json")
    assert code == 0 and json.loads(out)[0]["id"] == SID
    assert set(json.loads(out)[0]) == {"id", "agent", "host", "project", "started", "title", "parent", "snippet", "turn"}


def test_sql_reports_multiple_statements_and_bad_ids(kb_env, capsys):
    code, out = run(capsys, "sql", "SELECT 1; SELECT 2")
    assert code == 2 and "one SQL statement" in out
    code, out = run(capsys, "summary", "")
    assert code == 1 and "empty id" in out


def _path_with_gitleaks(monkeypatch, tmp_path, installed):
    bin_dir = tmp_path / ("gl-bin-installed" if installed else "gl-bin-missing")
    bin_dir.mkdir()
    if installed:
        (bin_dir / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "gitleaks").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def test_status_reports_whether_gitleaks_is_installed(kb_env, capsys, monkeypatch, tmp_path):
    _path_with_gitleaks(monkeypatch, tmp_path, installed=False)
    _, out = run(capsys, "status")
    assert "gitleaks: not installed (built-in redaction only)" in out.splitlines()
    _path_with_gitleaks(monkeypatch, tmp_path, installed=True)
    _, out = run(capsys, "status")
    assert "gitleaks: installed" in out.splitlines() and "not installed" not in out


def test_status_lists_quarantined_files_up_to_five(kb_env, capsys):
    _, out = run(capsys, "status")
    assert "quarantined:" not in out                                          # nothing held back: no line
    st = State.load(kb_env / ".kb" / "sync-state.json")
    st.quarantine = {f"sessions/h/claude/2026/10/f{i}.md": f"2026-10-0{i + 1}T10:00:00Z" for i in range(7)}
    st.save()
    _, out = run(capsys, "status")
    lines = out.splitlines()
    listed = lines[lines.index("quarantined: 7 file(s)") + 1:]
    assert [l for l in listed if l.startswith("  sessions/h/")] == listed[:5]
    assert listed[0].startswith("  sessions/h/claude/2026/10/f0.md") and "2026-10-01" in listed[0]
    assert any("2 more" in l for l in listed) and not any("f5.md" in l or "f6.md" in l for l in listed)
    st.quarantine = {"sessions/h/claude/2026/10/only.md": "2026-10-06T10:00:00Z"}
    st.save()
    _, out = run(capsys, "status")
    assert "quarantined: 1 file(s)" in out and "only.md" in out and "more" not in out


def test_status_backlog_follows_the_regrowth_rule(kb_env, capsys):
    """Same rule as the sync: no summary yet, or 4 or more turns beyond what the summary covers."""
    turns = tuple(f"turn {i}" for i in range(10))
    base = 2                                                                  # the two fixture sessions have no summary
    for covered, listed in ((7, 0), (6, 1), (10, 0), (0, 1)):
        put(kb_env, "h/claude/2026/10/long.md", "99999999-0000-0000-0000-000000000000", turns=turns,
            summary="s", summary_turns=covered)
        run(capsys, "reindex")                                                # status reads the index sync built
        _, out = run(capsys, "status")
        assert f"summary backlog: {base + listed}" in out.splitlines(), (covered, out)
