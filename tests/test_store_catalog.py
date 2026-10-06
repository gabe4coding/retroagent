import json
from collections import Counter

from fixtures import AID, GH_TOKEN, SID, make_claude_tree

from kb.adapters import claude
from kb.catalog import write_catalog
from kb.distill import split_front_matter, update_front_matter
from kb.store import write_session

MAIN = "sessions/h/claude/2026/10/2026-10-06_demo_11111111.md"
SUB = "sessions/h/claude/2026/10/2026-10-06_demo_11111111_sub-a1d0ec8d.md"


def _session(tmp_path):
    return claude.parse_unit(claude.discover(make_claude_tree(tmp_path / "src"))[0])


def test_write_session_creates_md_and_raw(tmp_path):
    root = tmp_path / "kb"
    red = Counter()
    written, sizes = write_session(root, "h", _session(tmp_path), red)
    assert written == [MAIN, SUB]
    md = (root / MAIN).read_text(encoding="utf-8")
    assert GH_TOKEN not in md and "[REDACTED:github-token]" in md
    assert "[subagent](2026-10-06_demo_11111111_sub-a1d0ec8d.md)" in md
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
    new = "sessions/h/claude/2026/10/2026-10-06_renamed_11111111.md"
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
