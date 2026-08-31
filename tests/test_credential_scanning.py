from pathlib import Path

import pytest

from research_agent.credential_scanning import contains_possible_credential


@pytest.mark.parametrize(
    "assignment",
    (
        b'FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\n',
        b"FIRECRAWL_KEY=your_firecrawl_keyoperator-secret-value-123\n",
        b"FIRECRAWL_KEY=operator-prefix-your_firecrawl_key\n",
        b'FIRECRAWL_KEY="your_firecrawl_key" operator-secret-value-123\n',
        b'FIRECRAWL_KEY="your_firecrawl_key${OPERATOR_SECRET}"\n',
        b'FIRECRAWL_KEY="your_firecrawl_key$(printenv)"\n',
        b"FIRECRAWL_KEY=your_firecrawl_key;printenv\n",
        b"FIRECRAWL_KEY=your_firecrawl_key#not-a-shell-comment\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\r\n',
    ),
)
def test_placeholder_must_be_the_complete_assignment_rhs(assignment: bytes) -> None:
    assert contains_possible_credential(assignment) is True


@pytest.mark.parametrize(
    "assignment",
    (
        b"FIRECRAWL_KEY=your_firecrawl_key\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"\n',
        b"FIRECRAWL_KEY='your_firecrawl_key'   \n",
        b"FIRECRAWL_KEY=your_firecrawl_key # public documentation\n",
        b'FIRECRAWL_KEY="your_firecrawl_key"  # public documentation\n',
        b'FIRECRAWL_KEY="your_firecrawl_key"  \r\n',
    ),
)
def test_literal_placeholder_allows_only_whitespace_eol_or_comment(
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
