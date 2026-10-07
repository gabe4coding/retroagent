import json
import random
import re
import string
import time
from collections import Counter

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
    # the markers are not joined into one block: the text between them stays. Only the marker itself is neutralized
    # (item 6: PRIVATE KEY becomes PRIVATE-KEY), so no scanner reads it as a key.
    line = json.dumps(["-----BEGIN PRIVATE KEY-----", "keep this text", "-----END PRIVATE KEY-----"])
    assert redact(line)[0] == json.dumps(["-----BEGIN PRIVATE-KEY-----", "keep this text", "-----END PRIVATE-KEY-----"])
    md = ("### Assistant\nhere is -----BEGIN PRIVATE KEY----- the header\n\n### User\nnice one\n\n"
          "-----END PRIVATE KEY----- footer")
    assert redact(md)[0] == md.replace("PRIVATE KEY", "PRIVATE-KEY")


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
    text = "-----BEGIN PRIVATE KEY----- " * 3_000
    t0 = time.perf_counter()
    redact(text)
    assert time.perf_counter() - t0 < 1


ADVERSARIAL = [
    ("hyphens", "a-" * 50_000),
    ("dots", "a." * 50_000),
    ("letters", "x" * 100_000),
    ("assignments", "password=" * 10_000),
]


@pytest.mark.parametrize("name,line", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_redact_is_fast_on_adversarial_lines(name, line):
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 1


@pytest.mark.parametrize("name,line", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_every_rule_is_linear_without_the_prefilter(name, line):
    t0 = time.perf_counter()
    _run(line, False)                                 # runs all rules, even where a prefilter would skip them
    assert time.perf_counter() - t0 < 1


ADVERSARIAL_MORE = [
    "-----BEGIN PRIVATE KEY----- " * 3_000, "Bearer a " * 10_000, "a://b:" * 10_000, "-u a " * 20_000,
    'password="' * 10_000, "password='a" * 10_000, "Basic " * 10_000, "-----BEGIN " + "A " * 50_000,
    "a-" * 50_000 + "://u:p@h", 'password="' + "a" * 100_000, "token=1" * 10_000, "token=" * 17_000, 'password=\\"' * 10_000,
]


@pytest.mark.parametrize("line", ADVERSARIAL_MORE, ids=[str(i) for i in range(len(ADVERSARIAL_MORE))])
def test_every_rule_is_linear_on_more_adversarial_lines(line):
    t0 = time.perf_counter()
    _run(line, False)
    assert time.perf_counter() - t0 < 1


def test_the_prefilter_never_changes_the_result():
    corpus = ([t for _, t, _ in POSITIVE] + [t for _, t, _ in NEW_POSITIVE] + NEGATIVE + NEW_NEGATIVE
              + [json.dumps(p) for p in JSON_PAYLOADS] + [json.dumps(JSON_PAYLOADS)]
              + [t for t, _, _ in FLAG_CASES] + FLAG_NEGATIVE + [t for _, t, _ in SUFFIX_POSITIVE] + SUFFIX_NEGATIVE
              + [t for _, t, _ in VENDOR_CASES] + VENDOR_NEGATIVE)
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
    "--password ", "--token=", "--pass '", "mysql -p", "mysql -u root -p'", "sshpass -p ", "redis-cli -a ", "az login -p ",
    "AccountKey=", "GOCSPX-", "xoxe-", "_PROD=", "apiKeyProd", "mysql", "\\n",
    # misses found by gitleaks: credentials, credential flags, URL query strings, a bare key, JWTs, private-key markers
    "credentials: ", "CREDENTIALS_PROD=", "--credentials-login=", "--api-token-value ", "--login-token '", "?key=", "&token=",
    "&amp;sig=", "x-amz-signature=", "key: ", "key=", "eyJhbGciOiJIUzI1NiI.", ".c29tZS1vcGFxdWUtcGF5bG9hZA.", "-----BEGIN RSA PRIVATE KEY-----",
    "-----END PGP PRIVATE KEY BLOCK-----", "Zk3" + "a" * 24, "#", ";",
]


def test_redaction_never_breaks_serialized_json_on_random_text():
    rng = random.Random(2026)
    for i in range(10_000):
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


# ---------------------------------------------------------------- password flags, suffixed names, Azure/Google/Slack formats

PW = "Hunter" + "22x"                       # a made-up password
PW_SPACE = "my S3cret" + " pw 9"           # a made-up password with spaces
MYPW = "S3cret" + "Pw9"
FLAG = "[REDACTED:password-flag]"
GEN = "[REDACTED:secret-assignment]"      # a name rule that runs first keeps its own name for `--password=...`

# (text, the secret in it, the exact expected output): the flag stays, only the value goes
FLAG_CASES = [
    (f"mysql -p{MYPW}", MYPW, f"mysql -p{FLAG}"),
    (f"mysql -u root -p{MYPW} appdb", MYPW, f"mysql -u root -p{FLAG} appdb"),
    (f"mariadb -h db.local -uroot -p{MYPW} -e 'select 1'", MYPW, f"mariadb -h db.local -uroot -p{FLAG} -e 'select 1'"),
    (f"mysqldump -u root -p{MYPW} appdb > dump.sql", MYPW, f"mysqldump -u root -p{FLAG} appdb > dump.sql"),
    (f"/usr/bin/mysql -p{MYPW}", MYPW, f"/usr/bin/mysql -p{FLAG}"),
    (f"docker exec -i mysql mysql -uroot -p{MYPW}", MYPW, f"docker exec -i mysql mysql -uroot -p{FLAG}"),
    (f"mysql -u root -p'{PW_SPACE}' appdb", PW_SPACE, f"mysql -u root -p'{FLAG}' appdb"),
    (f'mysql -u root -p"{PW_SPACE}" appdb', PW_SPACE, f'mysql -u root -p"{FLAG}" appdb'),
    (f"mysql -e 'select 1' -p{MYPW}", MYPW, f"mysql -e 'select 1' -p{FLAG}"),
    (f"--password {PW}", PW, f"--password {FLAG}"),
    (f"--password={PW}", PW, f"--password={GEN}"),
    (f"--passwd {PW}", PW, f"--passwd {FLAG}"),
    (f"--passwd={PW}", PW, f"--passwd={GEN}"),
    (f"--pass {PW}", PW, f"--pass {FLAG}"),
    (f"--token {PW}", PW, f"--token {FLAG}"),
    (f"--secret {PW}", PW, f"--secret {FLAG}"),
    (f"--api-key {PW}", PW, f"--api-key {FLAG}"),
    (f"--api-key={PW}", PW, f"--api-key={GEN}"),
    (f"--access-token {PW}", PW, f"--access-token {FLAG}"),
    (f"--client-secret={PW}", PW, f"--client-secret={GEN}"),
    (f"docker login -u me --password {PW} registry.local", PW, f"docker login -u me --password {FLAG} registry.local"),
    (f"tool --password '{PW_SPACE}' run", PW_SPACE, f"tool --password '{FLAG}' run"),
    (f'tool --password="{PW_SPACE}" run', PW_SPACE, f'tool --password="{GEN}" run'),
    # `=` forms that no name rule reaches: another name, or a value that is short or has no digit
    (f"--pass={PW}", PW, f"--pass={FLAG}"),
    ("--password=" + "ab", "ab", f"--password={FLAG}"),
    ("--token=" + "abc", "abc", f"--token={FLAG}"),
    ("--secret=" + "Hunter", "Hunter", f"--secret={FLAG}"),
    ("--password='" + "ab" + "'", "ab", f"--password='{FLAG}'"),
    (f'--pass="{PW_SPACE}"', PW_SPACE, f'--pass="{FLAG}"'),
    ("--api-key=" + "abc", "abc", f"--api-key={FLAG}"),
    ("--auth-token " + "abc", "abc", f"--auth-token {FLAG}"),
    ("--secret-key=" + "abc", "abc", f"--secret-key={FLAG}"),
    # a flag before and after, and two commands on one line
    (f"cmd --pass {PW} --token {PW}", PW, f"cmd --pass {FLAG} --token {FLAG}"),
    (f"mysql -u a -p{MYPW} -e 'x'; redis-cli -a {MYPW} ping", MYPW, f"mysql -u a -p{FLAG} -e 'x'; redis-cli -a {FLAG} ping"),
    # two flags of one command: the first match has used up the command word, so the rule runs again for the next one
    (f"mysql -u a -p{MYPW} -p{MYPW}", MYPW, f"mysql -u a -p{FLAG} -p{FLAG}"),
    (f"redis-cli -a {PW} -a {PW} ping", PW, f"redis-cli -a {FLAG} -a {FLAG} ping"),
    (f"az login -p {PW} --foo -p {PW}", PW, f"az login -p {FLAG} --foo -p {FLAG}"),
    (f"mysqldump -p{MYPW} db | mysql -p{MYPW} db2", MYPW, f"mysqldump -p{FLAG} db | mysql -p{FLAG} db2"),
    (f"sshpass -p '{PW_SPACE}' ssh me@host", PW_SPACE, f"sshpass -p '{FLAG}' ssh me@host"),
    (f"sshpass -p {PW} ssh me@host", PW, f"sshpass -p {FLAG} ssh me@host"),
    (f"sshpass -p{PW} ssh me@host", PW, f"sshpass -p{FLAG} ssh me@host"),
    (f"sshpass -p {PW} ssh -p 2222 me@host", PW, f"sshpass -p {FLAG} ssh -p 2222 me@host"),
    (f"redis-cli -a {PW}", PW, f"redis-cli -a {FLAG}"),
    (f"redis-cli -h cache.local -p 6379 -a {PW} ping", PW, f"redis-cli -h cache.local -p 6379 -a {FLAG} ping"),
    (f"redis-cli -a '{PW_SPACE}' ping", PW_SPACE, f"redis-cli -a '{FLAG}' ping"),
    (f"az login --service-principal -u app-id -p {PW} --tenant t", PW,
     f"az login --service-principal -u app-id -p {FLAG} --tenant t"),
    (f"az acr login -n reg -u me -p '{PW_SPACE}'", PW_SPACE, f"az acr login -n reg -u me -p '{FLAG}'"),
    (f"sudo az login -u me -p {PW}", PW, f"sudo az login -u me -p {FLAG}"),
]


@pytest.mark.parametrize("text,secret,expected", FLAG_CASES, ids=[str(i) for i in range(len(FLAG_CASES))])
def test_password_flags_are_redacted_and_the_flag_stays(text, secret, expected):
    out, counts = redact(text)
    assert out == expected and secret not in out
    assert sum(counts.values()) == len(re.findall(r"\[REDACTED:", expected))     # one count per placeholder
    assert redact(out)[0] == out                                   # idempotent
    assert redact(text) == _run(text, False)                       # the prefilter changes nothing


@pytest.mark.parametrize("text,secret,expected", FLAG_CASES, ids=[str(i) for i in range(len(FLAG_CASES))])
def test_password_flags_inside_serialized_json(text, secret, expected):
    line = json.dumps({"cmd": text, "n": 1})
    out, _ = redact(line)
    data = json.loads(out)                                         # still valid JSON
    assert data == {"cmd": expected, "n": 1}


def test_password_flag_does_not_cross_a_json_escaped_line_break():
    line = json.dumps({"cmd": "az login\nssh -p 2222 me@host\nredis-cli -h x\ngrep -a needle"})
    assert redact(line)[0] == line
    line = json.dumps({"cmd": f"mysql -u root\nls -p{MYPW}"})
    assert redact(line)[0] == line


FLAG_NEGATIVE = [
    # short options that are not a password
    "mkdir -p /tmp/a/b",
    "docker run -p 8080:80 nginx",
    "ssh -p 2222 me@host",
    "grep -a needle file.txt",
    "git log -p",
    "git log -p --stat main",
    "ls -pla",
    # the mysql family without a value, or with a port: the client prompts, or docker publishes a port
    "mysql -p",
    "mysql -u root -p appdb",
    "mysql -h db.local -P 3306 -u root",
    "mysql --port=3306 -u root",
    "docker run --name mysql -p 3306:3306 mysql:8",
    "docker run -d --name mysql -p3306:3306 mysql:8",
    "docker run -d --name mysql -p127.0.0.1:3306:3306 mysql:8",
    "systemctl restart mysql; ssh -p2222 me@host",
    "service mysql start && tar -pxf a.tar",
    "cat my.cnf | mysql_config | grep -pfoo",
    # other tools keep their short options
    "redis-cli -h cache.local -p 6379 ping",
    "redis-cli -a",
    "redis-cli -p 6379 -a --no-auth-warning ping",
    "redis-cli -h x; grep -a needle f",
    "az login; ssh -p 2222 me@host",
    "az account show && ssh -p 2222 me@host",
    "az vm list | grep -p x",
    "sshpass -p $SSHPASS ssh -p 2222 me@host",
    'sshpass -p "$DB_PASSWORD" ssh me@host',
    "sshpass -e ssh -p 2222 me@host",
    "sshpass -f /run/secrets/pw ssh -p 2222 me@host",
    # long flags that hold a placeholder, a path, another flag or no value
    "docker login --password-stdin",
    "docker login -u me --password-stdin registry.local",
    "--password $DB_PASSWORD",
    "--password ${DB_PASSWORD}",
    '--password "$DB_PASSWORD"',
    "--password=$DB_PASSWORD",
    "--token=$GITHUB_TOKEN",
    "--token ${{ secrets.GITHUB_TOKEN }}",
    "--token <your-token>",
    "--api-key {api_key}",
    "--password ********",
    "--password=[REDACTED:secret-assignment]",
    "--password /run/secrets/db_password",
    "--secret id=npm,src=.npmrc",
    "--password -u",
    "--token --verbose",
    "--password=",
    "--password ''",
    "--with-token",
    "--passwordless",
    "--tokenizer bert-base-uncased",
    "--secrets-file x.env",
    "--api-keys list",
    "parser.add_argument('--token', help='the token')",
    'parser.add_argument("--password", required=True)',
    # prose that names the flag
    "pass the --password flag to the script",
    "use --token to authenticate",
    "the --secret option is required",
    "set --pass and --token as arguments",
    "`--password` flag",
]


@pytest.mark.parametrize("text", FLAG_NEGATIVE, ids=[str(i) for i in range(len(FLAG_NEGATIVE))])
def test_password_flag_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


SUFFIX_POSITIVE = [
    ("secret-assignment", "DB_PASSWORD_PROD=" + "hunter2xyz9", "hunter2xyz9"),
    ("secret-assignment", "export GITHUB_TOKEN_RO=" + "r0t0k3n" + "abcd", "r0t0k3nabcd"),
    ("secret-assignment", "SECRET_KEY_BASE=" + HEX32, HEX32),
    ("secret-assignment", '"apiKeyProd": "' + "abc123" + 'def456"', "abc123def456"),
    ("secret-assignment", '{"clientSecretProd": "' + "abc123" + 'def456"}', "abc123def456"),
    ("secret-assignment", 'api_key_staging = "' + "abc123" + 'def456"', "abc123def456"),
    ("secret-assignment", "AUTH_TOKEN_STAGING: " + "abc123" + "def456", "abc123def456"),
    ("secret-assignment", "tokenSecret: " + "abc123" + "def456", "abc123def456"),
    ("secret-assignment", "apiKey2: " + "abc123" + "def456", "abc123def456"),
    ("secret-assignment", "TOKEN_" + "A" * 19 + "=" + "abc123" + "def456", "abc123def456"),          # 20-character suffix
    ("secret-assignment", "PASSWORD_2: " + "hunter.2xyz9abc", "hunter.2xyz9abc"),                    # a dot is not always code
    ("secret-assignment", "process.env.PASSWORD_2 = '" + "abc123" + "def456'", "abc123def456"),    # a string assigned to a suffixed name
]


@pytest.mark.parametrize("rule,text,secret", SUFFIX_POSITIVE, ids=[str(i) for i in range(len(SUFFIX_POSITIVE))])
def test_names_with_a_suffix_after_the_keyword(rule, text, secret):
    out, counts = redact(text)
    assert secret not in out and counts[rule] >= 1
    assert redact(out)[0] == out
    assert redact(text) == _run(text, False)
    json.loads(redact(json.dumps({"a": text}))[0])


SUFFIX_NEGATIVE = [
    "tokens = 12345678",
    "TOKENS = 12345678",
    "max_tokens: 4096",
    "max_tokens_per_request: 123456789",
    "MAX_TOKENS_PER_MINUTE=1234567890",
    "passwords: abc12345678",
    "api_keys = abc12345678",
    "password: ${DB_PASSWORD}",
    "DB_PASSWORD_PROD: ${DB_PASSWORD_PROD}",
    "GITHUB_TOKEN_RO=$GH_RO_TOKEN",
    "SECRET_KEY_BASE=<redacted>",
    "SECRET_KEY_BASE=****************",
    "apiKeyProd: ${API_KEY_PROD}",
    "SECRET_KEY_BASE: process.env.SECRET_KEY_BASE",
    "PASSWORD_2: process.env.PASSWORD_2 };",                # a dotted code reference with a digit is code, not a value
    "SECRET_KEY_BASE_V2 = process.env.SECRET_KEY_BASE_V2",
    "const x = { TOKEN_RO: config.secrets.token_ro2, a: 1 }",
    'apiKeyProd: req.body.apiKey2,',
    "secret_key_base = config.secrets.secret_key_base",
    "PWD=/Users/x/Repos/acme-web",
    "TOKEN_" + "A" * 20 + "=abc123def456",                  # 21-character suffix: too long to be a name
    "api_key_prod=short",
    '"secretProd": 12345678901',                            # a JSON number
    '"tokenCount": 12345678901',
]


@pytest.mark.parametrize("text", SUFFIX_NEGATIVE, ids=[str(i) for i in range(len(SUFFIX_NEGATIVE))])
def test_suffix_rule_keeps_the_false_positive_guards(text):
    out, counts = redact(text)
    assert out == text and not counts


AZURE_KEY = "A1b2C3d4E5f6G7h8I9j0" * 4 + "K1l2M3n4" + "=="                  # 88 characters
GOCSPX = "GOC" + "SPX-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
SLACK_XOXE = "xox" + "e-1-" + "A1b2C3d4E5f6G7h8I9j0"

VENDOR_CASES = [
    ("azure-account-key", "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + AZURE_KEY + ";EndpointSuffix=core.windows.net",
     "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=[REDACTED:azure-account-key];EndpointSuffix=core.windows.net"),
    ("azure-account-key", "AccountKey=" + AZURE_KEY, "AccountKey=[REDACTED:azure-account-key]"),
    ("azure-account-key", '{"cs": "AccountKey=' + AZURE_KEY + '"}', '{"cs": "AccountKey=[REDACTED:azure-account-key]"}'),
    ("azure-account-key", "AccountKey=" + "A" * 40, "AccountKey=[REDACTED:azure-account-key]"),
    ("google-client-secret", "client_secret " + GOCSPX + " ok", "client_secret [REDACTED:google-client-secret] ok"),
    ("google-client-secret", '{"installed": {"x": "' + GOCSPX + '"}}', '{"installed": {"x": "[REDACTED:google-client-secret]"}}'),
    ("google-client-secret", "GOCSPX-" + "a" * 20, "[REDACTED:google-client-secret]"),
    ("slack-token", "SLACK " + SLACK_XOXE, "SLACK [REDACTED:slack-token]"),
    ("slack-token", "SLACK " + "xox" + "p-1234567890-abcdefghij", "SLACK [REDACTED:slack-token]"),
    ("slack-token", "SLACK " + "xox" + "r-1234567890-abcdefghij", "SLACK [REDACTED:slack-token]"),
    ("slack-token", "SLACK " + "xox" + "s-1234567890-abcdefghij", "SLACK [REDACTED:slack-token]"),
]


@pytest.mark.parametrize("rule,text,expected", VENDOR_CASES, ids=[str(i) for i in range(len(VENDOR_CASES))])
def test_azure_google_and_slack_formats(rule, text, expected):
    out, counts = redact(text)
    assert out == expected and counts[rule] == 1
    assert redact(out)[0] == out
    assert redact(text) == _run(text, False)
    json.loads(redact(json.dumps({"a": text}))[0])


VENDOR_NEGATIVE = [
    "AccountKey=" + "A" * 39,
    "AccountKey=${STORAGE_KEY}",
    "AccountKey=<key>",
    "AccountKeyVault=" + "A" * 50,
    "GOCSPX-" + "a" * 19,
    "GOCSPX is the prefix of a client secret",
    "xoxe-short",
    "xoxe is a token family",
    "xoxz-1234567890-abcdefghij",
]


@pytest.mark.parametrize("text", VENDOR_NEGATIVE, ids=[str(i) for i in range(len(VENDOR_NEGATIVE))])
def test_vendor_format_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


ADVERSARIAL_FLAGS = [
    "mysql " * 17_000, "mariadb -u " * 9_000, "mysql -p" * 12_000, "mysql -p " * 11_000, "mysqldump " * 10_000,
    ("mysql " + "x" * 70 + " ") * 1_400, ("mysql " + "x" * 79 + " ") * 1_200, "mysql " + "-u a " * 25_000,
    "az " * 30_000, "az -p " * 16_000, ("az " + "x" * 79 + " ") * 1_200, "redis-cli " * 10_000, "redis-cli -a " * 8_000,
    "sshpass " * 12_000, "sshpass -p " * 9_000, "sshpass -x " * 9_000,
    "--password " * 9_000, "--password=" * 9_000, "--password='" * 8_000, "--token " * 12_000, "--pass " * 14_000,
    "--password " + "a" * 100_000, "--password='" + "a" * 100_000, "--password=" + "a" * 100_000,
    "--password \"" + "a b " * 25_000, "mysql -p'" + "a" * 100_000, "-p" * 50_000, "-a " * 33_000,
    "--api-key " * 9_000, "--access-token " * 6_000, "--client-secret=" * 6_000,
    "AccountKey=" * 9_000, "AccountKey=" + "A" * 100_000, "AccountKey=" + "A" * 39 + " ", ("AccountKey=" + "A" * 39 + " ") * 2_000,
    "GOCSPX-" * 14_000, "GOCSPX-" + "a" * 100_000, "xoxe-" * 20_000, "xoxe-" + "a" * 100_000,
    "token_" * 17_000, "secret_key_base" * 6_000, "api_key" * 14_000, "TOKEN_" + "A" * 100_000,
    "token" + "a" * 100_000, "password_" * 11_000, "apiKeyProd" * 10_000, "token_a=" * 12_000, "secret_" + "x" * 15 + "=" + " " * 10_000,
    # the dotted-identifier guard of the suffix rule
    "token_x=" + "a." * 50_000, "token_x=a." * 10_000, "token_x=" + "a" * 100_000, "token_x=" + "a.b" * 30_000 + "=",
    ("token_x=a.b" + "c" * 80) * 1_100, "token_x=a.b=" * 8_000, "password_2=" + "a." * 45_000 + "9",
    # one command with a very long run of flags: a rule runs at most 3 times
    "mysql " + "-pa " * 25_000, "redis-cli " + "-a a " * 20_000, "az " + "-p a " * 20_000, "mysql -pa " * 10_000,
    "az -p a " * 12_000, "redis-cli -a a " * 7_000, "sshpass -p a " * 8_000,
]


@pytest.mark.slow                                     # about 4 s for the whole list
@pytest.mark.parametrize("line", ADVERSARIAL_FLAGS, ids=[str(i) for i in range(len(ADVERSARIAL_FLAGS))])
def test_new_rules_are_linear_on_adversarial_lines(line):
    t0 = time.perf_counter()
    _run(line, False)                                 # all rules, no prefilter
    assert time.perf_counter() - t0 < 1
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 1


def test_flag_rules_keep_the_names_of_the_rules_that_run_first():
    # `--password=...` that a name rule already catches is still counted as secret-assignment (the slimraw counts rely on it)
    assert redact(f"--password={PW}")[1] == {"secret-assignment": 1}
    assert redact(f"--password {PW}")[1] == {"password-flag": 1}
    assert redact(f"--pass={PW}")[1] == {"password-flag": 1}


# ---------------------------------------------------------------- misses found by gitleaks on real data
# Every value is made up at run time (never one realistic literal).

def _mixed(n, seed):
    """A made-up token of n characters with at least one upper-case letter, one lower-case letter and one digit."""
    rng = random.Random(seed)
    chars = [rng.choice(string.ascii_uppercase), rng.choice(string.ascii_lowercase), rng.choice(string.digits)]
    chars += [rng.choice(string.ascii_letters + string.digits) for _ in range(n - 3)]
    rng.shuffle(chars)
    return "".join(chars)


def _hex(n, seed):
    rng = random.Random(seed)
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _check(text, secret, expected):
    out, counts = redact(text)
    assert out == expected and secret not in out
    assert sum(counts.values()) == len(re.findall(r"\[REDACTED:", expected))        # one count per placeholder
    assert redact(out)[0] == out                                                  # idempotent
    assert redact(text) == _run(text, False)                                      # the prefilter changes nothing
    assert json.loads(redact(json.dumps({"a": text}))[0])["a"] == expected         # a serialized line stays valid JSON


# --- item 1: the word "credentials" names a secret
CRED = _mixed(12, 11) + "-" + _mixed(16, 12) + "-" + _mixed(8, 13)                 # dashes and digits, like a pasted token
CRED_CASES = [
    ("use this credentials: " + CRED, "use this credentials: [REDACTED:secret-assignment]"),
    ("credentials=" + CRED, "credentials=[REDACTED:secret-assignment]"),
    ("my credential: " + CRED + " ok", "my credential: [REDACTED:secret-assignment] ok"),
    ('{"credentials": "' + CRED + '"}', '{"credentials": "[REDACTED:secret-assignment]"}'),
    ("credentials: '" + CRED + "'", "credentials: '[REDACTED:secret-assignment]'"),
    ("CREDENTIALS_PROD=" + CRED, "CREDENTIALS_PROD=[REDACTED:secret-assignment]"),
    ("credentialsStaging: " + CRED, "credentialsStaging: [REDACTED:secret-assignment]"),
    ("export GOOGLE_CREDENTIALS=" + CRED, "export GOOGLE_CREDENTIALS=[REDACTED:secret-assignment]"),
]


@pytest.mark.parametrize("text,expected", CRED_CASES, ids=[str(i) for i in range(len(CRED_CASES))])
def test_the_word_credentials_names_a_secret(text, expected):
    _check(text, CRED, expected)


CRED_NEGATIVE = [
    "credentials: include",
    "credentials: true",
    "credentials: 'same-origin'",
    "credentials=short1",
    "credentials: abcdefghijkl",                                  # no digit
    "credentials: ${GOOGLE_CREDENTIALS_1234}",
    "credentials: <your-credentials-1>",
    "credentials: ********1",
    "credentials: [REDACTED:secret-assignment]",
    '"credentials": 12345678901',                                 # a JSON number
    "GOOGLE_APPLICATION_CREDENTIALS=/Users/me/.config/gcloud/creds2024.json",
    "credentials: ~/.aws/credentials2",
    "credentialsFile: ./creds-2024.json",
    "credentials = google.auth.default2()",
    "credentials = service_account.Credentials.from_info2",
    "credentials: config.google.credentials2,",
    "credentials=https://example.com/a1b2c3d4",
    "the credentials: somewords",
    "no credentials 12345678 were provided",
    "credentials are stored in a vault",
    "credentialsabcdefghijklmnopqrstuvwxyz: abcdefgh12345",       # a name longer than the 20-character suffix
]


@pytest.mark.parametrize("text", CRED_NEGATIVE, ids=[str(i) for i in range(len(CRED_NEGATIVE))])
def test_credentials_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


# --- item 2: command-line flags with a credential word anywhere in their name
FLAG_HEX = _hex(70, 21)
FLAG_VAL = _mixed(24, 22)
FLAG_VAL2 = _mixed(16, 23)
FLAG2_CASES = [
    (f"--credentials-login='{FLAG_HEX}'", FLAG_HEX, f"--credentials-login='{FLAG}'"),
    (f"--credentials-login={FLAG_HEX}", FLAG_HEX, f"--credentials-login={FLAG}"),
    (f"--credentials-login {FLAG_HEX}", FLAG_HEX, f"--credentials-login {FLAG}"),
    (f'--credentials-login "{FLAG_HEX}"', FLAG_HEX, f'--credentials-login "{FLAG}"'),
    (f"--credential={FLAG_VAL}", FLAG_VAL, f"--credential={GEN}"),                            # a name rule runs first
    (f"--api-token-value={FLAG_VAL}", FLAG_VAL, f"--api-token-value={FLAG}"),
    (f"--api-token-value {FLAG_VAL}", FLAG_VAL, f"--api-token-value {FLAG}"),
    (f"--api_token_value={FLAG_VAL}", FLAG_VAL, f"--api_token_value={GEN}"),                  # a name with a suffix: a name rule
    (f"--client-secret {FLAG_VAL}", FLAG_VAL, f"--client-secret {FLAG}"),
    (f"--client-secret={FLAG_VAL}", FLAG_VAL, f"--client-secret={GEN}"),                  # a name rule runs first
    (f"--my-service-password-prod={FLAG_VAL}", FLAG_VAL, f"--my-service-password-prod={FLAG}"),
    (f"--db-passwd-file-content {FLAG_VAL}", FLAG_VAL, f"--db-passwd-file-content {FLAG}"),
    (f"--login-token {FLAG_VAL}", FLAG_VAL, f"--login-token {FLAG}"),
    (f"--db-apikey={FLAG_VAL}", FLAG_VAL, f"--db-apikey={GEN}"),
    (f"--db-api-key-prod {FLAG_VAL}", FLAG_VAL, f"--db-api-key-prod {FLAG}"),
    (f"--login={FLAG_VAL}", FLAG_VAL, f"--login={FLAG}"),
    (f"--secret-id {FLAG_VAL}", FLAG_VAL, f"--secret-id {FLAG}"),
    ("--token-x='" + "p4ss " + "w0rd 99'", "p4ss w0rd 99", f"--token-x='{FLAG}'"),            # a quoted value may hold spaces
    (f"tool --credentials-login={FLAG_HEX} --verbose run", FLAG_HEX, f"tool --credentials-login={FLAG} --verbose run"),
    (f"tool --api-token-value {FLAG_VAL} --login-token {FLAG_VAL2} x", FLAG_VAL,
     f"tool --api-token-value {FLAG} --login-token {FLAG} x"),
    (f"run.sh --credentials-login={FLAG_VAL};echo done", FLAG_VAL, f"run.sh --credentials-login={FLAG};echo done"),
]


@pytest.mark.parametrize("text,secret,expected", FLAG2_CASES, ids=[str(i) for i in range(len(FLAG2_CASES))])
def test_flags_with_a_credential_word_in_their_name(text, secret, expected):
    out, counts = redact(text)
    assert out == expected and secret not in out
    assert sum(counts.values()) == len(re.findall(r"\[REDACTED:", expected))
    assert redact(out)[0] == out
    assert redact(text) == _run(text, False)
    assert json.loads(redact(json.dumps({"cmd": text, "n": 1}))[0]) == {"cmd": expected, "n": 1}


FLAG2_NEGATIVE = [
    # the flag names a thing next to a secret, not the secret
    "docker login --password-stdin registry2.example.com",
    "docker login -u me --password-stdin registry-1.example.com",
    "--token-file creds2024.json",
    "--token-file=/run/secrets/token1234",
    "--credentials-file ~/.config/gcloud/key1.json",
    "--credentials-file=./creds2024.json",
    "--tokenizer bert-base-uncased",
    "--tokenizer-name bert-base-uncased2",
    "--login-url https://example.com/a1",
    "--login-timeout 30",
    "--password-policy strict-mode",
    "--token-expiry 12h",
    "--token-limit 20",
    # a placeholder, a path, another option, a short value or a value without a digit
    "--credentials-login=$LOGIN_VALUE_1",
    "--credentials-login ${LOGIN_VALUE_1}",
    '--credentials-login "$LOGIN_VALUE_1"',
    "--api-token-value=<your-token-1>",
    "--api-token-value {token1}",
    "--api-token-value=[REDACTED:password-flag]",
    "--api-token-value=********1",
    "--credentials-login short1",
    "--credentials-login abcdefghij",
    "--credentials-login=-abcdefg1",
    "--credentials-login=.abcdefg1",
    "--credentials-login=/run/secrets/login1",
    "--credentials-login=~/login1abc",
    "--credentials-login --verbose1",
    "--credentials-login=",
    "--credentials-login ''",
    "--secret id=npm,src=.npmrc2",
    "--credentials-login",
    # prose and code that name the flag
    "use --api-token-value to pass the value",
    "the --credentials-login flag is required",
    "parser.add_argument('--credentials-login', help='the login 12345678')",
    "x--credentials-login=abcdefg12345",                         # the flag must start a word
]


@pytest.mark.parametrize("text", FLAG2_NEGATIVE, ids=[str(i) for i in range(len(FLAG2_NEGATIVE))])
def test_credential_flag_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


# --- item 3: secrets in URL query strings
QV = "Ab9" + "%2F" + "cd-EF_gh" + ".ij~kl" + "+mn" + "12"                                  # a percent-encoded, mixed value
QRY = "[REDACTED:url-query-secret]"
URL_PARAMS = ["key", "api_key", "apikey", "api-key", "token", "access_token", "auth", "auth_token", "jwt", "sig", "signature",
              "secret", "password", "client_secret", "x-amz-signature", "x-amz-credential", "x-amz-security-token"]


@pytest.mark.parametrize("name", URL_PARAMS)
def test_url_query_secrets_keep_the_param_name(name):
    base = "https://api.example.com/v1/items"
    _check(f"{base}?{name}={QV}", QV, f"{base}?{name}={QRY}")
    _check(f"{base}?limit=10&{name}={QV}&page=2", QV, f"{base}?limit=10&{name}={QRY}&page=2")
    _check(f"{base}?limit=10&{name.upper()}={QV}#top", QV, f"{base}?limit=10&{name.upper()}={QRY}#top")


URL_CASES = [
    ("GET https://maps.example.com/api?key=" + QV + " HTTP/1.1", "GET https://maps.example.com/api?key=" + QRY + " HTTP/1.1"),
    ("see [docs](https://a.example.com/p?key=" + QV + ") now", "see [docs](https://a.example.com/p?key=" + QRY + ") now"),
    ('{"url": "https://a.example.com/p?a=1&token=' + QV + '", "n": 1}',
     '{"url": "https://a.example.com/p?a=1&token=' + QRY + '", "n": 1}'),
    ("'https://a.example.com/p?token=" + QV + "'", "'https://a.example.com/p?token=" + QRY + "'"),
    ("https://a.example.com/p?a=1&amp;token=" + QV + "&amp;b=2", "https://a.example.com/p?a=1&amp;token=" + QRY + "&amp;b=2"),
    ("https://a.example.com/p?token=" + QV + "\nnext line", "https://a.example.com/p?token=" + QRY + "\nnext line"),
    ("?sig=" + QV, "?sig=" + QRY),
    ("&secret=" + QV, "&secret=" + QRY),
    ("https://s3.example.com/b/o?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=" + "ABCDEFGH1234567890%2F20261007%2Feu-west-1"
     + "&X-Amz-Signature=" + _hex(64, 31),
     "https://s3.example.com/b/o?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=" + QRY + "&X-Amz-Signature=" + QRY),
]


@pytest.mark.parametrize("text,expected", URL_CASES, ids=[str(i) for i in range(len(URL_CASES))])
def test_url_query_secrets_in_context(text, expected):
    out, counts = redact(text)
    assert out == expected
    assert counts == {"url-query-secret": len(re.findall(r"\[REDACTED:", expected))}
    assert redact(out)[0] == out
    assert redact(text) == _run(text, False)
    json.loads(redact(json.dumps({"a": text}))[0])


def test_a_jwt_in_a_url_is_redacted_whatever_its_payload():
    jwt = "eyJhbGciOiJIUzI1NiJ9" + "." + "c29tZS1vcGFxdWUtcGF5bG9hZA" + "." + _mixed(30, 41)
    out, _ = redact(f"https://a.example.com/cb?jwt={jwt}&x=1")
    assert jwt not in out and out.endswith("&x=1") and "?jwt=[REDACTED:" in out
    out, counts = redact("https://a.example.com/cb?jwt=" + _mixed(40, 42))
    assert out == "https://a.example.com/cb?jwt=" + QRY and counts["url-query-secret"] == 1


URL_NEGATIVE = [
    "https://api.example.com/v1/items?pageToken=" + _mixed(30, 51),
    "https://api.example.com/v1/items?nextPageToken=" + _mixed(30, 52),
    "https://api.example.com/v1/items?limit=100000000",
    "https://api.example.com/v1/items?query=" + _mixed(30, 53),
    "https://api.example.com/v1/items?datetime=2026-10-06T10:00:00",
    "https://api.example.com/v1/items?monkey=" + _mixed(30, 54),
    "https://api.example.com/v1/items?keyword=" + _mixed(30, 55),
    "https://api.example.com/v1/items?sortkey=" + _mixed(30, 56),
    "https://api.example.com/v1/items?a=1&tokens=" + _mixed(30, 57),
    "https://api.example.com/v1/items?key=short",
    "https://api.example.com/v1/items?key=",
    "https://api.example.com/v1/items?key=${API_KEY_VALUE}",
    "https://api.example.com/v1/items?key={api_key_value}",
    "https://api.example.com/v1/items?token=<your-token-here>",
    "https://api.example.com/v1/items?token=[REDACTED:secret-assignment]",
    "https://api.example.com/v1/items?key=****************",
    "https://api.example.com/v1/items?key=$API_KEY_VALUE",
    'url = base + "?key=" + api_key',
    "a=1&b=2 and token: none",
    "x-amz-signature=" + _hex(64, 58),                              # not in a query string: no `?` or `&` before it
]


@pytest.mark.parametrize("text", URL_NEGATIVE, ids=[str(i) for i in range(len(URL_NEGATIVE))])
def test_url_query_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


# --- item 4: a high-entropy value after a bare `key`
KEYV = _mixed(32, 61)
KEY_CASES = [
    (f"key: {KEYV}", f"key: {GEN}"),
    (f"key={KEYV}", f"key={GEN}"),
    (f"  key: {KEYV}\n  other: 1", f"  key: {GEN}\n  other: 1"),
    (f'"key": "{KEYV}"', f'"key": "{GEN}"'),
    (f"key: '{KEYV}'", f"key: '{GEN}'"),
    (f'msg="saved" key={KEYV} level=info', f'msg="saved" key={GEN} level=info'),
    (f"API key: {KEYV}.", f"API key: {GEN}."),
    (f"ssh_key: {KEYV}", f"ssh_key: {GEN}"),
    (f"Key = {KEYV}", f"Key = {GEN}"),
    ("key: " + _mixed(24, 62), f"key: {GEN}"),                                    # exactly 24 characters
    ("key: " + _mixed(8, 63) + "-" + _mixed(8, 64) + "_" + _mixed(8, 65), f"key: {GEN}"),
    (f"key={KEYV}, next", f"key={GEN}, next"),
]


@pytest.mark.parametrize("text,expected", KEY_CASES, ids=[str(i) for i in range(len(KEY_CASES))])
def test_a_high_entropy_value_after_key(text, expected):
    secret = re.search(r"[A-Za-z0-9_-]{24,}", text).group(0)
    _check(text, secret, expected)


KEY_NEGATIVE = [
    "key: value",
    "key: someIdentifier",
    "key: 12345678",
    "primary_key: id",
    "key: " + _mixed(23, 71),                                                     # one character short
    "key: " + _hex(32, 72),                                                       # no upper-case letter
    "key: " + _hex(32, 73).upper(),                                               # no lower-case letter
    "key: " + "SomeLongIdentifierNameForAThing",                                  # no digit
    "key: " + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",                                   # no lower-case letter
    "key: " + _mixed(32, 74) + ".json",                                           # a file name, not one run
    "key: path/" + _mixed(32, 75),
    "key: " + _mixed(32, 76) + "/more",
    "key: " + _mixed(32, 77) + "=",
    "key: ${" + _mixed(32, 78) + "}",
    "key: <" + _mixed(32, 79) + ">",
    "key: [REDACTED:secret-assignment]",
    "monkey: " + _mixed(32, 80),
    "sshKey: " + _mixed(32, 81),
    "keys: " + _mixed(32, 82),
    "keyboard: " + _mixed(32, 83),
    "key == " + _mixed(32, 84),
    "key:\n" + _mixed(32, 85),
    "the key is " + _mixed(32, 86),
    "key: some words " + _mixed(32, 87),
]


@pytest.mark.parametrize("text", KEY_NEGATIVE, ids=[str(i) for i in range(len(KEY_NEGATIVE))])
def test_key_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


# --- item 5: a JWT whose payload does not start with eyJ
JWT_HDR = "eyJhbGciOiJIUzI1NiI" + "sInR5cCI6IkpXVCJ9"
JWT_SIG = _mixed(43, 91)
JWT_CASES = [
    JWT_HDR + "." + "c29tZS1vcGFxdWUtcGF5bG9hZA" + "." + JWT_SIG,
    JWT_HDR + "." + _mixed(10, 92) + "." + _mixed(10, 93),                     # the shortest parts
    JWT_HDR + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + JWT_SIG,            # the usual payload still works
    JWT_HDR + "." + "a-b_c-d_e-f_" + "." + JWT_SIG,
]


@pytest.mark.parametrize("jwt", JWT_CASES, ids=[str(i) for i in range(len(JWT_CASES))])
def test_a_jwt_with_any_payload_is_redacted(jwt):
    _check(f"Authorization token {jwt} end", jwt, "Authorization token [REDACTED:jwt] end")


JWT_NEGATIVE = [
    JWT_HDR + "." + "c29tZS1vcGFxdWUtcGF5bG9hZA",                                # two parts only
    JWT_HDR + "." + _mixed(9, 94) + "." + JWT_SIG,                                # payload too short
    JWT_HDR + "." + _mixed(20, 95) + "." + _mixed(9, 96),                         # signature too short
    "eyJ" + "a" * 9 + "." + _mixed(20, 97) + "." + _mixed(20, 98),                # header too short
    "abc" + "." + _mixed(20, 99) + "." + _mixed(20, 100),                         # not a JWT header
    JWT_HDR,
]


@pytest.mark.parametrize("text", JWT_NEGATIVE, ids=[str(i) for i in range(len(JWT_NEGATIVE))])
def test_jwt_false_positives_stay(text):
    out, counts = redact(text)
    assert out == text and not counts


# --- item 6: leftover private-key markers are neutralized (PRIVATE KEY -> PRIVATE-KEY)
MARKER = "private-key-marker"
MARKER_CASES = [
    ("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE-KEY-----", 1),
    ("-----END PRIVATE KEY-----", "-----END PRIVATE-KEY-----", 1),
    ("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN RSA PRIVATE-KEY-----", 1),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE-KEY-----", 1),
    ("-----BEGIN ENCRYPTED PRIVATE KEY-----", "-----BEGIN ENCRYPTED PRIVATE-KEY-----", 1),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "-----BEGIN PGP PRIVATE-KEY BLOCK-----", 1),
    ("-----END PGP PRIVATE KEY BLOCK-----", "-----END PGP PRIVATE-KEY BLOCK-----", 1),
    ("-----begin private key-----", "-----begin private-key-----", 1),
    ("Starts with -----BEGIN RSA PRIVATE KEY----- and that is all", "Starts with -----BEGIN RSA PRIVATE-KEY----- and that is all", 1),
    ("-----BEGIN PRIVATE KEY-----\nshort", "-----BEGIN PRIVATE-KEY-----\nshort", 1),
    ("-----BEGIN PRIVATE KEY----- the quick brown fox jumps over the lazy dog and keeps on running far away",
     "-----BEGIN PRIVATE-KEY----- the quick brown fox jumps over the lazy dog and keeps on running far away", 1),
    ("-----BEGIN RSA PRIVATE KEY-----\nthis is only a prose line that mentions the marker and has many words in it",
     "-----BEGIN RSA PRIVATE-KEY-----\nthis is only a prose line that mentions the marker and has many words in it", 1),
    # a test fixture or regex source that builds a key from pieces: both markers go, the pieces stay
    ("pem = '-----BEGIN RSA PRIVATE KEY-----\\n' + body + '\\n-----END RSA PRIVATE KEY-----'",
     "pem = '-----BEGIN RSA PRIVATE-KEY-----\\n' + body + '\\n-----END RSA PRIVATE-KEY-----'", 2),
    ("re.compile(r'-----BEGIN PRIVATE KEY-----(.*)-----END PRIVATE KEY-----')",
     "re.compile(r'-----BEGIN PRIVATE-KEY-----(.*)-----END PRIVATE-KEY-----')", 2),
    ("a -----BEGIN PRIVATE KEY----- b -----BEGIN PRIVATE KEY----- c", "a -----BEGIN PRIVATE-KEY----- b -----BEGIN PRIVATE-KEY----- c", 2),
]


@pytest.mark.parametrize("text,expected,n", MARKER_CASES, ids=[str(i) for i in range(len(MARKER_CASES))])
def test_leftover_private_key_markers_are_neutralized(text, expected, n):
    out, counts = redact(text)
    assert out == expected and dict(counts) == {MARKER: n}
    assert redact(out)[0] == out
    assert redact(text) == _run(text, False)
    assert json.loads(redact(json.dumps({"a": text}))[0])["a"] == expected


def test_a_real_key_is_still_redacted_whole_and_only_the_leftover_marker_is_neutralized():
    real = "-----BEGIN PRIVATE KEY-----\n" + B64_LINE + "\n-----END PRIVATE KEY-----"
    out, counts = redact("a " + real + " b -----BEGIN RSA PRIVATE KEY----- c")
    assert out == "a [REDACTED:private-key] b -----BEGIN RSA PRIVATE-KEY----- c"
    assert dict(counts) == {"private-key": 1, MARKER: 1}
    cut = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + B64_LINE + "\n" + B64_LINE
    assert redact(cut) == ("[REDACTED:private-key]", {"private-key": 1})


def test_markers_of_other_blocks_are_not_touched():
    for text in ("-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----", "-----BEGIN PRIVATE-----", "PRIVATE KEY",
                 "-----BEGIN PRIVATE KEY", "BEGIN PRIVATE KEY-----", "-----BEGIN [A-Z ]*PRIVATE KEY-----"):
        assert redact(text) == (text, Counter())


# --- all the new rules: the prefilter, JSON validity and linear time
GITLEAKS_POSITIVE = ([t for t, _ in CRED_CASES] + [t for t, _, _ in FLAG2_CASES] + [t for t, _ in URL_CASES]
                     + [t for t, _ in KEY_CASES] + JWT_CASES + [t for t, _, _ in MARKER_CASES])
GITLEAKS_NEGATIVE = CRED_NEGATIVE + FLAG2_NEGATIVE + URL_NEGATIVE + KEY_NEGATIVE + JWT_NEGATIVE


def test_the_new_rules_agree_with_and_without_the_prefilter():
    for text in GITLEAKS_POSITIVE + GITLEAKS_NEGATIVE:
        assert redact(text) == _run(text, False), text
    rng = random.Random(8)
    seps = [" ", "\n", '"', "'", "\\n", "\t", ", ", "=", "&", "?"]
    for _ in range(400):
        text = "".join(rng.choice(GITLEAKS_POSITIVE + GITLEAKS_NEGATIVE) + rng.choice(seps) for _ in range(rng.randint(1, 5)))
        assert redact(text) == _run(text, False), text


def test_the_new_rules_keep_serialized_json_valid():
    for text in GITLEAKS_POSITIVE + GITLEAKS_NEGATIVE:
        for payload in ({"a": text}, {"password": text, "n": 1}, [text, {"token": text}], {"x": json.dumps({"k": text})}):
            line = json.dumps(payload)
            json.loads(redact(line)[0])


ADVERSARIAL_NEW = [
    # credentials
    "credentials=" * 9_000, "credentials" + "a" * 100_000, "credentials_" + "a" * 15 + "=" + " " * 10_000,
    "credentials=a." * 10_000, "credentials=" + "a." * 45_000 + "9", "credentials_x=" * 7_000, "CREDENTIALS" * 9_000,
    # flags with a credential word in the name
    "--credentials-login=" * 5_000, "--credentials-login " * 5_000, "--" + "a-" * 50_000, "--" + "a" * 100_000,
    "--token-x " * 10_000, "--token=" + "a" * 100_000, "--token-x='" + "a " * 50_000, "--api-token-value='" * 5_000,
    ("--login-x=" + "a" * 190 + " ") * 500, " --" + "-" * 100_000, "--login-x=" + "a" * 199 + "1" + "b" * 100_000,
    ("--token-x " + "a" * 7 + " ") * 9_000, "--a-token-" * 10_000, "--password-" * 9_000, "-- " * 30_000,
    "--token-x=" + "a" * 7 + "1" + " " + "--token-x=" * 9_000,
    # URL query strings
    "?key=" * 20_000, "?key=" + "a" * 100_000, "&token=aaaaaaa " * 7_000, ("?key=" + "a" * 7 + " ") * 10_000,
    "?x-amz-signature=" * 6_000, "&" * 100_000, "?" + "a" * 100_000, "?key=&" * 15_000, "&amp;token=" * 9_000,
    "?" + "a=b&" * 25_000, "?access_token=" + "%2F" * 30_000, "?a=" * 30_000 + "key=" + "a" * 100_000,
    # a bare key
    "key=" * 25_000, "key: " + "Aa1" * 34_000 + ".", "key=" + "Aa1" * 8 + "=", ("key=" + "Aa1" * 8 + ".") * 4_000,
    "key=" + "A" * 100_000, "key=" + "-" * 100_000, ("key=" + "a" * 255 + "A") * 300, "key: " * 20_000,
    ("key=" + "aA1" * 90) * 300, "key=" + "a1" * 40_000 + "A", "key_" * 25_000, "_key=" * 17_000,
    # jwt
    "eyJ" + "a" * 100_000, "eyJaaaaaaaaaa." * 7_000, "eyJaaaaaaaaaa.aaaaaaaaaa" * 4_000, "eyJ" + "a." * 50_000,
    "eyJ" * 30_000, "eyJ-" * 25_000, ("eyJ" + "a" * 11 + "-") * 7_000, ".eyJ" + "a" * 11 + ".a" * 20_000, ("eyJ" + "a" * 11 + ".") * 7_000, "eyJ" + "a" * 11 + "." + "b" * 100_000,
    # private-key markers
    "-----BEGIN " * 9_000, "-----BEGIN " + "A " * 50_000, "-----BEGIN PRIVATE KEY" * 4_000, "-----END " + "A" * 100_000,
    ("-----BEGIN " + "A" * 100 + " ") * 800, "-----BEGIN PRIVATE KEY BLOCK" * 3_500, "-----BEGIN A-----" * 5_500,
    "-----END PRIVATE KEY-----" * 4_000, "-----BEGIN " + "A" * 100 + "-----BEGIN " + "A" * 100 + "PRIVATE KEY-----" * 1,
]


@pytest.mark.slow                                     # about 4 s for the whole list
@pytest.mark.parametrize("line", ADVERSARIAL_NEW, ids=[str(i) for i in range(len(ADVERSARIAL_NEW))])
def test_the_new_rules_are_linear_on_adversarial_lines(line):
    t0 = time.perf_counter()
    _run(line, False)                                 # all rules, no prefilter
    assert time.perf_counter() - t0 < 1
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 1
