import gzip
import json
import random
import string
from collections import Counter

import pytest
from fixtures import GH_TOKEN, T1, make_claude_tree, make_codex_tree, write_jsonl

import kb.slimraw as slimraw
from kb.slimraw import CAP, slim


def _lines(data):
    # split on "\n" only: U+2028 and friends are valid characters inside a JSON line
    lines = gzip.decompress(data).decode().split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


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


# ---------------------------------------------------------------- leaf-level redaction (synthetic secrets)

def _slim_one(tmp_path, line, agent="claude"):
    f = tmp_path / "one.jsonl"
    f.write_text(line + "\n", encoding="utf-8", errors="surrogatepass")
    data, counts = slim(agent, [str(f)])
    return _lines(data), counts


def test_secret_straddling_the_cap_is_redacted(tmp_path):
    text = "a" * (CAP - 10) + " " + GH_TOKEN + " tail words"
    rec = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}}
    lines, counts = _slim_one(tmp_path, json.dumps(rec))
    assert "ghp_" not in lines[0] and GH_TOKEN[4:14] not in lines[0]
    assert counts["github-token"] == 1
    out = json.loads(lines[0])["message"]["content"][0]["content"]
    assert out.startswith("a" * (CAP - 10) + " [REDACTED")        # the placeholder is what gets cut, not the token
    assert out.endswith("chars cut]") and len(out) < CAP + 100


def test_double_encoded_codex_arguments_are_redacted(tmp_path):
    args1 = json.dumps({"cmd": "curl -X POST -d '{\"password\":\"" + "hunter2" + "hunter2\"}' https://x.example/login"})
    args2 = json.dumps({"cmd": 'mytool --password="' + "s3cret" + 'value1" --verbose'})
    recs = [{"timestamp": "2026-09-22T09:41:00.000Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command", "arguments": a, "call_id": "c"}}
            for a in (args1, args2)]
    f = write_jsonl(tmp_path / "codex.jsonl", recs)
    data, counts = slim("codex", [str(f)])
    lines = _lines(data)
    assert len(lines) == 2
    for line in lines:
        assert "hunter2hunter2" not in line and "s3cretvalue1" not in line
        rec = json.loads(line)
        json.loads(rec["payload"]["arguments"])             # the inner JSON is still valid
    assert counts["secret-assignment"] >= 2


def test_lone_surrogate_does_not_crash(tmp_path):
    line = '{"type":"user","message":{"content":"bad \\ud83d here and ' + GH_TOKEN + '"}}'
    lines, counts = _slim_one(tmp_path, line)
    rec = json.loads(lines[0])
    assert rec["message"]["content"].startswith("bad ") and "here and [REDACTED:github-token]" in lines[0]
    assert counts["github-token"] == 1


def test_unparsable_giant_line_is_redacted_then_capped(tmp_path):
    text = "not json " + GH_TOKEN + " " + "word " * 600_000
    lines, counts = _slim_one(tmp_path, text)
    assert len(lines) == 1 and len(lines[0]) < CAP + 100
    assert lines[0].startswith("not json [REDACTED:github-token] word ") and lines[0].endswith("chars cut]")
    assert counts["github-token"] == 1


def test_unparsable_line_with_secret_before_the_cap_boundary(tmp_path):
    text = "q " * ((CAP - 5) // 2) + " " + GH_TOKEN + " end"
    lines, _ = _slim_one(tmp_path, text)
    assert "ghp_" not in lines[0]


def test_deeply_nested_json_does_not_crash(tmp_path):
    text = "[" * 5000 + '"' + GH_TOKEN + '"' + "]" * 5000
    lines, counts = _slim_one(tmp_path, text)
    assert len(lines) == 1 and "ghp_" not in lines[0] and counts["github-token"] == 1


def test_json_lines_that_are_not_objects_are_kept(tmp_path):
    for agent in ("claude", "codex"):
        assert [_slim_one(tmp_path, line, agent)[0] for line in ("null", "1")] == [["null"], ["1"]]


def test_nested_encrypted_content_is_dropped_at_any_depth(tmp_path):
    rec = {"type": "response_item", "payload": {"type": "message", "encrypted_content": "ENC1",
           "content": [{"a": {"b": [{"encrypted_content": "ENC2", "keep": "yes"}]}}], "x": {"encrypted_content": "ENC3"}}}
    lines, _ = _slim_one(tmp_path, json.dumps(rec), agent="codex")
    assert "ENC" not in lines[0] and "encrypted_content" not in lines[0]
    assert json.loads(lines[0])["payload"]["content"][0]["a"]["b"] == [{"keep": "yes"}]


def test_data_uri_leaves_are_omitted(tmp_path):
    img = "data:image/png;base64," + "iVBORw0KGgo" * 200
    rec = {"type": "user", "message": {"content": [{"type": "image_url", "image_url": {"url": img}},
                                                    {"type": "text", "text": "data:text/plain,hello world"}]}}
    lines, _ = _slim_one(tmp_path, json.dumps(rec))
    parts = json.loads(lines[0])["message"]["content"]
    assert parts[0]["image_url"]["url"] == "[data omitted]" and "iVBOR" not in lines[0]
    assert parts[1]["text"] == "data:text/plain,hello world"          # short data: strings stay


def test_line_level_redaction_catches_secrets_that_need_the_key(tmp_path):
    rec = {"type": "user", "env": {"DB_PASSWORD": "hunter2hunter2", "password": "my secret phrase 99"}}
    lines, counts = _slim_one(tmp_path, json.dumps(rec))
    assert "hunter2hunter2" not in lines[0] and "my secret phrase" not in lines[0]
    assert json.loads(lines[0])["env"]["password"] == "[REDACTED:secret-assignment]"
    assert counts["secret-assignment"] == 2


def test_backstop_that_would_break_json_is_ignored(tmp_path, monkeypatch):
    real = slimraw.redact

    def broken_for_whole_lines(text):
        if text.startswith("{") and text.endswith("}"):
            return text + "}", Counter({"x": 1})        # would no longer parse
        return real(text)

    monkeypatch.setattr(slimraw, "redact", broken_for_whole_lines)
    rec = {"type": "user", "message": {"content": "export T=" + GH_TOKEN + " now"}}
    lines, counts = _slim_one(tmp_path, json.dumps(rec))
    assert json.loads(lines[0])["message"]["content"] == "export T=[REDACTED:github-token] now"
    assert "x" not in counts


# ---------------------------------------------------------------- fuzz

_ALNUM = string.ascii_letters + string.digits
_FILLER = ["line1\nline2", "tab\there", 'say "hi"', "it's fine", "back\\slash", "caf\u00e9 \U0001f600", "line\u2028sep", "a=b: c", "{}[]()",
           "password", "token", "api_key is documented here", "x" * 40, "", " ", "\n\n", "C:\\path\\to"]
_KEYS = ["password", "token", "api_key", "secret", "note", "cmd", "name", "count", "items", "DB_PASSWORD",
         "refresh_token", "k", "tokens", "max_tokens"]
_SECRET_KEYS = ["password", "token", "api_key", "secret", "DB_PASSWORD", "refresh_token"]


def _tok(rng, n, alphabet=_ALNUM):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _plain_secret(rng):
    return _tok(rng, rng.randint(8, 12)) + str(rng.randint(0, 9)) + _tok(rng, rng.randint(4, 12))


def _prefixed_secret(rng):
    makers = [lambda: "ghp_" + _tok(rng, 36), lambda: "AKIA" + _tok(rng, 16, string.ascii_uppercase + string.digits),
              lambda: "sk-ant-" + _tok(rng, 30), lambda: "npm_" + _tok(rng, 36), lambda: "glpat-" + _tok(rng, 24),
              lambda: "xoxb-" + _tok(rng, 12, string.digits) + "-" + _tok(rng, 12), lambda: "hf_" + _tok(rng, 34)]
    return rng.choice(makers)()


def _scalar(rng):
    kind = rng.randint(0, 7)
    if kind == 0:
        return rng.randint(-10**13, 10**13)
    if kind == 1:
        return rng.random() * 1e6
    if kind == 2:
        return rng.choice([True, False, None])
    return "".join(rng.choice(_FILLER) for _ in range(rng.randint(1, 3)))


def _value(rng, depth, secrets):
    kind = rng.randint(0, 9)
    if depth > 3 or kind < 4:
        return _scalar(rng)
    if kind < 6:
        return [_value(rng, depth + 1, secrets) for _ in range(rng.randint(0, 3))]
    return _dict(rng, depth + 1, secrets)


def _planted(rng, secrets):
    """A (key-or-None, value) pair that carries a secret in one of several shapes."""
    shape = rng.randint(0, 6)
    if shape == 0:                                          # key context
        s = _plain_secret(rng)
        secrets.append(s)
        return rng.choice(_SECRET_KEYS), s
    if shape == 1:                                          # prefixed token in free text
        s = _prefixed_secret(rng)
        secrets.append(s)
        return rng.choice(_KEYS), f"{rng.choice(_FILLER)} export X={s}\n{rng.choice(_FILLER)}\t"
    if shape == 2:                                          # quoted password phrase
        s = _tok(rng, 5) + " " + _tok(rng, 6) + str(rng.randint(0, 9))
        secrets.append(s)
        return rng.choice(["password", "DB_PASSWORD", "secret"]), s
    if shape == 3:                                          # bearer header in a command
        s = _plain_secret(rng) + _tok(rng, 4)
        secrets.append(s)
        return "cmd", f"curl -H 'Authorization: Bearer {s}' \"https://x.example\"\n"
    if shape == 4:                                          # url with credentials
        s = _tok(rng, 10)
        secrets.append(s)
        return "cmd", f"psql postgres://admin:{s}@db.example.com:5432/app -c 'select 1'\n"
    if shape == 5:                                          # double-encoded JSON in a string
        s = _plain_secret(rng)
        secrets.append(s)
        return "arguments", json.dumps({"cmd": f"echo hi", "password": s, "n": rng.randint(0, 5)})
    s = _plain_secret(rng)                                  # NAME=value assignment in a shell command
    secrets.append(s)
    return "cmd", f"export {rng.choice(['API_KEY', 'DB_PASSWORD', 'SECRET_KEY', 'AUTH_TOKEN'])}={s} && run\n"


def _dict(rng, depth, secrets):
    out = {}
    for _ in range(rng.randint(1, 4)):
        if rng.random() < 0.3:
            k, v = _planted(rng, secrets)
        else:
            k, v = rng.choice(_KEYS), _value(rng, depth, secrets)
        out[k] = v
    return out


def test_fuzz_slim_output_is_valid_json_and_secret_free(tmp_path):
    rng = random.Random(20261006)
    records, planted = [], []
    for i in range(2000):
        secrets = []
        body = _dict(rng, 0, secrets)
        if not secrets:
            k, v = _planted(rng, secrets)
            body[k] = v
        records.append({"type": "assistant", "uuid": f"u{i}", "message": {"content": [{"type": "x", "data": body}]}})
        planted.append(secrets)
    f = write_jsonl(tmp_path / "fuzz.jsonl", [json.dumps(r, ensure_ascii=rng.random() < 0.5) for r in records])
    lines = _lines(slim("claude", [str(f)])[0])
    assert len(lines) == len(records)
    for i, (line, secrets) in enumerate(zip(lines, planted)):
        json.loads(line)                                    # must stay valid JSON
        for s in secrets:
            assert s not in line, f"record {i} leaked {s!r}: {line[:300]}"
