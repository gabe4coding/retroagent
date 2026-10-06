import json

import pytest

from kb.redact import redact

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

POSITIVE = [
    ("github-token", f"token is {GH} ok", GH),
    ("github-token", "github_pat_" + "A1b2" * 16, "github_pat_"),
    ("anthropic-key", "key sk-ant-api03-" + "Ab1_" * 10, "sk-ant-"),
    ("openai-key", "OPENAI sk-proj-" + "Zz9-" * 8, "sk-proj-"),
    ("aws-access-key", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("slack-token", "xoxb-1234567890-abcdefghij", "xoxb-"),
    ("slack-webhook", "https://hooks.slack.com/services/T000/B000/XXXX", "hooks.slack.com/services/T000"),
    ("google-api-key", "AIza" + "B" * 35, "AIza"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "eyJhbGci"),
    ("bearer", "Authorization: Bearer abcdef0123456789abcdef", "abcdef0123456789abcdef"),
    ("url-credentials", "postgres://admin:s3cretPass@db.local:5432/x", "s3cretPass"),
    ("aws-secret", "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("secret-assignment", "password=hunter2xyz9", "hunter2xyz9"),
    ("secret-assignment", 'API_KEY: "abc123def456"', "abc123def456"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
]


@pytest.mark.parametrize("rule,text,secret", POSITIVE)
def test_rules_redact(rule, text, secret):
    out, counts = redact(text)
    assert secret not in out
    assert f"[REDACTED:{rule}]" in out
    assert counts[rule] >= 1


NEGATIVE = [
    "commit 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c",
    "id 11111111-2222-3333-4444-555555555555",
    "see https://github.com/me/demo/pull/7",
    "max_tokens: 4096",
    "tokens = 12345678",
    "password: ${DB_PASSWORD}",
    "http://localhost:8080/path",
    "the secret: somewords",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_no_false_positive(text):
    out, counts = redact(text)
    assert out == text and not counts


def test_redaction_keeps_json_valid():
    line = json.dumps({"cmd": f"export GH={GH}", "env": {"password": "s3cretValue1"}})
    out, _ = redact(line)
    data = json.loads(out)
    assert GH not in out and "s3cretValue1" not in out
    assert "[REDACTED:github-token]" in data["cmd"]
