"""Secret redaction. A match becomes [REDACTED:<rule>]; a named group 'keep' is preserved before it."""
from __future__ import annotations

import re
from collections import Counter

_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer", re.compile(r"(?P<keep>\bBearer\s+)[A-Za-z0-9._~+/-]{16,}=*", re.I)),
    ("url-credentials", re.compile(r"(?P<keep>\b[a-z][a-z0-9+.-]*://[^\s:/@\"']+:)[^\s@/\"']+(?=@)", re.I)),
    ("aws-secret", re.compile(
        r"(?P<keep>aws_secret_access_key\\?[\"']?\s*[=:]\s*\\?[\"']?)[A-Za-z0-9/+=]{40}", re.I)),
    ("secret-assignment", re.compile(
        r"(?P<keep>(?<![A-Za-z0-9_])(?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key"
        r"|access[_-]?token|auth[_-]?token|token)\\?[\"']?\s*[=:]\s*\\?[\"']?)"
        r"(?![$<*{\[])(?=[^\s\"'\\,;)}]*\d)[^\s\"'\\,;)}]{8,}",
        re.I)),
]


def redact(text: str):
    """Return (redacted_text, Counter of rule -> matches)."""
    counts: Counter = Counter()
    for name, rx in _RULES:
        def _sub(m, name=name):
            counts[name] += 1
            keep = m.groupdict().get("keep") or ""
            return f"{keep}[REDACTED:{name}]"
        text = rx.sub(_sub, text)
    return text, counts
