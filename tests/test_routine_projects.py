import json

from kb import routine


def row(sid, project, cwd, files=(), prs=()):
    return {"id": sid, "short": sid[-8:], "project": project, "cwd": cwd, "files": json.dumps(list(files)),
            "prs": json.dumps(list(prs))}


ALPHA = row("s-0000001", "alpha", "/Users/me/Repositories/alpha", prs=["https://github.com/me/alp/pull/3"])
DOCS = row("s-0000002", "docs", "/Users/me/Repositories/docs")


def found(*rows):
    return routine._projects_of([ALPHA, DOCS, *rows], {"alpha", "docs"})


def test_a_session_works_in_its_own_project_and_in_the_repos_it_changed():
    home = row("s-0000010", "me", "/Users/me", files=["Repositories/alpha/src/a.py", ".config/x.json"])
    other = row("s-0000011", "beta", "/Users/me/Repositories/beta", files=["/Users/me/Repositories/docs/guide.md"])
    got = found(home, other)
    assert got["s-0000001"] == {"alpha"} and got["s-0000010"] == {"alpha"} and got["s-0000011"] == {"docs"}


def test_a_folder_named_like_a_project_is_not_that_project():
    """docs/ inside another repo is not the docs project: only the learned repo folders count."""
    beta = row("s-0000012", "beta", "/Users/me/Repositories/beta", files=["docs/plan.md", "src/docs/x.py"])
    sub = row("s-0000013", "gamma", "/Users/me/work/gamma", files=["/Users/me/work/gamma/docs/a.md"])
    got = found(beta, sub)
    assert "s-0000012" not in got and "s-0000013" not in got


def test_a_cloud_session_works_in_the_repos_under_home_user():
    cloud = row("s-0000014", "user", "/home/user", files=["alpha/src/kb/hint.py", "notes.md"])
    assert found(cloud)["s-0000014"] == {"alpha"}


def test_a_pr_maps_to_its_project_by_name_or_by_the_sessions_that_opened_it():
    by_vote = row("s-0000015", "me", "/Users/me", prs=["https://github.com/me/alp/pull/9"])     # repo alp → alpha
    by_name = row("s-0000016", "me", "/Users/me", prs=["https://github.com/org/docs/pull/1"])
    unknown = row("s-0000017", "me", "/Users/me", prs=["https://github.com/org/zeta/pull/1"])
    got = found(by_vote, by_name, unknown)
    assert got["s-0000015"] == {"alpha"} and got["s-0000016"] == {"docs"} and "s-0000017" not in got


def test_bad_json_is_no_evidence():
    broken = {**row("s-0000018", "me", "/Users/me"), "files": "not json", "prs": None}
    assert "s-0000018" not in found(broken)
