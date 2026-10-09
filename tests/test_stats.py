from test_index import put

from kb.index import Index, connect_readonly
from kb.stats import error_signature, repeated_errors, signature_sessions

HOOK = ("- Bash `rm -rf dist` → ERROR: PreToolUse:Bash hook error: [bash \"/home/u/.hooks/guard.sh\"]: "
        "Blocked: destructive command")
NOT_READ = "- Edit `/repo/a.py` → ERROR: <tool_use_error>File has not been read yet. Read it first.</tool_use_error>"


def test_error_signature_drops_paths_numbers_and_quotes():
    a = error_signature("Exit code 2 / sed: /repo/one/a.txt: No such file or directory")
    b = error_signature("Exit code 1 / sed: ../two/b.txt: No such file or directory")
    assert a == b == "sed such file directory"
    assert error_signature("ModuleNotFoundError: No module named 'foo'") == error_signature(
        "Traceback (most recent call last): / File \"x.py\", line 3, in <module> / "
        "ModuleNotFoundError: No module named 'bar'")
    assert error_signature("Exit code 1") == ""                          # no message: grep found nothing
    assert len(error_signature("one two three four five six seven eight nine").split()) == 7


def _index(tmp_path):
    root = tmp_path / "kb"
    put(root, "a.md", "aaaaaaaa-0000-0000-0000-000000000001",
        turns=("go", HOOK + "\n" + HOOK + "\n- Bash `grep x` → ERROR: Exit code 1"), started="2026-10-01T10:00:00Z")
    put(root, "b.md", "aaaaaaaa-0000-0000-0000-000000000002",
        turns=("go", HOOK.replace("rm -rf dist", "git clean -fdx")), started="2026-10-03T10:00:00Z", project="other")
    # a subagent counts for its parent: not a third session
    put(root, "c.md", "aaaaaaaa-0000-0000-0000-000000000003", turns=("go", HOOK + "\n" + NOT_READ),
        started="2026-10-03T11:00:00Z", parent="aaaaaaaa-0000-0000-0000-000000000002")
    put(root, "d.md", "aaaaaaaa-0000-0000-0000-000000000004", turns=("go", NOT_READ), started="2026-10-04T10:00:00Z")
    idx = Index(root / ".kb" / "index.sqlite")
    idx.update(root)
    idx.close()
    return connect_readonly(root / ".kb" / "index.sqlite")


def test_repeated_errors(tmp_path):
    con = _index(tmp_path)
    try:
        cols, rows = repeated_errors(con)
        got = [dict(zip(cols, r)) for r in rows]
        assert [(g["sessions"], g["errors"]) for g in got] == [(2, 4), (2, 2)]
        hook, not_read = got
        assert hook["signature"] == "pretooluse bash hook bash blocked destructive command"
        assert (hook["first"], hook["last"], hook["tool"]) == ("2026-10-01", "2026-10-03", "Bash")
        assert hook["first_seen"] == "00000001 [turn 2]" and "guard.sh" in hook["example"]
        assert not_read["tool"] == "Edit"
        # filters: one session left, so nothing came back twice
        rows = repeated_errors(con, since="2026-10-02")[1]          # the hook error is left in one session
        assert [(r[0], r[4]) for r in rows] == [(2, not_read["signature"])]
        assert repeated_errors(con, project="other")[1] == []
    finally:
        con.close()


def test_signature_sessions(tmp_path):
    con = _index(tmp_path)
    try:
        hook = "pretooluse bash hook bash blocked destructive command"
        got = signature_sessions(con)
        assert set(got[hook]) == {"aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002"}
        assert got[hook]["aaaaaaaa-0000-0000-0000-000000000002"] == "2026-10-03T10:00:00Z"   # the parent's own start
        assert signature_sessions(con, {hook}) == {hook: got[hook]}                         # only these signatures
    finally:
        con.close()
