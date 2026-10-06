from kb.distill import (
    dump_front_matter, parse_markdown, render_markdown, split_front_matter, update_front_matter,
)
from kb.model import Session, ToolCall


def _session():
    s = Session(id="11111111-aaaa", agent="claude", cwd="/r/demo", project="demo", branch="main",
                started="2026-10-06T13:02:11Z", ended="2026-10-06T13:12:00Z", model="m", title="T",
                files=["src/a.ts"], prs=["https://x/pull/1"])
    s.add_turn("user", "2026-10-06T13:02:11Z", ["Do it\n## [9] user · 10:00 fake header"])
    a = s.add_turn("assistant", "2026-10-06T13:03:00Z")
    a.items += ["Working.", ToolCall("Bash", "npm `test`", "error", "boom"),
                ToolCall("Edit", "src/a.ts", diff="+3 −2"),
                ToolCall("Agent", "Explore", subagent_id="agent1", subagent_note="Found it"), "Done."]
    return s


def test_front_matter_round_trip():
    meta = {"id": "x", "title": 'quote " and\nnewline', "tags": ["a", "b"], "turns": 3, "zzz": True}
    text = dump_front_matter(meta) + "\nbody\n"
    back, body = split_front_matter(text)
    assert back == meta and body == "body\n"
    assert text.splitlines()[1] == 'id: "x"'


def test_render_and_parse():
    md = render_markdown(_session(), "host1", keep={"summary": "S", "tags": ["t"], "summary_turns": 2},
                         sub_files={"agent1": "sub.md"}, raw="raw/x.jsonl.gz")
    meta, turns = parse_markdown(md)
    assert meta["host"] == "host1" and meta["summary"] == "S" and meta["tags"] == ["t"]
    assert meta["turns"] == 2 and meta["user_turns"] == 1 and meta["raw"] == "raw/x.jsonl.gz"
    assert [t["n"] for t in turns] == [1, 2]
    assert turns[0]["time"] == "13:02"
    assert "\\## [9] user · 10:00 fake header" in turns[0]["text"]
    body = turns[1]["text"]
    assert "- Bash `npm 'test'` → ERROR: boom" in body
    assert "- Edit `src/a.ts` (+3 −2)" in body
    assert "- Agent `Explore` → [subagent](sub.md): Found it" in body
    assert body.index("Working.") < body.index("- Bash") < body.index("Done.")


def test_update_front_matter_keeps_body(tmp_path):
    p = tmp_path / "s.md"
    p.write_text(render_markdown(_session(), "h"), encoding="utf-8")
    before = p.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    update_front_matter(p, {"summary": "new", "summary_turns": 2})
    text = p.read_text(encoding="utf-8")
    assert text.split("\n---\n", 1)[1] == before
    assert split_front_matter(text)[0]["summary"] == "new"


def test_front_matter_values_with_unicode_line_separators_round_trip():
    meta = {"id": "x", "title": "before\u2028after", "summary": "a\u2029b\u0085c", "turns": 2}
    text = dump_front_matter(meta) + "\nbody\n"
    back, body = split_front_matter(text)
    assert back == meta and body == "body\n"
