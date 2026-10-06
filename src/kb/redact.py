"""Secret redaction. A match becomes [REDACTED:<rule>]; a named group 'keep' is preserved before it.

This is the main barrier before transcripts are committed, so rules are ordered from specific to generic and
written with three properties in mind:
- JSON-safe: a rule never consumes a lone backslash, a closing quote or a newline it does not own, so redacting a
  serialized JSON line keeps it valid (a backslash is only consumed together with the quote or character it escapes).
- Linear time: every scan is bounded (scheme length, key body length, digit look-ahead), so 1 MB lines are cheap.
- Idempotent: placeholders start with "[", which no value pattern accepts.
Each rule also lists hint substrings. A text that holds none of them cannot match, so its regex is skipped
(a transcript has hundreds of thousands of short strings). A test checks that this never changes the result.
"""
from __future__ import annotations

import re
from collections import Counter

# Whitespace inside key material: a real one, or one written as a JSON escape (backslash + n/r/t).
_WS = r"(?:[ \t\r\n]|\\[nrt])"
_B64 = r"[A-Za-z0-9+/=]"

_PK_BEGIN = r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_PK_END = r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
# A key body is PEM/PGP text: base64, header lines (Proc-Type: ..., Version: ...) and whitespace. It never holds a
# quote or other punctuation, so a match cannot cross from one JSON string (or markdown turn) into another. A "-" that
# opens a BEGIN/END marker is not body either: a scan stops at the next marker, so many markers stay linear.
_PK_BODY = r"(?:[A-Za-z0-9+/=:,.\s_]|-(?!----(?:BEGIN|END))|\\[nrt])"

# Words that name a secret in an assignment (name=value, "name": "value"). The name may carry a prefix
# (DB_PASSWORD, STRIPE_API_KEY): only a letter or digit directly before it blocks the match.
_KEYWORDS = (r"password|passwd|secret|client[_-]?secret|secret[_-]?key|api[_-]?key|access[_-]?token|auth[_-]?token"
             r"|refresh[_-]?token|private[_-]?key|app[_-]?key|session[_-]?token|api[_-]?token|token")
_Q = r"""\\?["']"""                    # a quote, or a quote escaped for JSON
_NAME_START = r"(?<![A-Za-z0-9])"
# After the name: either `[=:]` (the name may be unquoted, the value may or may not be quoted), or a quoted name
# followed by `[=:]` and then a quoted value. A quoted name with a bare value is a JSON number/bool/null: leave it.
_ASSIGN = rf"(?:[ \t]*[=:][ \t]*(?:{_Q})?|{_Q}[ \t]*[=:][ \t]*{_Q})"
_PLACEHOLDER = r"(?![$<*{\[])"           # ${VAR}, <value>, ****, {{ x }}, [REDACTED...]
_VALUE_END = r"""\s"'\\,;)}"""
# Names whose value is a password even without a digit. PGPASSWORD and MYSQL_PWD are env vars that no prefix rule
# reaches ("PG" is glued to the word), and a bare PWD is a directory, so it is not here.
_PASSWORDS = r"password|passwd|pgpassword|mysql_pwd"

_RULES: list = []      # (name, compiled regex), in order
_HINTS: list = []      # per rule: lowercase substrings, at least one of which a matching text must contain


def _add(name: str, pattern: str, hints: tuple, flags: int = 0) -> None:
    _RULES.append((name, re.compile(pattern, flags)))
    _HINTS.append(hints)


# --- private keys (a full block, then a block that was cut off)
_add("private-key", _PK_BEGIN + _PK_BODY + "{0,20000}" + _PK_END, ("private key",))
_add("private-key", _PK_BEGIN + rf"(?={_WS}*(?:{_B64}{_WS}*){{40}})(?:{_B64}|{_WS}){{1,20000}}", ("private key",))
# --- vendor token formats
_add("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})",
     ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"))
_add("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}", ("sk-ant-",))
_add("stripe-key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}", ("_live_", "_test_"))
_add("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b", ("npm_",))
_add("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}", ("glpat-",))
_add("notion-token", r"\bntn_[A-Za-z0-9]{40,}", ("ntn_",))
_add("sentry-token", r"\bsntrys_[A-Za-z0-9+/=_-]{40,}", ("sntrys_",))
_add("huggingface-token", r"\bhf_[A-Za-z0-9]{30,}", ("hf_",))
_add("slack-app-token", r"\bxapp-\d-[A-Za-z0-9-]{10,}", ("xapp-",))
_add("sendgrid-key", r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", ("sg.",))
_add("google-oauth-token", r"\bya29\.[A-Za-z0-9_-]{20,}", ("ya29.",))
_add("linear-key", r"\blin_api_[A-Za-z0-9]{40,}", ("lin_api_",))
_add("atlassian-token", r"\bATATT3[A-Za-z0-9_=-]{50,}", ("atatt3",))
# a branch such as fix/sk-1234-... or feature/sk-learn-... is not a key: "sk-" must not continue a path or a word
_add("openai-key", r"(?<![\w/-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}", ("sk-",))
_add("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", ("akia", "asia"))
_add("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", ("xox",))
_add("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", ("hooks.slack.com",))
_add("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}", ("aiza",))
_add("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", ("eyj",))
# --- credentials in headers, commands and URLs
# a bearer value needs a digit, so "Bearer authentication-scheme-middleware" stays
_add("bearer", r"(?P<keep>\bBearer\s+)(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{16,}=*", ("bearer",), re.I)
_add("http-basic", r"(?P<keep>\bBasic\s+)[A-Za-z0-9+/]{12,}={0,2}", ("basic",))
_add("curl-user", r"""(?P<keep>(?:^|\s)(?:-u|--user)\s+["']?[^\s:"']+:)[^\s"'\\]+""", ("-u",))
_add("url-credentials", r"""(?P<keep>\b[a-z][a-z0-9+.-]{0,31}://[^\s:/@"']*:)[^\s@/"']+(?=@)""", ("://",), re.I)
_add("aws-secret",
     rf"(?P<keep>aws_secret_access_key(?:{_Q})?[ \t]*[=:][ \t]*(?:{_Q})?)[A-Za-z0-9/+=]{{40}}",
     ("aws_secret_access_key",), re.I)
# --- generic assignments, most specific first. All three are reported as "secret-assignment".
# A quoted password/secret value of 4-200 chars, up to its closing quote (it may hold spaces and no digit).
# The value is a run of whole tokens (a character, or a backslash + its character), so no escape is cut in half.
# A bare double quote ends it even inside single quotes: in a serialized JSON line it is a string delimiter.
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_PASSWORDS}|secret(?:[_-]?key)?)(?:{_Q})?[ \t]*[=:][ \t]*"
     r"""(?P<oq>(?P<bs>\\)?(?P<q>["'])))"""
     r"""(?![$<*{\[/~])(?:(?!(?P=q))[^\\\r\n"]|\\(?(bs)(?!(?P=q)))[^\r\n]){4,200}(?=(?(bs)\\)(?P=q))""",
     ("pass", "pwd", "secret"), re.I)
# An unquoted password of 6 or more characters, digit or not.
_add("secret-assignment",
     rf"""(?P<keep>{_NAME_START}(?:{_PASSWORDS})[ \t]*[=:][ \t]*)(?![$<*{{\[/~"'\\])[^{_VALUE_END}]{{6,}}""",
     ("pass", "mysql_pwd"), re.I)
# Any other secret-like name: the value needs 8 or more characters and a digit in its first 256 (the bound
# keeps the look-ahead, and so the whole scan, linear).
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_KEYWORDS}){_ASSIGN}){_PLACEHOLDER}"
     rf"(?=[^{_VALUE_END}]{{0,256}}\d)[^{_VALUE_END}]{{8,}}",
     ("pass", "secret", "token", "key"), re.I)


def _run(text: str, use_hints: bool):
    counts: Counter = Counter()
    low = text.lower() if use_hints else ""
    for (name, rx), hints in zip(_RULES, _HINTS):
        if use_hints and not any(h in low for h in hints):
            continue

        def _sub(m, name=name):
            keep = m.groupdict().get("keep") or ""
            return f"{keep}[REDACTED:{name}]"
        text, n = rx.subn(_sub, text)
        if n:
            counts[name] += n
            if use_hints:
                low = text.lower()
    return text, counts


def redact(text: str):
    """Return (redacted_text, Counter of rule -> matches)."""
    return _run(text, True)
