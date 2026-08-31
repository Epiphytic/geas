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
    rb"(?P<rhs>[^\r\n]*)\r?$"
)
_CREDENTIAL_VALUE_PREFIX = re.compile(rb"^[ \t]*['\"]?[^\s'\"]{12,}")


def contains_possible_credential(content: Buffer) -> bool:
    """Return whether inert content contains a credential-like value.

    The sole assignment exception is the complete public-documentation RHS
    ``NAME=your_name``, optionally quoted and followed only by whitespace or a
    whitespace-delimited ``#`` comment. It is derived from the variable name
    rather than a general placeholder allowlist, so concatenated or
    similar-looking operator values remain credential findings.
    """
    if contains_fixed_credential(content):
        return True
    for match in _CREDENTIAL_ASSIGNMENT.finditer(content):
        if _is_documented_placeholder(match.group("name"), match.group("rhs")):
            continue
        if _CREDENTIAL_VALUE_PREFIX.match(match.group("rhs")):
            return True
    return False


def contains_fixed_credential(content: Buffer) -> bool:
    """Return whether content contains a fixed-format credential signature."""
    return any(pattern.search(content) for pattern in _FIXED_CREDENTIAL_PATTERNS)


def _is_documented_placeholder(name: bytes, rhs: bytes) -> bool:
    expected = b"your_" + name.lower()
    value = rhs.lstrip(b" \t")
    if value.startswith(b'"'):
        literal = b'"' + expected + b'"'
    elif value.startswith(b"'"):
        literal = b"'" + expected + b"'"
    else:
        literal = expected
    if not value.startswith(literal):
        return False
    remainder = value[len(literal) :]
    if not remainder or not remainder.strip(b" \t"):
        return True
    if remainder[:1] not in {b" ", b"\t"}:
        return False
    return remainder.lstrip(b" \t").startswith(b"#")
