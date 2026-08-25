from __future__ import annotations

import shlex
from typing import Literal

from research_agent.models import ProviderConfig, StrictModel
from research_agent.operator_policy import ResearchPolicy
from research_agent.user_config import GeasProfile, UserConfigManager


class SetupCredential(StrictModel):
    environment_variable: str
    used_by: tuple[str, ...]


class SetupProvider(StrictModel):
    name: str
    kind: str
    model: str
    external: bool
    credential_environment_variable: str | None


class SetupStep(StrictModel):
    number: int
    title: str
    purpose: str
    commands: tuple[str, ...]
    notes: tuple[str, ...] = ()


class SetupGuide(StrictModel):
    version: Literal[1] = 1
    config_exists: bool
    config_path: str
    selected_profile: str
    ontology_directory: str
    ontology_repository: str | None
    secret_sources: tuple[str, ...]
    default_provider: str
    providers: tuple[SetupProvider, ...]
    credentials: tuple[SetupCredential, ...]
    steps: tuple[SetupStep, ...]


def build_setup_guide(
    *,
    manager: UserConfigManager,
    profile_name: str,
    profile: GeasProfile,
    default_provider: str,
    providers: dict[str, ProviderConfig],
    research_policy: ResearchPolicy,
) -> SetupGuide:
    """Build a deterministic setup guide without loading or exposing secrets."""
    credential_consumers: dict[str, set[str]] = {}
    for name, provider in providers.items():
        if provider.api_key_env:
            credential_consumers.setdefault(provider.api_key_env, set()).add(
                f"model provider: {name}"
            )
    for provider in (
        *research_policy.general_search_providers,
        *research_policy.domain_index_providers,
    ):
        if provider.enabled and provider.credential_env:
            credential_consumers.setdefault(provider.credential_env, set()).add(
                provider.connector_id
            )

    credentials = tuple(
        SetupCredential(
            environment_variable=name,
            used_by=tuple(sorted(consumers)),
        )
        for name, consumers in sorted(credential_consumers.items())
    )
    secret_paths = tuple(str(path) for path, _format in manager.secret_paths(profile))
    ontology_repository = (
        profile.ontology_git.url if profile.ontology_git is not None else None
    )
    steps = (
        SetupStep(
            number=1,
            title="Build and verify Geas",
            purpose="Install the Python 3.12 project and its development checks.",
            commands=(
                "uv sync --extra dev",
                "uv run ruff check .",
                "uv run pytest -q",
            ),
        ),
        SetupStep(
            number=2,
            title="Create user configuration",
            purpose="Create the OS-standard profile, ontology, and secret locations.",
            commands=("uv run geas config-init", "uv run geas providers"),
            notes=(f"Configuration: {manager.path}",),
        ),
        SetupStep(
            number=3,
            title="Configure API credentials and models",
            purpose="Add only the variables needed by the selected connectors and providers.",
            commands=(f'"${{EDITOR:-vi}}" {shlex.quote(secret_paths[0])}',),
            notes=(
                "Never commit secret files; Geas loads only allowlisted variable names.",
                "Edit config/providers.toml and the model/budget policies together when "
                "adding or changing a model route.",
            ),
        ),
        SetupStep(
            number=4,
            title="Verify a model route",
            purpose="Exercise one tool-free provider through policy and budget gates.",
            commands=(f"uv run geas model-smoke --provider {default_provider}",),
        ),
        SetupStep(
            number=5,
            title="Synchronize shared ontology configuration",
            purpose="Clone or fast-forward the selected profile's ontology repository.",
            commands=("uv run geas ontology-sync --pull",),
            notes=(
                "Skip this step when the selected profile has no ontology_git configuration.",
            ),
        ),
        SetupStep(
            number=6,
            title="Add a local repository or document corpus",
            purpose="Acquire matching files into an immutable local source store.",
            commands=(
                "uv run geas research-local \"What must the expert know?\" "
                "--corpus /path/to/repository --concept concept:project-expertise "
                "--root data/project-expertise",
                "uv run geas parse-document /path/to/repository/README.md "
                "--root data/project-expertise",
            ),
            notes=(
                "research-local acquires matching files; parse-document prepares each "
                "operator-selected document for exact anchor retrieval.",
            ),
        ),
        SetupStep(
            number=7,
            title="Create and build an ontology",
            purpose="Write explicit configuration, validate it, then run a resumable worker.",
            commands=(
                "uv run geas ontology-init --topic \"Project expertise\" "
                "--concept-id concept:project-expertise",
                "uv run geas ontology-build project-expertise "
                "--root data/project-expertise --check",
                "uv run geas ontology-build project-expertise "
                "--root data/project-expertise",
            ),
        ),
        SetupStep(
            number=8,
            title="Give bounded context to an agent",
            purpose="Retrieve exact attributable fragments without model-controlled ranking.",
            commands=(
                "uv run geas library-build project-expertise "
                "--root data/project-expertise "
                "--database data/project-expertise/library.sqlite",
                "uv run geas library-context \"question\" "
                "--database data/project-expertise/library.sqlite "
                "--max-characters 16000",
            ),
        ),
        SetupStep(
            number=9,
            title="Export project expert knowledge",
            purpose="Create a linked vault and a project instruction handoff.",
            commands=(
                "uv run geas topic-export concept:project-expertise "
                "/path/to/project/docs/geas-expert --format obsidian "
                "--database data/project-expertise/query.sqlite",
                "uv run geas topic-export concept:project-expertise "
                "/path/to/project/GEAS_EXPERT.md --format agent-instructions "
                "--vault-link docs/geas-expert/index.md "
                "--database data/project-expertise/query.sqlite",
            ),
            notes=(
                "Reference GEAS_EXPERT.md from the project's existing agent instructions.",
                "Exports are disposable projections; original source locators and exact "
                "evidence remain available for verification.",
            ),
        ),
    )
    return SetupGuide(
        config_exists=manager.path.is_file(),
        config_path=str(manager.path),
        selected_profile=profile_name,
        ontology_directory=str(manager.ontology_root(profile)),
        ontology_repository=ontology_repository,
        secret_sources=secret_paths,
        default_provider=default_provider,
        providers=tuple(
            SetupProvider(
                name=name,
                kind=provider.kind,
                model=provider.model,
                external=provider.external,
                credential_environment_variable=provider.api_key_env or None,
            )
            for name, provider in sorted(providers.items())
        ),
        credentials=credentials,
        steps=steps,
    )


def render_setup_guide_markdown(guide: SetupGuide) -> str:
    lines = [
        "# Geas quick setup walkthrough",
        "",
        f"- Configuration: `{guide.config_path}`",
        f"- Configuration exists: `{str(guide.config_exists).lower()}`",
        f"- Selected profile: `{guide.selected_profile}`",
        f"- Ontology directory: `{guide.ontology_directory}`",
        f"- Ontology repository: {guide.ontology_repository or 'none'}",
        f"- Default model provider: `{guide.default_provider}`",
        "",
        "## Credential names",
        "",
    ]
    for credential in guide.credentials:
        lines.append(
            f"- `{credential.environment_variable}` — {', '.join(credential.used_by)}"
        )
    if not guide.credentials:
        lines.append("- None")
    lines.append("")
    for step in guide.steps:
        lines.extend(
            (
                f"## {step.number}. {step.title}",
                "",
                step.purpose,
                "",
                "```bash",
                *step.commands,
                "```",
                "",
            )
        )
        lines.extend(f"- {note}" for note in step.notes)
        if step.notes:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
