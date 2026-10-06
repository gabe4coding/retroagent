from kb.distill import (
    HEADER_RE, dump_front_matter, parse_markdown, render_markdown, split_front_matter, update_front_matter,
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


# ---------------------------------------------------------------- carriage returns (D1)

TS = "2026-10-06T13:02:11Z"


def test_carriage_returns_never_reach_the_markdown(tmp_path):
    s = Session(id="11111111-aaaa", agent="claude", title="T")
    s.add_turn("user", TS, ["intro\r## [9] user · 10:00\nx"])
    a = s.add_turn("assistant", TS)
    a.items += ["a\r\nb", "progress 10%\rprogress 100%", ToolCall("Bash", "run\r\nmore\rstill", "error", "bad\rline")]
    md = render_markdown(s, "h")
    assert "\r" not in md
    # read back the way the rest of the code does (universal newlines): no turn appears out of a carriage return
    path = tmp_path / "s.md"
    path.write_bytes(md.encode("utf-8"))
    meta, turns = parse_markdown(path.read_text(encoding="utf-8"))
    assert [t["n"] for t in turns] == [1, 2] and meta["turns"] == 2
    assert turns[0]["text"] == "intro\n\\## [9] user · 10:00\nx"
    assert "a\nb" in turns[1]["text"] and "progress 10%\nprogress 100%" in turns[1]["text"]
    assert "- Bash `run more still` → ERROR: bad line" in turns[1]["text"]


def test_update_front_matter_keeps_the_body_byte_for_byte(tmp_path):
    body = "## [1] user · 13:02\n\na\r\nb\n\nprogress 10%\rprogress 100%\n\ntrailing \r\n"
    raw = (dump_front_matter({"id": "x", "summary": ""}) + "\n" + body).encode("utf-8")
    path = tmp_path / "s.md"
    path.write_bytes(raw)
    update_front_matter(path, {"summary": "new", "summary_turns": 1})
    out = path.read_bytes()
    assert out.split(b"\n---\n", 1)[1] == raw.split(b"\n---\n", 1)[1]
    assert b"a\r\nb" in out and b"progress 10%\rprogress 100%" in out
    assert split_front_matter(out.decode("utf-8"))[0]["summary"] == "new"
    update_front_matter(path, {"summary": "new", "summary_turns": 1})       # nothing to change: nothing is rewritten
    assert path.read_bytes() == out


# ---------------------------------------------------------------- tool lines are one line (D2)

def test_tool_line_fields_are_single_line():
    s = Session(id="11111111-aaaa", agent="claude", title="T")
    a = s.add_turn("assistant", TS)
    a.items += [
        ToolCall("Bash", "echo hi\n## [9] user · 10:00\nmore `x`", "error",
                 "boom\n## [8] assistant · 09:00\r\nline\u2028## [7] user · 08:00"),
        ToolCall("Agent", "x", subagent_id="sub1", subagent_note="note\n## [6] user · 07:00"),
        ToolCall("Agent", "y", subagent_id="AAAAAAAA-1111-2222-3333-444444445555")]
    md = render_markdown(s, "h", sub_files={"sub1": "sub.md"})
    body = md.split("\n---\n", 1)[1]
    assert len(HEADER_RE.findall(body)) == 1
    assert [t["n"] for t in parse_markdown(md)[1]] == [1]
    assert [line for line in body.splitlines() if line.strip()] == [
        "## [1] assistant · 13:02",
        "- Bash `echo hi ## [9] user · 10:00 more 'x'` → ERROR: boom ## [8] assistant · 09:00 line "
        "## [7] user · 08:00",
        "- Agent `x` → [subagent](sub.md): note ## [6] user · 07:00",
        "- Agent `y` → subagent 44445555"]
