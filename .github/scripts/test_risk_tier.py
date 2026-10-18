"""Tests of .github/scripts/risk_tier.py: the deterministic tier rules.

    uv run --no-project --with pyyaml --with pytest -- python -m pytest -q .github/scripts

Pure: no git, no network (the git part has one test on a temporary repo, marked `git`).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import risk_tier as rt  # noqa: E402

RULES = rt.load_rules(str(HERE.parent / "risk.yml"))


def f(path, added=(), removed=(), status="M", old_path=None):
    return rt.FileChange(path=path, status=status, added=list(added), removed=list(removed), old_path=old_path)


def tier(*files, title="", branch="", body="", read=None, automerge=""):
    change = rt.Change(files=list(files), title=title, branch=branch, body=body,
                       read=read or (lambda p, s: None), automerge=automerge)
    return rt.classify(change, RULES)


def manifests(**sides):
    """read() for base/head manifests: manifests(**{"base:path": text, "head:path": text})."""
    table = {tuple(k.split(":", 1)): v for k, v in sides.items()}
    return lambda path, side: table.get((side, path))


# ---------------------------------------------------------------- globs


@pytest.mark.parametrize("path,pattern,ok", [
    ("README.md", "*.md", True),
    ("docs/usage.mdx", "*.mdx", True),
    ("docs/a/b.png", "docs/**", True),
    ("skills/x/SKILL.md", "skills/**", True),
    ("src/kb/redact.py", "src/kb/redact.py", True),
    ("src/kb/redact.py.bak", "src/kb/redact.py", False),
    (".github/workflows/publish-npm.yml", "**/publish*.yml", True),
    ("a/b/gitleaks_fetch.py", "**/gitleaks_fetch.py", True),
    ("gitleaks_fetch.py", "**/gitleaks_fetch.py", True),
    ("tests/version.check.mjs", "tests/*.check.mjs", True),
    ("tests/sub/x.check.mjs", "tests/*.check.mjs", False),
])
def test_glob(path, pattern, ok):
    assert rt.matches(path, [pattern]) is ok


# ---------------------------------------------------------------- T0 / T1


def test_docs_only_is_t0():
    r = tier(f("README.md", ["new line"]), f("docs/usage.mdx", ["x"], ["y"]))
    assert r["tier"] == "T0" and r["label"] == "risk:t0"


def test_test_only_that_adds_assertions_is_t0():
    r = tier(f("tests/test_new.py", ["def test_x():", "    assert 1 == 1"], status="A"))
    assert r["tier"] == "T0"


def test_test_change_that_removes_assertions_is_t1():
    r = tier(f("tests/test_old.py", ["    pass"], ["    assert a == b", "    assert c"]))
    assert r["tier"] == "T1" and r["reasons"][0]["rule"] == "assertions-removed"


def test_deleted_test_file_is_t1():
    assert tier(f("tests/test_old.py", [], ["assert x"], status="D"))["tier"] == "T1"


def test_model_facing_markdown_is_not_docs():
    folder = next(p[:-3] for p in RULES["not_docs"] if p.endswith("/**") and "*" not in p[:-3])
    assert tier(f(f"{folder}/x/SKILL.md", ["new rule"]))["tier"] == "T1"
    assert tier(f("AGENTS.md", ["x"]))["tier"] == "T1"


def test_source_change_is_t1():
    assert tier(f("src/kb/sync.py", ["x = 1"], ["x = 2"]))["tier"] == "T1"


def test_empty_change_is_t1():
    assert tier()["tier"] == "T1"


def test_ci_check_script_is_not_a_test():
    if "tests/*.check.mjs" not in (RULES.get("not_tests") or []):
        pytest.skip("this repo has no check scripts under tests/")
    assert tier(f("tests/version.check.mjs", ["x"], ["y"]))["tier"] == "T1"


# ---------------------------------------------------------------- plugin versions and dependencies


PLUGIN = next(iter(RULES.get("plugin_manifests") or []), None)
PYPROJECT = next(iter((RULES.get("dependency_manifests") or {}).get("python") or []), None)


@pytest.mark.parametrize("old,new,expected", [
    ("0.2.3", "0.2.4", "T1"),
    ("0.2.3", "0.3.0", "T1"),
    ("0.11.4", "1.0.0", "T2-hard"),
    ("1.4.0", "2.0.0", "T2-hard"),
    ("0.2.3", "0.2.2", "T2-hard"),
])
def test_plugin_version_bumps(old, new, expected):
    if not PLUGIN:
        pytest.skip("this repo has no plugin manifest")
    read = manifests(**{f"base:{PLUGIN}": json.dumps({"version": old}), f"head:{PLUGIN}": json.dumps({"version": new})})
    assert tier(f(PLUGIN, ['"version": "x"'], ['"version": "y"']), read=read)["tier"] == expected


def test_new_runtime_npm_dependency_is_t2_hard():
    read = manifests(**{"base:package.json": json.dumps({"dependencies": {"a": "^1.0.0"}}),
                        "head:package.json": json.dumps({"dependencies": {"a": "^1.0.0", "b": "^2.0.0"}})})
    r = tier(f("package.json", ['"b": "^2.0.0"']), read=read)
    assert r["tier"] == "T2-hard" and r["reasons"][0]["rule"] == "new-runtime-dependency"


@pytest.mark.parametrize("old,new,expected", [
    ("^9.1.0", "^10.0.0", "T2-hard"),
    ("^0.6.2", "^0.7.0", "T2-hard"),
    ("^1.2.0", "^1.3.0", "T1"),
])
def test_runtime_npm_bumps(old, new, expected):
    read = manifests(**{"base:package.json": json.dumps({"dependencies": {"a": old}}),
                        "head:package.json": json.dumps({"dependencies": {"a": new}})})
    assert tier(f("package.json", ["x"], ["y"]), read=read)["tier"] == expected


def test_moved_manifest_compares_with_its_old_place():
    read = manifests(**{"base:old/package.json": json.dumps({"dependencies": {"a": "^1.0.0"}}),
                        "head:package.json": json.dumps({"dependencies": {"a": "^1.0.0"}})})
    r = tier(f("package.json", ["x"], status="R", old_path="old/package.json"), read=read)
    assert not any(x["rule"] == "new-runtime-dependency" for x in r["reasons"])


def test_dev_dependency_is_t1():
    read = manifests(**{"base:package.json": json.dumps({"devDependencies": {}}),
                        "head:package.json": json.dumps({"devDependencies": {"@types/node": "^24.0.0"}})})
    assert tier(f("package.json", ["x"]), read=read)["tier"] == "T1"


def test_new_python_runtime_dependency_is_t2_hard():
    base = '[project]\nname = "x"\ndependencies = ["pyyaml>=6"]\n'
    head = '[project]\nname = "x"\ndependencies = ["pyyaml>=6", "requests>=2"]\n'
    if not PYPROJECT:
        pytest.skip("this repo has no Python manifest")
    read = manifests(**{f"base:{PYPROJECT}": base, f"head:{PYPROJECT}": head})
    assert tier(f(PYPROJECT, ['"requests>=2"']), read=read)["tier"] == "T2-hard"


def test_publish_on_version(monkeypatch):
    monkeypatch.setitem(RULES, "publish_on_version", ["package.json"])
    read = manifests(**{"base:package.json": json.dumps({"version": "3.0.0"}),
                        "head:package.json": json.dumps({"version": "3.1.0"})})
    r = tier(f("package.json", ["x"], ["y"]), read=read)
    assert r["tier"] == "T2-hard" and any(x["rule"] == "publish-version" for x in r["reasons"])


# ---------------------------------------------------------------- T2 paths and workflows


@pytest.mark.parametrize("path", [".github/risk.yml", "LICENSE", ".gitleaks.toml", ".githooks/pre-commit",
                                  ".github/workflows/review.yml", ".github/workflows/risk.yml",
                                  ".github/scripts/risk_tier.py", "CODEOWNERS"])
def test_hard_paths(path):
    assert tier(f(path, ["x"]))["tier"] == "T2-hard"


def test_soft_paths():
    soft = RULES.get("soft") or []
    if not soft:
        pytest.skip("this repo lists no sensitive files")
    path = soft[0].replace("**/", "").replace("/**", "/x")
    assert tier(f(path, ["x"]))["tier"] == "T2-soft"


def test_soft_content(monkeypatch):
    monkeypatch.setitem(RULES, "soft_content", [{"paths": ["src/**"], "pattern": "secret|redact"}])
    r = tier(f("src/x/page.ts", ["  return redactAll(text)"], ["  return text"]))
    assert r["tier"] == "T2-soft" and r["reasons"][0]["rule"] == "sensitive-code"
    assert tier(f("src/x/page.ts", ["  return a + b"]))["tier"] == "T1"
    assert tier(f("lib/x.ts", ["secret"]))["tier"] == "T1"   # outside the paths
    monkeypatch.setitem(RULES, "soft_content", [{"pattern": "redact"}])
    assert tier(f("tests/test_x.py", ["    assert redact(x)"]))["tier"] == "T0"   # tests never match


def test_additive_workflow_edit_is_t2_soft():
    r = tier(f(".github/workflows/test.yml", ["  lint:", "    runs-on: ubuntu-latest", "      - run: make lint"]))
    assert r["tier"] == "T2-soft"


def test_pinned_version_bump_is_t2_soft():
    r = tier(f(".github/workflows/test.yml", ["      - uses: actions/checkout@v5"], ["      - uses: actions/checkout@v4"]))
    assert r["tier"] == "T2-soft"


def test_removed_step_is_t2_hard():
    r = tier(f(".github/workflows/test.yml", [], ["      - run: node tests/version.check.mjs"]))
    assert r["tier"] == "T2-hard" and r["reasons"][0]["rule"] == "workflow-weakened"


def test_changed_run_command_is_t2_hard():
    r = tier(f(".github/workflows/test.yml", ["      - run: echo ok"], ["      - run: scripts/test --all"]))
    assert r["tier"] == "T2-hard"


@pytest.mark.parametrize("line", ["permissions:", "  contents: write", "    env: { T: ${{ secrets.X }} }",
                                  "  pull_request_target:", "on:"])
def test_workflow_permissions_secrets_triggers_are_t2_hard(line):
    assert tier(f(".github/workflows/test.yml", [line]))["tier"] == "T2-hard"


def test_deleted_workflow_is_t2_hard():
    assert tier(f(".github/workflows/test.yml", [], ["x"], status="D"))["tier"] == "T2-hard"


def test_mass_deletion_and_deleted_source():
    r = tier(f("src/big.py", [], ["x = 1"] * 1200, status="D"))
    rules = {x["rule"] for x in r["reasons"]}
    assert r["tier"] == "T2-hard" and {"mass-deletion", "deleted-source"} <= rules


def test_size_limits():
    many = [f(f"src/m{i}.py", ["x"]) for i in range(41)]
    assert tier(*many)["tier"] == "T2-hard"
    assert tier(f("src/a.py", ["x"] * 1501))["tier"] == "T2-hard"
    assert tier(f("uv.lock", ["x"] * 5000), f("src/a.py", ["y"]))["tier"] == "T1"   # lockfiles do not count


def test_history_rewrite_request():
    assert tier(f("src/a.py", ["x"]), title="Rewrite history to drop a file")["tier"] == "T2-hard"
    assert tier(f("src/a.py", ["x"]), branch="fix/force-push-main")["tier"] == "T2-hard"
    assert tier(f("src/a.py", ["x"]), body="/verify: a force push was denied.")["tier"] == "T1"


def test_kill_switch_raises_everything():
    r = tier(f("README.md", ["x"]), automerge="off")
    assert r["tier"] == "T2-hard" and any(x["rule"] == "kill-switch" for x in r["reasons"])
    assert tier(f("README.md", ["x"]), automerge="on")["tier"] == "T0"


# ---------------------------------------------------------------- HOLD


def test_forbidden_name_is_hold(monkeypatch):
    # A made-up name: the real ones are hashes in risk.yml, so this public repo never spells them.
    monkeypatch.setitem(RULES, "hold", {**RULES["hold"], "forbidden_names_sha256": [hashlib.sha256(b"acmebrand").hexdigest()]})
    assert tier(f("README.md", ["Install the AcmeBrand plugin"]))["tier"] == "HOLD"
    assert tier(f("README.md", ["see acme-brand_tools"]))["tier"] == "HOLD"
    assert tier(f("plugins/acmebrand-x/plugin.json", ["{}"], status="A"))["tier"] == "HOLD"
    assert tier(f("cfg.yaml", ["checkout: ~/Repositories/acmebrand/app"]))["tier"] == "HOLD"
    assert tier(f("src/a.py", [], ["# AcmeBrand"]))["tier"] == "T1"   # removing a mention is fine
    assert tier(f("src/a.py", ["# the acme brand"]))["tier"] != "HOLD"   # words a space separates do not join
    assert tier(f("src/a.py", ["# acmebrandnew"]))["tier"] != "HOLD"


def test_rules_hold_the_names_as_hashes_only():
    hashes = RULES["hold"]["forbidden_names_sha256"]
    assert hashes and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)


def test_comment_pruning_by_title_is_hold():
    assert tier(f("src/a.py", ["x"]), title="Prune redundant comments")["tier"] == "HOLD"
    assert tier(f("src/a.py", ["x"]), branch="claude/remove-obvious-comments")["tier"] == "HOLD"


def test_comment_pruning_by_diff_is_hold():
    removed = [f"    # comment {i}" for i in range(30)] + ["x = 1"]
    assert tier(f("src/a.py", ["x = 2"], removed))["tier"] == "HOLD"


def test_doc_edits_are_not_comment_pruning():
    removed = [f"# Heading {i}" for i in range(30)]
    assert tier(f("docs/a.mdx", [], removed))["tier"] == "T0"


# ---------------------------------------------------------------- the rules file and git


def test_parse_diff():
    diff = "\n".join([
        "diff --git a/a.py b/a.py", "index 1..2 100644", "--- a/a.py", "+++ b/a.py", "@@ -1 +1 @@", "-old", "+new",
        "diff --git a/n.md b/n.md", "new file mode 100644", "--- /dev/null", "+++ b/n.md", "@@ -0,0 +1 @@", "+hi",
        "diff --git a/d.py b/d.py", "deleted file mode 100644", "--- a/d.py", "+++ /dev/null", "@@ -1 +0,0 @@", "-bye",
        "diff --git a/o.py b/r.py", "similarity index 90%", "rename from o.py", "rename to r.py",
        "diff --git a/i.png b/i.png", "Binary files a/i.png and b/i.png differ",
    ])
    files = {x.path: x for x in rt.parse_diff(diff)}
    assert files["a.py"].added == ["new"] and files["a.py"].removed == ["old"]
    assert files["n.md"].status == "A" and files["d.py"].status == "D"
    assert files["r.py"].status == "R" and files["r.py"].old_path == "o.py"
    assert files["i.png"].binary


def test_markdown_escapes_pipes():
    r = tier(f("LICENSE|x", ["x"], status="A"), f("LICENSE", ["x"]))
    r["reasons"][0]["detail"] = "a|b"
    assert "a\\|b" in rt.markdown(r) and "| a|b |" not in rt.markdown(r)


def test_cli_on_a_git_repo(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True,
                       capture_output=True)
    env_rules = tmp_path / "rules.yml"
    env_rules.write_text((HERE.parent / "risk.yml").read_text(encoding="utf-8"), encoding="utf-8")
    git("init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("a\n")
    git("add", "."); git("commit", "-qm", "base")
    git("checkout", "-qb", "pr")
    (tmp_path / "README.md").write_text("a\nb\n")
    git("commit", "-qam", "docs")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        out = tmp_path / "out.json"
        assert rt.main(["--rules", str(env_rules), "--base", "main", "--head", "pr", "--json", str(out)]) == 0
    finally:
        os.chdir(cwd)
    assert json.loads(out.read_text())["tier"] == "T0"
