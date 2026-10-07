import json

import pytest
from embed_fakes import FakeEmbedServer
from fixtures import SID
from test_cli import kb_env, run  # noqa: F401 - fixture

from kb import config, embed_runtime
from kb.cli import main


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
    assert server.calls == calls                                       # neither asked the model
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
