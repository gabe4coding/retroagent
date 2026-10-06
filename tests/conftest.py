import pytest

from kb import gitops


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Tests never read the real config, the user's home or git config, or start real summaries."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.delenv("KB_ROOT", raising=False)
    monkeypatch.delenv("KB_CHILD", raising=False)
    monkeypatch.setattr(gitops, "GITLEAKS_FALLBACKS", ())      # never the gitleaks of the machine that runs the tests
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "kb-test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "kb-test@example.com")
    yield
