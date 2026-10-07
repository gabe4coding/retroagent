"""The embedding runtime kb manages for semantic search: a pinned llama.cpp build and model, downloaded once into
~/.cache/retroagent/embed/ and checked against sha256, and a llama-server started on demand and stopped when idle.

The server listens on 127.0.0.1 only, on a free port, and answers only with the API key in the cache folder. After
SLEEP_IDLE seconds without work it unloads the model (the process stays, small); `reap_if_idle`, which every kb
command calls, stops the process after IDLE_STOP seconds without use. Every failure is an EmbedUnavailable with a
one-line reason: the caller then searches with BM25 alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from kb.lock import Lock
from kb.util import atomic_write

ASSETS = json.loads(Path(__file__).with_name("embed_assets.json").read_text(encoding="utf-8"))
MODEL = ASSETS["model"]["name"]
SLEEP_IDLE = 300            # the server unloads the model after this many idle seconds
IDLE_STOP = 3600            # kb stops the server process after this many seconds without use
PROBE = 0.15                # longest `kb find` waits for the server to answer
START_WAIT = 30.0           # longest a sync or `kb embed` waits for a starting server


class EmbedUnavailable(Exception):
    """Semantic search cannot run now. One line, safe to show."""


@dataclass
class Endpoint:
    url: str
    key: str = ""
    model: str = MODEL


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "retroagent" / "embed"


def platform_key():
    """darwin-arm64, darwin-x86_64, linux-aarch64 or linux-x86_64; None where no pinned build exists."""
    system = platform.system().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    key = f"{system}-{machine}"
    return key if key in ASSETS["runtime"] else None


# ---- install

def _download(url: str, dest: Path, size: int, sha256: str, progress=None) -> None:
    """url to dest, checked by size and sha256. A partial or wrong file never stays at dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest, got = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got, size)
    except (OSError, urllib.error.URLError) as e:
        _rm(part)
        raise EmbedUnavailable(f"download failed: {url}: {' '.join(str(e).split())}") from None
    if got != size or digest.hexdigest() != sha256:
        _rm(part)
        raise EmbedUnavailable(f"download does not match its pinned size and sha256: {url}")
    os.replace(part, dest)


def _rm(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract files, folders and relative symlinks that stay inside dest; anything else refuses the archive."""
    root = str(dest.resolve())
    with tarfile.open(archive) as t:
        members = t.getmembers()
        for m in members:
            target = os.path.normpath(os.path.join(root, m.name))
            if os.path.isabs(m.name) or ".." in Path(m.name).parts or not _inside(target, root):
                raise EmbedUnavailable(f"unsafe path in {archive.name}: {m.name}")
            if m.issym():
                link = os.path.normpath(os.path.join(os.path.dirname(target), m.linkname))
                if os.path.isabs(m.linkname) or not _inside(link, root):
                    raise EmbedUnavailable(f"unsafe link in {archive.name}: {m.name} -> {m.linkname}")
            elif not (m.isfile() or m.isdir()):
                raise EmbedUnavailable(f"unsupported member in {archive.name}: {m.name}")
        if hasattr(tarfile, "data_filter"):          # Python 3.12+: the checks above, plus the stdlib's own
            t.extractall(root, members=members, filter="data")
        else:
            t.extractall(root, members=members)


def _runtime_dir(cache: Path) -> Path:
    return cache / "runtime" / ASSETS["llama_build"]


def _model_path(cache: Path) -> Path:
    return cache / "models" / f"{MODEL}.gguf"


def server_binary(cache: Path = None):
    """The pinned llama-server, or None while it is not installed."""
    run = _runtime_dir(cache or cache_dir())
    return _find_binary(run) if (run / ".ok").exists() else None


def _find_binary(run: Path):
    found = sorted(run.glob("*/llama-server")) + sorted(run.glob("llama-server"))
    return found[0] if found else None


def installed(cache: Path = None) -> bool:
    cache = cache or cache_dir()
    return server_binary(cache) is not None and _model_path(cache).with_suffix(".ok").exists()


def install(cache: Path = None, progress=None) -> None:
    """Download and check the pinned runtime and model; nothing to do when they are there. Older pins are removed."""
    cache = cache or cache_dir()
    key = platform_key()
    if key is None:
        raise EmbedUnavailable(f"no embedding runtime for {platform.system()} {platform.machine()}")
    run = _runtime_dir(cache)
    if not (run / ".ok").exists():
        rt = ASSETS["runtime"][key]
        archive = cache / "downloads" / rt["url"].rsplit("/", 1)[1]
        _download(rt["url"], archive, rt["size"], rt["sha256"], progress)
        shutil.rmtree(run, ignore_errors=True)
        run.mkdir(parents=True)
        _safe_extract(archive, run)
        _rm(archive)
        if _find_binary(run) is None:
            raise EmbedUnavailable(f"no llama-server in {rt['url']}")
        (run / ".ok").write_text(rt["sha256"])
    model = _model_path(cache)
    marker = model.with_suffix(".ok")
    if not marker.exists():
        m = ASSETS["model"]
        _download(m["url"], model, m["size"], m["sha256"], progress)
        marker.write_text(m["sha256"])
    for folder, keep in ((cache / "runtime", run.name), (cache / "models", None)):
        for p in folder.iterdir() if folder.is_dir() else ():
            if keep is not None and p.name != keep:
                shutil.rmtree(p, ignore_errors=True)
            elif keep is None and not p.name.startswith(MODEL + "."):
                _rm(p)


# ---- the server process

class Server:
    """The llama-server kb started. State in server.json: pid, port, binary, started, last_used."""

    def __init__(self, cache: Path = None, clock=time.time):
        self.cache = cache or cache_dir()
        self.state_path = self.cache / "server.json"
        self.key_path = self.cache / "api-key"
        self.log_path = self.cache / "server.log"
        self.clock = clock

    def state(self):
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return s if isinstance(s, dict) and isinstance(s.get("port"), int) else None

    def _write(self, s) -> None:
        if s is None:
            _rm(self.state_path)
        else:
            atomic_write(self.state_path, json.dumps(s).encode("utf-8"))

    def key(self) -> str:
        try:
            return self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        self.cache.mkdir(parents=True, exist_ok=True)
        k = secrets.token_hex(24)
        fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(k)
        return k

    def endpoint(self):
        s = self.state()
        return Endpoint(f"http://127.0.0.1:{s['port']}", self.key()) if s else None

    def alive(self, timeout: float = PROBE) -> bool:
        """The server in the state answers with our key. /v1/models never wakes a sleeping model."""
        ep = self.endpoint()
        if ep is None:
            return False
        req = urllib.request.Request(ep.url + "/v1/models", headers={"Authorization": f"Bearer {ep.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def start(self, wait: bool) -> None:
        """Start the installed server unless it runs. wait: until it answers (else EmbedUnavailable)."""
        lock = Lock(self.cache / "server.lock")
        if not lock.acquire():                     # another kb is starting it
            if wait:
                self._wait()
            return
        try:
            if self.alive():
                return
            self._stop_locked()
            binary = server_binary(self.cache)
            if binary is None:
                raise EmbedUnavailable("embedding runtime not installed; run: kb embed")
            port = _free_port()
            self.key()
            log = open(self.log_path, "ab")
            try:
                proc = subprocess.Popen(
                    [str(binary), "-m", str(_model_path(self.cache)), "--embeddings", "--host", "127.0.0.1",
                     "--port", str(port), "-c", "8192", "-ub", "8192", "-b", "8192", "-np", "2",
                     "--sleep-idle-seconds", str(SLEEP_IDLE), "--api-key-file", str(self.key_path), "--offline"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            except OSError as e:
                raise EmbedUnavailable(f"cannot start {binary}: {e}") from None
            finally:
                log.close()
            now = self.clock()
            self._write({"pid": proc.pid, "port": port, "binary": str(binary), "started": now, "last_used": now})
        finally:
            lock.release()
        if wait:
            self._wait()

    def _wait(self) -> None:
        end = time.monotonic() + START_WAIT
        while time.monotonic() < end:
            if self.alive(timeout=1.0):
                return
            time.sleep(0.1)
        raise EmbedUnavailable(f"embedding server did not start; see {self.log_path}")

    def touch(self) -> None:
        s = self.state()
        if s:
            s["last_used"] = self.clock()
            try:
                self._write(s)
            except OSError:
                pass

    def stop(self) -> bool:
        lock = Lock(self.cache / "server.lock")
        if not lock.acquire():
            return False
        try:
            return self._stop_locked()
        finally:
            lock.release()

    def _stop_locked(self) -> bool:
        s = self.state()
        if s is None:
            return False
        pid = s.get("pid")
        if isinstance(pid, int) and _is_ours(pid, s.get("binary", "")):
            try:
                os.kill(pid, signal.SIGTERM)
                end = time.monotonic() + 3
                while time.monotonic() < end and _exists(pid):
                    time.sleep(0.05)
                if _exists(pid):
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self._write(None)
        return True

    def reap_if_idle(self) -> bool:
        """Stop the server when nobody used it for IDLE_STOP seconds. Cheap when there is nothing to stop."""
        s = self.state()
        if s is None or self.clock() - float(s.get("last_used") or 0) < IDLE_STOP:
            return False
        return self.stop()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                            # our own child that exited: reap it
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _is_ours(pid: int, binary: str) -> bool:
    """The pid runs our llama-server binary (a reused pid is never signalled)."""
    if not binary or not _exists(pid):
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return binary in out.stdout.split()            # first word for the real binary, after the interpreter for a script


def ensure(cfg, wait: bool, progress=None):
    """The endpoint to embed with, or None.

    wait=True (sync, `kb embed`): install what is missing (also a new pin after `kb update`), start, and wait; raises
    EmbedUnavailable.
    wait=False (`kb find`): never downloads and never waits. A running server gives its endpoint; an installed but
    stopped one is started in the background for the next call and None is returned."""
    if cfg.embed_url:
        return Endpoint(cfg.embed_url, "", "url:" + cfg.embed_url)
    srv = Server()
    if wait:
        install(progress=progress)
        srv.start(wait=True)
        return srv.endpoint()
    if srv.alive():
        return srv.endpoint()
    if installed():
        srv.start(wait=False)
    return None
