import functools
import hashlib
import io
import json
import os
import sys
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from embed_fakes import FAKE_LLAMA_SERVER

from kb import embed_runtime as er

BUILD = "b1"


def _tar(members) -> bytes:
    """members: (name, bytes or ('link', target), mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            if isinstance(data, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, data[1]
                t.addfile(info)
            else:
                info.size, info.mode = len(data), mode
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A local web server for the pinned files; ASSETS point at it. Returns a function to (re)publish files."""
    www = tmp_path / "www"
    www.mkdir()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(www)))
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(er, "platform_key", lambda: "test-x")

    def publish(runtime: bytes, model: bytes = b"GGUF fake model", runtime_sha=None, build=BUILD):
        (www / "rt.tgz").write_bytes(runtime)
        (www / "m.gguf").write_bytes(model)
        monkeypatch.setattr(er, "ASSETS", {
            "llama_build": build,
            "runtime": {"test-x": {"url": f"{base}/rt.tgz", "size": len(runtime),
                                   "sha256": runtime_sha or _sha(runtime)}},
            "model": {"name": er.MODEL, "url": f"{base}/m.gguf", "size": len(model), "sha256": _sha(model)}})
    yield publish
    httpd.shutdown()
    httpd.server_close()


def _fake_runtime() -> bytes:
    tests = str(Path(__file__).parent)
    script = FAKE_LLAMA_SERVER.format(python=sys.executable, tests=tests).encode()
    return _tar([("llama-b1/llama-server", script, 0o755), ("llama-b1/libx.0.dylib", b"lib", 0o644),
                 ("llama-b1/libx.dylib", ("link", "libx.0.dylib"), 0)])


def test_install_checks_extracts_and_is_idempotent(site):
    site(_fake_runtime())
    assert not er.installed()
    er.install()
    assert er.installed()
    binary = er.server_binary()
    assert binary.name == "llama-server" and os.access(binary, os.X_OK)
    assert (binary.parent / "libx.dylib").resolve().name == "libx.0.dylib"
    assert not list((er.cache_dir() / "downloads").iterdir())                 # the archive is gone
    mtime = binary.stat().st_mtime
    er.install()                                                              # nothing to do
    assert binary.stat().st_mtime == mtime


def test_wrong_sha_refuses_and_leaves_nothing(site):
    site(_fake_runtime(), runtime_sha="0" * 64)
    with pytest.raises(er.EmbedUnavailable, match="sha256"):
        er.install()
    assert not er.installed()
    assert not [p for p in er.cache_dir().rglob("*") if p.is_file()]


@pytest.mark.parametrize("members", [
    [("../evil", b"x", 0o644)],
    [("/abs/evil", b"x", 0o644)],
    [("llama-b1/link", ("link", "../../outside"), 0)],
    [("llama-b1/link", ("link", "/etc/passwd"), 0)],
])
def test_unsafe_archives_are_refused(site, members):
    site(_tar(members))
    with pytest.raises(er.EmbedUnavailable, match="unsafe"):
        er.install()
    assert not er.installed()


def test_new_pin_replaces_the_old_runtime(site):
    site(_fake_runtime())
    er.install()
    old = er.server_binary()
    site(_fake_runtime(), build="b2")
    er.install()
    assert er.server_binary() != old and not old.exists()


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(er, "platform_key", lambda: None)
    with pytest.raises(er.EmbedUnavailable, match="no embedding runtime"):
        er.install()


def test_reap_and_state_without_a_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    now = [1000.0]
    srv = er.Server(clock=lambda: now[0])
    assert srv.reap_if_idle() is False and srv.alive() is False and srv.endpoint() is None
    srv.cache.mkdir(parents=True)
    srv.state_path.write_text(json.dumps({"pid": 0, "port": 1, "binary": "", "started": 0, "last_used": 900}))
    assert srv.reap_if_idle() is False                     # used 100 s ago
    now[0] = 900 + er.IDLE_STOP + 1
    assert srv.reap_if_idle() is True and srv.state() is None     # pid 0 is never ours: only the state goes


@pytest.mark.slow
def test_server_starts_answers_and_stops(site):
    site(_fake_runtime())
    er.install()
    srv = er.Server()
    srv.start(wait=True)
    assert srv.alive()
    st = srv.state()
    assert oct(srv.key_path.stat().st_mode)[-3:] == "600"
    srv.start(wait=True)                                   # running: no second process
    assert srv.state()["pid"] == st["pid"]
    assert srv.stop() is True
    assert not srv.alive() and srv.state() is None and not er._exists(st["pid"])


@pytest.mark.slow
def test_a_reused_pid_is_never_killed(site, tmp_path):
    import subprocess
    site(_fake_runtime())
    er.install()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        srv = er.Server()
        srv.cache.mkdir(parents=True, exist_ok=True)
        srv.state_path.write_text(json.dumps({"pid": other.pid, "port": 1, "binary": str(er.server_binary()),
                                              "started": 0, "last_used": 0}))
        assert srv.stop() is True
        assert other.poll() is None                        # still running
    finally:
        other.kill()
        other.wait()


@pytest.mark.slow
def test_managed_path_end_to_end(site, tmp_path, monkeypatch, capsys):
    from fixtures import SID
    from test_cli import run
    from test_index import build_kb

    from kb import config
    root = build_kb(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"root": str(root), "host": "h", "claude_dir": str(tmp_path / "none"),
                               "codex_dirs": [], "codex_home": str(tmp_path / "none")}))
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    site(_fake_runtime())
    code, out = run(capsys, "embed")
    assert code == 0 and out.startswith("embedded 4 items") and config.load().embed is True
    srv = er.Server()
    assert srv.alive()
    code, out = run(capsys, "find", "unstable", "--no-pages", "--no-memories")
    assert code == 0 and SID[-8:] in out.splitlines()[0]
    assert "server: running" in run(capsys, "embed", "--status")[1]
    pid = srv.state()["pid"]
    assert run(capsys, "embed", "--stop")[1].strip() == "embedding server stopped"
    assert not er._exists(pid)
    run(capsys, "find", "unstable")                                    # stopped: BM25 now, start for next time
    srv._wait()
    assert srv.alive()
    srv.stop()
