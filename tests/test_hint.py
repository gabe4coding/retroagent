import io
import json

import pytest
from embed_fakes import FakeEmbedServer

from kb import cli
from kb.config import Config
from kb.hint import (MAX_CHARS, PREFIX, Bullet, bullets, denied, error_text, find, format_hint, keyword_match,
                     passes_gate, run)

PAGE = """---
kind: "project"
name: "demo-app"
updated: "2026-10-01T10:00:00Z"
sessions: 3
sources: ["1a2b3c4d", "5e6f7a8b"]
---

# demo-app

## Current state
- the parser module raises when the config file is missing → not an error bullet

## Errors seen → fixes
- `make check` fails: ModuleNotFoundError no module named yaml → install the dev extras with `pip install -e .[dev]` (1a2b3c4d)
- Lock file conflict after rebase in the build cache folder → delete the stale lock and rerun (5e6f7a8b, 9c9c9c9c)
- Snapshot timeouts on the flaky staging site → use the local site for benchmarks (memory demo-app/local-site)
- auto mode classifier denied the push to the main branch from the module → ran git push directly (0d0d0d0d)
- the parser crashed once
- none recorded

## Open threads
- module yaml missing → not in the errors section (2b2b2b2b)
"""

NO_YAML = "Exit code 1\nTraceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'"


@pytest.fixture
def kb(tmp_path):
    root = tmp_path / "data"
    (root / "pages" / "projects").mkdir(parents=True)
    (root / "pages" / "projects" / "demo-app.md").write_text(PAGE)
    cfg = Config(root=root, host="h1")
    cfg.hints = True
    return cfg


def _event(err, session="sess-1", cwd="/work/demo-app"):
    return {"hook_event_name": "PostToolUseFailure", "session_id": session, "cwd": cwd, "tool_name": "Bash",
            "tool_input": {"command": "make check"}, "error": err}


def test_bullets_keep_only_fixes_from_the_errors_section():
    got = bullets(PAGE)
    assert [b.problem[:20] for b in got] == ["`make check` fails: ", "Lock file conflict a", "Snapshot timeouts on",
                                             "auto mode classifier"]
    yaml, lock, snap, _ = got
    assert yaml.source == "kb summary 1a2b3c4d" and yaml.text.endswith("`pip install -e .[dev]`")
    assert lock.source == "kb summary 5e6f7a8b"                 # the first of several sources
    assert snap.source == "kb memory demo-app/local-site"
    assert bullets("no front matter") == []


def test_gate_needs_an_error_word_at_the_start():
    assert not passes_gate("Exit code 1")                       # grep found nothing
    assert not passes_gate("Exit code 1 / src/a.py:3: import os / src/b.py:9: import sys")
    assert passes_gate(NO_YAML)
    assert not passes_gate("x" * 200 + " error")                # only the first 200 chars count


def test_deny_list():
    assert denied("Permission to use Bash with command git push has been denied.")
    assert denied("The user doesn't want to proceed with this tool use.")
    assert denied("blocked by the auto mode classifier")
    assert not denied(NO_YAML)


def test_keyword_rule_needs_three_shared_words():
    items = bullets(PAGE)
    b, shared = keyword_match("ModuleNotFoundError: No module named 'yaml' while make check fails", items, 3)
    assert b.source == "kb summary 1a2b3c4d" and shared >= 3
    assert keyword_match("lock file is missing", items, 3) is None          # 2 shared words: lock, file


def test_find_never_hints_a_denial_or_a_bullet_about_one(kb):
    items = bullets(PAGE)
    assert find(kb, "Error: auto mode classifier denied the push to the main branch", items) is None
    # the words match the classifier bullet best, but it is never a hint
    hit = find(kb, "error: push to the main branch from the module failed", items)
    assert hit is None or "classifier" not in hit[0].full


def test_run_gives_one_hint_per_bullet_per_session_and_logs_it(kb):
    line = run(kb, _event(NO_YAML))
    assert line.startswith(PREFIX) and line.endswith("(kb summary 1a2b3c4d)") and len(line) <= MAX_CHARS
    assert run(kb, _event(NO_YAML)) == ""                       # same session: not again
    assert run(kb, _event(NO_YAML, session="sess-2")) == line   # another session gets it
    log = [json.loads(x) for x in (kb.kb_dir / "hints" / "log.jsonl").read_text().splitlines()]
    assert [(r["session"], r["method"], r["project"]) for r in log] == [("sess-1", "keyword", "demo-app"),
                                                                        ("sess-2", "keyword", "demo-app")]
    assert log[0]["score"] >= 3 and "yaml" in log[0]["bullet"]


def test_run_is_silent_without_a_strong_match(kb):
    assert run(kb, _event("Exit code 1")) == ""                                     # no message
    assert run(kb, _event("Error: something else entirely went wrong")) == ""       # no match
    assert run(kb, _event(NO_YAML, cwd="/work/other-project")) == ""               # no page
    assert run(kb, {"session_id": "s", "cwd": "/work/demo-app", "tool_response": {"stdout": NO_YAML}}) == ""
    kb.hints = False
    assert run(kb, _event(NO_YAML)) == ""
    assert not (kb.kb_dir / "hints" / "log.jsonl").exists()


def test_semantic_rule_with_an_embedding_server(kb):
    srv = FakeEmbedServer()
    try:
        kb.embed_url = srv.url
        kb.hint_semantic_min = 0.3
        # 2 shared words only, but "unstable" and "flaky" are one word to the fake model
        err = "Error: unstable staging snapshot"
        assert keyword_match(err, bullets(PAGE), 3) is None
        line = run(kb, _event(err))
        assert "local site" in line and line.endswith("(kb memory demo-app/local-site)")
        log = json.loads((kb.kb_dir / "hints" / "log.jsonl").read_text().splitlines()[-1])
        assert log["method"] == "semantic" and 0.3 <= log["score"] <= 1
        embedded = len(srv.inputs)
        assert run(kb, _event(err, session="sess-2")) == line
        assert len(srv.inputs) == embedded + 1                  # the bullet vectors came from the cache
        kb.hint_semantic_min = 0.99
        assert run(kb, _event(err, session="sess-3")) == ""    # below the limit: silent, no keyword fallback
    finally:
        srv.close()


def test_semantic_falls_back_to_keywords_when_the_server_fails(kb):
    srv = FakeEmbedServer()
    try:
        srv.fail = True
        kb.embed_url = srv.url
        assert "pip install" in run(kb, _event(NO_YAML))
    finally:
        srv.close()


def test_error_text_of_each_event_shape():
    assert error_text({"error": "Exit code 2\nboom: failed"}) == "Exit code 2 / boom: failed"
    assert error_text({"tool_response": "Exit code: 1\nError: not found"}) == "Exit code: 1 / Error: not found"
    assert error_text({"tool_response": "Exit code: 0\nerror is a word here"}) == ""
    assert error_text({"tool_response": "plain output that mentions an error"}) == ""     # Codex shell output
    assert error_text({"tool_response": {"exit_code": 1, "output": "fatal: not a git repository"}}) == \
        "Exit code 1 / fatal: not a git repository"
    assert error_text({"tool_response": {"isError": True, "content": [{"type": "text", "text": "Invalid id"}]}}) == \
        "Invalid id"
    assert error_text({"tool_response": {"stdout": "", "stderr": "error"}}) == ""          # a success
    assert error_text({}) == ""


DATED = PAGE.replace("- the parser module raises when the config file is missing → not an error bullet",
                     "- the release is out (3c3c3c3c · 2026-10-01)").replace(
    "`pip install -e .[dev]` (1a2b3c4d)", "`pip install -e .[dev]` (1a2b3c4d · 2026-09-20)")


def test_a_dated_bullet_keeps_its_date_and_a_clean_text():
    yaml = bullets(DATED)[0]
    assert yaml.date == "2026-09-20" and yaml.source == "kb summary 1a2b3c4d"
    assert yaml.text.endswith("`pip install -e .[dev]`") and bullets(PAGE)[0].date == ""


def test_a_stale_bullet_is_never_a_hint_and_the_hint_shows_the_date(kb):
    page = kb.root / "pages" / "projects" / "demo-app.md"
    page.write_text(DATED)
    line = run(kb, _event(NO_YAML))
    assert line.endswith("(kb summary 1a2b3c4d · 2026-09-20)")
    log = json.loads((kb.kb_dir / "hints" / "log.jsonl").read_text().splitlines()[-1])
    assert log["seen"] == "2026-09-20"
    page.write_text(DATED.replace("(1a2b3c4d · 2026-09-20)", "(1a2b3c4d · 2026-06-01)"))   # 122 days older
    assert run(kb, _event(NO_YAML, session="sess-2")) == ""
    (kb.root / "pages" / "config.json").write_text(json.dumps({"stale_days": 200}))
    assert "pip install" in run(kb, _event(NO_YAML, session="sess-3"))


def test_format_keeps_the_budget():
    b = Bullet("x → " + "y" * 400, "", "x", "kb summary 1a2b3c4d", "k")
    line = format_hint(b)
    assert len(line) == MAX_CHARS and line.endswith("… (kb summary 1a2b3c4d)")


def test_cli_hint_prints_the_hook_json(kb, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(NO_YAML))))
    monkeypatch.setattr(cli.config_mod, "load", lambda *a: kb)
    assert cli.main(["hint", "--event", "error", "--hook"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert out["hookSpecificOutput"]["additionalContext"].startswith(PREFIX)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hint", "--event", "error"]) == 0 and capsys.readouterr().out == ""
