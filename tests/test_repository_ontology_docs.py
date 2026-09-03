from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

from research_agent.cli import _build_parser
from research_agent.source_intent import DiscoveryKind, SourceIntent

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPOSITORY_ROOT / "docs" / "REPOSITORY_ONTOLOGIES.md"
README = REPOSITORY_ROOT / "README.md"
QUICKSTART = REPOSITORY_ROOT / "docs" / "QUICKSTART_ONTOLOGY.md"
AGENT_SKILLS = REPOSITORY_ROOT / "docs" / "AGENT_SKILLS.md"
PROMOTIONS = REPOSITORY_ROOT / "docs" / "PROMOTIONS.md"
SOURCE_OF_TRUTH = REPOSITORY_ROOT / "docs" / "SOURCE_OF_TRUTH.md"
NEXT_PHASE = REPOSITORY_ROOT / "docs" / "NEXT_PHASE.md"
CI = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _commands_between(text: str, start_marker: str, end_marker: str) -> tuple[str, ...]:
    start = text.index(start_marker)
    end = text.index(end_marker)
    return tuple(
        line.removeprefix("$ ")
        for line in text[start:end].splitlines()
        if line.startswith("$ geas ")
    )


def _normalized_text(*documents: str) -> str:
    return " ".join("\n".join(documents).split())


def test_repository_ontology_command_reference_matches_the_cli() -> None:
    """A renamed or removed CLI option must invalidate the operator guide."""
    text = GUIDE.read_text()
    commands = _commands_between(
        text,
        "<!-- CLI_REFERENCE_START -->",
        "<!-- CLI_REFERENCE_END -->",
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


def test_task7_repository_workflow_reference_has_the_approved_exact_commands() -> None:
    commands = _commands_between(
        GUIDE.read_text(),
        "<!-- TASK7_CLI_REFERENCE_START -->",
        "<!-- TASK7_CLI_REFERENCE_END -->",
    )

    assert commands == (
        "geas repository-install gold https://github.com/example/gold.git "
        "--ref refs/heads/main --trust-repository --link",
        "geas repository-install --current-repository --trust-repository --delegate-depth 1 --link",
        "geas repository-install archive https://github.com/example/archive.git "
        "--ref refs/tags/v1.0.0 --read-only --publish none",
        "geas repository-update gold",
        "geas repository-update gold --publish none",
        "geas repository-update gold --direct-push",
        "geas repository-remove gold",
        "geas ontology-update gold",
    )


def test_task7_repository_workflow_reference_matches_cli_after_fan_in() -> None:
    parser = _build_parser()
    commands = _commands_between(
        GUIDE.read_text(),
        "<!-- TASK7_CLI_REFERENCE_START -->",
        "<!-- TASK7_CLI_REFERENCE_END -->",
    )
    choices = next(
        action.choices
        for action in parser._actions  # noqa: SLF001 - executable CLI documentation
        if getattr(action, "choices", None)
    )
    if "repository-install" not in choices:
        pytest.skip("Task 7 CLI fan-in is not present on this branch")

    parsed = tuple(parser.parse_args(shlex.split(command)[1:]) for command in commands)
    assert [item.command for item in parsed] == [
        "repository-install",
        "repository-install",
        "repository-install",
        "repository-update",
        "repository-update",
        "repository-update",
        "repository-remove",
        "ontology-update",
    ]


def test_docs_cover_bootstrap_delegation_publication_and_recovery_boundaries() -> None:
    repository = _normalized_text(GUIDE.read_text())
    combined = _normalized_text(
        README.read_text(),
        repository,
        QUICKSTART.read_text(),
    )

    for phrase in (
        "operator-approved commit",
        "geas config-init",
        "geas repository-install",
        "--trust-repository",
        "--read-only",
        "one delegation edge",
        "--delegate-depth",
        "geas ontology-update",
        "pull request is the default",
        "--direct-push",
        "geas repository-remove",
    ):
        assert phrase in combined

    assert "repository content cannot grant" in repository
    assert "recovery_command" in repository
    assert "receipt-owned" in repository
    assert "git.pull_request" in repository
    assert "git.direct_push" in repository
    assert "knowledge.auto_promote" in repository


def test_quickstart_source_intent_example_matches_the_strict_schema() -> None:
    text = QUICKSTART.read_text()
    start = text.index("<!-- SOURCE_INTENT_REFERENCE_START -->")
    end = text.index("<!-- SOURCE_INTENT_REFERENCE_END -->")
    fenced = text[start:end]
    payload = fenced.split("```yaml", maxsplit=1)[1].rsplit("```", maxsplit=1)[0]

    document = yaml.safe_load(payload)
    intent = SourceIntent.model_validate(document["source_intent"][0])

    assert intent.id == "issuer-news"
    assert intent.discovery.kind is DiscoveryKind.RSS_ATOM


def test_docs_preserve_static_skill_and_knowledge_authority_boundaries() -> None:
    skills = _normalized_text(AGENT_SKILLS.read_text())
    promotions = _normalized_text(PROMOTIONS.read_text())
    truth = _normalized_text(SOURCE_OF_TRUTH.read_text())

    assert "readable without Geas" in skills
    assert "uv tool install" in skills
    assert "operator-approved" in skills
    assert "never installs Geas" in skills
    assert "GitHub App" in promotions
    assert "deterministic artifacts" in promotions
    assert "knowledge.auto_promote" in promotions
    assert "does not make semantic knowledge canonical" in promotions
    assert "source intent" in truth
    assert "capability decisions" in truth
    assert "proposal-only" in truth
    assert "SQLite" in truth


def test_docs_mark_browser_common_crawl_and_forge_policy_out_of_scope() -> None:
    text = _normalized_text(
        README.read_text(),
        QUICKSTART.read_text(),
        GUIDE.read_text(),
        NEXT_PHASE.read_text(),
    )
    assert "Common Crawl remains future" in text
    assert "browser automation remains future" in text
    assert "automated forge approval policy is operator-managed" in text


def test_ci_parallelizes_security_workflows_before_read_only_full_suite_fan_in() -> None:
    workflow = yaml.safe_load(CI.read_text())
    jobs = workflow["jobs"]
    expected = {
        "capability-security",
        "acquisition-work",
        "bootstrap-publishing",
        "maintained-demo-docs",
        "windows-sqlite-boundaries",
        "full-suite",
    }
    assert expected <= jobs.keys()
    assert set(jobs["full-suite"]["needs"]) == expected - {"full-suite"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in CI.read_text()
    assert "id-token: write" not in CI.read_text()
    assert "contents: write" not in CI.read_text()
