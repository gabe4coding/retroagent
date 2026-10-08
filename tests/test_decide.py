import datetime as dt
import io
import json

import pytest
from fixtures import clone, init_remote, make_config
from test_cli import kb_env, run  # noqa: F401 - kb_env is a fixture

from kb import brief, decide, gitops, ledger
from kb.sync import own_paths, run_sync

NOW = dt.datetime(2026, 10, 8, 9, 30, tzinfo=dt.timezone.utc)


def _ledger(root):
    (root / "pages").mkdir(exist_ok=True)
    ledger.save(root, {
        "s-000001": {"category": "Rules", "text": "wait for CI with a watch", "signature": "", "repo": "demo",
                     "sources": ["a0000001"], "weeks": ["2026-W39"]},
        "s-000002": {"category": "", "text": "split long sessions", "signature": "", "repo": "", "sources": [],
                     "weeks": ["2026-W40"]},
        "s-000003": {"category": "", "text": "already done", "signature": "", "repo": "", "sources": [],
                     "weeks": ["2026-W40"]}})
    (root / ledger.DECISIONS_REL).write_text(json.dumps(
        {"s-000003": {"state": "applied", "date": "2026-10-05", "by": "routine", "source": "a0000002"}}))


def _write_answers(root, host, *answers):
    path = root / ledger.answers_rel(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(a) + "\n" for a in answers))


def _a(sid, state, at, date=None, note=""):
    return {"id": sid, "state": state, "date": date or at[:10], "at": at, "note": note}


def test_the_newest_answer_of_any_host_counts(tmp_path):
    _ledger(tmp_path)
    _write_answers(tmp_path, "host-a", _a("s-000001", "accepted", "2026-10-06T08:00:00Z"))
    _write_answers(tmp_path, "host-b", _a("s-000001", "rejected", "2026-10-07T08:00:00Z", note="not worth it"),
                   {"id": "s-000002", "state": "applied", "date": "2026-10-07", "at": "x"})    # not an answer state
    (tmp_path / ledger.answers_rel("host-b")).open("a").write("not json\n")
    got = ledger.decisions(tmp_path)
    assert got["s-000001"] == {"state": "rejected", "date": "2026-10-07", "note": "not worth it",
                               "source": "kb decide on host-b", "by": "owner"}
    assert "s-000002" not in got and got["s-000003"]["by"] == "routine"


def test_a_later_routine_decision_wins_over_an_older_answer(tmp_path):
    _ledger(tmp_path)
    _write_answers(tmp_path, "h", _a("s-000003", "accepted", "2026-10-01T08:00:00Z"))
    assert ledger.decisions(tmp_path)["s-000003"]["state"] == "applied"           # applied on 10-05, after the answer
    _write_answers(tmp_path, "h", _a("s-000003", "rejected", "2026-10-05T08:00:00Z"))
    assert ledger.decisions(tmp_path)["s-000003"]["state"] == "rejected"          # same day: the owner wins


def test_waiting_lists_the_undecided_newest_week_first(tmp_path):
    _ledger(tmp_path)
    assert decide.waiting(tmp_path) == ["s-000002", "s-000001"]
    decide.answer(tmp_path, tmp_path / ".kb", "h", "s-000002", "accepted", now=NOW)
    assert decide.waiting(tmp_path) == ["s-000001"]


def test_answer_writes_the_host_file_and_the_local_copy(tmp_path):
    _ledger(tmp_path)
    kb_dir = tmp_path / ".kb"
    got = decide.answer(tmp_path, kb_dir, "h", "s-000001", "accepted", note="  yes,\n do it ", now=NOW)
    assert got == {"id": "s-000001", "state": "accepted", "date": NOW.astimezone().date().isoformat(),
                   "at": "2026-10-08T09:30:00Z", "note": "yes, do it"}
    line = (tmp_path / "decisions" / "h" / "answers.jsonl").read_text()
    assert (kb_dir / decide.BACKUP).read_text() == line and json.loads(line) == got
    with pytest.raises(ValueError, match="no suggestion s-999999"):
        decide.answer(tmp_path, kb_dir, "h", "s-999999", "accepted")
    with pytest.raises(ValueError, match="answer must be"):
        decide.answer(tmp_path, kb_dir, "h", "s-000001", "applied")


def test_restore_adds_back_only_the_lost_lines(tmp_path):
    _ledger(tmp_path)
    kb_dir = tmp_path / ".kb"
    decide.answer(tmp_path, kb_dir, "h", "s-000001", "accepted", now=NOW)
    decide.answer(tmp_path, kb_dir, "h", "s-000002", "rejected", now=NOW + dt.timedelta(minutes=1))
    path = tmp_path / ledger.answers_rel("h")
    first, second = path.read_text().splitlines()
    assert decide.restore(tmp_path, kb_dir, "h") == 0
    path.write_text(first)                                  # a repair went back to a version without the second line
    (kb_dir / decide.BACKUP).open("a").write("garbage\n")
    assert decide.restore(tmp_path, kb_dir, "h") == 1
    assert path.read_text() == f"{first}\n{second}\n"
    path.unlink()
    assert decide.restore(tmp_path, kb_dir, "h") == 2 and path.read_text() == f"{first}\n{second}\n"
    assert decide.restore(tmp_path / "other", tmp_path / "nothing", "h") == 0


def test_later_hides_the_brief_line_until_a_new_question_arrives(tmp_path):
    _ledger(tmp_path)
    kb_dir = tmp_path / ".kb"
    today = dt.date(2026, 10, 8)
    line = decide.brief_line(tmp_path, kb_dir, today)
    assert line.startswith("2 retro suggestions wait for the user") and "`kb decide`" in line
    assert decide.later(kb_dir, decide.waiting(tmp_path), days=2, today=today) == "2026-10-10"
    assert decide.brief_line(tmp_path, kb_dir, today) == ""
    assert decide.brief_line(tmp_path, kb_dir, dt.date(2026, 10, 10)).startswith("2 retro")     # the day is over
    known = ledger.load(tmp_path)
    ledger.save(tmp_path, {**known, "s-000004": {**known["s-000002"], "weeks": ["2026-W41"]}})
    assert decide.brief_line(tmp_path, kb_dir, today).startswith("3 retro")                      # a new one arrived
    (kb_dir / decide.LATER).write_text("{broken")
    assert decide.hidden_until(kb_dir, ["s-000001"], today) == ""


def test_the_brief_asks_the_agent_to_tell_the_user(tmp_path):
    _ledger(tmp_path)
    decide.answer(tmp_path, tmp_path / ".kb", "h", "s-000001", "accepted", now=NOW)
    out = brief.build(tmp_path, "/work/demo").splitlines()
    assert out[:2] == ["Changes the owner accepted for the demo repo (`kb suggestions`):",
                       "- [s-000001] Rules · wait for CI with a watch"]          # an answer is a decision
    assert out[2].startswith("1 retro suggestion waits for the user") and out[3] == brief.KB_LINE


def test_decide_cli(kb_env, capsys, monkeypatch):  # noqa: F811 - kb_env is a fixture
    assert run(capsys, "decide") == (0, "nothing waits for you\n")
    _ledger(kb_env)
    code, out = run(capsys, "decide")
    assert code == 0 and out.index("s-000002  proposed") < out.index("s-000001  proposed")
    assert "2 wait for you" in out and "kb decide later" in out and "s-000003" not in out
    assert [r["id"] for r in json.loads(run(capsys, "decide", "--json")[1])] == ["s-000002", "s-000001"]

    monkeypatch.setattr("sys.stdin", io.StringIO(""))                        # an agent: no terminal
    code, out = run(capsys, "decide", "accept", "s-000001")
    assert code == 2 and "only in your own terminal" in out
    assert not (kb_env / "decisions").exists()

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    assert run(capsys, "decide", "accept")[0] == 2
    code, out = run(capsys, "decide", "reject", "s-000001", "--note", "too broad")
    assert code == 0 and out.startswith("s-000001: rejected")
    assert ledger.decisions(kb_env)["s-000001"]["note"] == "too broad"
    code, out = run(capsys, "decide", "accept", "s-999999")
    assert code == 2 and "no suggestion s-999999" in out

    code, out = run(capsys, "decide", "later", "--days", "3")
    assert code == 0 and "stop asking about these 1 until" in out
    assert "New sessions stop asking until" in run(capsys, "decide")[1]
    code, out = run(capsys, "suggestions", "--all")
    assert "rejected by owner (kb decide on h): too broad" in out


@pytest.mark.slow
def test_an_answer_is_pushed_by_its_host_and_survives_a_dropped_commit(tmp_path):
    remote = init_remote(tmp_path)
    none = tmp_path / "none"
    a = make_config(clone(remote, tmp_path / "a"), "host-a", none, none, none)
    b = make_config(clone(remote, tmp_path / "b"), "host-b", none, none, none)
    assert "decisions/host-a" in own_paths(a)
    _ledger(a.root)
    decide.answer(a.root, a.kb_dir, "host-a", "s-000001", "accepted", now=NOW)
    ra = run_sync(a, summary_cap=0)
    assert ra.errors == [] and ra.pushed
    assert gitops.git(a.root, "ls-files", "decisions").stdout.split() == ["decisions/host-a/answers.jsonl"]
    assert run_sync(b, summary_cap=0).errors == []
    assert ledger.answers(b.root)["s-000001"]["host"] == "host-a"

    decide.answer(a.root, a.kb_dir, "host-a", "s-000002", "rejected", now=NOW)
    gitops.git(a.root, "checkout", "--", "decisions")         # what a repair does to a change not yet pushed
    assert "s-000002" not in ledger.answers(a.root)
    assert run_sync(a, summary_cap=0).errors == []
    assert run_sync(b, summary_cap=0).errors == []
    assert ledger.answers(b.root)["s-000002"]["state"] == "rejected"
