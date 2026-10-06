"""Which machine owns a host name.

Two machines that share a `host` write the same folders and block each other. Each machine has a random id in
`.kb/machine-id` (never committed). The first sync of a host also commits that id as `sessions/<host>/.machine-id`.
A committed marker that differs from the local id means another machine owns the host.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from kb import gitops
from kb.util import atomic_write

MARKER = ".machine-id"


def local_id(kb_dir) -> str:
    """This machine's id. Made once (random hex) and kept; an empty file is replaced."""
    path = Path(kb_dir) / "machine-id"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    text = secrets.token_hex(16)
    atomic_write(path, (text + "\n").encode("utf-8"))
    return text


def marker_path(root, host: str) -> Path:
    return Path(root) / "sessions" / host / MARKER


def read_marker(root, host: str) -> str:
    try:
        return marker_path(root, host).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def claim(root, host: str, mine: str) -> None:
    """Write the marker if it is missing and the host already has a folder (so it is committed with the host's paths)."""
    if not marker_path(root, host).exists() and (Path(root) / "sessions" / host).is_dir():
        atomic_write(marker_path(root, host), (mine + "\n").encode("utf-8"))


def owned_by_another(root, host: str, mine: str, branch: str) -> bool:
    """True if a marker for `host` differs from `mine`: the one in the working tree, or (on `branch`) the one the
    upstream has as of the last fetch, which a clone that has not pulled yet does not have in its files."""
    seen = [read_marker(root, host)]
    if gitops.current_branch(root) == branch:
        seen.append(gitops.upstream_text(root, f"sessions/{host}/{MARKER}").strip())
    return any(m and m != mine for m in seen)
