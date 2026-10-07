import subprocess

import pytest

from kb import gitops


def pytest_addoption(parser):
    parser.addoption("--all", action="store_true", help="also run the tests marked slow")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: starts processes (git, gitleaks, sh, python) or takes seconds; runs only with --all")


def pytest_collection_modifyitems(config, items):
    """Without --all, the tests marked slow are left out: the fast run must stay under a few seconds."""
    if config.getoption("--all"):
        return
    slow = [item for item in items if item.get_closest_marker("slow")]
    if slow:
        config.hook.pytest_deselected(items=slow)
        items[:] = [item for item in items if not item.get_closest_marker("slow")]


@pytest.fixture(autouse=True)
def _no_processes_unless_slow(request, monkeypatch):
    """A test that starts a process must be marked slow: each process costs tens of milliseconds."""
    if request.node.get_closest_marker("slow"):
        return

    def refuse(self, args, *a, **k):
        raise AssertionError(f"this test starts a process ({args!r}): mark it @pytest.mark.slow")

    monkeypatch.setattr(subprocess.Popen, "_execute_child", refuse)


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
