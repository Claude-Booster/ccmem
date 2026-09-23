from __future__ import annotations
import re

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL)

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{12,}"),
    re.compile(r"sk-proj-[A-Za-z0-9]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"://[^\s:/]+:[^\s@/]{6,}@"),
    # JWT / bearer tokens (base64url-encoded JSON header starts with eyJ)
    re.compile(r"eyJ[A-Za-z0-9\-_=]{16,}"),
    # Generic key=value / key: value credential assignments
    re.compile(
        r"\b(?:password|passwd|pwd|secret|api[_-]?key|service[_-]?role[_-]?key)"
        r"\s*[:=]\s*[\"']?(\S+)[\"']?",
        re.I,
    ),
]


def redact(text: str) -> str:
    text = _PRIVATE_RE.sub("[redacted]", text)
    for pat in _PATTERNS:
        text = pat.sub("[redacted]", text)
    return text
