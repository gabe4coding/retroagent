import datetime as dt
import json
import subprocess

import pytest
from fixtures import GH_TOKEN
from test_index import put
from test_pages import write_page

from kb import routine
from kb.distill import dump_front_matter
from kb.index import Index
from kb.pages import parse_page
from kb.routine import PagesError

pytestmark = pytest.mark.slow          # starts git and other processes; runs with scripts/test --all

NOW = dt.datetime(2026, 10, 7, 9, 0, tzinfo=dt.timezone.utc)      # a Wednesday in 2026-W41
ROME = routine.zone("Europe/Rome")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text(".kb/\n")
    return path


def session(root, sid, project, started, summary="did things", parent=""):
    rel = f"h/claude/{started[:4]}/{started[5:7]}/{started[:10]}_{project}_{sid}.md"
    return put(root, rel, sid, project=project, started=started, ended=started, summary=summary, parent=parent)


def commit(root, msg="sync(h): sessions"):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD")


def settings(**over):
    return {**routine.DEFAULTS, **over}


def plan(root, now=NOW, **over):
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        idx.update(root)
        return routine.make_plan(root, idx, settings(**over), now=now)
    finally:
        idx.close()


def demo(root):
    """alpha: 3 sessions (W39, W40, W41) and a subagent; beta: 2 sessions; scratch: 3 sessions (skipped)."""
    session(root, "a0000001", "alpha", "2026-09-22T10:00:00Z")
    session(root, "a0000002", "alpha", "2026-09-30T10:00:00Z")
    session(root, "a0000003", "alpha", "2026-10-06T10:00:00Z")
    session(root, "a0000009", "alpha", "2026-10-06T10:05:00Z", parent="a0000003")
    session(root, "b0000001", "beta", "2026-09-30T11:00:00Z")
    session(root, "b0000002", "beta", "2026-10-01T11:00:00Z")
    for i in range(1, 4):
        session(root, f"c000000{i}", "scratch", f"2026-09-2{i}T09:00:00Z")
    return commit(root)


def test_bootstrap_plan(tmp_path):
    root = repo(tmp_path / "kb")
    head = demo(root)
    p = plan(root)
    assert p["mode"] == "bootstrap" and p["head"] == head and p["branch"] == "main"
    assert p["projects"] == [{"name": "alpha", "page": "pages/projects/alpha.md", "action": "create",
                              "sessions": ["a0000001", "a0000002", "a0000003"]}]
    assert [(r["week"], r["action"]) for r in p["retros"]] == [("2026-W40", "create"), ("2026-W39", "create")]
    w40 = p["retros"][0]
    assert (w40["from"], w40["to"]) == ("2026-09-28", "2026-10-04")
    assert (w40["since"], w40["until"]) == ("2026-09-27T22:00:00Z", "2026-10-04T22:00:00Z")
    assert w40["sessions"] == ["a0000002", "b0000001", "b0000002"]
    assert p["pending"] == {"projects": {}, "weeks": []} and p["waiting"] == []
    assert json.loads((root / ".kb" / routine.PLAN_FILE).read_text())["head"] == head


def test_batches_leave_the_rest_pending_newest_first(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    for i in range(1, 4):
        session(root, f"d000000{i}", "gamma", f"2026-10-0{i + 4}T09:00:00Z")
    commit(root)
    p = plan(root, batch_projects=1, batch_retros=1)
    assert [i["name"] for i in p["projects"]] == ["gamma"]
    assert p["pending"]["projects"] == {"alpha": ["a0000001", "a0000002", "a0000003"]}
    assert [r["week"] for r in p["retros"]] == ["2026-W40"] and p["pending"]["weeks"] == ["2026-W39"]


def test_sessions_without_a_summary_wait_for_one(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    for sid, started in (("a0000004", "2026-10-07T07:00:00Z"), ("a0000005", "2026-10-05T07:00:00Z")):
        put(root, f"h/claude/2026/10/{started[:10]}_alpha_{sid}.md", sid, turns=("ask", "answer", "ask again"),
            project="alpha", started=started, ended=started, summary="")   # 2 hours old: waits; 2 days old: used
    put(root, "h/claude/2026/10/2026-10-07_alpha_a0000006.md", "a0000006", turns=("one prompt",), project="alpha",
        started="2026-10-07T07:30:00Z", ended="2026-10-07T07:30:00Z", summary="")   # 1 prompt: never summarized
    commit(root)
    p = plan(root)
    assert p["waiting"] == ["a0000004"]
    assert "a0000005" in p["projects"][0]["sessions"] and "a0000006" in p["projects"][0]["sessions"]


def write_alpha(root, sources=("a0000001", "a0000002", "a0000003")):
    write_page(root, "project", "alpha", sources=list(sources), sessions=99, updated="whenever")


def write_retro(root, week, sources):
    write_page(root, "retro", week, body=f"# Week {week}\n\n## What happened\n- work ({sources[0]})\n",
               sources=list(sources), sessions=len(sources))


def test_finish_then_incremental(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    p = plan(root)
    write_alpha(root)
    write_retro(root, "2026-W40", p["retros"][0]["sessions"])          # W39 is not written: it goes back to pending
    res = routine.finish(root, settings(), now=NOW, push=False)
    assert res["committed"] and res["projects"] == ["pages/projects/alpha.md"]
    assert res["retros"] == ["pages/retro/2026-W40.md"] and res["returned"] == ["2026-W39"]
    assert git(root, "log", "-1", "--format=%s") == "pages: 1 project page, 1 retro [skip ci]"
    assert git(root, "show", "--name-only", "--format=", "HEAD").split() == [
        "pages/.state.json", "pages/projects/alpha.md", "pages/retro/2026-W40.md"]
    state = json.loads((root / routine.STATE_REL).read_text())
    assert state["sha"] == p["head"] and state["mode"] == "bootstrap" and state["last_run"] == "2026-10-07T09:00:00Z"
    meta, body = parse_page((root / "pages" / "projects" / "alpha.md").read_text())
    assert (meta["updated"], meta["sessions"]) == ("2026-10-07T09:00:00Z", 3)      # set by finish, not the writer
    assert body.endswith("(a0000001 · 2026-09-22)\n")                     # dated by finish from the index
    assert "- the motion test is stable (a0000001 · 2026-09-22)" in body and res["undated"] == []
    assert (root / "pages" / "retro" / "2026-W40.md").read_text().endswith("- work (a0000002)\n")   # retros: no dates
    assert state["pending"] == {"projects": {}, "weeks": ["2026-W39"]}

    session(root, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    session(root, "b0000003", "beta", "2026-10-07T08:30:00Z")              # beta reaches 3 sessions
    head = commit(root)
    p = plan(root)
    assert p["mode"] == "incremental" and p["base"] == state["sha"] and p["head"] == head
    assert [(i["name"], i["action"], i["sessions"]) for i in p["projects"]] == [
        ("beta", "create", ["b0000001", "b0000002", "b0000003"]), ("alpha", "update", ["a0000004"])]
    assert [(r["week"], r["action"]) for r in p["retros"]] == [("2026-W39", "create")]


def test_finish_dates_only_the_pages_it_writes_and_lists_the_undated(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    plan(root)
    write_page(root, "project", "beta", sources=["b0000001"])
    commit(root, "an old page, written before dates")
    plan(root)
    memory(root, "alpha", "bare-fact.md")                                   # no modified time, no session
    commit(root, "sync(h): a memory")
    plan(root)
    write_alpha(root)
    path = root / "pages" / "projects" / "alpha.md"
    path.write_text(path.read_text() + "\n## Open threads\n- ask about it (ffffffff)\n- no source at all\n"
                    "- a memory fact (memory alpha/bare-fact)\n")
    res = routine.finish(root, settings(), now=NOW, push=False)
    assert res["undated"] == ["pages/projects/alpha.md: ask about it (ffffffff)",
                              "pages/projects/alpha.md: no source at all",
                              "pages/projects/alpha.md: a memory fact (memory alpha/bare-fact)"]   # no date known
    assert "· 20" not in (root / "pages" / "projects" / "beta.md").read_text()      # not written in this run


def test_finish_moves_stale_bullets_of_a_written_page_to_history(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    session(root, "a0000004", "alpha", "2026-05-01T10:00:00Z")
    commit(root)
    plan(root)
    write_page(root, "project", "alpha", sources=["a0000003", "a0000004"], body=(
        "# alpha\n\n## Current state\n- new (a0000003)\n\n## Key decisions\n- 2026-05-01 · old but kept (a0000004)\n"
        "\n## Errors seen → fixes\n- `boom` → old fix (a0000004)\n"))
    res = routine.finish(root, settings(), now=NOW, push=False)
    assert res["moved"] == ["pages/projects/alpha.md: unconfirmed since 2026-05-01 (Errors seen): `boom` → old fix "
                            "(a0000004 · 2026-05-01)"]
    body = parse_page((root / "pages" / "projects" / "alpha.md").read_text())[1]
    assert "- 2026-05-01 · old but kept (a0000004 · 2026-05-01)" in body
    assert body.endswith("## History\n- unconfirmed since 2026-05-01 (Errors seen): `boom` → old fix "
                         "(a0000004 · 2026-05-01)\n")


def test_finish_without_news_makes_no_commit(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    plan(root, retro_weeks_back=0)
    write_alpha(root)
    routine.finish(root, settings(), now=NOW, push=False)
    head = git(root, "rev-parse", "HEAD")
    p = plan(root, retro_weeks_back=0)
    assert p["projects"] == [] and p["retros"] == []
    res = routine.finish(root, settings(), now=NOW, push=False)
    assert res["committed"] is False and git(root, "rev-parse", "HEAD") == head


def test_skip_drops_an_unwritten_page(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    plan(root, retro_weeks_back=0)
    res = routine.finish(root, settings(), now=NOW, push=False, skip=["alpha"])
    assert res["returned"] == [] and json.loads((root / routine.STATE_REL).read_text())["pending"]["projects"] == {}


@pytest.mark.parametrize("change, problem", [
    (lambda root: (root / "README.md").write_text("x"), "README.md: outside pages/"),
    (lambda root: (root / routine.CONFIG_REL).write_text("{}"), "pages/config.json: only the owner writes it"),
    (lambda root: (root / "pages" / "notes.txt").write_text("x"), "pages/notes.txt: only .md pages may be written"),
    (lambda root: write_page(root, "project", "alpha", body=f"# alpha\n\n- token {GH_TOKEN}\n"),
     "looks like it holds a secret"),
    (lambda root: write_page(root, "project", "beta", rel="pages/projects/alpha.md"),
     "a project page named beta belongs in pages/projects/beta.md"),
    (lambda root: write_page(root, "project", "alpha", body="# alpha\n" + "x" * 50000), "the limit is 40000"),
])
def test_finish_refuses(tmp_path, change, problem):
    root = repo(tmp_path / "kb")
    demo(root)
    (root / "pages").mkdir()
    (root / routine.CONFIG_REL).write_text(json.dumps({"min_sessions": 3}))
    head = commit(root)
    plan(root)
    change(root)
    with pytest.raises(PagesError, match="refusing to commit") as e:
        routine.finish(root, settings(), now=NOW, push=False)
    assert problem in str(e.value) and git(root, "rev-parse", "HEAD") == head
    assert not (root / routine.STATE_REL).exists()


def test_finish_refuses_a_deleted_page_and_a_moved_head(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    write_alpha(root)
    commit(root)
    plan(root)
    (root / "pages" / "projects" / "alpha.md").unlink()
    with pytest.raises(PagesError, match="never deletes pages"):
        routine.finish(root, settings(), now=NOW, push=False)
    git(root, "checkout", "-q", "--", "pages")
    session(root, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    commit(root)
    with pytest.raises(PagesError, match="HEAD moved"):
        routine.finish(root, settings(), now=NOW, push=False)
    (root / ".kb" / routine.PLAN_FILE).unlink()
    with pytest.raises(PagesError, match="no plan"):
        routine.finish(root, settings(), now=NOW, push=False)


def test_late_sessions_rewrite_a_recent_retro_only(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    plan(root)
    write_alpha(root)
    write_retro(root, "2026-W40", ["a0000002", "b0000001", "b0000002"])
    write_retro(root, "2026-W39", ["a0000001", "c0000001", "c0000002", "c0000003"])
    routine.finish(root, settings(), now=NOW, push=False)
    put(root, "h/claude/2026/09/2026-09-30_alpha_a0000002.md", "a0000002", project="alpha",
        started="2026-09-30T10:00:00Z", ended="2026-09-30T10:00:00Z", summary="now with a better summary")
    commit(root)
    assert plan(root)["retros"] == []                                        # a known session changed: no rewrite
    session(root, "b0000003", "beta", "2026-10-02T11:00:00Z")                 # a laptop pushed W40 work late
    commit(root)
    p = plan(root)
    assert [(r["week"], r["action"]) for r in p["retros"]] == [("2026-W40", "update")]
    assert "b0000003" in p["retros"][0]["sessions"]
    later = NOW + dt.timedelta(days=20)
    assert "2026-W40" not in [r["week"] for r in plan(root, now=later)["retros"]]   # too late to rewrite it


def test_a_lost_watermark_falls_back_to_time(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    (root / "pages").mkdir()
    (root / routine.STATE_REL).write_text(json.dumps({"sha": "0" * 40, "last_run": "2026-10-07T08:00:00Z"}))
    commit(root)
    p = plan(root, retro_weeks_back=0)
    assert p["mode"] == "since" and p["base"] == "2026-10-05T08:00:00Z"
    assert [i["name"] for i in p["projects"]] == ["alpha"]
    (root / routine.STATE_REL).write_text("{not json")
    commit(root)
    assert plan(root, retro_weeks_back=0)["mode"] == "rebuild"


def test_weeks_follow_rome_time_across_the_dst_change():
    start, end = routine.week_bounds("2026-W43", ROME)                  # summer time ends on Sunday 25 October
    assert routine._iso(start) == "2026-10-18T22:00:00Z" and routine._iso(end) == "2026-10-25T23:00:00Z"
    assert routine.week_of("2026-10-25T22:30:00Z", ROME) == "2026-W43"
    assert routine.week_of("2026-10-25T23:30:00Z", ROME) == "2026-W44"
    assert routine.week_of("2026-10-04T22:30:00Z", ROME) == "2026-W41"  # Monday 00:30 in Rome
    assert routine.week_of("garbage", ROME) == ""
    monday = dt.datetime(2026, 10, 5, 21, 0, tzinfo=dt.timezone.utc)     # 23:00 Monday in Rome
    assert routine.closed_weeks(monday, ROME, settings())[0] == "2026-W39"   # W40 waits 24 h for summaries
    assert routine.closed_weeks(monday + dt.timedelta(hours=2), ROME, settings())[0] == "2026-W40"


def test_settings(tmp_path):
    root = tmp_path / "kb"
    assert routine.load_settings(root) == routine.DEFAULTS
    (root / "pages").mkdir(parents=True)
    (root / routine.CONFIG_REL).write_text(json.dumps({"_note": "x", "min_sessions": 5, "skip_projects": ["a"]}))
    s = routine.load_settings(root)
    assert s["min_sessions"] == 5 and s["skip_projects"] == ["a"] and s["batch_projects"] == 5
    (root / routine.CONFIG_REL).write_text(json.dumps({"stale_days": 120}))
    assert (routine.load_settings(root)["stale_days"], routine.load_settings(root)["stale_days_current"]) == (120, 30)
    for bad in ({"min_sessions": "3"}, {"min_sessions": True}, {"skip_projects": [1]}, {"branch": ""}, [],
                {"stale_days": 0}, {"stale_days_current": -1}):
        (root / routine.CONFIG_REL).write_text(json.dumps(bad))
        with pytest.raises(PagesError):
            routine.load_settings(root)


def test_digest(tmp_path):
    root = repo(tmp_path / "kb")
    demo(root)
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        idx.update(root)
        out = routine.digest(idx, project="alpha")
        assert out.startswith("3 sessions\n\n### a0000001 · 2026-09-22 · claude · alpha · outcome: -")
        assert "summary: did things" in out and "subagents: 1" in out and "a0000009" not in out
        assert routine.digest(idx, shorts=["b0000002"]).startswith("1 sessions\n\n### b0000002")
        week = routine.digest(idx, since="2026-09-27T22:00:00Z", until="2026-10-04T22:00:00Z")
        assert week.startswith("3 sessions")
        cut = routine.digest(idx, project="alpha", limit_chars=100)
        assert "sessions left: kb pages digest --only a0000002,a0000003]" in cut
    finally:
        idx.close()


# ---- branches and pushes, against a local bare remote

def remote_pair(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    root = repo(tmp_path / "kb")
    demo(root)
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "origin", "main")
    return bare, root


def clone(bare, path):
    subprocess.run(["git", "clone", "-q", str(bare), str(path)], check=True)
    return path


def prepare(root, s, now=NOW):
    """start + plan, then write every planned page the way Claude would."""
    routine.start(root, root / ".kb" / "index.sqlite", s)
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        p = routine.make_plan(root, idx, s, now=now)
    finally:
        idx.close()
    for item in p["projects"]:
        write_page(root, "project", item["name"], sources=item["sessions"])
    for item in p["retros"]:
        write_retro(root, item["week"], item["sessions"])
    return p


def run_once(root, now=NOW, push=True):
    p = prepare(root, settings(), now)
    return p, routine.finish(root, settings(), now=now, push=push)


def test_first_build_goes_to_the_bootstrap_branch(tmp_path):
    bare, root = remote_pair(tmp_path)
    p, res = run_once(root)
    assert p["branch"] == "claude/pages-bootstrap" and res["push"] == "pushed"
    assert git(bare, "ls-tree", "--name-only", "claude/pages-bootstrap", "pages/") == "pages/.state.json\npages/projects\npages/retro"
    assert "pages" not in git(bare, "ls-tree", "--name-only", "main")

    # a second bootstrap run continues the branch, with main merged in
    other = clone(bare, tmp_path / "second")
    session(other, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    commit(other)
    git(other, "push", "-q", "origin", "main")
    p, res = run_once(clone(bare, tmp_path / "third"))
    assert p["mode"] == "incremental" and p["branch"] == "claude/pages-bootstrap" and res["push"] == "pushed"
    assert [(i["name"], i["sessions"]) for i in p["projects"]] == [("alpha", ["a0000004"])]

    # main never gets the first build directly
    fourth = clone(bare, tmp_path / "fourth")
    idx = Index(fourth / ".kb" / "index.sqlite")
    idx.update(fourth)
    routine.make_plan(fourth, idx, settings(), now=NOW)
    idx.close()
    with pytest.raises(PagesError, match="first build goes to claude/pages-bootstrap"):
        routine.finish(fourth, settings(), now=NOW)

    # after the merge, runs work on main
    git(fourth, "merge", "-q", "--no-edit", "origin/claude/pages-bootstrap")
    git(fourth, "push", "-q", "origin", "main")
    fifth = clone(bare, tmp_path / "fifth")
    started = routine.start(fifth, fifth / ".kb" / "index.sqlite", settings())
    assert started["branch"] == "main" and started["bootstrap"] is False and started["pages"] == 3


def test_push_rebases_over_sessions_and_loses_a_race_cleanly(tmp_path):
    bare, root = remote_pair(tmp_path)
    run_once(root)
    git(root, "push", "-q", "origin", "claude/pages-bootstrap:main")         # as if the bootstrap PR was merged
    writer = clone(bare, tmp_path / "writer")                                 # a machine that syncs sessions
    session(writer, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    commit(writer)
    git(writer, "push", "-q", "origin", "main")

    s = settings()
    a, b = clone(bare, tmp_path / "a"), clone(bare, tmp_path / "b")
    assert prepare(a, s)["branch"] == "main"
    prepare(b, s)                                                             # two runs from the same watermark
    write_page(b, "project", "alpha", sources=["a0000004", "zzzzzzzz"])       # ... that wrote the page differently
    session(writer, "b0000003", "beta", "2026-10-07T08:30:00Z")               # more sessions arrive meanwhile
    commit(writer)
    git(writer, "push", "-q", "origin", "main")

    assert routine.finish(a, s, now=NOW)["push"] == "pushed"                  # rebased over the sessions commit
    assert git(bare, "log", "-2", "--format=%s", "main").splitlines() == [
        "pages: 1 project page, 0 retros [skip ci]", "sync(h): sessions"]
    assert routine.finish(b, s, now=NOW)["push"] == "lost"                    # conflicts with A: dropped cleanly
    assert git(b, "status", "--porcelain") == ""
    assert git(bare, "rev-parse", "main") == git(a, "rev-parse", "HEAD")


def test_start_unshallows_and_finds_the_bootstrap_branch(tmp_path):
    bare, root = remote_pair(tmp_path)
    run_once(root)
    writer = clone(bare, tmp_path / "writer")
    session(writer, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    commit(writer)
    git(writer, "push", "-q", "origin", "main")
    shallow = tmp_path / "shallow"                                  # how a cloud checkout may look
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--single-branch", "--branch", "main", bare.as_uri(),
                    str(shallow)], check=True)
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    started = routine.start(shallow, shallow / ".kb" / "index.sqlite", settings())
    assert started["branch"] == "claude/pages-bootstrap" and (shallow / routine.STATE_REL).is_file()
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "false"


def test_due(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path / "kb")
    demo(root)
    d = routine.due(plan(root), settings())
    assert d == {"due": True, "reason": "1 project page and 2 retros to write", "mode": "bootstrap",
                 "pending_after": 0, "min_hours_between_fires": 3}
    plan(root)
    write_alpha(root)
    for week, shorts in (("2026-W40", ["a0000002"]), ("2026-W39", ["a0000001"])):
        write_retro(root, week, shorts)
    routine.finish(root, settings(), now=NOW, push=False)
    session(root, "a0000004", "alpha", "2026-10-07T08:00:00Z", summary="")    # 1 prompt: used at once
    put(root, "h/claude/2026/10/2026-10-07_beta_b0000003.md", "b0000003", turns=("a", "b", "c"), project="beta",
        started="2026-10-07T08:30:00Z", ended="2026-10-07T08:30:00Z", summary="")   # waits for its summary
    commit(root)
    assert routine.due(plan(root), settings())["reason"] == "1 project page and 0 retros to write"
    routine.finish(root, settings(), now=NOW, push=False)
    d = routine.due(plan(root), settings())
    assert d["due"] is False and d["reason"] == "nothing to write (1 sessions waiting for a summary)"

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"root": str(root), "host": "h"}))
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    from kb.cli import main
    assert main(["pages", "due"]) == 1 and json.loads(capsys.readouterr().out)["due"] is False


# ---- memories

def memory(root, project, file, body="A fact worth keeping.\n", folder=None, host="h", **meta):
    """A memory file as kb sync writes it, under memories/<host>/claude/<folder>/<file>."""
    folder = folder or f"-Users-me-{project}"
    m = {"kind": "memory", "agent": "claude", "host": host, "project": project, "cwd": f"/Users/me/{project}",
         "folder": folder, "file": file, "name": file[:-3], "description": f"about {file[:-3]}", "type": "project",
         "origin_session": "", "modified": "", **meta}
    path = root / "memories" / host / "claude" / folder / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_front_matter(m) + "\n" + body, encoding="utf-8")
    return path


def built(root):
    """demo, with the alpha page and both retros written and finished: the next plan is incremental."""
    demo(root)
    plan(root)
    write_alpha(root)
    for week, shorts in (("2026-W40", ["a0000002"]), ("2026-W39", ["a0000001"])):
        write_retro(root, week, shorts)
    routine.finish(root, settings(), now=NOW, push=False)


def test_a_new_or_changed_memory_updates_its_project_page(tmp_path):
    root = repo(tmp_path / "kb")
    built(root)
    note = memory(root, "alpha", "deploy-gotcha.md")
    memory(root, "alpha", "MEMORY.md", "- [Deploy gotcha](deploy-gotcha.md)\n")       # an index: never a change
    memory(root, "beta", "beta-fact.md")                                                 # beta has 2 sessions: no page
    commit(root)
    p = plan(root, retro_weeks_back=0)
    rel = "memories/h/claude/-Users-me-alpha/deploy-gotcha.md"
    assert p["projects"] == [{"name": "alpha", "page": "pages/projects/alpha.md", "action": "update",
                              "sessions": [], "memories": [rel]}]
    routine.finish(root, settings(), now=NOW, push=False)
    note.write_text(note.read_text() + "More.\n")
    session(root, "a0000004", "alpha", "2026-10-07T08:00:00Z")
    commit(root)
    item = plan(root, retro_weeks_back=0)["projects"][0]
    assert item["sessions"] == ["a0000004"] and item["memories"] == [rel] and "memories_removed" not in item


def test_a_removed_memory_is_named_by_its_ref(tmp_path):
    root = repo(tmp_path / "kb")
    built(root)
    note = memory(root, "alpha", "deploy-gotcha.md")
    commit(root)
    plan(root, retro_weeks_back=0)
    routine.finish(root, settings(), now=NOW, push=False)
    note.unlink()
    commit(root)
    item = plan(root, retro_weeks_back=0)["projects"][0]
    assert item["name"] == "alpha" and item["memories_removed"] == ["alpha/deploy-gotcha"]
    assert item["sessions"] == [] and "memories" not in item


def test_memory_changes_wait_in_pending_with_the_sessions(tmp_path):
    root = repo(tmp_path / "kb")
    built(root)
    for i in range(1, 4):
        session(root, f"d000000{i}", "gamma", f"2026-10-0{i + 4}T09:00:00Z")
    memory(root, "alpha", "deploy-gotcha.md")
    commit(root)
    p = plan(root, retro_weeks_back=0, batch_projects=1)
    rel = "memories/h/claude/-Users-me-alpha/deploy-gotcha.md"
    assert [i["name"] for i in p["projects"]] == ["gamma"] and p["pending"]["projects"] == {"alpha": [rel]}
    write_page(root, "project", "gamma", sources=p["projects"][0]["sessions"])
    routine.finish(root, settings(), now=NOW, push=False)
    item = plan(root, retro_weeks_back=0)["projects"][0]
    assert item["name"] == "alpha" and item["memories"] == [rel]


def test_a_lost_watermark_finds_memory_changes_by_time(tmp_path):
    root = repo(tmp_path / "kb")
    built(root)
    state = json.loads((root / routine.STATE_REL).read_text())
    (root / routine.STATE_REL).write_text(json.dumps({**state, "sha": "0" * 40}))
    memory(root, "alpha", "deploy-gotcha.md")
    commit(root)
    p = plan(root, now=NOW + dt.timedelta(hours=1), retro_weeks_back=0)
    assert p["mode"] == "since"
    item = next(i for i in p["projects"] if i["name"] == "alpha")
    assert item["memories"] == ["memories/h/claude/-Users-me-alpha/deploy-gotcha.md"]


def test_digest_puts_the_memories_first(tmp_path, monkeypatch):
    root = repo(tmp_path / "kb")
    demo(root)
    memory(root, "alpha", "old.md", "## Heading inside\nold fact\n", modified="2026-09-01T10:00:00Z")
    memory(root, "alpha", "new.md", "new fact\n", modified="2026-10-02T10:00:00Z", type="feedback",
           origin_session="a0000002")
    memory(root, "alpha", "MEMORY.md", "- [New](new.md)\n")
    memory(root, "beta", "from-w40.md", "beta fact\n", origin_session="b0000001")   # no date: its session's
    idx = Index(root / ".kb" / "index.sqlite")
    try:
        idx.update(root)
        out = routine.digest(idx, project="alpha")
        assert out.startswith("3 sessions, 2 memories\n\n### memory alpha/new · feedback · h · modified 2026-10-02 · "
                              "from session a0000002\ndescription: about new\n> new fact\n\n### memory alpha/old")
        assert "> ## Heading inside" in out and "MEMORY" not in out
        assert out.index("### memory alpha/old") < out.index("### a0000001")
        only = routine.digest(idx, shorts=["a0000003"], memories=["memories/h/claude/-Users-me-alpha/old.md"])
        assert only.startswith("1 sessions, 1 memories\n\n### memory alpha/old")
        assert routine.digest(idx, shorts=["a0000003"]).startswith("1 sessions\n\n### a0000003")
        alone = routine.digest(idx, memories=["memories/h/claude/-Users-me-alpha/new.md"])
        assert alone.startswith("0 sessions, 1 memories\n\n### memory alpha/new")
        week = routine.digest(idx, since="2026-09-27T22:00:00Z", until="2026-10-04T22:00:00Z")
        assert week.startswith("3 sessions, 2 memories") and "alpha/new" in week and "beta/from-w40" in week
        monkeypatch.setattr(routine, "MEMORY_CHARS", 5)
        assert "> new f\n[… cut; the rest: kb memory memories/h/claude/-Users-me-alpha/new.md]" in \
            routine.digest(idx, project="alpha")
        monkeypatch.setattr(routine, "MEMORY_DIGEST_CHARS", 200)
        listed = routine.digest(idx, project="alpha")
        assert "1 more memories (read one with kb memory <path>):\n- alpha/old · project · about old " \
               "[memories/h/claude/-Users-me-alpha/old.md]" in listed
    finally:
        idx.close()


def test_cli_digest_takes_memory_paths(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path / "kb")
    demo(root)
    memory(root, "alpha", "note.md")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"root": str(root), "host": "h"}))
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    from kb.cli import main
    assert main(["pages", "digest", "--memories", "memories/h/claude/-Users-me-alpha/note.md"]) == 0
    assert capsys.readouterr().out.startswith("0 sessions, 1 memories\n\n### memory alpha/note")
    assert main(["pages", "digest"]) == 2 and "--memories" in capsys.readouterr().out
