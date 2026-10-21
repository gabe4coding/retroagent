"""Shared sandbox for the repro scripts: real kb code, test fixtures, an isolated HOME and git config, and a `git`
wrapper first on PATH. The wrapper only adds outside events (another machine's push, a person's commit, a turn) at
fixed points; every kb step runs unchanged."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]
REAL_GIT = shutil.which("git")


def sandbox(name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=f"kb-repro-{name}-"))
    if not os.environ.get("KB_REPRO_KEEP"):         # KB_REPRO_KEEP=1 keeps the sandbox to look at
        atexit.register(shutil.rmtree, tmp, True)
    (tmp / "home").mkdir()
    (tmp / "gitconfig").write_text("[user]\n\tname = repro\n\temail = repro@example.invalid\n"
                                   "[init]\n\tdefaultBranch = main\n")
    os.environ.update(HOME=str(tmp / "home"), GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                      KB_CONFIG=str(tmp / "home" / "no-config.json"), REAL_GIT=REAL_GIT, REPRO_STATE=str(tmp))
    return tmp


def install_wrapper(tmp: Path, body: str) -> None:
    """Put a `git` first on PATH: it runs `body` (bash), then the real git with the same arguments."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "git"
    wrapper.write_text("#!/bin/bash\n" + body + '\nexec "$REAL_GIT" "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
