import gzip
import json

from fixtures import GH_TOKEN, T1, make_claude_tree, make_codex_tree, write_jsonl

from kb.slimraw import CAP, slim


def _lines(data):
    return gzip.decompress(data).decode().splitlines()


def test_claude_slim_drops_attachments_redacts_and_is_deterministic(tmp_path):
    projects = make_claude_tree(tmp_path)
    main = next(projects.rglob("11111111-*.jsonl"))
    data, counts = slim("claude", [str(main)])
    lines = _lines(data)
    text = "\n".join(lines)
    assert not any('"type":"attachment"' in l for l in lines)
    assert GH_TOKEN not in text and counts["github-token"] >= 1
    assert "this line is not json" in lines          # unparsable lines are kept (redacted)
    assert "private thoughts" in text                # raw keeps thinking
    assert slim("claude", [str(main)])[0] == data      # gzip mtime=0 -> same bytes


def test_images_and_long_strings(tmp_path):
    big = "z" * (CAP + 500)
    f = write_jsonl(tmp_path / "x.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
            {"type": "tool_result", "tool_use_id": "t", "content": big}]}}])
    rec = json.loads(_lines(slim("claude", [str(f)])[0])[0])
    parts = rec["message"]["content"]
    assert parts[0] == {"type": "image", "omitted": True}
    assert len(parts[1]["content"]) < CAP + 100 and parts[1]["content"].endswith("[… 500 chars cut]")


def test_codex_slim_drops_encrypted_and_instructions(tmp_path):
    sessions, _ = make_codex_tree(tmp_path)
    seg = next(sessions.rglob(f"*{T1}.jsonl"))
    text = gzip.decompress(slim("codex", [str(seg)])[0]).decode()
    assert "ENCRYPTED" not in text and "very long system prompt" not in text
    assert "Add a retry to the fetch client" in text
