"""Secret redaction. A match becomes [REDACTED:<rule>]; a named group 'keep' is preserved before it.

This is the main barrier before transcripts are committed, so rules are ordered from specific to generic and
written with three properties in mind:
- JSON-safe: a rule never consumes a lone backslash, a closing quote or a newline it does not own, so redacting a
  serialized JSON line keeps it valid (a backslash is only consumed together with the quote or character it escapes).
- Linear time: every scan is bounded (scheme length, key body length, digit look-ahead, the 12 words between a command and
  its password flag, a name suffix of 20 characters), so 1 MB lines are cheap.
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
_PASSES: list = []     # per rule: how many times it may run again while it still finds something


def _add(name: str, pattern: str, hints: tuple, flags: int = 0, passes: int = 1) -> None:
    _RULES.append((name, re.compile(pattern, flags)))
    _HINTS.append(hints)
    _PASSES.append(passes)


# --- private keys (a full block, then a block that was cut off)
_add("private-key", _PK_BEGIN + _PK_BODY + "{0,20000}" + _PK_END, ("private key",))
# A key that lost its END marker is cut only when 40 base64 characters follow in one unbroken run. A line break (and the
# indentation after it) does not break the run, a space does: prose after a bare BEGIN marker stays.
_BRK = r"(?:\r?\n|\\[nr])[ \t]*"
_add("private-key", _PK_BEGIN + rf"(?={_WS}*(?:{_B64}(?:{_BRK})*){{40}})(?:{_B64}|{_WS}){{1,20000}}", ("private key",))
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
_add("slack-token", r"\bxox[abprse]-[A-Za-z0-9-]{10,}", ("xox",))
_add("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", ("hooks.slack.com",))
_add("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}", ("aiza",))
_add("google-client-secret", r"\bGOCSPX-[A-Za-z0-9_-]{20,}", ("gocspx-",))
# an Azure storage connection string: AccountName=...;AccountKey=<88 base64 characters>;EndpointSuffix=...
_add("azure-account-key", r"(?P<keep>AccountKey=)[A-Za-z0-9+/=]{40,}", ("accountkey=",), re.I)
_add("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", ("eyj",))
# --- credentials in headers, commands and URLs
# a bearer value needs a digit, so "Bearer authentication-scheme-middleware" stays
_add("bearer", r"(?P<keep>\bBearer\s+)(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{16,}=*", ("bearer",), re.I)
# a Basic value needs a digit, a "+", "/" or "=" (padding): "Basic authentication" is prose
_add("http-basic", r"(?P<keep>\bBasic\s+)(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{12,}={0,2}", ("basic",))
# not `date -u +%Y-%m-%d...` (a format) or `docker run -u 1000:1000` (uid:gid)
_add("curl-user", r"""(?P<keep>(?:^|\s)(?:-u|--user)\s+["']?(?![+%])(?!\d+:)[^\s:"']+:)[^\s"'\\]+""", ("-u",))
_add("url-credentials", r"""(?P<keep>\b[a-z][a-z0-9+.-]{0,31}://[^\s:/@"']*:)[^\s@/"']+(?=@)""", ("://",), re.I)
_add("aws-secret",
     rf"(?P<keep>aws_secret_access_key(?:{_Q})?[ \t]*[=:][ \t]*(?:{_Q})?)[A-Za-z0-9/+=]{{40}}",
     ("aws_secret_access_key",), re.I)
# --- generic assignments, most specific first. All of them are reported as "secret-assignment".
# A quoted password/secret value of 4-200 chars, up to its closing quote (it may hold spaces and no digit).
# The value is a run of whole tokens (a character, or a backslash + its character), so no escape is cut in half.
# A bare double quote ends it even inside single quotes: in a serialized JSON line it is a string delimiter.
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_PASSWORDS}|secret(?:[_-]?key)?)(?:{_Q})?[ \t]*[=:][ \t]*"
     r"""(?P<oq>(?P<bs>\\)?(?P<q>["'])))"""
     r"""(?![$<*{\[/~])(?:(?!(?P=q))[^\\\r\n"]|\\(?(bs)(?!(?P=q)))[^\r\n]){4,200}(?=(?(bs)\\)(?P=q))""",
     ("pass", "pwd", "secret"), re.I)
# An unquoted password of 6 or more characters, digit or not. A dotted identifier (process.env.X, req.body.pw) is code
# that reads a password, not a password: it ends at white space, a quote, a delimiter or a bracket.
_CODE_REF = r"(?![A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?=[\s\"'\\,;)}(\[\]]|$))"
_add("secret-assignment",
     rf"""(?P<keep>{_NAME_START}(?:{_PASSWORDS})[ \t]*[=:][ \t]*)(?![$<*{{\[/~"'\\]){_CODE_REF}[^{_VALUE_END}]{{6,}}""",
     ("pass", "mysql_pwd"), re.I)
# Any other secret-like name: the value needs 8 or more characters and a digit in its first 256 (the bound
# keeps the look-ahead, and so the whole scan, linear).
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_KEYWORDS}){_ASSIGN}){_PLACEHOLDER}"
     rf"(?=[^{_VALUE_END}]{{0,256}}\d)[^{_VALUE_END}]{{8,}}",
     ("pass", "secret", "token", "key"), re.I)
# The same with a suffix after the keyword (DB_PASSWORD_PROD, GITHUB_TOKEN_RO, SECRET_KEY_BASE, apiKeyProd): 1 to 20
# name characters before the "=" or ":". The suffix does not start with a plural "s" (`tokens = 12345678`, `max_tokens_per_x`
# count things): a lower-case "s", or an upper-case "S" that does not open a camelCase word (tokenSecret is a name). A suffix
# also names things that are not secrets (TOKEN_URL, SECRET_KEY_FILE), so a path, a URL or a dotted identifier is not a value.
_PLURAL = r"(?-i:s|S(?![a-z]))"
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_KEYWORDS})(?!{_PLURAL})[A-Za-z0-9_]{{1,20}}{_ASSIGN}){_PLACEHOLDER}(?![/~]|\w{{1,16}}://)"
     rf"{_CODE_REF}"
     rf"(?=[^{_VALUE_END}]{{0,256}}\d)[^{_VALUE_END}]{{8,}}",
     ("pass", "secret", "token", "key"), re.I)
# --- password flags: a command-line option that names a secret. The flag stays, only the value goes. They run after the
# generic assignments: `--password=...` that a rule above already catches keeps its name (secret-assignment); these add
# the forms no name rule reaches (`--password X`, `--pass=X`, a short value, `mysql -pX`, `sshpass -p X`, ...).
# A value is quoted (spaces allowed, up to the closing quote, as in the quoted-password rule above) or bare (up to white
# space, a quote or a shell delimiter, so a JSON string or an inline-code span is not eaten). A placeholder (${VAR}, $VAR,
# <value>, ****, {x}, [REDACTED...]) or a path is no value, and neither is another option after a space (`--password -u`).
# A quoted value does not start with `,` `:` `]` `}` or white space either: in serialized JSON a plain quote right after
# "--pass " is the end of the string, and what follows it is JSON structure that must stay.
_FLAG_QUOTE = r"""(?P<oq>(?P<bs>\\)?(?P<q>["']))?"""     # a quote, or a quote escaped for JSON; stays in the "keep" group
_FLAG_VALUE = (r"""(?(oq)(?![$<*{\[/~,:\]}\s])(?:(?!(?P=q))[^\\\r\n"]|\\(?(bs)(?!(?P=q)))[^\r\n]){1,200}(?=(?(bs)\\)(?P=q))"""
               r"""|(?![$<*{\[/~"'\\])[^\s"'\\;&|<>)}`]{1,200})""")
# One word of a command line: it ends at white space and at a command separator (`;`, `&`, `|`, a line break written as
# a real one or as a JSON escape), so an option is only looked for in the same command. 12 words of up to 80 characters.
# The command word itself is not a word of the gap: the next one starts its own search, so a line of repeated command words
# costs one step per word, not twelve.
_ARG = r"(?:[^\s;&|\\]|\\[^nr\s]){1,80}"


def _gap(cmd: str) -> str:
    return rf"(?:[ \t]+(?!{cmd}(?![\w-])){_ARG}){{0,12}}?"


_MYSQL = r"(?:mysql(?:dump|admin|import|check|show)?|mariadb(?:-(?:dump|admin|import|check|show))?)"
_FLAGS = (r"pass(?:word|wd)?|token|secret|api[-_]?key|(?:access|auth|refresh)[-_]token|client[-_]secret|secret[-_]key")
# A bare word after the flag that is prose about the flag ("the --password flag"), not a value.
_PROSE = (r"(?:flags?|options?|arguments?|args?|param(?:eter)?s?|values?|to|is|are|and|or|for|in|on|the|an?|as|with|when|if"
          r"|that|which|from|it|this|so|but|be|by|via|instead|only)(?![\w-])")
_FLAG_HINTS = ("--pass", "--token", "--secret", "--api", "--access", "--auth", "--refresh", "--client")
_add("password-flag", rf"(?P<keep>(?<![\w-])--(?:{_FLAGS})={_FLAG_QUOTE}){_FLAG_VALUE}", _FLAG_HINTS)
_add("password-flag", rf"(?P<keep>(?<![\w-])--(?:{_FLAGS})[ \t]+(?!-)(?!{_PROSE})(?!id=){_FLAG_QUOTE}){_FLAG_VALUE}", _FLAG_HINTS)
# The short options below only mean a password after their own command (`mkdir -p`, `ssh -p 2222`, `grep -a` do not).
# For mysql, redis-cli and az the command word is part of the match, so a second flag of the same command is only reached
# by running the rule again (passes=3): the first match has used up the command word. A run that finds nothing ends the
# loop, and a placeholder is never a value, so a rule cannot go on matching its own output.
# mysql, mariadb and their dump/admin tools: `-p` is glued to the value (`-p secret` prompts; `-p3306:3306` publishes a port).
_add("password-flag",
     rf"(?P<keep>(?<![\w.-]){_MYSQL}(?![\w-]){_gap(_MYSQL)}[ \t]+-p(?![\d.]+:\d){_FLAG_QUOTE}){_FLAG_VALUE}",
     ("mysql", "mariadb"), passes=3)
# sshpass: `-p` comes first among its options (a later `-p` belongs to ssh), with or without a space before the value.
_add("password-flag",
     rf"(?P<keep>(?<![\w.-])sshpass(?:[ \t]+-[^\s;&|\\p][^\s;&|\\]{{0,40}}){{0,3}}[ \t]+-p[ \t]*{_FLAG_QUOTE}){_FLAG_VALUE}",
     ("sshpass",))
_add("password-flag", rf"(?P<keep>(?<![\w.-])redis-cli(?![\w-]){_gap('redis-cli')}[ \t]+-a[ \t]+(?!-){_FLAG_QUOTE}){_FLAG_VALUE}",
     ("redis-cli",), passes=3)
_add("password-flag", rf"(?P<keep>(?<![\w.-])az{_gap('az')}[ \t]+-p[ \t]+(?!-){_FLAG_QUOTE}){_FLAG_VALUE}", ("az ", "az\t"), passes=3)


def _run(text: str, use_hints: bool):
    counts: Counter = Counter()
    low = text.lower() if use_hints else ""
    for (name, rx), hints, passes in zip(_RULES, _HINTS, _PASSES):
        if use_hints and not any(h in low for h in hints):
            continue

        def _sub(m, name=name):
            keep = m.groupdict().get("keep") or ""
            return f"{keep}[REDACTED:{name}]"
        total = 0
        for _ in range(passes):
            text, n = rx.subn(_sub, text)
            total += n
            if not n:
                break
        if total:
            counts[name] += total
            if use_hints:
                low = text.lower()
    return text, counts


def redact(text: str):
    """Return (redacted_text, Counter of rule -> matches)."""
    return _run(text, True)
