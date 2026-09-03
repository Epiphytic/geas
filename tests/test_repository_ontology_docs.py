from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    DeterministicCapabilityEvaluator,
)
from research_agent.cli import _build_parser
from research_agent.source_intent import DiscoveryKind, SourceIntent

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPOSITORY_ROOT / "docs" / "REPOSITORY_ONTOLOGIES.md"
README = REPOSITORY_ROOT / "README.md"
QUICKSTART = REPOSITORY_ROOT / "docs" / "QUICKSTART_ONTOLOGY.md"
AGENT_SKILLS = REPOSITORY_ROOT / "docs" / "AGENT_SKILLS.md"
GETTING_STARTED = REPOSITORY_ROOT / "docs" / "GETTING_STARTED.md"
USER_CONFIG = REPOSITORY_ROOT / "docs" / "USER_CONFIG.md"
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


def _yaml_between(text: str, start_marker: str, end_marker: str) -> object:
    start = text.index(start_marker)
    end = text.index(end_marker)
    fenced = text[start:end]
    return yaml.safe_load(fenced.split("```yaml", maxsplit=1)[1].rsplit("```", maxsplit=1)[0])


def _concrete_geas_commands(document: Path) -> tuple[str, ...]:
    """Return complete executable Geas invocations from shell/console fences."""
    commands: list[str] = []
    active = False
    continued = ""
    for raw_line in document.read_text().splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if active:
                assert not continued, f"unfinished Geas command in {document}"
                active = False
            else:
                active = stripped in {"```bash", "```console", "```shell"}
            continue
        if not active:
            continue
        line = stripped.removeprefix("$ ")
        if continued:
            line = f"{continued} {line}"
        if line.endswith("\\"):
            continued = line[:-1].rstrip()
            continue
        continued = ""
        if line.startswith("uv run geas "):
            line = line.removeprefix("uv run ")
        if (line == "geas" or line.startswith("geas ")) and "[--" not in line:
            # Bracketed synopsis lines describe optional grammar; every other
            # invocation is a concrete example consumed by the real parser.
            commands.append(line)
    return tuple(commands)


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


def test_first_publication_prescope_example_is_valid_and_grants_only_pull_requests() -> None:
    document = _yaml_between(
        GUIDE.read_text(),
        "<!-- PUBLICATION_PRESCOPE_GRANT_START -->",
        "<!-- PUBLICATION_PRESCOPE_GRANT_END -->",
    )
    grant = CapabilityGrant.model_validate(document)

    assert grant.capabilities == (Capability.GIT_PULL_REQUEST,)
    assert grant.delegable_capabilities == ()
    assert grant.subject.repository == "https://github.com/example/gold"
    assert grant.subject.refs == ("refs/heads/main",)
    assert grant.subject.paths == "*"
    assert grant.subject.bundle_sha256 == "*"
    assert grant.resources == CapabilityResources(git_refs=("refs/heads/main",))
    assert grant.max_delegation_depth == 0

    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    evaluator = DeterministicCapabilityEvaluator((grant,), {}, clock=lambda: now)

    def decision(
        capability: Capability,
        *,
        repository: str = "https://github.com/example/gold",
        ref: str = "refs/heads/main",
    ) -> str:
        return evaluator.evaluate(
            CapabilityRequest(
                authority_repository=repository,
                target_repository=repository,
                capabilities=(capability,),
                ref=ref,
                path=".geas-publication-preauthorization",
                dirty=False,
                requested_at=now,
            )
        ).decision

    assert decision(Capability.GIT_PULL_REQUEST) == "allow"
    assert decision(Capability.GIT_DIRECT_PUSH) == "deny"
    assert decision(
        Capability.GIT_PULL_REQUEST,
        repository="https://github.com/example/other",
    ) == "deny"
    assert decision(Capability.GIT_PULL_REQUEST, ref="refs/heads/other") == "deny"


def test_path_specific_publication_flow_is_parseable_and_fail_closed() -> None:
    commands = _commands_between(
        GUIDE.read_text(),
        "<!-- PATH_SPECIFIC_PUBLICATION_FLOW_START -->",
        "<!-- PATH_SPECIFIC_PUBLICATION_FLOW_END -->",
    )
    parser = _build_parser()
    choices = next(
        action.choices
        for action in parser._actions  # noqa: SLF001 - executable CLI documentation
        if getattr(action, "choices", None)
    )
    if "repository-install" not in choices:
        pytest.skip("Task 7 CLI fan-in is not present on this branch")

    assert commands == (
        "geas repository-install gold https://github.com/example/gold.git "
        "--ref refs/heads/main --trust-repository --link --publish none",
        "geas repository-update gold",
    )
    install, publish = (
        parser.parse_args(shlex.split(command)[1:]) for command in commands
    )

    assert install.command == "repository-install"
    assert install.publish == "none"
    assert install.direct_push is False
    assert publish.command == "repository-update"
    assert publish.publish == "pull-request"
    assert publish.direct_push is False

    repository = _normalized_text(GUIDE.read_text())
    readme = _normalized_text(README.read_text())
    for text in (repository, readme):
        assert "unknown closed publication manifest" in text
        assert "root-local" in text
        assert "exact repository and writable ref" in text
        assert "paths: `\"*\"`" in text
        assert "bundle_sha256: `\"*\"`" in text
    assert "only the named Git capability" in repository
    assert "complete, durable, non-pending receipt" in repository
    assert "every receipt-owned leaf" in repository
    assert "Ambiguity falls back to the wildcard pre-scope" in repository


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
    assert "receipt-owned" in repository
    assert "git.pull_request" in repository
    assert "git.direct_push" in repository
    assert "knowledge.auto_promote" in repository


def test_optional_install_snippets_do_not_treat_placeholders_as_shell_redirection() -> None:
    for document in (README, AGENT_SKILLS):
        text = document.read_text()
        assert "@<operator-approved-commit>" not in text
        assert "approved_commit='REPLACE_WITH_OPERATOR_APPROVED_FULL_COMMIT_ID'" in text
        assert (
            '--from "git+https://github.com/Epiphytic/geas.git@${approved_commit}"'
            in text
        )


def test_legacy_subscription_push_is_not_documented_as_a_publication_path() -> None:
    repository = _normalized_text(GUIDE.read_text())

    assert "$ geas ontology-sync geas-samples --push" not in GUIDE.read_text()
    assert "Push is available only for writable branch refs" not in repository
    assert "legacy `--push` is ignored" in repository
    assert "compatibility input to this read-only synchronization path" in repository


def test_legacy_git_settings_are_read_only_not_publication_authority() -> None:
    getting_started = _normalized_text(GETTING_STARTED.read_text())
    user_config = _normalized_text(USER_CONFIG.read_text())
    repository = _normalized_text(GUIDE.read_text())

    assert "uv run geas ontology-sync --push" not in getting_started
    assert "uv run geas ontology-sync --pull --push" not in user_config
    assert "legacy `--push` is ignored" in getting_started
    assert "legacy `push_on_update` is ignored" in getting_started
    assert "legacy `--push` is ignored" in user_config
    assert "legacy `push_on_update` is ignored" in user_config
    assert "root-local `git.direct_push`" in repository
    assert "Delegation cannot authorize direct push" in repository
    assert "generated branches" not in repository


def test_every_concrete_documented_geas_command_matches_the_real_parser() -> None:
    documents = (
        README,
        QUICKSTART,
        AGENT_SKILLS,
        GETTING_STARTED,
        USER_CONFIG,
        GUIDE,
    )
    parser = _build_parser()
    choices = next(
        action.choices
        for action in parser._actions  # noqa: SLF001 - executable CLI documentation
        if getattr(action, "choices", None)
    )
    pending_task7 = {
        "ontology-update",
        "repository-install",
        "repository-remove",
        "repository-update",
    }
    command_count = 0
    skipped: set[str] = set()
    for document in documents:
        commands = _concrete_geas_commands(document)
        assert commands, f"no concrete Geas commands found in {document}"
        command_count += len(commands)
        for command in commands:
            arguments = shlex.split(command)[1:]
            command_name = next(
                (
                    token
                    for token in arguments
                    if token in choices or token in pending_task7
                ),
                None,
            )
            if command_name is None:
                with pytest.raises(SystemExit) as help_exit:
                    parser.parse_args(arguments)
                assert help_exit.value.code == 0
                continue
            if command_name not in choices:
                assert command_name in pending_task7
                skipped.add(command_name)
                continue
            parser.parse_args(arguments)

    assert command_count >= 90
    assert skipped <= pending_task7


def test_bootstrap_recovery_uses_persisted_state_not_a_nonexistent_receipt_field() -> None:
    repository = _normalized_text(GUIDE.read_text())
    skills = _normalized_text(AGENT_SKILLS.read_text())

    assert "recovery_command" not in repository
    assert "recovery_command" not in skills
    assert "rerun the same repository command" in repository
    assert "durable receipt and operation journal" in repository


def test_protected_app_automation_is_distinct_from_local_auto_merge_authority() -> None:
    repository = _normalized_text(GUIDE.read_text())

    assert "does not consult local `git.auto_merge` grants" in repository
    assert "Geas publisher auto-merge route requires `git.auto_merge`" in repository


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


def test_ci_runs_bootstrap_state_and_task7_cli_fan_in_suites() -> None:
    workflow = yaml.safe_load(CI.read_text())
    bootstrap_steps = workflow["jobs"]["bootstrap-publishing"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in bootstrap_steps)

    assert {
        "tests/test_bootstrap_state_adapters.py",
        "tests/test_automatic_acquisition_cli.py",
        "tests/test_repository_catalog_cli.py",
        "tests/test_repository_subscription_end_to_end.py",
        "tests/test_ontology_sync.py",
        "tests/test_skill_cli.py",
    } <= set(commands.split())
