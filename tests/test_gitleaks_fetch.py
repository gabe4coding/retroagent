"""kb.gitleaks_fetch (the pinned gitleaks `kb setup gitleaks` downloads) and `kb setup repo-check`."""
import io
import os
import shutil
import tarfile

import pytest

from fixtures import init_remote
from kb import embed_runtime, gitleaks_fetch, gitops, setup


def _archive(dest, members):
    """A .tar.gz at dest with {name: bytes}."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            t.addfile(info, io.BytesIO(data))


@pytest.fixture
def fake_download(monkeypatch):
    """_download writes an archive with the given members instead of a network fetch; records the urls."""
    calls = []
    members = {"gitleaks": b"#!/bin/sh\necho gitleaks\n", "LICENSE": b"MIT\n", "README.md": b"r\n"}

    def download(url, dest, size, sha256, progress=None):
        calls.append(url)
        _archive(dest, members)
    monkeypatch.setattr(embed_runtime, "_download", download)
    return calls, members


def test_install_keeps_only_the_binary_executable_and_removes_older_pins(tmp_path, fake_download):
    calls, _ = fake_download
    old = tmp_path / "8.0.0"
    old.mkdir()
    path = gitleaks_fetch.install(tmp_path, key="darwin-arm64")
    assert path == gitleaks_fetch.binary_path(tmp_path) and os.access(path, os.X_OK)
    assert calls == [f"https://github.com/gitleaks/gitleaks/releases/download/v{gitleaks_fetch.VERSION}/"
                     f"gitleaks_{gitleaks_fetch.VERSION}_darwin_arm64.tar.gz"]
    assert sorted(p.name for p in path.parent.iterdir()) == ["gitleaks"]
    assert not old.exists()
    assert not any((tmp_path / "downloads").iterdir())          # archive and unpack folder are gone


def test_install_does_nothing_when_the_pin_is_there(tmp_path, fake_download):
    calls, _ = fake_download
    gitleaks_fetch.install(tmp_path, key="linux-x86_64")
    gitleaks_fetch.install(tmp_path, key="linux-x86_64")
    assert len(calls) == 1


def test_install_refuses_an_archive_without_the_binary(tmp_path, fake_download):
    _, members = fake_download
    del members["gitleaks"]
    with pytest.raises(gitleaks_fetch.FetchError, match="no gitleaks binary"):
        gitleaks_fetch.install(tmp_path, key="linux-aarch64")
    assert not gitleaks_fetch.binary_path(tmp_path).exists()


def test_a_bad_download_is_one_fetch_error(tmp_path, monkeypatch):
    def bad(url, dest, size, sha256, progress=None):
        raise embed_runtime.EmbedUnavailable(f"download does not match its pinned size and sha256: {url}")
    monkeypatch.setattr(embed_runtime, "_download", bad)
    with pytest.raises(gitleaks_fetch.FetchError, match="sha256"):
        gitleaks_fetch.install(tmp_path, key="darwin-x86_64")


def test_an_unknown_platform_says_to_install_it_by_hand(tmp_path, monkeypatch):
    monkeypatch.setattr(gitleaks_fetch, "platform_key", lambda: None)
    with pytest.raises(gitleaks_fetch.FetchError, match="install it yourself"):
        gitleaks_fetch.install(tmp_path)


def test_every_platform_of_the_embed_runtime_has_a_pin():
    assert set(gitleaks_fetch.ASSETS) == set(embed_runtime.ASSETS["runtime"])


def test_find_gitleaks_uses_the_downloaded_one(monkeypatch, tmp_path, fake_download):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert gitops.find_gitleaks(None) is None
    gitleaks_fetch.install(key="darwin-arm64")                  # the default cache, under the test's HOME
    assert gitops.find_gitleaks(None) == str(gitleaks_fetch.binary_path())


@pytest.mark.slow
def test_repo_check_reachable_and_privacy(tmp_path):
    remote = init_remote(tmp_path)
    seen = []

    def status(slug):
        seen.append(slug)
        return {"me/public": 200, "me/private": 404}.get(slug, 0)
    local = setup.repo_check(str(remote), status=status)
    assert local["reachable"] is True and local["public"] is None and local["slug"] == "" and seen == []
    missing = setup.repo_check(str(tmp_path / "nope.git"), status=status)
    assert missing["reachable"] is False and missing["error"]
    assert setup.repo_check("https://github.com/me/public.git", status=status)["public"] is True
    assert setup.repo_check("git@github.com:me/private.git", status=status)["public"] is False
    assert seen == ["me/public", "me/private"]


def test_repo_check_needs_a_url():
    with pytest.raises(setup.SetupError):
        setup.repo_check("  ")
