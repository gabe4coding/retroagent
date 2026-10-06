import json
import os
import re
import sqlite3

import pytest
from fixtures import AID, SID, T1
from test_index import OLD_SCHEMA, build_kb, put

from kb import cli
from kb.cli import main
from kb.state import State
from kb.stats import REPORTS
from kb.util import short_id


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
    assert code == 0 and first.startswith("55555555 2026-10-06 claude")
    assert "Fix flaky motion test" in first and "«" in first
    code, out = run(capsys, "find", "nothing-matches-this")
    assert code == 1 and out.strip() == "no matches"
    code, out = run(capsys, "find", "retry", "--agent", "codex", "--json")
    assert json.loads(out)[0]["id"] == T1


def test_summary(kb_env, capsys):
    code, out = run(capsys, "summary", SID[:8])
    assert code == 0 and "title: Fix flaky motion test" in out
    assert "subagent: e94ad30f Explore tests" in out and "pr: https://github.com/me/demo/pull/7" in out
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
    assert [l[:8] for l in out.splitlines()] == ["55555555", short_id(T1)]
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
    assert code == 0 and out.startswith("55555555 2026-10-06 claude")


def test_find_fts_reports_a_bad_query(kb_env, capsys):
    code, out = run(capsys, "find", "--fts", '"unterminated')
    assert code == 2 and out.startswith("bad FTS query")
    code, out = run(capsys, "find", "--fts", "title:flaky", "--json")
    assert code == 0 and json.loads(out)[0]["id"] == SID
    assert set(json.loads(out)[0]) == {"id", "short", "agent", "host", "project", "started", "title", "parent", "snippet",
                                       "turn"}


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


# ---- short ids

V7 = ("01a0d000-0000-7000-8000-5f3c9a1be7d2", "01a0d000-0001-7123-9abc-0e4b7c2d91a6")    # same first 8 characters


def _add_v7_sessions(root):
    from collections import Counter

    from kb.model import Session
    from kb.store import write_session
    paths = []
    for n, sid in enumerate(V7):
        s = Session(id=sid, agent="codex", project="demo", started=f"2026-10-06T10:0{n}:00Z", title=f"uuid7 session {n}")
        s.add_turn("user", s.started, [f"question {n}"])
        s.add_turn("assistant", s.started, [f"answer {n}"])
        paths.append(write_session(root, "h", s, Counter())[0][0])
    return paths


def test_sessions_with_the_same_first_eight_characters_have_distinct_files_and_shorts(kb_env, capsys):
    paths = _add_v7_sessions(kb_env)
    assert paths[0] != paths[1] and paths[0].endswith("_9a1be7d2.md") and paths[1].endswith("_7c2d91a6.md")
    run(capsys, "reindex")
    _, out = run(capsys, "recent")
    shown = [l.split()[0] for l in out.splitlines()]
    assert "9a1be7d2" in shown and "7c2d91a6" in shown
    for n, short in enumerate(("9a1be7d2", "7c2d91a6")):
        code, out = run(capsys, "summary", short)
        assert code == 0 and f"title: uuid7 session {n}" in out and f"md: {paths[n]}" in out
        code, out = run(capsys, "show", short)
        assert code == 0 and out.startswith(f"{short} · uuid7 session {n}") and f"answer {n}" in out
    code, out = run(capsys, "summary", "01a0d000")                       # the old prefix is ambiguous: list the shorts
    assert code == 1 and out.startswith("ambiguous id") and "9a1be7d2" in out and "7c2d91a6" in out


def test_find_json_rows_carry_the_short_id(kb_env, capsys):
    _add_v7_sessions(kb_env)
    run(capsys, "reindex")
    code, out = run(capsys, "find", "uuid7", "--json")
    rows = json.loads(out)
    assert code == 0 and {r["id"]: r["short"] for r in rows} == {V7[0]: "9a1be7d2", V7[1]: "7c2d91a6"}
    _, out = run(capsys, "find", "uuid7")
    assert all(l.split()[0] in ("9a1be7d2", "7c2d91a6") for l in out.splitlines())


def test_subagent_lines_show_the_short_ids_of_the_parent_and_the_child(kb_env, capsys):
    _, out = run(capsys, "find", "setTimeout", "--agent", "claude")
    assert out.startswith(f"{short_id(AID)} ") and f"↳{short_id(SID)} " in out
    code, out = run(capsys, "summary", short_id(AID))
    assert code == 0 and f"parent: {SID}" in out


def test_a_too_short_prefix_says_so(kb_env, capsys):
    code, out = run(capsys, "summary", "55")
    assert code == 1 and "no session with id 55" in out and "at least 4" in out


def test_summary_caps_the_subagent_list(kb_env, capsys, monkeypatch):
    from kb.index import Index
    kids = [{"id": f"0000000{i:x}-aaaa-bbbb-cccc-{i:012x}", "title": f"Sub {i}"} for i in range(14)]
    monkeypatch.setattr(Index, "children", lambda self, sid: kids)
    code, out = run(capsys, "summary", SID[:8])
    lines = [l for l in out.splitlines() if l.startswith("subagent")]
    assert code == 0 and len(lines) == 11
    assert lines[-1] == "subagents: … and 4 more"


# ---- review fixes: read-only readers, one-line errors, short find output

def _freeze(kb_dir):
    """Read-only like a sandbox: checkpoint the WAL, drop its side files, 0o444 on the files, 0o555 on the folder."""
    if (kb_dir / "index.sqlite").exists():
        con = sqlite3.connect(kb_dir / "index.sqlite")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    for f in kb_dir.glob("index.sqlite-*"):
        f.unlink()
    for f in kb_dir.iterdir():
        if f.is_file():
            f.chmod(0o444)
    kb_dir.chmod(0o555)


def _thaw(path):
    path.chmod(0o755)
    for f in path.iterdir():
        if f.is_file():
            f.chmod(0o644)


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


@needs_permissions
def test_read_commands_work_when_the_kb_folder_is_read_only(kb_env, capsys):
    run(capsys, "reindex")
    kb_dir = kb_env / ".kb"
    _freeze(kb_dir)
    try:
        code, out = run(capsys, "find", "flaky", "motion")
        assert code == 0 and out.startswith("55555555 2026-10-06 claude")
        code, out = run(capsys, "sql", "SELECT COUNT(*) AS n FROM sessions")
        assert code == 0 and out.splitlines()[1].strip() == "4"
        for argv in (("recent",), ("summary", SID[:8]), ("show", SID[:8]), ("stats", "overview"), ("status",)):
            code, out = run(capsys, *argv)
            assert code == 0 and "kb:" not in out and "not built" not in out, argv
        assert [p.name for p in kb_dir.iterdir() if p.is_dir()] == [] and not (kb_dir / "sync-state.json").exists()
    finally:
        _thaw(kb_dir)


@needs_permissions
@pytest.mark.parametrize("state", ["no folder", "no index", "old index"])
def test_read_commands_say_the_index_is_not_built_when_they_cannot_build_it(kb_env, capsys, state):
    kb_dir = kb_env / ".kb"
    if state == "old index":
        kb_dir.mkdir()
        con = sqlite3.connect(kb_dir / "index.sqlite")
        con.executescript(OLD_SCHEMA)                                         # user_version 0: not this version's schema
        con.close()
    elif state == "no index":
        kb_dir.mkdir()
    guarded = kb_dir if kb_dir.exists() else kb_env
    _freeze(guarded)
    try:
        for argv in (("find", "flaky"), ("recent",), ("summary", SID[:8]), ("show", SID[:8]), ("stats", "overview"),
                     ("sql", "SELECT 1"), ("status",)):
            code, out = run(capsys, *argv)
            assert (code, out) == (2, "index not built yet; run: kb reindex\n"), (state, argv, out)
    finally:
        _thaw(guarded)


def test_a_missing_or_old_index_is_still_built_when_the_folder_is_writable(kb_env, capsys):
    assert not (kb_env / ".kb").exists()
    code, out = run(capsys, "find", "flaky", "motion")
    assert code == 0 and out.startswith("55555555") and (kb_env / ".kb" / "index.sqlite").exists()


def test_a_reader_does_not_wait_for_a_sync_that_holds_a_write_transaction(kb_env, capsys):
    import time

    from kb.index import Index
    run(capsys, "reindex")
    writer = Index(kb_env / ".kb" / "index.sqlite")                           # what a running sync looks like to a reader
    writer.db.execute("BEGIN IMMEDIATE")
    writer.db.execute("DELETE FROM sessions")
    try:
        started = time.monotonic()
        code, out = run(capsys, "find", "flaky", "motion")
        assert code == 0 and out.startswith("55555555")
        code, out = run(capsys, "sql", "SELECT COUNT(*) AS n FROM sessions")
        assert code == 0 and out.splitlines()[1].strip() == "4"
        assert time.monotonic() - started < 3
    finally:
        writer.db.execute("ROLLBACK")
        writer.close()


def test_a_bad_grep_regex_is_one_line(kb_env, capsys):
    code, out = run(capsys, "show", SID[:8], "--grep", "(")
    assert code == 2 and out.startswith("kb: bad --grep regex: ") and len(out.splitlines()) == 1
    code, out = run(capsys, "show", SID[:8], "--grep", "timer|[")
    assert code == 2 and out.startswith("kb: bad --grep regex: ")


def test_show_says_so_when_the_markdown_file_is_missing(kb_env, capsys):
    run(capsys, "reindex")
    md = next((kb_env / "sessions").rglob("*_55555555.md"))
    md.unlink()
    code, out = run(capsys, "show", SID[:8])
    assert code == 2 and out == f"kb: session file missing: {md}; run: kb reindex\n"


def test_show_reads_a_markdown_file_that_is_not_utf8(kb_env, capsys):
    run(capsys, "reindex")
    md = next((kb_env / "sessions").rglob("*_55555555.md"))
    md.write_bytes(md.read_bytes().replace(b"Fixed: the timer", b"Fixed: the caf\xe9 timer"))
    code, out = run(capsys, "show", SID[:8], "--turn", "2")
    assert code == 0 and "caf" in out and "kb:" not in out


@pytest.mark.parametrize("error", [sqlite3.OperationalError("disk I/O error"), OSError(28, "No space left on device"),
                                   re.error("unbalanced parenthesis"), ValueError("something\nwith lines")])
def test_unexpected_errors_are_one_line_and_exit_2(kb_env, capsys, monkeypatch, error):
    def boom(args, cfg):
        raise error
    monkeypatch.setattr(cli, "cmd_recent", boom)
    code, out = run(capsys, "recent")
    assert code == 2 and out.startswith("kb: ") and len(out.splitlines()) == 1 and "Traceback" not in out
    assert out == "kb: " + " ".join(str(error).split()) + "\n"


def test_reindex_in_a_read_only_folder_fails_with_one_line(kb_env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    run(capsys, "reindex")
    _freeze(kb_env / ".kb")
    try:
        code, out = run(capsys, "reindex")
        assert code == 2 and out.startswith("kb: ") and len(out.splitlines()) == 1
    finally:
        _thaw(kb_env / ".kb")


def test_a_closed_pipe_is_not_an_error(kb_env, capsys, monkeypatch):
    def gone(args, cfg):
        raise BrokenPipeError(32, "Broken pipe")
    monkeypatch.setattr(cli, "cmd_recent", gone)
    code, out = run(capsys, "recent")
    assert code == 0 and "kb:" not in out


def test_find_output_stays_short_with_a_huge_token(kb_env, capsys):
    put(kb_env, "h/claude/2026/10/big.md", "99999999-0000-0000-0000-00000000bbbb",
        turns=("alpha " + "z" * 3000 + " beta needle", "ok"), title="Huge token")
    code, out = run(capsys, "find", "needle")
    assert code == 0 and "Huge token" in out and "«needle»" in out
    assert max(len(line) for line in out.splitlines()) < 330 and len(out) < 700
    code, out = run(capsys, "find", "needle", "--json")
    snippets = [h["snippet"] for h in json.loads(out)]
    assert snippets and all(0 < len(s) <= 200 and s == " ".join(s.split()) for s in snippets)
    assert len(out) < 1500


# ---- item 1: approval gate (kb sync --auto, kb enable, kb disable)

def _config_file(tmp_path):
    return tmp_path / "config.json"


def _set_config(tmp_path, **keys):
    p = _config_file(tmp_path)
    data = json.loads(p.read_text())
    data.update(keys)
    p.write_text(json.dumps(data))


def _spy_run_sync(monkeypatch):
    from kb import sync as sync_mod
    calls = []

    def fake(cfg, **kw):
        calls.append(kw)
        return sync_mod.Report()

    monkeypatch.setattr(sync_mod, "run_sync", fake)
    return calls


def test_sync_auto_exits_silently_when_auto_sync_is_off(kb_env, tmp_path, capsys, monkeypatch):
    _set_config(tmp_path, auto_sync=False)
    calls = _spy_run_sync(monkeypatch)
    code, out = run(capsys, "sync", "--auto")
    assert code == 0 and out == "" and calls == []


def test_sync_auto_runs_when_auto_sync_is_on_or_absent(kb_env, tmp_path, capsys, monkeypatch):
    calls = _spy_run_sync(monkeypatch)
    assert run(capsys, "sync", "--auto")[0] == 0 and len(calls) == 1                 # absent: backward compatible
    _set_config(tmp_path, auto_sync=True)
    assert run(capsys, "sync", "--auto")[0] == 0 and len(calls) == 2


def test_plain_sync_and_backfill_ignore_the_gate(kb_env, tmp_path, capsys, monkeypatch):
    _set_config(tmp_path, auto_sync=False)
    calls = _spy_run_sync(monkeypatch)
    assert run(capsys, "sync", "--now")[0] == 0 and len(calls) == 1
    assert run(capsys, "backfill")[0] == 0 and len(calls) == 2


def test_sync_auto_accepts_the_other_sync_flags(kb_env, tmp_path, capsys, monkeypatch):
    calls = _spy_run_sync(monkeypatch)
    assert run(capsys, "sync", "--auto", "--now", "--no-summaries")[0] == 0
    assert calls[0]["now"] is True and calls[0]["summary_cap"] == 0


def test_disable_and_enable_write_the_config_and_print_one_line(kb_env, tmp_path, capsys):
    before = json.loads(_config_file(tmp_path).read_text())
    code, out = run(capsys, "disable")
    after = json.loads(_config_file(tmp_path).read_text())
    assert code == 0 and len(out.splitlines()) == 1 and "disabled" in out
    assert after == {**before, "auto_sync": False}                                   # other keys are kept
    code, out = run(capsys, "enable")
    assert code == 0 and len(out.splitlines()) == 1 and "enabled" in out
    assert json.loads(_config_file(tmp_path).read_text()) == {**before, "auto_sync": True}


def test_enable_creates_the_config_when_there_is_none(tmp_path, monkeypatch, capsys):
    p = tmp_path / "fresh" / "config.json"
    monkeypatch.setenv("KB_CONFIG", str(p))
    assert run(capsys, "enable")[0] == 0
    assert json.loads(p.read_text()) == {"auto_sync": True}


def test_enable_does_not_overwrite_a_broken_config(tmp_path, monkeypatch, capsys):
    p = tmp_path / "config.json"
    p.write_text("{broken")
    monkeypatch.setenv("KB_CONFIG", str(p))
    code, out = run(capsys, "enable")
    assert code == 2 and len(out.splitlines()) == 1 and str(p) in out
    assert p.read_text() == "{broken"


def test_status_shows_whether_automatic_syncs_are_on(kb_env, tmp_path, capsys):
    _, out = run(capsys, "status")
    assert "auto sync: on" in out.splitlines()
    _set_config(tmp_path, auto_sync=False)
    _, out = run(capsys, "status")
    assert any(l.startswith("auto sync: off") and "kb enable" in l for l in out.splitlines())


def test_status_uses_the_configured_gitleaks_path_and_says_when_it_is_required(kb_env, tmp_path, capsys, monkeypatch):
    _path_with_gitleaks(monkeypatch, tmp_path, installed=False)
    away = tmp_path / "away"
    away.mkdir()
    (away / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
    (away / "gitleaks").chmod(0o755)
    _set_config(tmp_path, gitleaks_path=str(away / "gitleaks"))
    _, out = run(capsys, "status")
    assert "gitleaks: installed" in out.splitlines()
    _set_config(tmp_path, gitleaks_path="", require_gitleaks=True)
    _, out = run(capsys, "status")
    line = next(l for l in out.splitlines() if l.startswith("gitleaks:"))
    assert "not installed" in line and "required" in line
