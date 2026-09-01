from __future__ import annotations

import re
from collections.abc import Buffer, Iterator
from dataclasses import dataclass

_FIXED_CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_CREDENTIAL_NAME = rb"[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)"
_CREDENTIAL_ASSIGNMENT_MARKER = re.compile(
    rb"(?i)(?P<name>" + _CREDENTIAL_NAME + rb")\s*[:=]"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?i)[ \t]*(?P<name>" + _CREDENTIAL_NAME + rb")[ \t]*[:=]"
    rb"(?P<rhs>[^\n]*)"
)
_CREDENTIAL_MARKER_CONTROL_BYTES = bytes(range(0x20)) + b"\x7f"
_INERT_LITERAL = re.compile(rb"[A-Za-z0-9._/-]*")
_MIN_CREDENTIAL_LENGTH = 12
_MAX_BINARY_ASSIGNMENT_BYTES = 4096
_MAX_COMPACTED_SQLITE_FINDINGS = 1024


@dataclass(frozen=True)
class _BinaryCredentialFinding:
    candidate: bytes
    payload: bytes | None
    assignment: bytes | None


def contains_possible_credential(content: Buffer) -> bool:
    """Return whether inert content contains a credential-like value.

    Credential-like assignment markers are found before the original line is
    validated. Assignment values must then be complete inert literals. The
    exact public-documentation RHS ``NAME=your_name`` bypasses the normal
    literal length finding. Composition, controls, operators, interpolation,
    substitution, escapes, split quotes, and ambiguous surrounding syntax are
    findings at any length.
    """
    if contains_fixed_credential(content):
        return True
    normalized = _normalize_line_separators(content)
    for line in normalized.split(b"\n"):
        if not contains_credential_assignment_marker(line):
            continue
        match = _CREDENTIAL_ASSIGNMENT.fullmatch(line)
        if match is None or _assignment_rhs_is_sensitive(
            match.group("name"), match.group("rhs")
        ):
            return True
    return False


def contains_fixed_credential(content: Buffer) -> bool:
    """Return whether content contains a fixed-format credential signature."""
    return any(pattern.search(content) for pattern in _FIXED_CREDENTIAL_PATTERNS)


def contains_binary_credential_residue(content: Buffer) -> bool:
    """Scan assignment fragments embedded in arbitrary binary bytes.

    Marker discovery is one forward pass with constant state. It recognizes
    controls interleaved by UTF-16/UTF-32 and fails closed on every such
    obfuscated marker. Contiguous ASCII assignments are classified by the
    canonical scanner over one bounded candidate; binary record bytes can
    terminate an exact placeholder, but can never downgrade a canonical
    finding.
    """
    return next(_iter_binary_credential_findings(content), None) is not None


def binary_residue_matches_compacted_sqlite(
    content: Buffer,
    compacted_content: Buffer,
) -> bool:
    """Return whether raw findings are reproduced by compacted live records.

    The compacted database must be produced from the same valid SQLite input.
    Its exact bounded finding multiset contains live record structure but no
    deleted cells, freelist content, or page slack. Opaque findings can never
    be reconciled.
    """
    allowed: dict[bytes, int] = {}
    allowed_count = 0
    for finding in _iter_binary_credential_findings(compacted_content):
        if finding.assignment is None:
            continue
        allowed_count += 1
        if allowed_count > _MAX_COMPACTED_SQLITE_FINDINGS:
            return False
        allowed[finding.assignment] = allowed.get(finding.assignment, 0) + 1

    found = False
    for finding in _iter_binary_credential_findings(content):
        found = True
        if finding.assignment is None:
            return False
        remaining = allowed.get(finding.assignment, 0)
        if remaining == 0:
            return False
        allowed[finding.assignment] = remaining - 1
    return found


def _iter_binary_credential_findings(
    content: Buffer,
) -> Iterator[_BinaryCredentialFinding]:
    if contains_fixed_credential(content):
        yield _BinaryCredentialFinding(candidate=b"", payload=None, assignment=None)
        return
    size = len(content)
    for context_start, _marker_start, marker_end, obfuscated in (
        _iter_binary_assignment_markers(content)
    ):
        if obfuscated:
            yield _BinaryCredentialFinding(candidate=b"", payload=None, assignment=None)
            continue
        if marker_end - context_start > _MAX_BINARY_ASSIGNMENT_BYTES:
            yield _BinaryCredentialFinding(candidate=b"", payload=None, assignment=None)
            continue
        start = context_start
        end = marker_end
        limit = min(size, start + _MAX_BINARY_ASSIGNMENT_BYTES)
        while end < limit:
            value = content[end]
            if value != 0x09 and not 0x20 <= value < 0x7F:
                break
            end += 1
        if end == limit and end < size:
            next_value = content[end]
            if next_value == 0x09 or 0x20 <= next_value < 0x7F:
                yield _BinaryCredentialFinding(candidate=b"", payload=None, assignment=None)
                continue
        payload = bytes(content[start:end])
        assignment = bytes(content[_marker_start:end])
        # Preserve CR/LF record boundaries, but include any other binary
        # delimiter. The canonical scanner must see a control adjacent to an
        # otherwise inert placeholder and reject it.
        if end < size and content[end] not in {0x0A, 0x0D}:
            end += 1
        candidate = bytes(content[start:end])
        if contains_possible_credential(candidate):
            yield _BinaryCredentialFinding(
                candidate=candidate,
                payload=payload,
                assignment=assignment,
            )


def _iter_binary_assignment_markers(
    content: Buffer,
) -> Iterator[tuple[int, int, int, bool]]:
    """Yield assignment marker spans in one bounded-memory forward pass."""
    context_start = 0
    token_start: int | None = None
    suffix = bytearray()
    after_name = False
    obfuscated = False

    for index in range(len(content)):
        value = content[index]
        if value in {0x0A, 0x0D}:
            context_start = index + 1
            token_start = None
            suffix.clear()
            after_name = False
            obfuscated = False
            continue
        if value < 0x20 or value == 0x7F:
            context_start = index + 1
            if token_start is not None:
                obfuscated = True
            continue
        if value > 0x7E:
            context_start = index + 1
            token_start = None
            suffix.clear()
            after_name = False
            obfuscated = False
            continue

        upper = value - 0x20 if 0x61 <= value <= 0x7A else value
        is_alpha = 0x41 <= upper <= 0x5A
        is_name_byte = is_alpha or 0x30 <= value <= 0x39 or value == 0x5F

        if token_start is None:
            if is_alpha:
                token_start = index
                suffix.clear()
                suffix.append(upper)
                after_name = False
                obfuscated = False
            continue

        if not after_name and is_name_byte:
            suffix.append(upper)
            if len(suffix) > len(b"PASSWORD"):
                del suffix[0]
            continue

        has_credential_suffix = _is_credential_name_suffix(suffix)
        if value in {0x3A, 0x3D} and has_credential_suffix:
            yield context_start, token_start, index + 1, obfuscated
            token_start = None
            suffix.clear()
            after_name = False
            obfuscated = False
            continue
        if value in {0x20, 0x09} and (after_name or has_credential_suffix):
            after_name = True
            continue

        token_start = None
        suffix.clear()
        after_name = False
        obfuscated = False
        if is_alpha:
            token_start = index
            suffix.append(upper)


def _is_credential_name_suffix(suffix: bytearray) -> bool:
    value = bytes(suffix)
    return value.endswith((b"KEY", b"TOKEN", b"SECRET", b"PASSWORD"))


def contains_credential_assignment_marker(content: Buffer) -> bool:
    """Find a credential marker without letting control bytes split its name."""
    comparison = bytes(content).translate(
        None,
        _CREDENTIAL_MARKER_CONTROL_BYTES,
    )
    return _CREDENTIAL_ASSIGNMENT_MARKER.search(comparison) is not None


def _normalize_line_separators(content: Buffer) -> bytes:
    """Represent CRLF, lone CR, and LF separators uniformly without decoding."""
    return bytes(content).replace(b"\r\n", b"\n").replace(b"\r", b"\n")


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
