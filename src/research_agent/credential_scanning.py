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
_INERT_LITERAL = re.compile(rb"[A-Za-z0-9._/-]*")
_MIN_CREDENTIAL_LENGTH = 12


def contains_possible_credential(content: Buffer) -> bool:
    """Return whether inert content contains a credential-like value.

    Assignment values must be complete inert literals. The exact
    public-documentation RHS ``NAME=your_name`` bypasses the normal literal
    length finding. Composition, operators, interpolation, substitution,
    escapes, split quotes, and ambiguous trailing syntax are findings at any
    length.
    """
    if contains_fixed_credential(content):
        return True
    for match in _CREDENTIAL_ASSIGNMENT.finditer(content):
        if _assignment_rhs_is_sensitive(match.group("name"), match.group("rhs")):
            return True
    return False


def contains_fixed_credential(content: Buffer) -> bool:
    """Return whether content contains a fixed-format credential signature."""
    return any(pattern.search(content) for pattern in _FIXED_CREDENTIAL_PATTERNS)


def _assignment_rhs_is_sensitive(name: bytes, rhs: bytes) -> bool:
    literal = _parse_inert_literal(rhs)
    if literal is None:
        return True
    expected = b"your_" + name.lower()
    return literal != expected and len(literal) >= _MIN_CREDENTIAL_LENGTH


def _parse_inert_literal(rhs: bytes) -> bytes | None:
    """Parse one complete inert literal without interpreting shell syntax.

    Literals contain only ASCII letters, digits, ``._/-`` and may use one pair
    of matching quotes. Horizontal whitespace may surround the literal. A
    trailing comment requires whitespace before ``#``. Any other byte or token
    makes the RHS ambiguous and therefore sensitive.
    """
    value = rhs.lstrip(b" \t")
    if not value:
        return b""
    if value[:1] in {b"'", b'"'}:
        quote = value[:1]
        end = value.find(quote, 1)
        if end < 0:
            return None
        literal = value[1:end]
        remainder = value[end + 1 :]
    else:
        match = _INERT_LITERAL.match(value)
        assert match is not None
        literal = match.group()
        remainder = value[match.end() :]
    if not _INERT_LITERAL.fullmatch(literal):
        return None
    if not remainder or not remainder.strip(b" \t"):
        return literal
    if remainder[:1] not in {b" ", b"\t"}:
        return None
    if not remainder.lstrip(b" \t").startswith(b"#"):
        return None
    return literal
