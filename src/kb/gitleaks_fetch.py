"""A pinned gitleaks that kb downloads when the machine has none (`kb setup gitleaks`; install.sh runs it).

The release archive is checked against its pinned size and sha256 (the same checked download as the embedding
runtime, kb.embed_runtime), then only the `gitleaks` binary is kept, in ~/.cache/retroagent/gitleaks/<version>/.
kb.gitops.find_gitleaks looks there after PATH and the usual folders, so a hook with a short PATH finds it too.
"""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

VERSION = "8.30.1"
_URL = "https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{p}.tar.gz"
ASSETS = {      # platform key (as kb.embed_runtime.platform_key) -> (release name, size, sha256)
    "darwin-arm64": ("darwin_arm64", 7897593, "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
    "darwin-x86_64": ("darwin_x64", 8359235, "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    "linux-aarch64": ("linux_arm64", 7601421, "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    "linux-x86_64": ("linux_x64", 8230402, "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
}


class FetchError(Exception):
    """gitleaks could not be downloaded. One line, safe to show."""


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "retroagent" / "gitleaks"


def binary_path(cache: Path = None) -> Path:
    """Where the pinned gitleaks lives once downloaded (it may not exist)."""
    return (cache or cache_dir()) / VERSION / "gitleaks"


def platform_key():
    system = platform.system().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    key = f"{system}-{machine}"
    return key if key in ASSETS else None


def install(cache: Path = None, key: str = None) -> Path:
    """Download, check and unpack the pinned gitleaks; nothing to do when it is there. Older versions are removed."""
    from kb.embed_runtime import EmbedUnavailable, _download, _rm, _safe_extract
    cache = cache or cache_dir()
    dest = binary_path(cache)
    if dest.is_file() and os.access(dest, os.X_OK):
        return dest
    key = key or platform_key()
    if key is None:
        raise FetchError(f"no pinned gitleaks for {platform.system()} {platform.machine()}; install it yourself")
    name, size, sha256 = ASSETS[key]
    url = _URL.format(v=VERSION, p=name)
    archive = cache / "downloads" / url.rsplit("/", 1)[1]
    unpack = cache / "downloads" / "unpack"
    try:
        _download(url, archive, size, sha256)
        shutil.rmtree(unpack, ignore_errors=True)
        unpack.mkdir(parents=True)
        _safe_extract(archive, unpack)
    except EmbedUnavailable as e:
        raise FetchError(str(e)) from None
    finally:
        _rm(archive)
    found = unpack / "gitleaks"
    if not found.is_file():
        shutil.rmtree(unpack, ignore_errors=True)
        raise FetchError(f"no gitleaks binary in {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(found, dest)
    dest.chmod(0o755)
    shutil.rmtree(unpack, ignore_errors=True)
    for p in cache.iterdir():                       # older pins
        if p.is_dir() and p.name not in (VERSION, "downloads"):
            shutil.rmtree(p, ignore_errors=True)
    return dest
