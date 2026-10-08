"""Secret redaction: replace each secret in a text with [REDACTED:<rule>].

A rule is a named regex. If the match has a named group "keep" (for example the name in `password=...`), that text
stays before the placeholder. This is the main barrier before transcripts are committed. So the rules go from
specific to generic, and each rule has four properties:
- JSON-safe: redacting a line of serialized JSON leaves valid JSON. A rule takes a backslash only together with
  the character it escapes, and never takes a closing quote or a newline that is not part of the secret.
- Linear time: each scan that could run long has an upper bound (the comments on the rules give them). So a
  1 MB line stays cheap.
- Idempotent: a second run changes nothing. A placeholder starts with "[", and no value pattern accepts "[".
- Scanner-proof: a private-key marker that a text only mentions is rewritten, not removed. Otherwise a secret
  scanner reads it as a key (see "private-key-marker").

Hints: each rule also has a list of lower-case substrings. A text that holds none of them cannot match the rule,
so redact skips that regex. This saves time: a transcript has hundreds of thousands of short strings. A test checks
that the hints never change the result.
"""
from __future__ import annotations

import re
from collections import Counter

# Whitespace inside key material: a real one, or one written as a JSON escape (backslash + n/r/t).
_KEY_WHITESPACE = r"(?:[ \t\r\n]|\\[nrt])"
_BASE64_CHAR = r"[A-Za-z0-9+/=]"

_PRIVATE_KEY_BEGIN = r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_PRIVATE_KEY_END = r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
# A key body is PEM/PGP text: base64, header lines (Proc-Type: ..., Version: ...) and whitespace. It never holds a
# quote or other punctuation, so a match cannot cross from one JSON string (or markdown turn) into another. A "-" that
# opens a BEGIN/END marker is not body either: a scan stops at the next marker, so many markers stay linear.
_PRIVATE_KEY_BODY_CHAR = r"(?:[A-Za-z0-9+/=:,.\s_]|-(?!----(?:BEGIN|END))|\\[nrt])"

# Words that name a secret in an assignment (name=value, "name": "value"). The name may carry a prefix
# (DB_PASSWORD, STRIPE_API_KEY): only a letter or digit directly before it blocks the match.
_KEYWORDS = (r"password|passwd|secret|client[_-]?secret|secret[_-]?key|api[_-]?key|access[_-]?token|auth[_-]?token"
             r"|refresh[_-]?token|private[_-]?key|app[_-]?key|session[_-]?token|api[_-]?token|token")
_QUOTE = r"""\\?["']"""                # a quote, or a quote escaped for JSON
_NAME_START = r"(?<![A-Za-z0-9])"
# After the name: either `[=:]` (the name may be unquoted, the value may or may not be quoted), or a quoted name
# followed by `[=:]` and then a quoted value. A quoted name with a bare value is a JSON number/bool/null: leave it.
_ASSIGN = rf"(?:[ \t]*[=:][ \t]*(?:{_QUOTE})?|{_QUOTE}[ \t]*[=:][ \t]*{_QUOTE})"
# One character of a quoted value. Use it after a group that captured the opening quote:
#   oq = the whole opening quote, bs = the backslash before it (set only for a JSON-escaped quote), q = the quote.
# A character is anything but a backslash, a line break, a double quote or the closing quote q. A backslash is taken
# only together with the character it escapes. When bs is set, a backslash + q is the closing quote: it ends the value.
_QUOTED_CHAR = r"""(?:(?!(?P=q))[^\\\r\n"]|\\(?(bs)(?!(?P=q)))[^\r\n])"""
_PLACEHOLDER = r"(?![$<*{\[])"           # ${VAR}, <value>, ****, {{ x }}, [REDACTED...]
_VALUE_END = r"""\s"'\\,;)}"""
# Names whose value is a password even without a digit. PGPASSWORD and MYSQL_PWD are env vars that no prefix rule
# reaches ("PG" is glued to the word), and a bare PWD is a directory, so it is not here.
_PASSWORDS = r"password|passwd|pgpassword|mysql_pwd"

_RULES: list = []      # (name, compiled regex), in order
_HINTS: list = []      # per rule: lowercase substrings, at least one of which a matching text must contain
_PASSES: list = []     # per rule: how many times it may run again while it still finds something
_REPLS: list = []      # per rule: a function match -> text, or None for "<keep>[REDACTED:<rule>]"


def _add(name: str, pattern: str, hints: tuple, flags: int = 0, passes: int = 1, repl=None) -> None:
    _RULES.append((name, re.compile(pattern, flags)))
    _HINTS.append(hints)
    _PASSES.append(passes)
    _REPLS.append(repl)


# --- private keys (a full block, then a block that was cut off)
# Both private-key rules read at most 20000 key characters: the bound keeps the scan linear.
_add("private-key", _PRIVATE_KEY_BEGIN + _PRIVATE_KEY_BODY_CHAR + "{0,20000}" + _PRIVATE_KEY_END, ("private key",))
# A key that lost its END marker is cut only when 40 base64 characters follow in one unbroken run. A line break (and the
# indentation after it) does not break the run, a space does: prose after a bare BEGIN marker stays.
_LINE_BREAK = r"(?:\r?\n|\\[nr])[ \t]*"
_add("private-key", _PRIVATE_KEY_BEGIN
     + rf"(?={_KEY_WHITESPACE}*(?:{_BASE64_CHAR}(?:{_LINE_BREAK})*){{40}})"
     + rf"(?:{_BASE64_CHAR}|{_KEY_WHITESPACE}){{1,20000}}",
     ("private key",))
# A marker that is left (no key body, a cut-off key, regex source code, a test fixture built by string concatenation) is not
# a key, but a secret scanner reads it as one. PRIVATE KEY becomes PRIVATE-KEY in the BEGIN and the END marker. This runs
# after the rules above, so a real key is redacted whole first. At most 100 characters of words sit between the two.
_add("private-key-marker", r"(?P<head>-----(?:BEGIN|END) [A-Z0-9_ ]{0,100}?PRIVATE) (?P<tail>KEY(?: BLOCK)?-----)", ("private key",),
     re.I, repl=lambda m: f"{m.group('head')}-{m.group('tail')}")
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
# The payload is any base64url text (a claim set usually starts with eyJ, but not always). The token starts a run of base64url
# characters (not just a word: `eyJ-eyJ-eyJ-...` would restart the scan at every "-" and take quadratic time).
_add("jwt", r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", ("eyj",))
# --- credentials in headers, commands and URLs
# a bearer value needs a digit, so "Bearer authentication-scheme-middleware" stays
_add("bearer", r"(?P<keep>\bBearer\s+)(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{16,}=*", ("bearer",), re.I)
# a Basic value needs a digit, a "+", "/" or "=" (padding): "Basic authentication" is prose
_add("http-basic", r"(?P<keep>\bBasic\s+)(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{12,}={0,2}", ("basic",))
# not `date -u +%Y-%m-%d...` (a format) or `docker run -u 1000:1000` (uid:gid)
_add("curl-user", r"""(?P<keep>(?:^|\s)(?:-u|--user)\s+["']?(?![+%])(?!\d+:)[^\s:"']+:)[^\s"'\\]+""", ("-u",))
# The URL scheme is at most 32 characters: the bound keeps the scan linear.
_add("url-credentials", r"""(?P<keep>\b[a-z][a-z0-9+.-]{0,31}://[^\s:/@"']*:)[^\s@/"']+(?=@)""", ("://",), re.I)
_add("aws-secret",
     rf"(?P<keep>aws_secret_access_key(?:{_QUOTE})?[ \t]*[=:][ \t]*(?:{_QUOTE})?)[A-Za-z0-9/+=]{{40}}",
     ("aws_secret_access_key",), re.I)
# A secret in a URL query string (or a form body): the parameter name stays, the value goes. The name must be the whole name
# (`?pageToken=`, `?query=`, `?monkey=` are not keys). The value is 8 or more characters up to `&`, `#`, `;`, white space, a
# quote or a closing bracket, and never holds a backslash, so a serialized JSON line stays valid. `&amp;` is the HTML `&`.
# A placeholder (${KEY}, $KEY, {key}, <key>, [REDACTED...], ****) is no value.
_QUERY_NAMES = (r"key|api[_-]?key|token|access[_-]token|auth|auth[_-]token|jwt|sig|signature|secret|password|client[_-]secret"
                r"|x-amz-signature|x-amz-credential|x-amz-security-token")
_add("url-query-secret",
     rf"""(?P<keep>(?:[?&]|&amp;)(?:{_QUERY_NAMES})=)(?![$<*{{\[])[^\s&#;"'\\<>)\]}}]{{8,}}""",
     ("key=", "token=", "auth=", "jwt=", "sig=", "signature=", "secret=", "password=", "credential="), re.I)
# --- generic assignments, most specific first. All of them are reported as "secret-assignment".
# A quoted password/secret value of 4-200 chars, up to its closing quote (it may hold spaces and no digit).
# The value is a run of whole tokens (a character, or a backslash + its character), so no escape is cut in half.
# A bare double quote ends it even inside single quotes: in a serialized JSON line it is a string delimiter.
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}(?:{_PASSWORDS}|secret(?:[_-]?key)?)(?:{_QUOTE})?[ \t]*[=:][ \t]*"
     r"""(?P<oq>(?P<bs>\\)?(?P<q>["'])))"""
     rf"""(?![$<*{{\[/~]){_QUOTED_CHAR}{{4,200}}(?=(?(bs)\\)(?P=q))""",
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
# The word credentials (credential) names a secret as well: "use this credentials: <token>". It follows the rules of the suffix
# rule above (a suffix of 0 to 20 characters, no path, URL or dotted identifier, 8 or more characters with a digit), and a value
# that starts with "." is a relative path too.
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}credentials?(?!{_PLURAL})[A-Za-z0-9_]{{0,20}}{_ASSIGN}){_PLACEHOLDER}(?![/~.]|\w{{1,16}}://)"
     rf"{_CODE_REF}"
     rf"(?=[^{_VALUE_END}]{{0,256}}\d)[^{_VALUE_END}]{{8,}}",
     ("credential",), re.I)
# A bare `key` is too common a word for the rules above (`key: value`, `primary_key: id`). Its value is a secret only when it
# looks like one: a single run of 24 or more letters, digits, "_" and "-" with an upper-case letter, a lower-case letter and a
# digit (the look-aheads read the first 256 characters), and the run ends at white space, a quote, a delimiter or a full stop
# that ends a sentence (`key: abc...xyz.json` is a file name, `key: a/b` a path).
_add("secret-assignment",
     rf"(?P<keep>{_NAME_START}key{_ASSIGN})"
     r"(?=[A-Za-z0-9_-]{0,256}?(?-i:[A-Z]))(?=[A-Za-z0-9_-]{0,256}?(?-i:[a-z]))(?=[A-Za-z0-9_-]{0,256}?[0-9])"
     r"""[A-Za-z0-9_-]{24,}(?=[\s"'\\,;&#)}\]<>]|\.(?![A-Za-z0-9_-])|$)""",
     ("key",), re.I)
# --- password flags: a command-line option that names a secret. The flag stays, only the value goes. They run after the
# generic assignments: `--password=...` that a rule above already catches keeps its name (secret-assignment); these add
# the forms no name rule reaches (`--password X`, `--pass=X`, a short value, `mysql -pX`, `sshpass -p X`, ...).
# A value is quoted (spaces allowed, up to the closing quote, as in the quoted-password rule above) or bare (up to white
# space, a quote or a shell delimiter, so a JSON string or an inline-code span is not eaten). A placeholder (${VAR}, $VAR,
# <value>, ****, {x}, [REDACTED...]) or a path is no value, and neither is another option after a space (`--password -u`).
# A quoted value does not start with `,` `:` `]` `}` or white space either: in serialized JSON a plain quote right after
# "--pass " is the end of the string, and what follows it is JSON structure that must stay.
_FLAG_QUOTE = r"""(?P<oq>(?P<bs>\\)?(?P<q>["']))?"""     # a quote, or a quote escaped for JSON; stays in the "keep" group
_FLAG_VALUE = (rf"""(?(oq)(?![$<*{{\[/~,:\]}}\s]){_QUOTED_CHAR}{{1,200}}(?=(?(bs)\\)(?P=q))"""
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
# A flag whose name holds a credential word anywhere (--credentials-login, --api-token-value, --my-service-password-prod):
# the value is 8 or more characters with a digit (so `--login-timeout 30` and `--password-policy strict` stay), not a placeholder,
# a path or another option, and not an `id=...` pair (`--secret id=npm,src=.npmrc`). A name that ends in -file, -path, -dir,
# -url, -stdin, -env or -name says where a secret is, not what it is. The look-ahead finds the word in the first 80 characters
# of the name, so a long name is read once and not once per word it holds.
_NAME_HOLDS = r"(?=[A-Za-z0-9_-]{0,80}?(?:password|passwd|secret|token|credentials?|login|api[-_]?key))"
_NAME_SAYS_WHERE = r"(?<![-_]file)(?<![-_]path)(?<![-_]dir)(?<![-_]url)(?<![-_]stdin)(?<![-_]env)(?<![-_]name)"
_CREDENTIAL_FLAG = rf"(?<![\w-])--{_NAME_HOLDS}[A-Za-z0-9_-]+{_NAME_SAYS_WHERE}"
_BARE_CHAR = r"""[^\s"'\\;&|<>)}`]"""        # one character of an unquoted value
_CREDENTIAL_FLAG_VALUE = (
    rf"""(?(oq)(?![$<*{{\[/~.,:\]}}\s-])(?={_QUOTED_CHAR}{{0,200}}?\d){_QUOTED_CHAR}{{8,200}}(?=(?(bs)\\)(?P=q))"""
    rf"""|(?![$<*{{\[/~.\-"'\\])(?={_BARE_CHAR}{{0,200}}?\d){_BARE_CHAR}{{8,2000}})""")
_CREDENTIAL_FLAG_HINTS = ("password", "passwd", "secret", "token", "credential", "login", "api-key", "api_key",
                          "apikey")
_add("password-flag", rf"(?P<keep>{_CREDENTIAL_FLAG}={_FLAG_QUOTE}){_CREDENTIAL_FLAG_VALUE}", _CREDENTIAL_FLAG_HINTS,
     re.I)
_add("password-flag", rf"(?P<keep>{_CREDENTIAL_FLAG}[ \t]+(?!-)(?!id=){_FLAG_QUOTE}){_CREDENTIAL_FLAG_VALUE}",
     _CREDENTIAL_FLAG_HINTS, re.I)
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
    """Apply every rule in order. Returns (redacted text, Counter of rule name -> matches).

    use_hints=True skips a rule when the text holds none of its hints: redact does this. use_hints=False runs every
    rule; the tests use it to check that the hints never change the result. A rule with passes > 1 runs again while
    it still finds something, at most passes times.
    """
    counts: Counter = Counter()
    low = text.lower() if use_hints else ""
    for (name, rx), hints, passes, repl in zip(_RULES, _HINTS, _PASSES, _REPLS):
        if use_hints and not any(h in low for h in hints):
            continue

        def _sub(m, name=name, repl=repl):
            if repl:
                return repl(m)
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
