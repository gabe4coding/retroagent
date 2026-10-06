import json
from collections import Counter

import pytest
from fixtures import AID, GH_TOKEN, SID, make_claude_tree

from kb.adapters import claude
from kb.catalog import write_catalog
from kb.distill import split_front_matter, update_front_matter
from kb.model import Session
from kb.store import PathCollision, write_session

MAIN = "sessions/h/claude/2026/10/2026-10-06_demo_55555555.md"
SUB = "sessions/h/claude/2026/10/2026-10-06_demo_55555555_sub-e94ad30f.md"


def _session(tmp_path):
    return claude.parse_unit(claude.discover(make_claude_tree(tmp_path / "src"))[0])


def test_write_session_creates_md_and_raw(tmp_path):
    root = tmp_path / "kb"
    red = Counter()
    written, sizes = write_session(root, "h", _session(tmp_path), red)
    assert written == [MAIN, SUB]
    md = (root / MAIN).read_text(encoding="utf-8")
    assert GH_TOKEN not in md and "[REDACTED:github-token]" in md
    assert "[subagent](2026-10-06_demo_55555555_sub-e94ad30f.md)" in md
    assert (root / f"raw/h/claude/2026/10/{SID}.jsonl.gz").exists()
    assert (root / f"raw/h/claude/2026/10/{SID}__sub-{AID}.jsonl.gz").exists()
    # the token in the tool call is now redacted inside first_line (before the cut), so only the raw copy counts it here
    assert red["github-token"] >= 1 and sizes["md"] > 0 and sizes["raw"] > 0


def test_summary_fields_survive_rewrite_and_move(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    update_front_matter(root / MAIN, {"summary": "S", "tags": ["x"], "summary_turns": 4})
    s.project = "renamed"                       # e.g. the session was relocated
    written, _ = write_session(root, "h", s, Counter(), known={SID: MAIN})
    new = "sessions/h/claude/2026/10/2026-10-06_renamed_55555555.md"
    assert written[0] == new and not (root / MAIN).exists()
    meta, _ = split_front_matter((root / new).read_text(encoding="utf-8"))
    assert meta["summary"] == "S" and meta["tags"] == ["x"]


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "kb"
    written, sizes = write_session(root, "h", _session(tmp_path), Counter(), dry_run=True)
    assert written and sizes["md"] > 0 and not root.exists()


def test_catalog(tmp_path):
    root = tmp_path / "kb"
    write_session(root, "h", _session(tmp_path), Counter())
    write_catalog(root, "h", {"2026/10"})
    rows = [json.loads(l) for l in (root / "catalog/h/2026-10.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == [SID, AID]           # sorted by start time
    assert rows[0]["md"] == MAIN and rows[0]["raw"].endswith(f"{SID}.jsonl.gz")
    assert rows[1]["parent"] == SID
    for f in list((root / "sessions").rglob("*.md")):
        f.unlink()
    write_catalog(root, "h", {"2026/10"})
    assert not (root / "catalog/h/2026-10.jsonl").exists()


# ---------------------------------------------------------------- short ids, collisions, moves, odd text

def _meta(root, rel):
    return split_front_matter((root / rel).read_text(encoding="utf-8"))[0]


def _simple(sid, started="2026-10-06T10:00:00Z", text="hello", parent="", agent="codex"):
    s = Session(id=sid, agent=agent, project="demo", started=started, ended=started, title="t " + sid, parent=parent)
    s.add_turn("user", started, [text])
    s.add_turn("assistant", started, ["ok"])
    return s


def test_uuidv7_sessions_that_share_their_first_eight_characters_do_not_overwrite_each_other(tmp_path):
    root = tmp_path / "kb"
    a = _simple("01a0d000-0000-7000-8000-5f3c9a1be7d2", text="first")
    b = _simple("01a0d000-0001-7123-9abc-0e4b7c2d91a6", text="second")
    (pa,), _ = write_session(root, "h", a, Counter())
    (pb,), _ = write_session(root, "h", b, Counter())
    assert pa != pb and pa.endswith("_9a1be7d2.md") and pb.endswith("_7c2d91a6.md")
    assert _meta(root, pa)["id"] == a.id and _meta(root, pb)["id"] == b.id
    assert "first" in (root / pa).read_text(encoding="utf-8") and "second" in (root / pb).read_text(encoding="utf-8")


def test_a_file_of_another_session_at_the_target_path_is_never_overwritten(tmp_path):
    root = tmp_path / "kb"
    mine = _simple("01a0d000-0000-7000-8000-5f3c9a1be7d2")
    other = _simple("77777777-0000-7000-8000-5f3c9a1be7d2")             # same tail, so the same file name
    (path,), _ = write_session(root, "h", other, Counter())
    before = (root / path).read_bytes()
    with pytest.raises(PathCollision) as e:
        write_session(root, "h", mine, Counter())
    assert path in str(e.value) and other.id in str(e.value) and mine.id in str(e.value)
    assert (root / path).read_bytes() == before
    assert not any(f.is_file() and f.name.startswith(mine.id) for f in (root / "raw").rglob("*"))   # no raw copy either
    with pytest.raises(PathCollision):                                   # a dry run reports the problem too
        write_session(root, "h", mine, Counter(), dry_run=True)
    write_session(root, "h", other, Counter())                           # the owner may still rewrite its own file
    assert (root / path).read_bytes() == before


def test_a_collision_stops_the_whole_session_before_anything_is_written(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)                                               # main 55555555 + subagent e94ad30f
    squatter = _simple("ffffffff-e94ad30f", parent=SID, agent="claude")   # another file where the subagent goes
    (path,), _ = write_session(root, "h", squatter, Counter())
    assert path == SUB
    with pytest.raises(PathCollision):
        write_session(root, "h", s, Counter())
    assert not (root / MAIN).exists() and not (root / f"raw/h/claude/2026/10/{SID}.jsonl.gz").exists()


def test_a_file_with_the_same_id_is_rewritten_in_place(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    s.turns[0].items[0] = "a changed first prompt"
    write_session(root, "h", s, Counter())
    assert "a changed first prompt" in (root / MAIN).read_text(encoding="utf-8")


def _moved(s, month="2026-11"):
    """The same session started in another month (e.g. a Codex session whose first record was dated later)."""
    for sess in [s] + s.subagents:
        sess.started = sess.started.replace("2026-10", month)
    return s


def test_a_session_that_moves_to_another_month_leaves_no_old_md_raw_or_catalog_rows(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    update_front_matter(root / MAIN, {"summary": "S", "tags": ["x"], "summary_turns": 4})
    write_catalog(root, "h", {"2026/10"})
    old_raw = [f"raw/h/claude/2026/10/{SID}.jsonl.gz", f"raw/h/claude/2026/10/{SID}__sub-{AID}.jsonl.gz"]
    assert all((root / r).exists() for r in old_raw) and (root / "catalog/h/2026-10.jsonl").exists()
    touched = set()
    written, _ = write_session(root, "h", _moved(s), Counter(), known={SID: MAIN, AID: SUB}, touched=touched)
    assert len(written) == 2 and all("/2026/11/" in w for w in written)           # still a 2-tuple, new paths
    assert not (root / MAIN).exists() and not (root / SUB).exists()
    assert not any((root / r).exists() for r in old_raw)
    assert (root / f"raw/h/claude/2026/11/{SID}.jsonl.gz").exists()
    assert (root / f"raw/h/claude/2026/11/{SID}__sub-{AID}.jsonl.gz").exists()
    assert touched == {"2026/10"}                                                  # the month to rebuild is reported
    meta = _meta(root, written[0])
    assert meta["summary"] == "S" and meta["tags"] == ["x"] and meta["summary_turns"] == 4
    write_catalog(root, "h", touched | {"2026/11"})
    assert not (root / "catalog/h/2026-10.jsonl").exists()
    rows = [json.loads(l) for l in (root / "catalog/h/2026-11.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == [SID, AID] and rows[0]["summary"] == "S"
    assert all("/2026/11/" in r["raw"] for r in rows)


def test_touched_is_optional_and_a_dry_run_changes_nothing(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    touched = set()
    write_session(root, "h", _moved(s), Counter(), dry_run=True, known={SID: MAIN, AID: SUB}, touched=touched)
    assert (root / MAIN).exists() and (root / f"raw/h/claude/2026/10/{SID}.jsonl.gz").exists() and touched == set()


def test_a_rename_inside_the_month_keeps_the_raw_file(tmp_path):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    s.project = "renamed"
    touched = set()
    write_session(root, "h", s, Counter(), known={SID: MAIN}, touched=touched)
    assert (root / f"raw/h/claude/2026/10/{SID}.jsonl.gz").exists() and not (root / MAIN).exists()


@pytest.mark.parametrize("hostile", ["../README.md", "raw/other-host/claude/2026/10/x.jsonl.gz", "/etc/hosts",
                                     "README.md", "raw/h/claude/2026/10/../../../../README.md", 5, ["x"]])
def test_a_raw_value_that_points_outside_this_hosts_raw_folder_is_never_deleted(tmp_path, hostile):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    (root / "README.md").write_text("keep me")
    (root / "raw/other-host/claude/2026/10").mkdir(parents=True)
    (root / "raw/other-host/claude/2026/10/x.jsonl.gz").write_text("keep me too")
    update_front_matter(root / MAIN, {"raw": hostile})
    write_session(root, "h", _moved(s), Counter(), known={SID: MAIN})
    assert (root / "README.md").read_text() == "keep me"
    assert (root / "raw/other-host/claude/2026/10/x.jsonl.gz").read_text() == "keep me too"
    assert not (root / MAIN).exists()                                              # the old markdown still moves


@pytest.mark.parametrize("old,new,want", [
    (("OLD", 4), ("", 0), ("OLD", 4)),          # a blank file at the new path must not win over a summarized old one
    (("OLD", 2), ("NEW", 6), ("NEW", 6)),
    (("OLD", 6), ("NEW", 2), ("OLD", 6)),
    (("", 0), ("NEW", 3), ("NEW", 3)),
    (("OLD", 3), ("NEW", 3), ("NEW", 3)),       # a tie keeps the file already at the new path
    (("", 0), ("", 0), ("", 0)),
])
def test_summary_comes_from_the_candidate_with_a_summary_and_the_higher_turn_count(tmp_path, old, new, want):
    root = tmp_path / "kb"
    s = _session(tmp_path)
    write_session(root, "h", s, Counter())
    if old[0]:
        update_front_matter(root / MAIN, {"summary": old[0], "tags": ["old"], "summary_turns": old[1]})
    _moved(s)
    path = write_session(root, "h", s, Counter())[0][0]                    # blank files at the new paths
    if new[0]:
        update_front_matter(root / path, {"summary": new[0], "tags": ["new"], "summary_turns": new[1]})
    assert (root / MAIN).exists()                                          # the old file is still there
    write_session(root, "h", s, Counter(), known={SID: MAIN})
    meta = _meta(root, path)
    assert (meta["summary"], meta["summary_turns"]) == want and not (root / MAIN).exists()
    assert meta["tags"] == ([] if not want[0] else ["old"] if want[0] == "OLD" else ["new"])


def test_a_lone_surrogate_in_the_text_does_not_stop_the_write(tmp_path):
    root = tmp_path / "kb"
    s = _simple("abcd1234-0000-0000-0000-000000000001", text="broken emoji \ud83d half")
    (path,), sizes = write_session(root, "h", s, Counter())
    text = (root / path).read_text(encoding="utf-8")
    assert "broken emoji" in text and "half" in text and sizes["md"] == len(text.encode("utf-8"))


def test_catalog_survives_a_lone_surrogate_in_front_matter(tmp_path):
    root = tmp_path / "kb"
    md = root / "sessions/h/claude/2026/10/2026-10-06_demo_00000001.md"
    md.parent.mkdir(parents=True)
    md.write_text('---\nid: "x-1"\nstarted: "2026-10-06T10:00:00Z"\ntitle: "half \\ud83d emoji"\nraw: ""\n---\n\nbody\n',
                  encoding="utf-8")
    write_catalog(root, "h", {"2026/10"})
    row = json.loads((root / "catalog/h/2026-10.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["id"] == "x-1" and row["title"].startswith("half ") and row["title"].endswith(" emoji")
