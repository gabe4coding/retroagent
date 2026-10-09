import io
import json

import pytest
from fixtures import make_claude_tree, make_codex_tree, make_config
from test_cli import run
from test_memories import ENC, NOTE, write_memory

from kb import decide, ledger, memedits
from kb import memories as mem
from kb.index import Index
from kb.sync import Report

OTHER = "-Users-me-Repositories-other"
REF = "demo/prefer-small-prs"
OLD = "memories/h/claude/" + ENC + "/prefer-small-prs.md"
INDEX = "- [prefer-small-prs](prefer-small-prs.md) — small pull requests\n- [keep](keep.md) — another one\n"


@pytest.fixture
def cfg(tmp_path):
    projects = make_claude_tree(tmp_path / "src")
    sessions, home = make_codex_tree(tmp_path / "src")
    root = tmp_path / "kb"
    root.mkdir()
    cfg = make_config(root, "h", projects, sessions, home)
    write_memory(projects)
    write_memory(projects, name="MEMORY.md", text=INDEX)
    write_memory(projects, name="keep.md", text="---\nname: keep\nmodified: 2026-10-01T08:00:00Z\n---\n\nkeep me\n")
    write_memory(projects, name="other.md", folder=OTHER,
                 text="---\nname: other\nmodified: 2026-10-02T08:00:00Z\n---\n\nanother project\n")
    sync(cfg)
    return cfg


def sync(cfg):
    report = Report()
    mem.sync_memories(cfg, None, report, lambda cwd: False)
    assert report.errors == []
    idx = Index(cfg.kb_dir / "index.sqlite")
    idx.update(cfg.root)
    return idx


def propose(cfg, **entries):
    idx = sync(cfg)
    try:
        return memedits.check(cfg.root, None, entries, idx, "2026-10-08")
    finally:
        idx.close()


def record(cfg, **entries):
    problems, out = propose(cfg, **entries)
    assert problems == []
    memedits.save(cfg.root, out)
    return out


REWRITE = {"ref": REF, "kind": "rewrite", "text": "Keep each pull request to one change.\n\nSplit big ones.",
           "description": "User wants small, split pull requests", "why": "a later memory says split them",
           "sources": ["memory demo/keep"]}


def test_check_records_a_proposal_with_what_finish_knows(cfg):
    out = record(cfg, **{"new-1": REWRITE})
    (eid, e), = out.items()
    assert memedits.is_id(eid) and e["path"] == OLD and e["host"] == "h" and e["agent"] == "claude"
    assert e["digest"] == memedits.digest((cfg.root / OLD).read_bytes()) and e["date"] == "2026-10-08"
    assert e["sources"] == ["memory demo/keep"] and e["text"].startswith("Keep each")
    idx = sync(cfg)
    try:                                        # the same entry again: unchanged, kept
        assert memedits.check(cfg.root, out, dict(out), idx, "2026-10-09") == ([], out)
        problems, _ = memedits.check(cfg.root, out, {}, idx, "2026-10-09")
        assert "never removes a proposal" in problems[0]
        problems, _ = memedits.check(cfg.root, out, {eid: {**e, "why": "x"}}, idx, "2026-10-09")
        assert "never changes a proposal" in problems[0]
        problems, _ = memedits.check(cfg.root, out, {**out, "new-2": REWRITE}, idx, "2026-10-09")
        assert "already has a proposal for this version" in problems[0]     # waiting, or answered: not again
    finally:
        idx.close()
    write_memory(cfg.claude_dir, text=NOTE + "\nA new line.\n")
    idx = sync(cfg)
    try:                                                          # the memory changed: a new proposal is fine
        problems, out2 = memedits.check(cfg.root, out, {**out, "new-2": REWRITE}, idx, "2026-10-09")
        assert problems == [] and len(out2) == 2
    finally:
        idx.close()


@pytest.mark.parametrize("change, problem", [
    ({"kind": "edit"}, "kind must be one of"),
    ({"why": ""}, "give `why`"),
    ({"ref": "demo/nothing"}, "no memory"),
    ({"ref": "demo/MEMORY"}, "no memory"),
    ({"sources": ["a9a9a9a9"]}, "give the sources"),
    ({"sources": []}, "give the sources"),
    ({"text": ""}, "give the new text"),
    ({"text": "token [REDACTED:aws]"}, "give the new text"),
    ({"description": "two\nlines"}, "description must be one line"),
    ({"text": "key AKIAIOSFODNN7EXAMPLQ in it"}, "looks like it holds a secret"),
    ({"kind": "move", "folder": ENC}, "folder must be"),
    ({"kind": "move", "folder": "-Users-me-nowhere"}, "folder must be"),
])
def test_check_refuses(cfg, change, problem):
    problems, out = propose(cfg, **{"new-1": {**REWRITE, **change}})
    assert len(problems) == 1 and problem in problems[0] and out == {}


def test_check_refuses_an_id_too_many_and_a_rewrite_that_loses_a_secret(cfg):
    assert "no such proposal" in propose(cfg, **{"m-000000": REWRITE})[0][0]
    many = {f"new-{i}": {**REWRITE, "kind": "delete"} for i in range(memedits.MAX_NEW + 1)}
    assert any("at most" in p for p in propose(cfg, **many)[0])
    write_memory(cfg.claude_dir, text=NOTE + "\npassword=hunter2hunter2 for the db\n")
    problems, _ = propose(cfg, **{"new-1": REWRITE})
    assert "holds a redacted secret" in problems[0]


def _waiting(cfg):
    return [eid for eid, _ in memedits.waiting(cfg.root, cfg.host)]


def test_apply_rewrite_keeps_the_front_matter(cfg):
    eid, = record(cfg, **{"new-1": REWRITE})
    assert _waiting(cfg) == [eid] and memedits.waiting(cfg.root, "other-host") == []
    entry = memedits.load(cfg.root)[eid]
    assert "-description: User prefers small pull requests" in memedits.detail(cfg.root, entry)
    assert memedits.apply(cfg, eid, entry).startswith("rewrote ")
    text = (cfg.claude_dir / ENC / "memory" / "prefer-small-prs.md").read_text()
    assert text.startswith("---\nname: prefer-small-prs\ndescription: \"User wants small, split pull requests\"\n")
    assert "originSessionId: 0f1e2d3c" in text and text.endswith("---\n\nKeep each pull request to one change.\n\n"
                                                                 "Split big ones.\n")
    sync(cfg).close()
    assert _waiting(cfg) == []                                     # the memory changed: no longer waiting


def test_apply_delete_and_move_update_the_memory_indexes(cfg):
    out = record(cfg, **{"new-1": {**REWRITE, "kind": "delete", "ref": "demo/keep"},
                         "new-2": {**REWRITE, "kind": "move", "folder": OTHER}})
    by_kind = {e["kind"]: eid for eid, e in out.items()}
    assert memedits.apply(cfg, by_kind["delete"], out[by_kind["delete"]]).startswith("deleted ")
    here = cfg.claude_dir / ENC / "memory"
    assert not (here / "keep.md").exists()
    assert (here / "MEMORY.md").read_text() == "- [prefer-small-prs](prefer-small-prs.md) — small pull requests\n"
    assert memedits.apply(cfg, by_kind["move"], out[by_kind["move"]]).startswith("moved ")
    there = cfg.claude_dir / OTHER / "memory"
    assert not (here / "prefer-small-prs.md").exists() and (there / "prefer-small-prs.md").read_text() == NOTE
    assert (here / "MEMORY.md").read_text() == ""
    assert (there / "MEMORY.md").read_text() == "- [prefer-small-prs](prefer-small-prs.md) — small pull requests\n"


def test_apply_refuses_when_anything_changed(cfg):
    out = record(cfg, **{"new-1": REWRITE})
    (eid, entry), = out.items()
    with pytest.raises(memedits.ApplyError, match="is for host h"):
        memedits.apply(make_config(cfg.root, "x", cfg.claude_dir, cfg.codex_dirs[0], cfg.codex_home), eid, entry)
    path = cfg.claude_dir / ENC / "memory" / "prefer-small-prs.md"
    path.write_text(NOTE + "\nedited here\n")
    with pytest.raises(memedits.ApplyError, match="changed since the last sync"):
        memedits.apply(cfg, eid, entry)
    assert path.read_text() == NOTE + "\nedited here\n"
    sync(cfg).close()
    with pytest.raises(memedits.ApplyError, match="changed since the proposal"):
        memedits.apply(cfg, eid, entry)
    move = {**entry, "kind": "move", "folder": "-Users-me-gone", "digest": memedits.digest((cfg.root / OLD).read_bytes())}
    with pytest.raises(memedits.ApplyError, match="no project folder"):
        memedits.apply(cfg, eid, move)


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_decide_cli_lists_applies_and_answers_memory_fixes(cfg, capsys, monkeypatch):
    out = record(cfg, **{"new-1": REWRITE, "new-2": {**REWRITE, "kind": "delete", "ref": "demo/keep"}})
    by_kind = {e["kind"]: eid for eid, e in out.items()}
    cfg_file = cfg.root.parent / "config.json"
    cfg_file.write_text(json.dumps({"root": str(cfg.root), "host": "h", "claude_dir": str(cfg.claude_dir),
                                    "codex_dirs": [], "codex_home": str(cfg.codex_home)}))
    monkeypatch.setenv("KB_CONFIG", str(cfg_file))
    code, text = run(capsys, "decide")
    assert code == 0 and f"{by_kind['rewrite']}  memory    rewrite memory demo/prefer-small-prs" in text
    assert "    +Split big ones." in text and "2 wait for you" in text
    rows = json.loads(run(capsys, "decide", "--json")[1])["items"]
    assert {r["kind"] for r in rows} == {"memory"} and {r["change"] for r in rows} == {"rewrite", "delete"}

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(capsys, "decide", "accept", by_kind["delete"])[0] == 2            # no terminal: refused
    code, text = run(capsys, "decide", "accept", by_kind["delete"], "--yes")     # the mod
    assert code == 0 and text.startswith("deleted ") and "accepted" in text
    assert not (cfg.claude_dir / ENC / "memory" / "keep.md").exists()
    monkeypatch.setattr("sys.stdin", Tty(""))
    code, text = run(capsys, "decide", "reject", by_kind["rewrite"], "--note", "still true")
    assert code == 0 and "rejected" in text
    assert (cfg.claude_dir / ENC / "memory" / "prefer-small-prs.md").read_text() == NOTE
    assert set(ledger.answers(cfg.root)) == set(out) and ledger.decisions(cfg.root) == {}
    code, text = run(capsys, "decide", "accept", by_kind["rewrite"])
    assert code == 2 and "does not wait on this machine" in text
    assert run(capsys, "decide")[1] == "nothing waits for you\n"


def test_the_brief_counts_this_hosts_memory_fixes(cfg):
    record(cfg, **{"new-1": REWRITE})
    line = decide.brief_line(cfg.root, cfg.kb_dir, "h")
    assert line.startswith("1 memory fix waits for the user")
    assert decide.brief_line(cfg.root, cfg.kb_dir, "other") == ""
    ledger.save(cfg.root, {"s-000001": {"category": "", "text": "t", "signature": "", "repo": "", "sources": [],
                                        "weeks": ["2026-W40"]}})
    assert decide.brief_line(cfg.root, cfg.kb_dir, "h").startswith("1 retro suggestion and 1 memory fix wait")
