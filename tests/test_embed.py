import pytest
from embed_fakes import FakeEmbedServer, fake_vector
from test_index import put
from test_pages import write_page

from kb import embed
from kb.distill import dump_front_matter
from kb.embed_runtime import Endpoint
from kb.index import Filters, Index, fuse


def write_memory(root, name, description, body="", project="demo"):
    path = root / "memories" / "h" / "claude" / project / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"kind": "memory", "host": "h", "agent": "claude", "project": project, "name": name,
            "description": description, "file": f"{name}.md"}
    path.write_text(dump_front_matter(meta) + "\n" + body, encoding="utf-8")
    return path


@pytest.fixture
def server():
    s = FakeEmbedServer()
    yield s
    s.close()


@pytest.fixture
def kbx(tmp_path):
    """An index with sessions, a page and a memory, and an empty vector store."""
    root = tmp_path / "kb"
    put(root, "h/a.md", "a-1", title="Reduce cost of the e2e suite", summary="Cut the cost of runs by a third.")
    put(root, "h/b.md", "b-1", title="Fix flaky waits", summary="Retry flaky waits instead of sleeping.",
        project="other")
    put(root, "h/c.md", "c-1", title="Voli Torino Londra", summary="Searched flights.", tags=["travel"])
    write_page(root, "project", "demo")
    write_memory(root, "budget", "keep the cost low", "Prefer the cheaper model.")
    idx = Index(root / ".kb" / "index.sqlite")
    idx.update(root)
    store = embed.Vectors(root / ".kb" / "embeddings.sqlite")
    yield root, idx, store
    store.close()
    idx.close()


def test_embed_batches_orders_and_normalizes(server):
    texts = [f"text {i}" for i in range(70)]
    vecs = embed.embed(texts, server.url)
    assert server.calls == 3 and len(vecs) == 70
    assert vecs[5] == pytest.approx(fake_vector("text 5"))                 # order kept, length 1
    assert sum(x * x for x in vecs[0]) == pytest.approx(1.0)


def test_embed_errors_are_one_line(server):
    server.fail = True
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], server.url)
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], "http://127.0.0.1:9")                             # nothing listens there
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], server.url + "/nope")                             # 404


def test_prompts():
    assert embed.query_text("flaky") == "task: search result | query: flaky"
    assert embed.doc_text("", "body") == "title: none | text: body"
    assert len(embed.doc_text("t", "x" * 5000)) == embed.DOC_CHARS


def test_documents_cover_every_kind(kbx):
    _, idx, _ = kbx
    docs = {(k, key): text for k, key, text in embed.documents(idx.db)}
    assert docs[("session", "c-1")] == "title: Voli Torino Londra | text: Searched flights.\nTags: travel\nFirst prompt: hello world"
    assert docs[("page", "pages/projects/demo.md")].startswith("title: ")
    assert docs[("memory", "memories/h/claude/demo/budget.md")] == ("title: budget | text: keep the cost low\n"
                                                                    "Prefer the cheaper model.")


def test_run_embed_is_incremental_and_follows_changes(kbx, server):
    root, idx, store = kbx
    ep = Endpoint(server.url, "", "m1")
    rep = embed.run_embed(idx.db, store, ep)
    assert (rep.done, rep.left, rep.error) == (5, 0, "")
    assert embed.run_embed(idx.db, store, ep).done == 0                       # nothing new
    put(root, "h/a.md", "a-1", title="Reduce cost of the e2e suite", summary="Cut the cost by half.")
    (root / "memories/h/claude/demo/budget.md").unlink()
    idx.update(root)
    rep = embed.run_embed(idx.db, store, ep)
    assert (rep.done, rep.removed) == (1, 1)                                  # the edited summary; the gone memory
    assert embed.run_embed(idx.db, store, Endpoint(server.url, "", "m2")).done == 4    # another model: all again
    assert store.counts("m2") == {"session": 3, "page": 1}


def test_run_embed_stops_at_deadline_limit_and_errors(kbx, server):
    _, idx, store = kbx
    ep = Endpoint(server.url, "", "m1")
    assert embed.run_embed(idx.db, store, ep, deadline=0, clock=lambda: 1).done == 0
    rep = embed.run_embed(idx.db, store, ep, limit=2)
    assert (rep.done, rep.left) == (2, 3)
    server.fail = True
    rep = embed.run_embed(idx.db, store, ep)
    assert rep.done == 0 and rep.left == 3 and rep.error
    assert sum(store.counts("m1").values()) == 2                              # finished batches stay


def test_store_round_trip_and_readonly(kbx, server, tmp_path):
    root, idx, store = kbx
    embed.run_embed(idx.db, store, Endpoint(server.url, "", "m1"))
    ro = embed.Vectors.open_readonly(root / ".kb" / "embeddings.sqlite")
    vecs = ro.load("session", "m1")
    assert set(vecs) == {"a-1", "b-1", "c-1"} and len(vecs["a-1"]) == 64
    assert list(vecs["a-1"]) == pytest.approx(fake_vector(embed.documents(idx.db)[0][2]), abs=1e-6)
    ro.close()
    assert embed.Vectors.open_readonly(tmp_path / "missing.sqlite") is None


def test_dense_ranks_and_filters():
    vecs = {"a": [1.0, 0.0], "b": [0.6, 0.8], "c": [0.0, 1.0]}
    assert embed.dense(vecs, [1.0, 0.0]) == ["a", "b", "c"]
    assert embed.dense(vecs, [1.0, 0.0], allowed={"b", "c"}, n=1) == ["b"]


def test_fuse_rewards_agreement_and_breaks_ties_by_key():
    assert fuse(["a", "b"], ["b", "c"], 3) == ["b", "a", "c"]
    assert fuse(["x"], ["y"], 2) == ["x", "y"]


def test_find_fuses_and_keeps_filters(kbx, server):
    root, idx, store = kbx
    ep = Endpoint(server.url, "", "m1")
    embed.run_embed(idx.db, store, ep)
    vecs = store.load("session", "m1")
    q = embed.embed([embed.query_text("cheaper spend")], server.url)[0]
    assert [h["id"] for h in idx.find("cheaper spend")] == []                   # BM25 alone: no shared word
    hits = idx.find("cheaper spend", dense=embed.dense(vecs, q, idx.keys("session")))
    assert hits[0]["id"] == "a-1" and hits[0]["snippet"].startswith("Cut the cost")
    f = Filters(project="other")
    hits = idx.find("cheaper spend", f, dense=embed.dense(vecs, q, idx.keys("session", f)))
    assert {h["id"] for h in hits} == {"b-1"}
    mem = idx.find_memories("cheaper", dense=embed.dense(store.load("memory", "m1"), q, idx.keys("memory")))
    assert mem[0]["name"] == "budget"
    pages = idx.find_pages("cheaper", dense=embed.dense(store.load("page", "m1"), q, idx.keys("page")))
    assert pages[0]["name"] == "demo"


def test_dense_none_keeps_todays_results(kbx):
    _, idx, _ = kbx
    assert idx.find("flaky", dense=None) == idx.find("flaky")
    assert idx.find("flaky", dense=[]) == idx.find("flaky")                    # an empty dense ranking: same order


def _cfg(root, **kw):
    from kb.config import Config
    cfg = Config(root=root, host="h")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_sync_step_embeds_and_never_fails_the_sync(kbx, server):
    from kb.sync import Report, embed_new
    root, idx, _ = kbx
    rep = Report()
    embed_new(_cfg(root, embed_url=server.url), idx, rep)
    assert rep.embedded == 5 and rep.notes == [] and "5 embedded" in rep.line()
    server.fail = True
    rep = Report()
    embed_new(_cfg(root, embed_url=server.url), idx, rep)                 # nothing new: no call at all
    assert rep.embedded == 0 and rep.notes == []
    put(root, "h/d.md", "d-1", title="New one")
    idx.update(root)
    rep = Report()
    embed_new(_cfg(root, embed_url=server.url), idx, rep)                 # server failing
    assert rep.embedded == 0 and rep.errors == [] and rep.notes[0].startswith("embed:") and rep.happened


def test_sync_step_installs_a_missing_pin_and_notes_failures(kbx, monkeypatch):
    from kb import embed_runtime
    from kb.sync import Report, embed_new
    root, idx, _ = kbx
    calls = []

    def install(progress=None):
        calls.append(1)
        raise embed_runtime.EmbedUnavailable("download failed: offline")
    monkeypatch.setattr(embed_runtime, "install", install)
    rep = Report()
    embed_new(_cfg(root, embed=True), idx, rep)
    assert calls == [1] and rep.notes == ["embed: download failed: offline"] and rep.errors == []
