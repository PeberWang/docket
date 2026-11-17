"""Strip credentials from conversation text before it leaves the machine.

Anything sent to the judge (Jev) passes through `redact` first. The patterns are
deliberately broad: a false positive only hides a harmless string from the judge,
a false negative leaks a key.
"""

import re

_PATTERNS = [
    # Provider-style API keys: sk-..., sk-ant-..., rk_live_..., etc.
    (re.compile(r"\b(?:sk|rk|pk)[-_](?:[A-Za-z]+[-_])?[A-Za-z0-9]{16,}\b"), "[KEY]"),
    # GitHub, Slack, npm, Google tokens
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"), "[KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[KEY]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"), "[KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[KEY]"),
    (re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}\b"), "[KEY]"),
    # Bearer / basic auth headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [KEY]"),
    # key=value style secrets
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret|access[_-]?key)"
            r"(\s*[:=]\s*|\"\s*:\s*\")([^\s\"',;]{6,})"
        ),
        r"\1\2[KEY]",
    ),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[KEY]"),
    # Long hex or base64-ish blobs (32+ chars) that look like secrets
    (re.compile(r"\b[a-f0-9]{32,}\b"), "[HEX]"),
    # No "/" so file paths survive; must mix letters and digits so long words survive.
    (re.compile(r"\b(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Za-z])[A-Za-z0-9+_-]{40,}={0,2}(?![A-Za-z0-9])"), "[BLOB]"),
    # Private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE KEY]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
