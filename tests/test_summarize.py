import json
import os
import subprocess

import pytest

from kb import summarize as summ
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
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True, "alwaysThinkingEnabled": False}
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "haiku"


def _fake_claude(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.mark.slow
def test_summarize_runs_claude_with_guard(tmp_path, monkeypatch):
    log = tmp_path / "env.txt"
    _fake_claude(tmp_path, monkeypatch,
                 f"echo \"KB_CHILD=$KB_CHILD\" > '{log}'\ncat > /dev/null\nprintf '%s' '{envelope(GOOD)}'\n")
    r = summarize("## [1] user · 10:00\n\nhello", "haiku", cwd=str(tmp_path))
    assert r["outcome"] == "done"
    assert log.read_text().strip() == "KB_CHILD=1"


@pytest.mark.slow
def test_summarize_nonzero_exit_is_unavailable(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, "cat > /dev/null\nexit 1\n")
    with pytest.raises(summ.SummaryUnavailable):
        summarize("x", "haiku", cwd=str(tmp_path))


# ---------------------------------------------------------------- item 2: unavailable vs unusable

def _runner(returncode=0, stdout="", exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append(kw)
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")
    run.calls = calls
    return run


@pytest.mark.parametrize("exc", [OSError("no claude"), subprocess.TimeoutExpired(["claude"], 1), RuntimeError("odd"),
                                 UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])
def test_summarize_runner_failure_is_unavailable(exc):
    with pytest.raises(summ.SummaryUnavailable):
        summarize("x", runner=_runner(exc=exc))


def test_summarize_error_envelope_is_unavailable():
    with pytest.raises(summ.SummaryUnavailable) as e:
        summarize("x", runner=_runner(stdout=envelope("Credit balance is too low", is_error=True)))
    assert "Credit balance" in str(e.value)


@pytest.mark.parametrize("stdout", ["not json", envelope("no json here"), envelope(json.dumps({"summary": ""})),
                                    envelope(json.dumps({"summary": ["a"]})), json.dumps([1])])
def test_summarize_unusable_output_returns_none(stdout):
    assert summarize("x", runner=_runner(stdout=stdout)) is None


def test_summarize_passes_utf8_decoding_to_the_runner():
    run = _runner(stdout=envelope(GOOD))
    assert summarize("x", runner=run)["outcome"] == "done"
    kw = run.calls[0]
    assert kw["encoding"] == "utf-8" and kw["errors"] == "replace" and kw["env"]["KB_CHILD"] == "1"


@pytest.mark.slow
def test_summarize_survives_invalid_utf8_from_claude(tmp_path, monkeypatch):
    out = envelope(json.dumps({"summary": "bad BYTE here", "tags": ["x"], "outcome": "done", "decisions": []}))
    head, tail = out.split("BYTE")
    _fake_claude(tmp_path, monkeypatch,
                 f"cat > /dev/null\nprintf '%s' '{head}'\nprintf '\\377'\nprintf '%s' '{tail}'\n")
    monkeypatch.setenv("LC_ALL", "C")
    r = summarize("x", "haiku", cwd=str(tmp_path))
    assert r is not None and r["summary"].startswith("bad ") and r["summary"].endswith(" here")


# ---------------------------------------------------------------- item 3: output validation

def parsed(**obj):
    return parse_result(envelope(json.dumps({"summary": "s", "tags": [], "outcome": "done", "decisions": [], **obj})))


@pytest.mark.parametrize("summary", [["a", "b"], {"a": 1}, 7, None, True])
def test_summary_must_be_a_string(summary):
    assert parsed(summary=summary) is None


@pytest.mark.parametrize("key", ["tags", "decisions"])
@pytest.mark.parametrize("value", ["abc def", {"a": "b"}, 5, None])
def test_tags_and_decisions_must_be_lists(key, value):
    assert parsed(**{key: value})[key] == []


def test_non_string_items_in_lists_are_dropped():
    r = parsed(tags=["ok", 5, None, ["x"], {"a": 1}], decisions=["fine", 7, None, ["y"]])
    assert r["tags"] == ["ok"] and r["decisions"] == ["fine"]


def test_control_characters_are_stripped():
    r = parsed(summary="a\x00b\x1b[31mred\x07\nsecond\x7f line\r\nthird",
               tags=["t\x00a\x1bg"], decisions=["d\x00e\x08c"])
    assert r["summary"] == "ab[31mred\nsecond line\nthird"
    assert r["tags"] == ["tag"] and r["decisions"] == ["dec"]
    for text in [r["summary"], *r["tags"], *r["decisions"]]:
        assert not any(ord(c) < 32 and c != "\n" or ord(c) == 127 for c in text)
    assert "\n" not in r["decisions"][0]


def test_whitespace_is_collapsed_per_line_tag_and_decision():
    r = parsed(summary="  a   b\t c  \n\n   d    e  ", tags=["foo   bar", "  x\ty  "], decisions=["use   A\t over  B\nfor C"])
    assert r["summary"] == "a b c\nd e"
    assert r["tags"] == ["foo-bar", "x-y"]
    assert r["decisions"] == ["use A over B for C"]


def test_lengths_are_capped():
    r = parsed(summary="\n".join(["x" * 500, "y" * 50, "z" * 50, "w" * 50]), tags=["t" * 100, "u" * 39 + "-" + "v" * 9],
               decisions=["d" * 500, "e" * 10])
    lines = r["summary"].split("\n")
    assert len(lines) == 3 and len(lines[0]) == 200 and lines[1:] == ["y" * 50, "z" * 50]
    assert r["tags"] == ["t" * 40, "u" * 39] and all(len(t) <= 40 for t in r["tags"])
    assert len(r["decisions"][0]) == 200 and r["decisions"][1] == "e" * 10


def test_tags_are_deduplicated_in_order_before_the_cap():
    r = parsed(tags=["B", "a", "b", "A", "b-", "c", "d", "e", "f", "g", "h"])
    assert r["tags"] == ["b", "a", "c", "d", "e", "f"]


GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_summary_and_decisions_are_redacted():
    r = parsed(summary=f"Used token {GH} to push\npassword=hunter22secret", decisions=[f"Rotate {GH}"])
    assert GH not in json.dumps(r) and "hunter22secret" not in json.dumps(r)
    assert "[REDACTED:github-token]" in r["summary"] and "[REDACTED:github-token]" in r["decisions"][0]


def test_redaction_sees_through_control_characters_and_runs_before_truncation():
    split = GH[:10] + "\x00" + GH[10:]
    assert GH[:12] not in parsed(summary=f"tok {split}")["summary"]
    long = "x" * 184 + " " + GH
    assert "ghp_" not in parsed(summary=long)["summary"] and "ghp_" not in parsed(decisions=[long])["decisions"][0]
    key = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    out = parsed(summary=f"key follows\n{key}\nthe end")["summary"]
    assert "MIIEvQ" not in out and "REDACTED:private-key" in out
