import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Tests never read the real config or start real summaries."""
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.delenv("KB_ROOT", raising=False)
    monkeypatch.delenv("KB_CHILD", raising=False)
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "kb-test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "kb-test@example.com")
    yield
