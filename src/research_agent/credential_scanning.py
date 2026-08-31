from __future__ import annotations

import re
from collections.abc import Buffer

_FIXED_CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)^\s*(?P<name>[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\s*[:=]"
    rb"\s*['\"]?(?P<value>[^\s'\"]{12,})"
)


def contains_possible_credential(content: Buffer) -> bool:
    """Return whether inert content contains a credential-like value.

    The sole assignment exception is the explicit public-documentation form
    ``NAME=your_name``. It is derived from the variable name rather than a
    general placeholder allowlist, so similar-looking operator values remain
    credential findings.
    """
    if any(pattern.search(content) for pattern in _FIXED_CREDENTIAL_PATTERNS):
        return True
    return any(
        match.group("value") != b"your_" + match.group("name").lower()
        for match in _CREDENTIAL_ASSIGNMENT.finditer(content)
    )
