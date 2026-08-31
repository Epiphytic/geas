from pathlib import Path

import pytest

from research_agent.credential_scanning import contains_possible_credential

_FORBIDDEN_CONTROL_VALUES = (
    *range(0x00, 0x09),
    0x0B,
    0x0C,
    *range(0x0E, 0x20),
    0x7F,
)
_FORBIDDEN_CONTROL_PARAMS = tuple(
    pytest.param(bytes((value,)), id=f"0x{value:02x}")
    for value in _FORBIDDEN_CONTROL_VALUES
)
_CONTROL_POSITION_PARAMS = (
    pytest.param(
        b"",
        b"FIRECRAWL_KEY=your_firecrawl_key\n",
        id="prefix",
    ),
    pytest.param(
        b"FIRE",
        b"CRAWL_KEY=your_firecrawl_key\n",
        id="inside-name",
    ),
    pytest.param(
        b"FIRECRAWL_KEY",
        b"=your_firecrawl_key\n",
        id="before-operator",
    ),
    pytest.param(
        b"FIRECRAWL_KEY=",
        b"your_firecrawl_key\n",
        id="before-rhs",
    ),
    pytest.param(
        b"FIRECRAWL_KEY=your_firecrawl_key",
        b"\n",
        id="suffix",
    ),
)


@pytest.mark.parametrize(
    "assignment",
    (
        b'FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\n',
        b"FIRECRAWL_KEY='your_''firecrawl_key''operator-secret-value-123'\n",
        b"FIRECRAWL_KEY=your_firecrawl_keyoperator-secret-value-123\n",
        b"FIRECRAWL_KEY=operator-prefix-your_firecrawl_key\n",
        b'FIRECRAWL_KEY="your_firecrawl_key" operator-secret-value-123\n',
        b'FIRECRAWL_KEY="your_firecrawl_key${OPERATOR_SECRET}"\n',
        b'FIRECRAWL_KEY="your_firecrawl_key$(printenv)"\n',
        b"FIRECRAWL_KEY=your_${K}\n",
        b"FIRECRAWL_KEY=your_$(x)\n",
        b"FIRECRAWL_KEY=your_;id\n",
        b"FIRECRAWL_KEY=your_\\x\n",
        b"FIRECRAWL_KEY=your_firecrawl_key;printenv\n",
        b"FIRECRAWL_KEY=your_firecrawl_key#not-a-shell-comment\n",
        b"FIRECRAWL_KEY='your_firecrawl_key\"\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\r\n',
    ),
)
def test_placeholder_must_be_the_complete_assignment_rhs(assignment: bytes) -> None:
    assert contains_possible_credential(assignment) is True


@pytest.mark.parametrize(
    "content",
    (
        b"FIRECRAWL_KEY=operator-secret-value-123\r",
        b"FIRECRAWL_KEY=operator-secret-value-123\rNEXT=value\n",
        b"FIRECRAWL_KEY=operator-secret-value-123\r\rNEXT=value\n",
        b"FIRECRAWL_KEY=operator-secret-value-123\r\nNEXT=value\n",
        b"prefix=value\nFIRECRAWL_KEY=operator-secret-value-123\nNEXT=value",
        b"prefix=value\r\nFIRECRAWL_KEY=operator-secret-value-123\rNEXT=value\n",
        b"prefix=\x00\rFIRECRAWL_KEY=operator-secret-value-123\r\x01NEXT=value\n",
        b"FIRECRAWL_KEY=operator-secret-value-123\x00\rNEXT=value\n",
    ),
    ids=(
        "terminal-cr",
        "interior-cr",
        "repeated-cr",
        "crlf",
        "lf",
        "mixed-separators",
        "controls-around-separators",
        "nul-before-separator",
    ),
)
def test_line_separator_variants_cannot_hide_sensitive_assignment(
    content: bytes,
) -> None:
    assert contains_possible_credential(content) is True


@pytest.mark.parametrize(("before", "after"), _CONTROL_POSITION_PARAMS)
@pytest.mark.parametrize("control", _FORBIDDEN_CONTROL_PARAMS)
def test_forbidden_control_cannot_hide_or_enter_placeholder_assignment(
    before: bytes,
    after: bytes,
    control: bytes,
) -> None:
    assert contains_possible_credential(before + control + after) is True


@pytest.mark.parametrize(
    "content",
    (
        b"\tFIRECRAWL_KEY=your_firecrawl_key\n",
        b"FIRECRAWL_KEY\t=your_firecrawl_key\n",
        b"FIRECRAWL_KEY=\tyour_firecrawl_key\n",
        b"FIRECRAWL_KEY=your_firecrawl_key\t\n",
    ),
    ids=("prefix", "before-operator", "before-rhs", "suffix"),
)
def test_tab_remains_permitted_horizontal_assignment_syntax(content: bytes) -> None:
    assert contains_possible_credential(content) is False


def test_tab_cannot_split_credential_variable_name() -> None:
    assert (
        contains_possible_credential(
            b"FIRE\tCRAWL_KEY=your_firecrawl_key\n"
        )
        is True
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        pytest.param(
            b"",
            b"FIRECRAWL_KEY=your_firecrawl_key\n",
            id="prefix",
        ),
        pytest.param(
            b"FIRECRAWL_KEY",
            b"=your_firecrawl_key\n",
            id="before-operator",
        ),
        pytest.param(
            b"FIRECRAWL_KEY=",
            b"your_firecrawl_key\n",
            id="before-rhs",
        ),
        pytest.param(
            b"FIRECRAWL_KEY=your_firecrawl_key",
            b"\n",
            id="suffix",
        ),
    ),
)
@pytest.mark.parametrize(
    "separator",
    (b"\n", b"\r", b"\r\n"),
    ids=("lf", "cr", "crlf"),
)
def test_normalized_line_separators_keep_assignments_on_independent_lines(
    before: bytes,
    after: bytes,
    separator: bytes,
) -> None:
    assert contains_possible_credential(before + separator + after) is False


@pytest.mark.parametrize(
    "separator",
    (b"\n", b"\r", b"\r\n"),
    ids=("lf", "cr", "crlf"),
)
def test_line_separator_inside_name_exposes_sensitive_credential_suffix(
    separator: bytes,
) -> None:
    assert (
        contains_possible_credential(
            b"FIRE" + separator + b"CRAWL_KEY=your_firecrawl_key\n"
        )
        is True
    )


def test_marker_elsewhere_on_line_makes_nonassignment_line_sensitive() -> None:
    assert (
        contains_possible_credential(
            b"documentation: FIRECRAWL_KEY=your_firecrawl_key\n"
        )
        is True
    )


def test_normal_utf8_inert_content_remains_nonsecret() -> None:
    assert contains_possible_credential("Snowman ☃ and café.\n".encode()) is False


@pytest.mark.parametrize(
    "assignment",
    (
        b"FIRECRAWL_KEY=your_firecrawl_key\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"\n',
        b"FIRECRAWL_KEY='your_firecrawl_key'   \n",
        b"FIRECRAWL_KEY=your_firecrawl_key # public documentation\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"  # public documentation\n',
        b'FIRECRAWL_KEY="your_firecrawl_key"  \r\n',
        b"FIRECRAWL_KEY=your_firecrawl_key\rNEXT=value\n",
        b"FIRECRAWL_KEY=your_firecrawl_key\r\rNEXT=value\n",
        b'FIREWORKS_KEY="api_key"\n',
    ),
)
def test_inert_literal_allows_only_whitespace_eol_or_comment(
    assignment: bytes,
) -> None:
    assert contains_possible_credential(assignment) is False


def test_exact_archived_public_placeholders_remain_nonsecret() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    source = (
        repository_root
        / "ontology"
        / "open-source-research-agents"
        / "generated"
        / "dzhng-deep-research"
        / "sources"
        / "dzhng-deep-research-7813045fe377.md"
    ).read_bytes()

    assert contains_possible_credential(source) is False
