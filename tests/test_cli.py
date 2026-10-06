import json

import pytest
from fixtures import SID, T1
from test_index import build_kb

from kb.cli import main
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
