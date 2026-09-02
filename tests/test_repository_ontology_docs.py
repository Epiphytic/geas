from __future__ import annotations

import shlex
from pathlib import Path

from research_agent.cli import _build_parser

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPOSITORY_ROOT / "docs" / "REPOSITORY_ONTOLOGIES.md"


def test_repository_ontology_command_reference_matches_the_cli() -> None:
    """A renamed or removed CLI option must invalidate the operator guide."""
    text = GUIDE.read_text()
    start = text.index("<!-- CLI_REFERENCE_START -->")
    end = text.index("<!-- CLI_REFERENCE_END -->")
    commands = tuple(
        line.removeprefix("$ ")
        for line in text[start:end].splitlines()
        if line.startswith("$ geas ")
    )

    assert commands
    parser = _build_parser()
    parsed = tuple(parser.parse_args(shlex.split(command)[1:]) for command in commands)

    assert {item.command for item in parsed} == {
        "catalog-refresh",
        "catalog-verify",
        "list",
        "ontology-snapshot-remove",
        "ontology-subscribe",
        "ontology-sync",
        "ontology-unsubscribe",
        "skill-export",
        "topic-export",
    }
