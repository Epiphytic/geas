from pathlib import Path

import pytest

from research_agent.credential_scanning import contains_possible_credential


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
