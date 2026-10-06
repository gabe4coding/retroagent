import json
import os

from kb.summarize import build_input, command, parse_result, summarize

GOOD = json.dumps({"summary": "Fixed flaky test.\nTimer not awaited.\nline3\nline4",
                   "tags": ["Testing", "playwright", "a", "b", "c", "d", "e"],
                   "outcome": "DONE", "decisions": ["Await timers", "", "x", "y"]})


def envelope(result, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


def test_parse_result_normalizes():
    r = parse_result(envelope("```json\n" + GOOD + "\n```"))
    assert r == {"summary": "Fixed flaky test.\nTimer not awaited.\nline3",
                 "tags": ["testing", "playwright", "a", "b", "c", "d"],
                 "outcome": "done", "decisions": ["Await timers", "x", "y"]}


def test_parse_result_rejects_bad_output():
    assert parse_result("not json") is None
    assert parse_result(envelope(GOOD, is_error=True)) is None
    assert parse_result(envelope("no json here")) is None
    assert parse_result(envelope(json.dumps({"summary": ""}))) is None


def test_build_input_keeps_head_and_tail():
    out = build_input("A" * 100 + "B" * 100, limit=50)
    assert out.startswith("A" * 20) and out.endswith("B" * 30) and "omitted" in out


def test_command_is_lean_and_has_no_bare():
    cmd = command("haiku")
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt"):
        assert flag in cmd
    assert "--bare" not in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "haiku"


def _fake_claude(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def test_summarize_runs_claude_with_guard(tmp_path, monkeypatch):
    log = tmp_path / "env.txt"
    _fake_claude(tmp_path, monkeypatch,
                 f"echo \"KB_CHILD=$KB_CHILD\" > '{log}'\ncat > /dev/null\nprintf '%s' '{envelope(GOOD)}'\n")
    r = summarize("## [1] user · 10:00\n\nhello", "haiku", cwd=str(tmp_path))
    assert r["outcome"] == "done"
    assert log.read_text().strip() == "KB_CHILD=1"


def test_summarize_failure_returns_none(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, "cat > /dev/null\nexit 1\n")
    assert summarize("x", "haiku", cwd=str(tmp_path)) is None
