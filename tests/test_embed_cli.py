import json

import pytest
from embed_fakes import FakeEmbedServer
from fixtures import SID
from test_cli import kb_env, run  # noqa: F401 - fixture

from kb import config, embed_runtime
from kb.cli import main


@pytest.fixture(autouse=True)
def fills(monkeypatch):
    """Background fills `kb find` would start, recorded instead of run (a fast test starts no process)."""
    from kb import cli
    started = []
    monkeypatch.setattr(cli, "_spawn_fill", lambda: started.append(1))
    return started


@pytest.fixture
def server():
    s = FakeEmbedServer()
    yield s
    s.close()


def _set(**keys):
    for k, v in keys.items():
        config.set_key(k, v)


def test_embed_then_find_a_paraphrase(kb_env, server, capsys):
    _set(embed_url=server.url)
    code, out = run(capsys, "find", "unstable", "--no-pages", "--no-memories")
    assert code == 1                                                   # no vectors yet: BM25 alone, no match
    code, out = run(capsys, "embed")
    assert code == 0 and out.startswith("embedded 5 items (4 sessions, 1 turn,") and "0 left" in out
    assert config.load().embed is True
    code, out = run(capsys, "find", "unstable", "--no-pages", "--no-memories")
    assert code == 0 and SID[-8:] in out.splitlines()[0]               # "unstable" means "flaky" to the model
                                                                       # (the session or its subagent)
    calls = server.calls
    assert run(capsys, "find", "unstable", "--no-embed")[0] == 1
    assert run(capsys, "find", "unstable", "--fts")[0] == 1
    assert run(capsys, "find", "unstable", "--role", "user")[0] == 1    # the vectors do not know who wrote a text
    assert server.calls == calls                                       # none asked the model
    code, out = run(capsys, "embed")
    assert out.startswith("embedded 0 items")
    code, out = run(capsys, "embed", "--rebuild")
    assert code == 0 and out.startswith("embedded 5 items")                # everything again


def test_a_dead_server_gives_plain_bm25(kb_env, server, capsys):
    _set(embed_url=server.url)
    run(capsys, "embed")
    plain = run(capsys, "find", "flaky", "--no-embed")
    server.fail = True
    assert run(capsys, "find", "flaky") == plain
    main(["find", "flaky", "-v"])
    assert "keyword search only" in capsys.readouterr().err
    _set(embed_url="http://127.0.0.1:9")                               # nothing listens
    assert run(capsys, "find", "flaky") == plain


def test_feature_on_but_not_installed_never_starts_anything(kb_env, capsys):
    _set(embed=True)                                                   # conftest fails any started process
    plain = run(capsys, "find", "flaky", "--no-embed")
    assert run(capsys, "find", "flaky") == plain


def test_embed_status_stop_off_and_remove(kb_env, server, capsys):
    code, out = run(capsys, "embed", "--status")
    assert code == 0 and "semantic search: off" in out and "not installed" in out and "vectors: none yet" in out
    assert run(capsys, "embed", "--stop")[1].strip() == "embedding server was not running"
    _set(embed_url=server.url)
    run(capsys, "embed")
    code, out = run(capsys, "embed", "--status")
    assert "semantic search: on" in out and "4 sessions" in out
    code, out = run(capsys, "embed", "--off")
    assert "off" in out and config.load().embed is False
    code, out = run(capsys, "embed", "--remove")
    assert not (kb_env / ".kb" / "embeddings.sqlite").exists()


def test_every_command_stops_an_idle_server(kb_env, capsys):
    srv = embed_runtime.Server()
    srv.cache.mkdir(parents=True)
    srv.state_path.write_text(json.dumps({"pid": 0, "port": 1, "binary": "", "started": 0, "last_used": 0}))
    run(capsys, "recent")
    assert srv.state() is None


def test_a_broken_reap_never_breaks_a_command(kb_env, capsys, monkeypatch):
    monkeypatch.setattr(embed_runtime.Server, "reap_if_idle", lambda self: 1 / 0)
    assert run(capsys, "recent")[0] == 0


def test_find_through_the_bit_index_and_remove_drops_it(kb_env, server, capsys, monkeypatch):
    from kb import embed
    _set(embed_url=server.url)
    run(capsys, "embed")
    exact = run(capsys, "find", "unstable", "--no-pages", "--no-memories")
    monkeypatch.setattr(embed, "EXACT_BELOW", 1)
    run(capsys, "embed", "--rebuild")                                  # writes the bit index too
    bits = kb_env / ".kb" / embed.BITS
    assert bits.exists()
    assert run(capsys, "find", "unstable", "--no-pages", "--no-memories") == exact
    run(capsys, "embed", "--remove")
    assert not bits.exists()


def test_find_starts_one_background_fill_when_vectors_are_behind(kb_env, server, capsys, fills, monkeypatch):
    import os
    import time

    from kb import cli, embed_runtime
    run(capsys, "find", "flaky")
    assert fills == []                                                 # feature off: never
    _set(embed_url=server.url)
    main(["find", "flaky", "-v"])
    assert fills == [1] and "filling them in the background" in capsys.readouterr().err   # no store yet
    run(capsys, "find", "flaky")
    assert fills == [1]                                                # at most once per FILL_EVERY
    stamp = embed_runtime.cache_dir() / "last-fill"
    old = time.time() - cli.FILL_EVERY - 1
    os.utime(stamp, (old, old))
    run(capsys, "embed")                                               # complete now
    run(capsys, "find", "flaky")
    assert fills == [1]                                                # not behind: nothing to fill
    os.utime(stamp, (old, old))
    from test_index import put
    put(kb_env, "h/new.md", "new-1", title="A new session")
    run(capsys, "reindex")
    run(capsys, "find", "flaky")
    assert fills == [1, 1]                                             # behind again


def test_no_fill_without_a_runnable_model(kb_env, capsys, fills):
    _set(embed=True)                                                   # on, but the runtime is not installed
    run(capsys, "find", "flaky")
    assert fills == []


def test_quiet_does_nothing_when_off_and_embeds_silently_when_on(kb_env, server, capsys):
    code, out = run(capsys, "embed", "--quiet")
    assert (code, out) == (0, "") and config.load().embed is False and server.calls == 0
    _set(embed_url=server.url)
    code, out = run(capsys, "embed", "--quiet")
    assert (code, out) == (0, "") and server.calls > 0
    assert "5 items" not in out and "4 sessions" in run(capsys, "embed", "--status")[1]


def test_install_downloads_and_turns_on_without_starting_anything(kb_env, capsys, monkeypatch, tmp_path):
    from kb import embed_runtime
    calls = []
    monkeypatch.setattr(embed_runtime, "install", lambda progress=None: calls.append(1))
    code, out = run(capsys, "embed", "--install", "--root", str(tmp_path / "data"))
    assert code == 0 and calls == [1] and "installed" in out
    cfg = config.load()
    assert cfg.embed is True and cfg.root == (tmp_path / "data").resolve()
    assert not embed_runtime.Server().state_path.exists()              # no server started
    monkeypatch.setattr(embed_runtime, "install", lambda progress=None: (_ for _ in ()).throw(
        embed_runtime.EmbedUnavailable("download failed: offline")))
    assert run(capsys, "embed", "--install") == (2, "kb: download failed: offline\n")


@pytest.mark.slow
def test_the_background_fill_really_runs(kb_env, server, capsys, fills):
    import importlib
    import time

    from kb import cli, embed
    real = importlib.reload(cli)._spawn_fill                           # the unpatched function (env stays isolated)
    _set(embed_url=server.url)
    run(capsys, "reindex")                                             # the child opens the index read-only
    real()
    store = kb_env / ".kb" / embed.STORE
    deadline = time.time() + 30
    while time.time() < deadline:
        st = embed.Vectors.open_readonly(store)
        if st is not None:
            n = sum(st.counts(embed.model_key("url:" + server.url)).values())
            st.close()
            if n >= 5:
                break
        time.sleep(0.2)
    else:
        raise AssertionError("the background kb embed --quiet did not fill the store")
