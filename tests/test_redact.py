import json
import random
import time

import pytest

from kb.redact import _run, redact

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


# ---------------------------------------------------------------- hardening (synthetic secrets, built from pieces)

AWS40 = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"          # 13 + 27 = 40 chars
HEX32 = "0123456789abcdef" + "fedcba9876543210"
B64_LINE = "QUJD" * 16                                           # one 64-char key line
BASIC_B64 = "dXNlcjpw" + "YXNzd29yZDEyMw=="                        # base64 of a made-up user:password pair
STRIPE = "sk_" + "live_" + "A1b2C3d4E5f6G7h8I9j0"
NPM = "npm_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8"
GITLAB = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
NOTION = "ntn_" + "A1b2C3d4E5f6G7h8I9j0" * 2
SENTRY = "sntrys_" + "eyJpYXQiOjE2" * 4
HF = "hf_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5"
SLACK_APP = "xapp-" + "1-A01B2C3D4E5-" + "1234567890123-abcdef"
SENDGRID = "SG." + "A1b2C3d4E5f6G7h8I9j0K1" + "." + "A1b2C3d4E5f6G7h8I9j0" * 2 + "K1l"   # 22 and 43 chars
GOOGLE_OAUTH = "ya29." + "a0AfH6SMBx" + "A1b2C3d4E5f6G7h8"
LINEAR = "lin_api_" + "A1b2C3d4E5f6G7h8I9j0" * 2
ATLASSIAN = "ATATT3" + "A1b2C3d4E5f6G7h8I9j0" * 3

NEW_POSITIVE = [
    # prefixed names (item 1)
    ("secret-assignment", "DB_PASSWORD=" + "hunter2xyz9", "hunter2xyz9"),
    ("secret-assignment", "SECRET_KEY=" + "abc123def4567890", "abc123def4567890"),
    ("secret-assignment", "STRIPE_API_KEY=" + "q1w2e3r4t5y6u7i8", "q1w2e3r4t5y6u7i8"),
    ("secret-assignment", "DD_API_KEY=" + HEX32, HEX32),
    ("secret-assignment", "AWS_SESSION_TOKEN=" + "FwoGZXIv" + "YXdzEJr1abcd", "YXdzEJr1abcd"),
    ("secret-assignment", '{"refresh_token": "' + "rt-8f3a2b1c9d7e" + '"}', "rt-8f3a2b1c9d7e"),
    ("secret-assignment", "export PRIVATE_KEY=" + "k3yMat3rial99", "k3yMat3rial99"),
    ("secret-assignment", "app-key: " + "a1b2c3d4e5f6", "a1b2c3d4e5f6"),
    ("secret-assignment", "export PGPASSWORD=" + "hunter2xyz9", "hunter2xyz9"),
    ("secret-assignment", "MYSQL_PWD=" + "s3cret", "s3cret"),
    # token formats (item 2)
    ("stripe-key", "key " + STRIPE, STRIPE[8:]),
    ("stripe-key", "key " + "rk_" + "test_" + "A1b2C3d4E5f6G7h8I9j0", "A1b2C3d4E5f6G7h8I9j0"),
    ("npm-token", "//registry.npmjs.org/:_authToken=" + NPM, NPM[4:]),
    ("gitlab-token", "token " + GITLAB, GITLAB[6:]),
    ("notion-token", "NOTION " + NOTION, NOTION[4:]),
    ("sentry-token", "SENTRY " + SENTRY, SENTRY[7:]),
    ("huggingface-token", "HF " + HF, HF[3:]),
    ("slack-app-token", "app " + SLACK_APP, SLACK_APP[5:]),
    ("sendgrid-key", "mail " + SENDGRID, SENDGRID[3:]),
    ("google-oauth-token", "oauth " + GOOGLE_OAUTH, GOOGLE_OAUTH[5:]),
    ("linear-key", "linear " + LINEAR, LINEAR[8:]),
    ("atlassian-token", "jira " + ATLASSIAN, ATLASSIAN[6:]),
    ("http-basic", "Authorization: Basic " + BASIC_B64, BASIC_B64[:22]),
    ("curl-user", "curl -u admin:" + "p4ssw0rd https://api.example.com/x", "p4ssw0rd"),
    ("curl-user", 'curl --user "admin:' + 'p4ssw0rd" https://api.example.com/x', "p4ssw0rd"),
    ("curl-user", "-u deploy:" + "s3cr3tvalue", "s3cr3tvalue"),
    # quoted and unquoted password values without a digit (item 4)
    ("secret-assignment", 'password="my secret phrase 99"', "my secret phrase"),
    ("secret-assignment", "DB_PASSWORD='correct horse'", "correct horse"),
    ("secret-assignment", 'password = "abcd"', "abcd"),
    ("secret-assignment", 'secret: "two words"', "two words"),
    ("secret-assignment", "password=correcthorse", "correcthorse"),
    ("secret-assignment", "password=abcdef", "abcdef"),
    ("secret-assignment", "DB_PASSWORD=battery-staple", "battery-staple"),
    # url credentials with an empty user (item 6)
    ("url-credentials", "redis://:" + "pw" + "@cache.local:6379", "pw@"),
]


@pytest.mark.parametrize("rule,text,secret", NEW_POSITIVE)
def test_new_rules_redact(rule, text, secret):
    out, counts = redact(text)
    assert secret not in out
    assert f"[REDACTED:{rule}]" in out
    assert counts[rule] >= 1
    assert redact(out)[0] == out                      # redacting twice changes nothing more


def test_keep_group_is_preserved():
    assert redact("Authorization: Basic " + BASIC_B64)[0] == "Authorization: Basic [REDACTED:http-basic]"
    assert redact("curl -u admin:p4ssw0rd https://x.io")[0] == "curl -u admin:[REDACTED:curl-user] https://x.io"
    assert redact("redis://:pw@host")[0] == "redis://:[REDACTED:url-credentials]@host"
    assert redact('DB_PASSWORD="my secret phrase 99" run')[0] == 'DB_PASSWORD="[REDACTED:secret-assignment]" run'


NEW_NEGATIVE = [
    "PWD=/Users/x/Repos/acme-web",
    "npm_config_user_agent=npm/10.2.4 node/v20.10.0 darwin arm64",
    "fix/sk-1234-implement-the-new-feature-flow",
    "feature/sk-learn-pipeline-refactor-step2",
    "Bearer authentication-scheme-middleware",
    "max_tokens: 4096",
    "tokens = 12345678",
    "password: ${DB_PASSWORD}",
    'password="${DB_PASSWORD}"',
    "password=$DB_PASSWORD_FROM_ENV",
    "password: <your-password-here>",
    "password=********",
    "password: {{ vault_password }}",
    "password=[REDACTED:secret-assignment]",
    "password=/run/secrets/db_password",
    "password=~/.secrets/db_password",
    "password=short",
    'password="abc"',
    "the secret: somewords",
    "curl -u admin https://api.example.com",
    "https://example.com:8080/a@b",
    "-----BEGIN PRIVATE KEY-----",
    "Starts with -----BEGIN RSA PRIVATE KEY----- and that is all",
    "-----BEGIN PRIVATE KEY-----\nshort",
    "-----BEGIN CERTIFICATE-----\n" + B64_LINE + "\n-----END CERTIFICATE-----",
    # review fixes: command lines, code and prose that looked like secrets
    "date -u +%Y-%m-%dT%H:%M:%SZ",
    "ts=$(date -u +%Y%m%d:%H%M%S)",
    "docker run -u 1000:1000 alpine id",
    "docker run --user 1000:1000 alpine id",
    "docker run -u '1000:1000' alpine id",
    "const password = process.env.DB_PASSWORD;",
    "password: req.body.password",
    "passwd=ENV.database_password",
    "password = ENV.x",
    "password=config.db.user_password,",
    "password=process.env.X",
    "HTTP Basic authentication is required",
    "Basic authentication",
    "uses Basic configuration for the proxy",
    "-----BEGIN PRIVATE KEY----- the quick brown fox jumps over the lazy dog and keeps on running far away",
    "-----BEGIN RSA PRIVATE KEY-----\nthis is only a prose line that mentions the marker and has many words in it",
]


@pytest.mark.parametrize("text", NEW_NEGATIVE)
def test_new_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


def test_assignment_does_not_eat_the_next_line():
    text = "password:\nnext-line-value-1234567890"
    assert redact(text)[0] == text
    text = "token=\nnext-line-value-1234567890"
    assert redact(text)[0] == text


JSON_PAYLOADS = [
    {"cmd": "echo password=\nabcdefgh12345"},
    {"cmd": "echo token=\\n123456789012"},
    {"cmd": "export SECRET_KEY=abc123def456\nnext line"},
    {"env": "aws_secret_access_key=" + AWS40 + "\nrest"},
    {"a": 'password="my secret phrase 99"\nmore'},
    {"a": 'password="two\\nlines 99"\nmore'},
    {"nested": json.dumps({"password": "hunter2hunter2", "n": 1})},
    {"cmd": 'x --password="s3cretvalue1" y'},
    {"token": 12345678901, "secret": 1234567890123, "password": None, "api_key": True},
    {"password": "my secret phrase 99", "token": "abc12345def"},
    {"k": "-----BEGIN PRIVATE KEY-----\n" + B64_LINE + "\n" + B64_LINE + "\n-----END PRIVATE KEY-----\n"},
    {"k": "-----BEGIN PRIVATE KEY-----\n" + B64_LINE + "\n" + B64_LINE, "after": "ok"},
    {"u": "postgres://admin:s3cretPass@db.local/x", "v": "redis://:pw@cache"},
    {"h": "curl -u admin:p4ssw0rd https://x.io\nnext", "i": "Authorization: Basic " + BASIC_B64 + "\n"},
]


@pytest.mark.parametrize("payload", JSON_PAYLOADS)
def test_redaction_keeps_serialized_json_valid(payload):
    line = json.dumps(payload)
    out, _ = redact(line)
    data = json.loads(out)
    assert isinstance(data, dict) and set(data) == set(payload)


def test_json_redaction_removes_the_secrets_it_is_meant_to():
    secrets = ["hunter2hunter2", "s3cretvalue1", "my secret phrase", "abc12345def", AWS40, "s3cretPass",
               "p4ssw0rd", BASIC_B64[:22], B64_LINE]
    out, _ = redact(json.dumps([p for p in JSON_PAYLOADS]))
    for s in secrets:
        assert s not in out
    assert json.loads(out)[-3]["after"] == "ok"


def test_private_key_is_bounded_and_does_not_join_separate_strings():
    line = json.dumps(["-----BEGIN PRIVATE KEY-----", "keep this text", "-----END PRIVATE KEY-----"])
    assert redact(line)[0] == line
    md = ("### Assistant\nhere is -----BEGIN PRIVATE KEY----- the header\n\n### User\nnice one\n\n"
          "-----END PRIVATE KEY----- footer")
    assert redact(md)[0] == md


def test_private_key_with_pem_headers_pgp_and_limits():
    pgp = ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\n" + B64_LINE + "\n" + B64_LINE
           + "\n=abcd\n-----END PGP PRIVATE KEY BLOCK-----")
    out, counts = redact(pgp)
    assert out == "[REDACTED:private-key]" and counts["private-key"] == 1
    enc = "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B2C\n\n" + B64_LINE \
          + "\n-----END RSA PRIVATE KEY-----"
    assert redact(enc)[0] == "[REDACTED:private-key]"
    big = "-----BEGIN PRIVATE KEY-----\n" + "A" * 19_000 + "\n-----END PRIVATE KEY-----"
    assert redact(big)[0] == "[REDACTED:private-key]"
    text = "a -----BEGIN PRIVATE KEY-----\n" + B64_LINE + "\n-----END PRIVATE KEY-----\nb -----BEGIN PRIVATE KEY-----\n" \
           + B64_LINE + "\n-----END PRIVATE KEY----- c"
    assert redact(text)[0] == "a [REDACTED:private-key]\nb [REDACTED:private-key] c"


def test_unterminated_private_key_is_redacted():
    text = "key:\n-----BEGIN OPENSSH PRIVATE KEY-----\n" + B64_LINE + "\n" + B64_LINE
    out, counts = redact(text)
    assert "QUJD" not in out and "BEGIN" not in out and counts["private-key"] == 1 and out.startswith("key:\n")
    # JSON-escaped newlines, cut by a quote
    line = '{"k":"-----BEGIN PRIVATE KEY-----\\n' + B64_LINE + '\\n' + B64_LINE + '","n":"ok"}'
    out, _ = redact(line)
    assert "QUJD" not in out and json.loads(out)["n"] == "ok"
    # key material split over short lines
    short = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(["MIIEvQIB" + "ADAN"] * 5)
    assert "ADAN" not in redact(short)[0]


def test_private_key_scan_stays_linear_with_many_markers():
    text = "-----BEGIN PRIVATE KEY----- " * 30_000
    t0 = time.perf_counter()
    redact(text)
    assert time.perf_counter() - t0 < 2


ADVERSARIAL = [
    ("hyphens", "a-" * 500_000),
    ("dots", "a." * 500_000),
    ("letters", "x" * 1_000_000),
    ("assignments", "password=" * 100_000),
]


@pytest.mark.parametrize("name,line", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_redact_is_fast_on_adversarial_lines(name, line):
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 2


@pytest.mark.parametrize("name,line", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_every_rule_is_linear_without_the_prefilter(name, line):
    t0 = time.perf_counter()
    _run(line, False)                                 # runs all rules, even where a prefilter would skip them
    assert time.perf_counter() - t0 < 2


ADVERSARIAL_MORE = [
    "-----BEGIN PRIVATE KEY----- " * 30_000, "Bearer a " * 100_000, "a://b:" * 100_000, "-u a " * 200_000,
    'password="' * 100_000, "password='a" * 100_000, "Basic " * 100_000, "-----BEGIN " + "A " * 500_000,
    "a-" * 500_000 + "://u:p@h", 'password="' + "a" * 1_000_000, "token=1" * 100_000, "token=" * 170_000, 'password=\\"' * 100_000,
]


@pytest.mark.parametrize("line", ADVERSARIAL_MORE, ids=[str(i) for i in range(len(ADVERSARIAL_MORE))])
def test_every_rule_is_linear_on_more_adversarial_lines(line):
    t0 = time.perf_counter()
    _run(line, False)
    assert time.perf_counter() - t0 < 2


def test_the_prefilter_never_changes_the_result():
    corpus = ([t for _, t, _ in POSITIVE] + [t for _, t, _ in NEW_POSITIVE] + NEGATIVE + NEW_NEGATIVE
              + [json.dumps(p) for p in JSON_PAYLOADS] + [json.dumps(JSON_PAYLOADS)])
    for text in corpus:
        assert redact(text) == _run(text, False), text
    rng = random.Random(7)
    seps = [" ", "\n", '"', "'", "\\n", "\t", ", ", "="]
    for _ in range(400):
        text = "".join(rng.choice(corpus) + rng.choice(seps) for _ in range(rng.randint(1, 5)))
        assert redact(text) == _run(text, False), text


def test_url_scheme_is_bounded():
    assert redact("postgres://admin:s3cretPass@db.local/x")[0] == "postgres://admin:[REDACTED:url-credentials]@db.local/x"
    assert redact("mongodb+srv://u:p4ss@cluster.example.net")[0] == "mongodb+srv://u:[REDACTED:url-credentials]@cluster.example.net"


_FUZZ_PIECES = [
    "password", "PASSWORD", "token", "secret", "api_key", "DB_PASSWORD", "refresh_token", "=", ":", ": ", " = ", '"', "'",
    "\\", "\n", "\t", " ", "  ", "abc12345", "hunter2hunter2", "my phrase 99", "12345678901", "$", "[", "{", "<", "~", "*",
    "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----", B64_LINE, "QUJD" * 4, "Bearer ", "Basic ",
    BASIC_B64, " -u ", "--user ", "admin:", "http://", "redis://", "u:p@h", "@", "/", "\u00e9", "\u2028",
    GH, "aws_secret_access_key", "A" * 40, ",", ";", ")", "}", "\r", '"password":"', 'password\\":\\"', '\\"', "\\\\",
    "\\n", "\\u00e9", 'token":', "secret'", "=\\'", "-----BEGIN RSA PRIVATE KEY-----\n", "QUJDQUJD",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
]


def test_redaction_never_breaks_serialized_json_on_random_text():
    rng = random.Random(2026)
    for i in range(60_000):
        s = "".join(rng.choice(_FUZZ_PIECES) for _ in range(rng.randint(2, 14)))
        doc = rng.choice([
            {"a": s}, {"password": s, "n": 1}, [s, {"token": s}], {"x": json.dumps({"password": s, "t": s})},
            {"k": rng.choice(_FUZZ_PIECES), "v": s, "w": [1, 2.5, None, True]},
            {"x": json.dumps(json.dumps({"password": s, "t": s}))},
        ])
        line = json.dumps(doc, ensure_ascii=rng.random() < 0.5, separators=rng.choice([(",", ":"), (", ", ": ")]))
        out, _ = redact(line)
        try:
            json.loads(out)
        except ValueError:
            pytest.fail(f"case {i} broke the JSON:\n  in : {line[:300]!r}\n  out: {out[:300]!r}")


REVIEW_POSITIVE = [
    ("curl-user", "curl -u 1a2b:p4ssw0rd https://x.io", "p4ssw0rd"),
    ("curl-user", "curl -u 'root:p4ssw0rd' https://x.io", "p4ssw0rd"),
    ("curl-user", "curl --user 5000x:p4ssw0rd https://x.io", "p4ssw0rd"),
    ("secret-assignment", "password=hunter.2xyz9abc", "hunter.2xyz9abc"),
    ("secret-assignment", "PGPASSWORD=correcthorse", "correcthorse"),
    ("http-basic", "Authorization: Basic " + "dGVzdHVzZXI6dGVzdHBhc3M=", "dGVzdHVzZXI6"),
    ("http-basic", "Authorization: Basic " + "YWJjZGVmZ2hpamtsbW5vcA==", "YWJjZGVmZ2hpamts"),
    ("http-basic", "Basic " + "QWxhZGRpbjpvcGVuIHNlc2FtZQ==", "QWxhZGRpbjpv"),
    ("private-key", "-----BEGIN PRIVATE KEY-----\n" + B64_LINE + "\n" + B64_LINE, "QUJD"),
    ("private-key", "key: |\n  -----BEGIN PRIVATE KEY-----\n  " + B64_LINE + "\n  " + B64_LINE, "QUJD"),
    ("private-key", "-----BEGIN PRIVATE KEY-----\r\n" + B64_LINE + "\r\n" + B64_LINE, "QUJD"),
]


@pytest.mark.parametrize("rule,text,secret", REVIEW_POSITIVE)
def test_the_tighter_rules_still_redact_real_secrets(rule, text, secret):
    out, counts = redact(text)
    assert secret not in out and counts[rule] >= 1
    assert redact(out)[0] == out


def test_curl_user_guard_keeps_the_rest_of_the_command_line_intact():
    assert redact("docker run -u 1000:1000 -e A=1 img")[0] == "docker run -u 1000:1000 -e A=1 img"
    assert redact("date -u +%FT%T; curl -u admin:p4ssw0rd https://x.io")[0] == \
        "date -u +%FT%T; curl -u admin:[REDACTED:curl-user] https://x.io"


def test_new_rules_agree_with_and_without_the_prefilter():
    for _, text, _ in REVIEW_POSITIVE:
        assert redact(text) == _run(text, False), text
