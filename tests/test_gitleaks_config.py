"""The repo's .gitleaks.toml, checked with a real gitleaks (skipped when there is none).

The samples are made up at run time. The control tests scan the same files without the config: they prove that every
sample is one that gitleaks reports by default, so the config tests cannot pass by accident.
"""
import gzip
import os
import random
import shutil
import string
from pathlib import Path

import pytest

from kb import gitops

CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"


def _find_gitleaks():
    # tests/conftest.py empties gitops.GITLEAKS_FALLBACKS, so look in the usual places here
    for cand in (shutil.which("gitleaks"), "/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


GITLEAKS = _find_gitleaks()
pytestmark = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks is not installed")


def _hex(n, seed):
    """n hex digits that use all 16 symbols, so that their entropy is high enough for every gitleaks rule."""
    rng = random.Random(seed)
    digits = list("0123456789abcdef") + [rng.choice("0123456789abcdef") for _ in range(n - 16)]
    rng.shuffle(digits)
    return "".join(digits)


def _pick(n, seed, alphabet):
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


# file name -> text. Each one is reported by gitleaks with its default rules.
NOISE = {
    "notes.md": "fixed the sourcegraph search regression in commit " + _hex(40, 1) + "\n",       # sourcegraph-access-token
    "code.py": "github_token=" + _hex(24, 2) + "\n",                                             # generic-api-key
    "jira.json": '{"atlassianAccountId":"' + _hex(24, 3) + '"}\n',                              # atlassian-api-token
}
SECRETS = {
    "gh.txt": "GH=" + "ghp_" + _pick(36, 4, string.ascii_letters + string.digits) + "\n",         # github-pat
    "aws.txt": "AWS=" + "AKIA" + _pick(16, 5, string.ascii_uppercase + "234567") + "\n",          # aws-access-token
}


def _repo(tmp_path, name, with_config):
    repo = tmp_path / name
    repo.mkdir()
    gitops.git(repo, "init", "-q")
    if with_config:
        shutil.copy(CONFIG, repo / ".gitleaks.toml")
        gitops.git(repo, "add", ".gitleaks.toml")
    gitops.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _stage_text(repo, files):
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    gitops.git(repo, "add", *files)


def _stage_raw(repo, rel, lines):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("".join(line if line.endswith("\n") else line + "\n" for line in lines))
    gitops.git(repo, "add", rel)


def test_the_config_file_exists_and_names_what_it_disables():
    text = CONFIG.read_text(encoding="utf-8")
    assert "useDefault = true" in text
    assert '"generic-api-key"' in text and '"sourcegraph-access-token"' in text and "atlassianAccountId" in text


@pytest.fixture
def no_shipped_config(tmp_path, monkeypatch):
    """The control tests: no config in the data repo and none shipped with the code, so gitleaks uses its defaults."""
    monkeypatch.setattr(gitops, "CODE_ROOT", tmp_path / "no-code")


def test_staged_files_default_rules_report_every_sample(tmp_path, no_shipped_config):
    repo = _repo(tmp_path, "plain", with_config=False)
    _stage_text(repo, {**NOISE, **SECRETS})
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.ran and result.error == ""
    assert result.files == sorted({**NOISE, **SECRETS})


def test_staged_files_only_real_secrets_are_reported_with_the_repo_config(tmp_path):
    repo = _repo(tmp_path, "configured", with_config=True)
    _stage_text(repo, {**NOISE, **SECRETS})
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.ran and result.error == ""
    assert result.files == ["aws.txt", "gh.txt"]


def _raw(name):
    return f"raw/h/claude/2026/10/{name}.jsonl.gz"


def _stage_all_raw(repo):
    for name, text in {**NOISE, **SECRETS}.items():            # one raw file per sample
        _stage_raw(repo, _raw(name), ['{"type":"user"}', text, '{"type":"assistant"}'])


def test_raw_files_default_rules_report_every_sample(tmp_path, no_shipped_config):
    repo = _repo(tmp_path, "plain-raw", with_config=False)
    _stage_all_raw(repo)
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.ran and result.error == ""
    assert result.files == sorted(_raw(name) for name in {**NOISE, **SECRETS})


def test_raw_files_honour_the_repo_config(tmp_path):
    repo = _repo(tmp_path, "configured-raw", with_config=True)
    _stage_all_raw(repo)
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.ran and result.error == ""
    assert result.files == sorted(_raw(name) for name in SECRETS)


def test_a_data_repo_without_a_config_uses_the_one_shipped_with_the_code(tmp_path):
    repo = _repo(tmp_path, "data-without-config", with_config=False)
    assert gitops.gitleaks_config(repo) == CONFIG
    _stage_text(repo, {**NOISE, **SECRETS})
    _stage_all_raw(repo)
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.ran and result.error == ""
    assert result.files == sorted(["aws.txt", "gh.txt"] + [_raw(name) for name in SECRETS])


def test_a_data_repo_config_wins_over_the_shipped_one(tmp_path):
    repo = _repo(tmp_path, "own-config", with_config=False)
    (repo / ".gitleaks.toml").write_text('title = "mine"\n[extend]\nuseDefault = true\n')
    assert gitops.gitleaks_config(repo) == repo / ".gitleaks.toml"
    _stage_text(repo, {**NOISE, **SECRETS})
    result = gitops.secrets_check(repo, exe=GITLEAKS)
    assert result.files == sorted({**NOISE, **SECRETS})
