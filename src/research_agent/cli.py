from __future__ import annotations

import argparse
import getpass
import json
import math
import mimetypes
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic_core import to_jsonable_python

from research_agent.approvals import ApprovalRegistry, AuthenticatedPrincipal
from research_agent.audit import DeterministicKnowledgeAuditor
from research_agent.benchmark import ProjectionBenchmark
from research_agent.budget import BudgetPolicy, UsageLedger
from research_agent.bundles import KnowledgeBundleImporter
from research_agent.citations import CitationDocumentManager, IdentifierKind
from research_agent.connectors import (
    CrossrefDiscoveryConnector,
    EuropePmcDiscoveryConnector,
    LocalFileConnector,
    MojeekDiscoveryConnector,
    OpenAlexDiscoveryConnector,
    UnpaywallResolver,
)
from research_agent.connectors.unpaywall import UnpaywallError
from research_agent.deposits import (
    AcquisitionMethod,
    DepositManager,
    DepositOverrides,
    DepositPolicy,
    ModelRoute,
    NostrClaim,
    NostrEvent,
    PermissionStatus,
    RedistributionStatus,
    UsagePermissionOverrides,
)
from research_agent.discovery import (
    AccessConstraint,
    AccessConstraintReason,
    CompilerIdentity,
    ConnectorCapability,
    OpenAccessResolution,
    SourceClass,
    identified,
)
from research_agent.discovery_acquisition import GitHubDiscoveryAcquirer
from research_agent.extraction import AnchorGroundedExtractionManager
from research_agent.identifiers import doi_locator, normalize_doi
from research_agent.knowledge import KnowledgeImporter, KnowledgePack
from research_agent.library import (
    SourceLibraryBuilder,
    SourceLibraryManifest,
    SourceLibraryQueryEngine,
)
from research_agent.model_evaluation import (
    compare_proposals,
    find_proposal,
    slice_proposal,
)
from research_agent.model_policy import (
    DataClass,
    InputKind,
    ModelOperation,
    ModelUseContext,
    ModelUseGate,
    ModelUsePolicy,
)
from research_agent.models import (
    ModelParameters,
    PolicyStage,
    ThreatObservation,
    ThreatTarget,
)
from research_agent.onboarding import build_setup_guide, render_setup_guide_markdown
from research_agent.ontology_artifacts import (
    ArtifactRole,
    GitHubReleaseArtifactStore,
    OntologyArtifactManager,
)
from research_agent.ontology_build import (
    OntologyBuildConfig,
    OntologyBuilder,
    OntologyBuildReceipt,
)
from research_agent.ontology_catalog import inventory_ontologies
from research_agent.ontology_sync import OntologyRepositoryManager
from research_agent.operator_policy import ResearchPolicy
from research_agent.parsing import ParsedDocumentManager
from research_agent.paths import (
    resolve_ontology_build_config,
    resolve_profile_ontology_config,
    shared_ontology_directory,
)
from research_agent.planning import (
    ConceptVocabulary,
    ModelQueryCompiler,
    QueryPlanValidator,
    QueryProposal,
    deterministic_proposal,
)
from research_agent.policy import PolicyEngine
from research_agent.projection import (
    KnowledgeQueryEngine,
    QueryRecordType,
    SQLiteKnowledgeProjection,
)
from research_agent.promotion import GitPromotionManager
from research_agent.providers import ModelClient, load_provider_configs
from research_agent.remote_acquisition import LicenseGatedAcquirer
from research_agent.render import (
    render_agent_instructions,
    render_topic_markdown,
    render_topic_obsidian,
    write_obsidian_vault,
)
from research_agent.research import DiscoveryExecutor, OfflineResearchRunner
from research_agent.secrets import load_env_file, load_secret_sources
from research_agent.store import ImmutableStore
from research_agent.structure import AnchorKind, StructuralAnchor, StructuralDocumentManager
from research_agent.truth import SQLiteProjectionGuard, TruthManager, TruthPolicy, TruthSnapshot
from research_agent.user_config import (
    GeasUserConfig,
    UserConfigManager,
    default_config_path,
)
from research_agent.workflow import ActorKind, WorkflowEngine, WorkflowState
from research_agent.workload import WorkloadPolicy


def _json(value: object) -> None:
    value = to_jsonable_python(value)
    print(json.dumps(value, indent=2, sort_keys=True))


def _ontology_build_exit_code(receipt: OntologyBuildReceipt) -> int:
    if receipt.token_limit_exhaustions:
        for item in receipt.token_limit_exhaustions:
            print(
                (
                    "ERROR: The model ran out of output tokens while extracting "
                    f"{item.source}. Requested {item.requested_output_tokens}; "
                    f"provider/model capacity is {item.provider_output_token_limit}."
                ),
                file=sys.stderr,
            )
            for action in item.recommendations:
                print(f"  - {action}", file=sys.stderr)
        return 3
    if receipt.resumable and not receipt.failures:
        print(
            (
                "Ontology worker checkpointed successfully with "
                f"{receipt.work_remaining} source(s) remaining; rerun the same "
                "command to continue."
            ),
            file=sys.stderr,
        )
        return 0
    return 0 if receipt.completed or receipt.checked_only else 2


def _local_approval_principal(root: Path) -> AuthenticatedPrincipal:
    uid = getattr(os, "getuid", lambda: -1)()
    return AuthenticatedPrincipal(
        actor_id=f"os-user:{uid}:{getpass.getuser()}",
        deployment_id=f"local:{root.resolve()}",
        session_id=f"process:{os.getpid()}",
        authenticated_at=datetime.now(UTC),
        authentication_method="local_os_session",
    )


def _user_config_manager(args: argparse.Namespace) -> UserConfigManager:
    return UserConfigManager(args.geas_config)


def _profile_ontology_root(
    args: argparse.Namespace,
    *,
    pull_before_read: bool = False,
    ontology: Path | None = None,
) -> Path | None:
    manager = _user_config_manager(args)
    if not manager.path.exists():
        return None
    user_config = manager.load()
    profile_name, profile = user_config.profile(args.geas_profile)
    root = manager.ontology_root(profile)
    ontology_name = (
        ontology.name
        if ontology is not None
        and not ontology.is_absolute()
        and len(ontology.parts) == 1
        else None
    )
    freshness = user_config.ontology_freshness
    check_before_use = freshness.check_before_use
    max_age_seconds = freshness.max_age_seconds
    hydrate = freshness.hydrate_artifacts_before_use
    local_build = root / ontology_name / "build.yaml" if ontology_name else None
    if local_build is not None and local_build.is_file() and not local_build.is_symlink():
        try:
            override = OntologyBuildConfig.from_yaml(
                local_build,
                defaults=user_config.ontology_defaults,
            ).repository_sync
        except (OSError, ValueError):
            override = None
        if override is not None:
            if override.check_before_use is not None:
                check_before_use = override.check_before_use
            if override.max_age_seconds is not None:
                max_age_seconds = override.max_age_seconds
            if override.hydrate_artifacts_before_use is not None:
                hydrate = override.hydrate_artifacts_before_use
    repository = None
    if pull_before_read and profile.ontology_git is not None:
        repository = OntologyRepositoryManager(
            checkout=root,
            config=profile.ontology_git,
        )
        if (
            check_before_use
            or profile.ontology_git.pull_before_update
            or not (root / ".git").is_dir()
        ):
            repository.freshen(
                state_path=(
                    manager.root / "state" / "ontology-sync" / f"{profile_name}.json"
                ),
                max_age_seconds=max_age_seconds,
                force=profile.ontology_git.pull_before_update,
            )
    if hydrate and ontology_name is not None:
        artifact_manager = OntologyArtifactManager(root / ontology_name)
        if artifact_manager.manifest_path.is_file():
            if profile.ontology_git is None:
                raise ValueError(
                    f"Geas profile {profile_name!r} has no ontology_git artifact source"
                )
            artifact_manager.hydrate(
                store=GitHubReleaseArtifactStore(
                    profile.ontology_git.url,
                    branch=profile.ontology_git.branch,
                )
            )
    return root


def _resolve_portable_database(
    args: argparse.Namespace,
    value: Path,
    *,
    role: ArtifactRole,
) -> Path:
    """Resolve a named ontology to one lazily hydrated portable database."""
    if value.exists() or value.is_absolute() or len(value.parts) != 1:
        return value
    manager = _user_config_manager(args)
    if not manager.path.exists():
        return value
    profile_name, profile = manager.profile(args.geas_profile)
    if profile.ontology_git is None:
        return value
    root = _profile_ontology_root(
        args,
        pull_before_read=True,
        ontology=value,
    )
    if root is None:
        return value
    ontology_directory = root / value.name
    artifact_manager = OntologyArtifactManager(ontology_directory)
    if not artifact_manager.manifest_path.is_file():
        return value
    receipt = artifact_manager.hydrate(
        store=GitHubReleaseArtifactStore(
            profile.ontology_git.url,
            branch=profile.ontology_git.branch,
        ),
        roles=(role,),
    )
    if len(receipt.hydrated) != 1:
        raise ValueError(
            f"portable database role {role.value!r} is unavailable for profile "
            f"{profile_name!r}"
        )
    return Path(receipt.hydrated[0].path)


def _load_allowed_secrets(
    args: argparse.Namespace,
    *,
    allowed_names: frozenset[str],
) -> frozenset[str]:
    if args.env_file is not None:
        return load_env_file(args.env_file, allowed_names=allowed_names)
    manager = _user_config_manager(args)
    if manager.path.exists():
        _name, profile = manager.profile(args.geas_profile)
        return load_secret_sources(
            manager.secret_paths(profile),
            allowed_names=allowed_names,
        )
    return load_env_file(Path(".env"), allowed_names=allowed_names)


def _resolve_cli_config_paths(args: argparse.Namespace) -> None:
    manager = _user_config_manager(args)
    fields = {
        "providers": "providers.toml",
        "policy": "source-policy.yaml",
        "research_policy": "research-policy.yaml",
        "truth_policy": "truth-policy.yaml",
        "deposit_policy": "deposit-policy.yaml",
        "model_policy": "model-policy.yaml",
        "budget_policy": "budget-policy.yaml",
        "workload_policy": "workload-policy.yaml",
        "vocabulary": "query-vocabulary.yaml",
        "query_vocabulary": "query-vocabulary.yaml",
    }
    for field, filename in fields.items():
        if not hasattr(args, field) or getattr(args, field) is not None:
            continue
        user_path = manager.policy_path(filename)
        setattr(
            args,
            field,
            user_path if user_path.is_file() else default_config_path(filename),
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="geas")
    parser.add_argument(
        "--providers",
        type=Path,
        default=None,
        help="provider configuration path",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="deterministic source policy path",
    )
    parser.add_argument(
        "--research-policy",
        type=Path,
        default=None,
        help="connector priority, storage, and cost policy path",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="explicit legacy dotenv file; otherwise use selected Geas profile sources",
    )
    parser.add_argument(
        "--geas-config",
        type=Path,
        default=None,
        help="per-user Geas config.yaml path",
    )
    parser.add_argument(
        "--geas-profile",
        help="named Geas profile for ontology location, Git sync, and secret sources",
    )
    parser.add_argument(
        "--truth-policy",
        type=Path,
        default=None,
        help="canonical-source and projection reconciliation policy",
    )
    parser.add_argument(
        "--deposit-policy",
        type=Path,
        default=None,
        help="user-deposit defaults and authorization-boundary policy",
    )
    parser.add_argument(
        "--model-policy",
        type=Path,
        default=None,
        help="deterministic local and external model-use policy",
    )
    parser.add_argument(
        "--budget-policy",
        type=Path,
        default=None,
        help="automatic external-use envelope and accounting treatment",
    )
    parser.add_argument(
        "--workload-policy",
        type=Path,
        default=None,
        help="local deployment workload and benchmark tiers",
    )
    parser.add_argument(
        "--query-vocabulary",
        type=Path,
        default=None,
        help="controlled query vocabulary used by knowledge projections",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("providers", help="list configured providers without secrets")

    setup_guide = subparsers.add_parser(
        "setup-guide",
        help="show a project build and end-to-end Geas setup walkthrough",
    )
    setup_guide.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
    )

    config_init = subparsers.add_parser(
        "config-init",
        help="create the per-user Geas profile and modular-secret scaffold",
    )
    config_init.add_argument(
        "--update-defaults",
        action="store_true",
        help="update unchanged managed defaults and preserve modified files as-is",
    )

    ontology_sync = subparsers.add_parser(
        "ontology-sync",
        help="pull or safely commit and push the selected profile's ontology repository",
    )
    ontology_sync.add_argument("--pull", action="store_true")
    ontology_sync.add_argument("--push", action="store_true")
    ontology_sync.add_argument("--message", default="geas: update ontologies")

    artifact_publish = subparsers.add_parser(
        "ontology-artifact-publish",
        help=(
            "publish changed SQLite and generated ontology artifacts as "
            "content-addressed GitHub release assets"
        ),
    )
    artifact_publish.add_argument("ontology")
    artifact_publish.add_argument("--source-library", type=Path)
    artifact_publish.add_argument("--knowledge-projection", type=Path)
    artifact_publish.add_argument("--generated-content", type=Path)
    artifact_publish.add_argument("--published-by", required=True)
    artifact_publish.add_argument("--storage-rights-basis", required=True)
    artifact_publish.add_argument(
        "--message",
        default=None,
        help="Git commit message for the updated artifact manifest",
    )

    artifact_sync = subparsers.add_parser(
        "ontology-artifact-sync",
        help="lazily download and verify stale artifacts for one ontology",
    )
    artifact_sync.add_argument("ontology")
    artifact_sync.add_argument(
        "--role",
        type=ArtifactRole,
        choices=list(ArtifactRole),
        action="append",
        default=[],
    )

    ontology_list = subparsers.add_parser(
        "ontology-list",
        help="list ontologies in the selected profile or a provided directory",
    )
    ontology_list.add_argument(
        "directory",
        type=Path,
        nargs="?",
        help="ontology root to inspect; omit for the selected Geas profile",
    )

    smoke = subparsers.add_parser("model-smoke", help="run a tool-free model smoke test")
    smoke.add_argument("--provider")
    smoke.add_argument("--root", type=Path, default=Path("data"))
    smoke.add_argument("--run-id")
    smoke.add_argument("--approval-receipt-id")
    smoke.add_argument("--override-external-budget", action="store_true")

    init = subparsers.add_parser("store-init", help="initialize an immutable store")
    init.add_argument("--root", type=Path, default=Path("data"))

    ontology_init = subparsers.add_parser(
        "ontology-init",
        help="create complete explicit ontology and source-library configuration files",
    )
    ontology_init.add_argument(
        "directory",
        type=Path,
        nargs="?",
        help=(
            "explicit workspace-relative directory; omit to use the per-user "
            "Geas ontology directory"
        ),
    )
    ontology_init.add_argument("--topic", required=True)
    ontology_init.add_argument("--concept-id", required=True)
    ontology_init.add_argument(
        "--provider",
        default=None,
        help="override the provider inherited from global ontology_defaults",
    )
    ontology_init.add_argument("--force", action="store_true")
    ontology_init.add_argument(
        "--pull",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override profile pull_before_update for this update",
    )
    ontology_init.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override profile push_on_update for this update",
    )

    source = subparsers.add_parser("source-add", help="archive a local source file")
    source.add_argument("path", type=Path)
    source.add_argument("--root", type=Path, default=Path("data"))
    source.add_argument("--uri")
    source.add_argument("--license")

    deposit = subparsers.add_parser(
        "deposit-add",
        help="archive a file with provenance and user-controlled handling defaults",
    )
    deposit.add_argument("path", type=Path)
    deposit.add_argument("--root", type=Path, default=Path("data"))
    deposit.add_argument("--deposited-by", required=True)
    deposit.add_argument(
        "--method",
        type=AcquisitionMethod,
        choices=list(AcquisitionMethod),
        default=AcquisitionMethod.LOCAL_FILE,
    )
    deposit.add_argument("--original-locator")
    deposit.add_argument("--source-uri")
    deposit.add_argument("--license")
    deposit.add_argument("--author", action="append", default=[])
    deposit.add_argument("--usage-condition", action="append", default=[])
    deposit.add_argument("--rights-basis")
    deposit.add_argument("--provenance-note")
    deposit.add_argument("--nostr-ownership-event", type=Path, action="append", default=[])
    deposit.add_argument("--nostr-authorship-event", type=Path, action="append", default=[])
    deposit.add_argument("--nostr-publication-event", type=Path, action="append", default=[])
    deposit.add_argument("--scope-label")
    deposit.add_argument(
        "--index-content",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    deposit.add_argument(
        "--include-in-ontology",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    deposit.add_argument("--model-route", type=ModelRoute, choices=list(ModelRoute))
    deposit.add_argument(
        "--redistribution-status",
        type=RedistributionStatus,
        choices=list(RedistributionStatus),
    )
    deposit.add_argument(
        "--archive-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--quote-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--transform-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--redistribute-original-permission",
        type=PermissionStatus,
        choices=list(PermissionStatus),
    )
    deposit.add_argument("--retention-policy")

    offline = subparsers.add_parser(
        "research-local",
        help="run deterministic discovery and acquisition over local roots",
    )
    offline.add_argument("question")
    offline.add_argument("--root", type=Path, default=Path("data"))
    offline.add_argument("--corpus", type=Path, action="append", required=True)
    offline.add_argument("--term", action="append", default=[])
    offline.add_argument("--concept", action="append", default=[])
    offline.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    offline.add_argument("--result-limit", type=int, default=50)
    offline.add_argument("--approve-budget", action="store_true")
    offline.add_argument(
        "--compiler-provider",
        help="tool-free configured model provider; omit for deterministic lexical compilation",
    )
    offline.add_argument(
        "--compiler-data-class",
        type=DataClass,
        choices=list(DataClass),
        default=DataClass.UNKNOWN,
        help="trusted classification of compiler input; unknown forbids external use",
    )
    offline.add_argument("--approval-receipt-id")
    offline.add_argument("--override-external-budget", action="store_true")
    offline.add_argument("--run-id")
    offline.add_argument("--topic-branch", default="topic:local")

    mojeek = subparsers.add_parser(
        "discover-mojeek",
        help="run discovery-only search; results are not evidence",
    )
    mojeek.add_argument("question")
    mojeek.add_argument("--root", type=Path, default=Path("data"))
    mojeek.add_argument("--term", action="append", default=[])
    mojeek.add_argument("--concept", action="append", default=[])
    mojeek.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    mojeek.add_argument("--result-limit", type=int, default=10)
    mojeek.add_argument("--approve-budget", action="store_true")

    acquire_discovery = subparsers.add_parser(
        "acquire-discovery",
        help=(
            "resolve supported saved discovery hits through official immutable "
            "sources and parse them as inert evidence"
        ),
    )
    acquire_discovery.add_argument("discovery", type=Path)
    acquire_discovery.add_argument("--root", type=Path, default=Path("data"))
    acquire_discovery.add_argument("--limit", type=int, default=20)

    ontology_build = subparsers.add_parser(
        "ontology-build",
        help="autonomously build, resume, audit, and project an ontology from one config",
    )
    ontology_build.add_argument(
        "config",
        type=Path,
        help="build.yaml path or ontology name from the per-user Geas config directory",
    )
    ontology_build.add_argument("--root", type=Path, default=Path("data/ontology-build"))
    ontology_build.add_argument("--workspace", type=Path, default=Path("."))
    ontology_build.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    ontology_build.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and local dependencies without network or model calls",
    )
    ontology_build.add_argument(
        "--refresh",
        action="store_true",
        help="refresh completed discovery queries and known repository snapshots",
    )
    ontology_build.add_argument(
        "--reextract",
        action="store_true",
        help="reconsider sources that already have validator-compatible proposals",
    )

    crossref = subparsers.add_parser(
        "discover-crossref",
        help="run open scholarly DOI and bibliographic metadata discovery",
    )
    crossref.add_argument("question")
    crossref.add_argument("--root", type=Path, default=Path("data"))
    crossref.add_argument("--term", action="append", default=[])
    crossref.add_argument("--concept", action="append", default=[])
    crossref.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    crossref.add_argument("--result-limit", type=int, default=20)
    crossref.add_argument("--approve-budget", action="store_true")

    europe_pmc = subparsers.add_parser(
        "discover-europe-pmc",
        help="run Europe PMC lite bibliographic metadata discovery",
    )
    europe_pmc.add_argument("question")
    europe_pmc.add_argument("--root", type=Path, default=Path("data"))
    europe_pmc.add_argument("--term", action="append", default=[])
    europe_pmc.add_argument("--concept", action="append", default=[])
    europe_pmc.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    europe_pmc.add_argument("--result-limit", type=int, default=20)
    europe_pmc.add_argument("--approve-budget", action="store_true")

    openalex = subparsers.add_parser(
        "discover-openalex",
        help="run authenticated OpenAlex scholarly metadata discovery",
    )
    openalex.add_argument("question")
    openalex.add_argument("--root", type=Path, default=Path("data"))
    openalex.add_argument("--term", action="append", default=[])
    openalex.add_argument("--concept", action="append", default=[])
    openalex.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
    )
    openalex.add_argument("--result-limit", type=int, default=20)
    openalex.add_argument("--approve-budget", action="store_true")
    openalex.add_argument("--run-id")

    unpaywall = subparsers.add_parser(
        "resolve-unpaywall",
        help="resolve DOI records to license-attributed open-access locations",
    )
    unpaywall.add_argument("doi", nargs="+")
    unpaywall.add_argument("--root", type=Path, default=Path("data"))

    parse_document = subparsers.add_parser(
        "parse-document",
        help="preserve original bytes and derive quarantined inert text",
    )
    parse_document.add_argument("path", type=Path)
    parse_document.add_argument("--root", type=Path, default=Path("data"))
    parse_document.add_argument("--source-uri")
    parse_document.add_argument("--media-type")
    parse_document.add_argument("--license")

    derive_structure = subparsers.add_parser(
        "derive-structure",
        help="derive stable structural anchors from a stored text derivation",
    )
    derive_structure.add_argument("text_derivation_id")
    derive_structure.add_argument("--root", type=Path, default=Path("data"))

    structure_show = subparsers.add_parser(
        "structure-show",
        help="list deterministic structural anchors for one derivation",
    )
    structure_show.add_argument("structural_derivation_id")
    structure_show.add_argument("--root", type=Path, default=Path("data"))
    structure_show.add_argument("--leaf-only", action="store_true")
    structure_show.add_argument("--limit", type=int, default=1000)

    structure_list = subparsers.add_parser(
        "structure-list",
        help="list stored structural derivations without exposing source text",
    )
    structure_list.add_argument("--root", type=Path, default=Path("data"))
    structure_list.add_argument("--limit", type=int, default=100)

    derive_citations = subparsers.add_parser(
        "derive-citations",
        help="derive deterministic identifier and reference records from structural text",
    )
    derive_citations.add_argument("structural_derivation_id")
    derive_citations.add_argument("--root", type=Path, default=Path("data"))

    propose_extraction = subparsers.add_parser(
        "propose-extraction",
        help="ask a tool-free model for proposal-only claims grounded in selected anchors",
    )
    propose_extraction.add_argument("structural_derivation_id")
    propose_extraction.add_argument("--anchor", action="append", required=True)
    propose_extraction.add_argument("--question", required=True)
    propose_extraction.add_argument("--concept", action="append", default=[])
    propose_extraction.add_argument("--provider")
    propose_extraction.add_argument("--root", type=Path, default=Path("data"))
    propose_extraction.add_argument(
        "--data-class",
        type=DataClass,
        choices=list(DataClass),
        default=DataClass.UNKNOWN,
    )
    propose_extraction.add_argument(
        "--model-route",
        type=ModelRoute,
        choices=list(ModelRoute),
        default=ModelRoute.LOCAL_PREFERRED,
    )
    propose_extraction.add_argument("--max-output-tokens", type=int, default=65_536)
    propose_extraction.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="enable provider reasoning for extraction",
    )
    propose_extraction.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default="high",
    )
    propose_extraction.add_argument("--temperature", type=float, default=0.0)
    propose_extraction.add_argument("--top-p", type=float)
    propose_extraction.add_argument("--top-k", type=int)
    propose_extraction.add_argument("--min-p", type=float)
    propose_extraction.add_argument("--seed", type=int)
    propose_extraction.add_argument("--stop", action="append", default=[])
    propose_extraction.add_argument(
        "--debug-reasoning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write deterministically redacted reasoning to a mode-0600 debug log",
    )
    propose_extraction.add_argument(
        "--allow-partial-items",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="retain individually valid items and log rejected output items",
    )
    propose_extraction.add_argument("--timeout", type=float, default=3600.0)
    propose_extraction.add_argument("--run-id")
    propose_extraction.add_argument("--approval-receipt-id")
    propose_extraction.add_argument("--override-external-budget", action="store_true")

    compare_extractions = subparsers.add_parser(
        "compare-extractions",
        help="deterministically compare two validated extraction proposals",
    )
    compare_extractions.add_argument("baseline_proposal_id")
    compare_extractions.add_argument("candidate_proposal_id")
    compare_extractions.add_argument("--root", type=Path, default=Path("data"))

    proposal_slice = subparsers.add_parser(
        "proposal-slice",
        help="return one deterministic concept subtree from a validated proposal",
    )
    proposal_slice.add_argument("proposal_id")
    proposal_slice.add_argument("concept_id")
    proposal_slice.add_argument("--root", type=Path, default=Path("data"))
    proposal_slice.add_argument(
        "--descendants",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    promotion_stage = subparsers.add_parser(
        "promotion-stage",
        help="render a validated extraction proposal as a forge-neutral Git manifest",
    )
    promotion_stage.add_argument("proposal_id")
    promotion_stage.add_argument("--topic", required=True)
    promotion_stage.add_argument("--topic-concept-id", required=True)
    promotion_stage.add_argument("--output", type=Path, required=True)
    promotion_stage.add_argument("--target-ref", default="refs/heads/main")
    promotion_stage.add_argument("--root", type=Path, default=Path("data"))
    promotion_stage.add_argument("--workspace", type=Path, default=Path("."))

    promotion_verify = subparsers.add_parser(
        "promotion-verify",
        help="verify a promotion manifest from a canonical local Git branch",
    )
    promotion_verify.add_argument("manifest", type=Path)
    promotion_verify.add_argument("--canonical-ref", default="refs/heads/main")
    promotion_verify.add_argument("--root", type=Path, default=Path("data"))
    promotion_verify.add_argument("--workspace", type=Path, default=Path("."))

    promotion_apply = subparsers.add_parser(
        "promotion-apply",
        help="materialize an exact promotion only after it reaches a canonical Git branch",
    )
    promotion_apply.add_argument("manifest", type=Path)
    promotion_apply.add_argument("--canonical-ref", default="refs/heads/main")
    promotion_apply.add_argument("--root", type=Path, default=Path("data"))
    promotion_apply.add_argument("--workspace", type=Path, default=Path("."))

    acquire_oa = subparsers.add_parser(
        "acquire-open-access",
        help="fetch and parse a stored license-qualified DOI resolution",
    )
    acquire_oa.add_argument("doi")
    acquire_oa.add_argument("--root", type=Path, default=Path("data"))

    truth_snapshot = subparsers.add_parser(
        "truth-snapshot",
        help="capture the canonical ontology, schemas, records, and blobs",
    )
    truth_snapshot.add_argument("--root", type=Path, default=Path("data"))
    truth_snapshot.add_argument("--workspace", type=Path, default=Path("."))
    truth_snapshot.add_argument("--created-by", required=True)
    truth_snapshot.add_argument("--predecessor")

    truth_check = subparsers.add_parser(
        "truth-check",
        help="detect drift from a canonical truth snapshot",
    )
    truth_check.add_argument("snapshot", type=Path)
    truth_check.add_argument("--root", type=Path, default=Path("data"))
    truth_check.add_argument("--workspace", type=Path, default=Path("."))

    projection_stamp = subparsers.add_parser(
        "projection-stamp",
        help="stamp a completely built SQLite projection",
    )
    projection_stamp.add_argument("snapshot", type=Path)
    projection_stamp.add_argument("database", type=Path)
    projection_stamp.add_argument("--schema-version", type=int, required=True)
    projection_stamp.add_argument("--builder-version", required=True)

    projection_check = subparsers.add_parser(
        "projection-check",
        help="detect canonical or SQLite projection drift",
    )
    projection_check.add_argument("snapshot", type=Path)
    projection_check.add_argument("database", type=Path)
    projection_check.add_argument("--root", type=Path, default=Path("data"))
    projection_check.add_argument("--workspace", type=Path, default=Path("."))

    knowledge_import = subparsers.add_parser(
        "knowledge-import",
        help="validate and commit a trusted structured knowledge pack",
    )
    knowledge_import.add_argument("pack", type=Path)
    knowledge_import.add_argument("--root", type=Path, default=Path("data"))
    knowledge_import.add_argument("--imported-by", required=True)

    bundle_import = subparsers.add_parser(
        "bundle-import",
        help="reproducibly archive and import a maintained ontology bundle",
    )
    bundle_import.add_argument("bundle", type=Path)
    bundle_import.add_argument("--root", type=Path, default=Path("data"))
    bundle_import.add_argument("--imported-by", required=True)

    library_build = subparsers.add_parser(
        "library-build",
        help="build a reusable, ontology-independent source-library snapshot and index",
    )
    library_build.add_argument(
        "manifest",
        type=Path,
        help="library.yaml path or ontology name from the selected Geas profile",
    )
    library_build.add_argument("--root", type=Path, default=Path("data"))
    library_build.add_argument(
        "--database",
        type=Path,
        default=Path("data/source-library.sqlite"),
    )

    library_query = subparsers.add_parser(
        "library-query",
        help="search exact source anchors in a source-library snapshot",
    )
    library_query.add_argument("question")
    library_query.add_argument(
        "--database",
        type=Path,
        default=Path("data/source-library.sqlite"),
    )
    library_query.add_argument("--limit", type=int, default=25)

    library_show = subparsers.add_parser(
        "library-show",
        help="show a source-library manifest, snapshot identity, and source inventory",
    )
    library_show.add_argument(
        "--database",
        type=Path,
        default=Path("data/source-library.sqlite"),
    )

    library_context = subparsers.add_parser(
        "library-context",
        help="return a bounded, exact, agent-readable context package from a source library",
    )
    library_context.add_argument("question")
    library_context.add_argument(
        "--database",
        type=Path,
        default=Path("data/source-library.sqlite"),
    )
    library_context.add_argument("--limit", type=int, default=25)
    library_context.add_argument("--max-characters", type=int, default=16_000)

    projection_build = subparsers.add_parser(
        "projection-build",
        help="atomically rebuild the disposable SQLite knowledge projection",
    )
    projection_build.add_argument("snapshot", type=Path)
    projection_build.add_argument("database", type=Path)
    projection_build.add_argument("--root", type=Path, default=Path("data"))
    projection_build.add_argument("--workspace", type=Path, default=Path("."))

    knowledge_query = subparsers.add_parser(
        "knowledge-query",
        help="compile natural language into deterministic FTS5 retrieval",
    )
    knowledge_query.add_argument("question")
    knowledge_query.add_argument("--database", type=Path, default=Path("data/query.sqlite"))
    knowledge_query.add_argument(
        "--kind",
        type=QueryRecordType,
        choices=list(QueryRecordType),
        action="append",
        default=[],
    )
    knowledge_query.add_argument("--limit", type=int, default=25)

    knowledge_audit = subparsers.add_parser(
        "knowledge-audit",
        help="run deterministic evidence, dissent, freshness, and retraction checks",
    )
    knowledge_audit.add_argument("--root", type=Path, default=Path("data"))
    knowledge_audit.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    knowledge_audit.add_argument("--fail-on-error", action="store_true")

    identifier_show = subparsers.add_parser(
        "identifier-show",
        help="show exact inbound references and deterministic metadata resolutions",
    )
    identifier_show.add_argument("kind", type=IdentifierKind, choices=list(IdentifierKind))
    identifier_show.add_argument("value")
    identifier_show.add_argument(
        "--database",
        type=Path,
        default=Path("data/query.sqlite"),
    )

    topic_show = subparsers.add_parser(
        "topic-show",
        help="return a complete hierarchy, claim, provenance, dissent, gap, and threat view",
    )
    topic_show.add_argument("concept_id")
    topic_show.add_argument("--database", type=Path, default=Path("data/query.sqlite"))
    topic_show.add_argument("--as-of", type=datetime.fromisoformat)

    topic_export = subparsers.add_parser(
        "topic-export",
        help="generate a deterministic agent-readable Markdown topic projection",
    )
    topic_export.add_argument("concept_id")
    topic_export.add_argument("output", type=Path)
    topic_export.add_argument("--database", type=Path, default=Path("data/query.sqlite"))
    topic_export.add_argument("--as-of", type=datetime.fromisoformat)
    topic_export.add_argument(
        "--format",
        choices=("markdown", "obsidian", "agent-instructions"),
        default="markdown",
        help="topic page, cross-linked Obsidian vault, or project agent handoff",
    )
    topic_export.add_argument(
        "--force",
        action="store_true",
        help="replace a differing Obsidian export directory atomically",
    )
    topic_export.add_argument(
        "--vault-link",
        help="relative link to a companion Obsidian export for agent instructions",
    )

    benchmark = subparsers.add_parser(
        "projection-benchmark",
        help="measure canonical writes, rebuilds, and deterministic queries",
    )
    benchmark.add_argument(
        "--tier",
        choices=("smoke", "standard", "scale"),
        default="smoke",
    )
    benchmark.add_argument("--claims", type=int)
    benchmark.add_argument("--workspace", type=Path, default=Path("."))

    policy = subparsers.add_parser("policy-check", help="evaluate source policy")
    policy.add_argument("--workflow-id", required=True)
    policy.add_argument("--source-version", required=True)
    policy.add_argument("--stage", type=PolicyStage, choices=list(PolicyStage), required=True)
    policy.add_argument("observations", nargs="*", type=Path)

    transition = subparsers.add_parser("workflow-transition", help="validate a state transition")
    transition.add_argument("--workflow-id", required=True)
    transition.add_argument("--source-version", required=True)
    transition.add_argument(
        "--from-state",
        type=WorkflowState,
        choices=list(WorkflowState),
        required=True,
    )
    transition.add_argument(
        "--to-state",
        type=WorkflowState,
        choices=list(WorkflowState),
        required=True,
    )
    transition.add_argument("--actor-kind", type=ActorKind, choices=list(ActorKind), required=True)
    transition.add_argument("--actor-id", required=True)
    transition.add_argument("--artifact-hash", action="append", default=[])
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    _resolve_cli_config_paths(args)

    if args.command == "setup-guide":
        manager = _user_config_manager(args)
        user_config = manager.load() if manager.path.exists() else GeasUserConfig.default()
        profile_name, profile = user_config.profile(args.geas_profile)
        default_provider, providers = load_provider_configs(args.providers)
        guide = build_setup_guide(
            manager=manager,
            profile_name=profile_name,
            profile=profile,
            default_provider=default_provider,
            providers=providers,
            research_policy=ResearchPolicy.from_yaml(args.research_policy),
        )
        if args.format == "markdown":
            print(render_setup_guide_markdown(guide), end="")
        else:
            _json(guide)
        return

    if args.command == "config-init":
        manager = _user_config_manager(args)
        config = manager.load_or_create(update_defaults=args.update_defaults)
        print("Ensuring packaged Geas agent skill is installed.", file=sys.stderr)
        skills = manager.install_builtin_skill()
        if skills.conflicts:
            print(
                "Preserved unmanaged Geas agent-skill conflicts.",
                file=sys.stderr,
            )
        profile_name, profile = config.profile(args.geas_profile)
        _json(
            {
                "config": str(manager.path),
                "config_root": str(manager.root),
                "created_or_validated": True,
                "default_profile": config.default_profile,
                "profiles": tuple(sorted(config.profiles)),
                "selected_profile": profile_name,
                "ontology_directory": str(manager.ontology_root(profile)),
                "ontology_repository": (
                    profile.ontology_git.url if profile.ontology_git is not None else None
                ),
                "secret_sources": tuple(
                    {
                        "path": str(path),
                        "format": format,
                    }
                    for path, format in manager.secret_paths(profile)
                ),
                "managed_defaults": manager.last_defaults_receipt,
                "skills": skills,
            }
        )
        return

    if args.command == "ontology-sync":
        manager = _user_config_manager(args)
        user_config = manager.load_or_create()
        profile_name, profile = user_config.profile(args.geas_profile)
        if profile.ontology_git is None:
            raise ValueError(f"Geas profile {profile_name!r} has no ontology_git config")
        repository = OntologyRepositoryManager(
            checkout=manager.ontology_root(profile),
            config=profile.ontology_git,
        )
        pull_requested = args.pull or not args.push
        result: dict[str, object] = {
            "profile": profile_name,
            "config": str(manager.path),
        }
        state_path = manager.root / "state" / "ontology-sync" / f"{profile_name}.json"
        if pull_requested:
            result["pull"] = repository.freshen(
                state_path=state_path,
                max_age_seconds=user_config.ontology_freshness.max_age_seconds,
                force=args.pull or profile.ontology_git.pull_before_update,
            )
        if args.push:
            if not pull_requested:
                result["preflight"] = repository.freshen(
                    state_path=state_path,
                    max_age_seconds=user_config.ontology_freshness.max_age_seconds,
                    force=profile.ontology_git.pull_before_update,
                )
            result["push"] = repository.push(
                relative_paths=(Path("."),),
                message=args.message,
                freshness_state_path=state_path,
            )
        _json(result)
        return

    if args.command in {"ontology-artifact-publish", "ontology-artifact-sync"}:
        manager = _user_config_manager(args)
        profile_name, profile = manager.profile(args.geas_profile)
        if profile.ontology_git is None:
            raise ValueError(f"Geas profile {profile_name!r} has no ontology_git config")
        ontology_value = Path(args.ontology)
        if (
            ontology_value.is_absolute()
            or len(ontology_value.parts) != 1
            or ontology_value.name.startswith(".")
        ):
            raise ValueError("ontology artifact commands require one configured ontology name")
        ontology_root = _profile_ontology_root(
            args,
            pull_before_read=True,
            ontology=ontology_value,
        )
        assert ontology_root is not None
        ontology_directory = ontology_root / ontology_value.name
        if not ontology_directory.is_dir() or ontology_directory.is_symlink():
            raise ValueError(f"configured ontology does not exist: {ontology_value.name}")
        artifact_manager = OntologyArtifactManager(ontology_directory)
        artifact_store = GitHubReleaseArtifactStore(
            profile.ontology_git.url,
            branch=profile.ontology_git.branch,
        )
        if args.command == "ontology-artifact-sync":
            _json(
                {
                    "profile": profile_name,
                    "artifact_sync": artifact_manager.hydrate(
                        store=artifact_store,
                        roles=tuple(args.role),
                    ),
                }
            )
            return
        publication = artifact_manager.publish(
            store=artifact_store,
            published_by=args.published_by,
            storage_rights_basis=args.storage_rights_basis,
            source_library=args.source_library,
            knowledge_projection=args.knowledge_projection,
            generated_content=args.generated_content,
        )
        repository = OntologyRepositoryManager(
            checkout=ontology_root,
            config=profile.ontology_git,
        )
        push = repository.push(
            relative_paths=(Path(ontology_value.name),),
            message=(
                args.message
                or f"geas: publish artifacts for {ontology_value.name}"
            ),
            freshness_state_path=(
                manager.root / "state" / "ontology-sync" / f"{profile_name}.json"
            ),
        )
        _json(
            {
                "profile": profile_name,
                "publication": publication,
                "push": push,
            }
        )
        return

    if args.command == "ontology-list":
        manager = _user_config_manager(args)
        profile_name = None
        user_config = (
            manager.load() if manager.path.exists() else GeasUserConfig.default()
        )
        if args.directory is None:
            if manager.path.exists():
                profile_name, _profile = manager.profile(args.geas_profile)
                root = _profile_ontology_root(args, pull_before_read=True)
                assert root is not None
            else:
                profile_name, profile = user_config.profile(args.geas_profile)
                root = manager.ontology_root(profile)
            location = "selected_profile"
        else:
            root = args.directory
            location = "provided_directory"
        inventory = inventory_ontologies(
            root,
            defaults=user_config.ontology_defaults,
        )
        _json(
            {
                **inventory.model_dump(mode="json"),
                "location": location,
                "profile": profile_name,
            }
        )
        return

    if args.command == "providers":
        default, providers = load_provider_configs(args.providers)
        _json(
            {
                "default": default,
                "providers": {
                    name: {
                        "kind": config.kind,
                        "base_url": str(config.base_url),
                        "model": config.model,
                        "external": config.external,
                        "api_key_env": config.api_key_env or None,
                        "max_output_tokens": config.max_output_tokens,
                        "context_window_tokens": config.context_window_tokens,
                    }
                    for name, config in providers.items()
                },
            }
        )
        return

    if args.command == "model-smoke":
        default, providers = load_provider_configs(args.providers)
        name = args.provider or default
        if providers[name].api_key_env:
            _load_allowed_secrets(
                args,
                allowed_names=frozenset({providers[name].api_key_env}),
            )
        gate = ModelUseGate(
            ModelUsePolicy.from_yaml(args.model_policy),
            ModelUseContext(
                operation=ModelOperation.MODEL_SMOKE,
                data_class=DataClass.PUBLIC,
                input_kind=InputKind.METADATA_ONLY,
                approval_receipt_id=args.approval_receipt_id,
                run_id=args.run_id or f"run:model-smoke:{uuid4()}",
            ),
            budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
            usage_ledger=UsageLedger(args.root / "usage.sqlite"),
            approval_registry=ApprovalRegistry(args.root / "usage.sqlite"),
            override_principal=(
                _local_approval_principal(args.root) if args.override_external_budget else None
            ),
        )
        client = ModelClient(
            name,
            providers[name],
            gate=gate,
        )
        result = client.complete_json(
            system=(
                "Return one JSON object only. Do not call tools. "
                'The schema is {"status":"ok","capabilities":["string"]}.'
            ),
            user="Report that the tool-free research extraction model endpoint is available.",
            max_output_tokens=256,
        )
        store = ImmutableStore(args.root)
        store.initialize()
        authorization_hash = store.put_record(
            "model-authorization",
            gate.last_authorization,
        )
        settlement_hash = (
            store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_settlement is not None
            else None
        )
        approval_hash = (
            store.put_record("approval-receipt", gate.last_approval_receipt)
            if gate.last_approval_receipt is not None
            else None
        )
        _json(
            {
                "provider": name,
                "result": result,
                "authorization": gate.last_authorization,
                "authorization_record_hash": authorization_hash,
                "usage_settlement": gate.last_settlement,
                "usage_settlement_record_hash": settlement_hash,
                "approval_receipt": gate.last_approval_receipt,
                "approval_receipt_record_hash": approval_hash,
            }
        )
        return

    if args.command == "store-init":
        store = ImmutableStore(args.root)
        store.initialize()
        _json({"root": str(store.root), "initialized": True})
        return

    if args.command == "ontology-init":
        shared_default = args.directory is None
        manager = _user_config_manager(args)
        user_config = (
            manager.load_or_create()
            if shared_default
            else (
                manager.load() if manager.path.exists() else GeasUserConfig.default()
            )
        )
        profile_name = None
        repository = None
        pull_receipt = None
        push_receipt = None
        if shared_default:
            profile_name, profile = user_config.profile(args.geas_profile)
            ontology_root = manager.ontology_root(profile)
            ontology_name = shared_ontology_directory(
                args.concept_id,
                config_home=manager.root,
            ).name
            directory = ontology_root / ontology_name
            pull_requested = bool(args.pull)
            if args.pull is None and profile.ontology_git is not None:
                pull_requested = (
                    user_config.ontology_freshness.check_before_use
                    or profile.ontology_git.pull_before_update
                    or not (ontology_root / ".git").is_dir()
                )
            push_requested = (
                profile.ontology_git.push_on_update
                if args.push is None and profile.ontology_git is not None
                else bool(args.push)
            )
            if (pull_requested or push_requested) and profile.ontology_git is None:
                raise ValueError(
                    f"Geas profile {profile_name!r} has no ontology_git config"
                )
            if profile.ontology_git is not None:
                repository = OntologyRepositoryManager(
                    checkout=ontology_root,
                    config=profile.ontology_git,
                )
            if pull_requested and repository is not None:
                pull_receipt = repository.freshen(
                    state_path=(
                        manager.root / "state" / "ontology-sync" / f"{profile_name}.json"
                    ),
                    max_age_seconds=user_config.ontology_freshness.max_age_seconds,
                    force=bool(args.pull) or profile.ontology_git.pull_before_update,
                )
        else:
            directory = args.directory
            ontology_name = directory.name
            push_requested = False
            if args.pull is not None or args.push is not None:
                raise ValueError(
                    "--pull and --push apply only to the default profile ontology location"
                )
        if not shared_default and (directory.is_absolute() or ".." in directory.parts):
            raise ValueError("explicit ontology directory must be workspace-relative")
        build_path = directory / "build.yaml"
        library_path = directory / "library.yaml"
        existing = tuple(path for path in (build_path, library_path) if path.exists())
        if existing and not args.force:
            raise ValueError(
                "ontology configuration already exists; pass --force to replace: "
                + ", ".join(str(path) for path in existing)
            )
        ontology_values: dict[str, object] = {
            "version": 1,
            "topic": args.topic,
            "topic_concept_id": args.concept_id,
            "topic_recorded_at": datetime.now(UTC),
            "topic_recorded_by": f"ontology-init:os-user:{getpass.getuser()}",
            "output_directory": (
                Path("data") / "ontologies" / ontology_name / "generated"
                if shared_default
                else directory / "generated"
            ),
        }
        if args.provider is not None:
            ontology_values["provider"] = args.provider
        config = OntologyBuildConfig.from_defaults(
            user_config.ontology_defaults,
            **ontology_values,
        )
        library = SourceLibraryManifest(
            version=1,
            id=f"library:{args.concept_id.removeprefix('concept:')}",
            title=f"{args.topic} source library",
            description=(
                "Reusable immutable sources for this ontology. Replace the default "
                "selection with explicit repositories, source IDs, URI prefixes, "
                "or connectors as the collection becomes defined."
            ),
            include_all_parsed_sources=True,
        )
        directory.mkdir(parents=True, exist_ok=True)
        build_path.write_text(config.explicit_yaml())
        library_path.write_text(library.explicit_yaml())
        if push_requested and repository is not None:
            push_receipt = repository.push(
                relative_paths=(Path(ontology_name),),
                message=f"geas: update ontology {ontology_name}",
                freshness_state_path=(
                    manager.root / "state" / "ontology-sync" / f"{profile_name}.json"
                ),
            )
        _json(
            {
                "directory": str(directory),
                "build_config": str(build_path),
                "library_config": str(library_path),
                "defaults_explicit": True,
                "location": "user_config" if shared_default else "explicit_workspace",
                "ontology_name": ontology_name,
                "profile": profile_name,
                "pull": pull_receipt,
                "push": push_receipt,
            }
        )
        return

    if args.command == "source-add":
        store = ImmutableStore(args.root)
        store.initialize()
        source = store.ingest_file(
            args.path,
            source_uri=args.uri,
            license=args.license,
        )
        _json(source)
        return

    if args.command == "deposit-add":
        store = ImmutableStore(args.root)
        store.initialize()
        nostr_evidence = tuple(
            (NostrEvent.model_validate_json(path.read_bytes()), claim)
            for paths, claim in (
                (args.nostr_ownership_event, NostrClaim.OWNERSHIP),
                (args.nostr_authorship_event, NostrClaim.AUTHORSHIP),
                (args.nostr_publication_event, NostrClaim.PUBLICATION),
            )
            for path in paths
        )
        permission_values = {
            "archive": args.archive_permission,
            "quote": args.quote_permission,
            "transform": args.transform_permission,
            "redistribute_original": args.redistribute_original_permission,
        }
        result = DepositManager(
            store=store,
            policy=DepositPolicy.from_yaml(args.deposit_policy),
        ).deposit_file(
            args.path,
            deposited_by=args.deposited_by,
            acquisition_method=args.method,
            original_locator=args.original_locator,
            source_uri=args.source_uri,
            license=args.license,
            authors=tuple(args.author),
            usage_conditions=tuple(args.usage_condition),
            rights_basis=args.rights_basis,
            provenance_note=args.provenance_note,
            nostr_evidence=nostr_evidence,
            overrides=DepositOverrides(
                scope_label=args.scope_label,
                index_content=args.index_content,
                include_in_ontology=args.include_in_ontology,
                model_route=args.model_route,
                redistribution_status=args.redistribution_status,
                usage_permissions=(
                    UsagePermissionOverrides.model_validate(permission_values)
                    if any(value is not None for value in permission_values.values())
                    else None
                ),
                retention_policy=args.retention_policy,
            ),
        )
        _json(result)
        return

    if args.command == "research-local":
        store = ImmutableStore(args.root)
        store.initialize()
        connector = LocalFileConnector(args.corpus)
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        if args.compiler_provider:
            _, providers = load_provider_configs(args.providers)
            if args.compiler_provider not in providers:
                raise ValueError(f"unknown provider: {args.compiler_provider}")
            provider = providers[args.compiler_provider]
            gate = ModelUseGate(
                ModelUsePolicy.from_yaml(args.model_policy),
                ModelUseContext(
                    operation=ModelOperation.QUERY_COMPILATION,
                    data_class=args.compiler_data_class,
                    input_kind=InputKind.METADATA_ONLY,
                    approval_receipt_id=args.approval_receipt_id,
                    run_id=args.run_id or f"run:research-local:{uuid4()}",
                ),
                budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
                usage_ledger=UsageLedger(args.root / "usage.sqlite"),
                approval_registry=ApprovalRegistry(args.root / "usage.sqlite"),
                override_principal=(
                    _local_approval_principal(args.root) if args.override_external_budget else None
                ),
            )
            client = ModelClient(
                args.compiler_provider,
                provider,
                gate=gate,
            )
            proposal = ModelQueryCompiler(client).compile(
                args.question,
                vocabulary=vocabulary,
                manifests={connector.manifest.id: connector.manifest},
            )
            store.put_record("model-authorization", gate.last_authorization)
            if gate.last_settlement is not None:
                store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_approval_receipt is not None:
                store.put_record("approval-receipt", gate.last_approval_receipt)
            compiler = CompilerIdentity(
                id=f"compiler:model:{args.compiler_provider}:{provider.model}",
                version=ModelQueryCompiler.version,
            )
        else:
            proposal = deterministic_proposal(
                args.question,
                connector_id=connector.manifest.id,
                concept_ids=tuple(args.concept),
            )
            compiler = CompilerIdentity(id="compiler:deterministic-lexical", version="1")
        if args.concept:
            proposal = proposal.model_copy(
                update={"concept_ids": tuple(sorted(set(proposal.concept_ids) | set(args.concept)))}
            )
        if args.term:
            proposal = QueryProposal.model_validate(
                {
                    **proposal.model_dump(mode="json"),
                    "exact_terms": args.term,
                    "result_limit": args.result_limit,
                }
            )
        else:
            proposal = proposal.model_copy(update={"result_limit": args.result_limit})
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=compiler,
            human_approved=False,
        )
        result = OfflineResearchRunner(store=store, connector=connector).run(
            plan,
            topic_branch=args.topic_branch,
        )
        _json(result)
        return

    if args.command == "discover-mojeek":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.provider("connector:mojeek")
        if not provider_policy.enabled:
            raise ValueError("Mojeek is disabled by the research policy")
        _load_allowed_secrets(
            args,
            allowed_names=frozenset({provider_policy.credential_env}),
        )
        connector = MojeekDiscoveryConnector()
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        base = deterministic_proposal(
            args.question,
            connector_id=connector.manifest.id,
            concept_ids=tuple(args.concept),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "exact_terms": args.term or base.exact_terms,
                "source_classes": [SourceClass.WEB],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": args.result_limit,
                "page_limit": min(
                    math.ceil(args.result_limit / 40),
                    provider_policy.max_requests_per_run,
                ),
            }
        )
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=args.approve_budget,
        )
        execution = DiscoveryExecutor().run(plan, connector)
        store = ImmutableStore(args.root)
        store.initialize()
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "query-plan": (store.put_record("query-plan", plan),),
            "connector-manifest": (store.put_record("connector-manifest", connector.manifest),),
            "discovery-run": (store.put_record("discovery-run", execution.discovery_run),),
        }
        if provider_policy.persist_normalized_results:
            record_hashes["discovery-hit"] = tuple(
                store.put_record("discovery-hit", hit) for hit in execution.hits
            )
        _json(
            {
                "query_plan": plan,
                "discovery_run": execution.discovery_run,
                "hits": execution.hits,
                "persistence": {
                    "normalized_results": provider_policy.persist_normalized_results,
                    "storage_rights": provider_policy.storage_rights,
                    "raw_response_retention_days": (provider_policy.raw_response_retention_days),
                    "note": "Search results are discovery metadata, never evidence.",
                },
                "record_hashes": record_hashes,
                "acquisition_priority": research_policy.open_source_acquisition_order,
            }
        )
        return

    if args.command == "acquire-discovery":
        store = ImmutableStore(args.root)
        store.initialize()
        receipt = GitHubDiscoveryAcquirer(store=store).acquire_file(
            args.discovery,
            limit=args.limit,
        )
        _json(receipt)
        return

    if args.command == "ontology-build":
        ontology_root = _profile_ontology_root(
            args,
            pull_before_read=not args.check,
            ontology=args.config,
        )
        config_path = (
            resolve_profile_ontology_config(
                args.config,
                filename="build.yaml",
                ontology_root=ontology_root,
            )
            if ontology_root is not None
            else resolve_ontology_build_config(args.config)
        )
        manager = _user_config_manager(args)
        user_config = (
            manager.load() if manager.path.exists() else GeasUserConfig.default()
        )
        config = OntologyBuildConfig.from_yaml(
            config_path,
            defaults=user_config.ontology_defaults,
        )
        acceptance_repository = None
        ontology_directory = None
        resolved_config = config_path.resolve()
        if ontology_root is not None and resolved_config.is_relative_to(
            ontology_root.resolve()
        ):
            _profile_name, selected_profile = user_config.profile(args.geas_profile)
            if selected_profile.ontology_git is not None:
                acceptance_repository = ontology_root
                ontology_directory = resolved_config.parent.relative_to(
                    ontology_root.resolve()
                )
        resolved_workspace = args.workspace.resolve()
        if (
            acceptance_repository is None
            and (resolved_workspace / ".git").is_dir()
            and resolved_config.is_relative_to(resolved_workspace)
        ):
            acceptance_repository = resolved_workspace
            ontology_directory = resolved_config.parent.relative_to(resolved_workspace)
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        _, providers = load_provider_configs(args.providers)
        credential_names = {
            research_policy.provider("connector:mojeek").credential_env,
            providers[config.provider].api_key_env,
        }
        _load_allowed_secrets(
            args,
            allowed_names=frozenset(item for item in credential_names if item),
        )
        builder = OntologyBuilder(
            config=config,
            root=args.root,
            workspace=args.workspace,
            providers_path=args.providers,
            research_policy_path=args.research_policy,
            model_policy_path=args.model_policy,
            budget_policy_path=args.budget_policy,
            truth_policy_path=args.truth_policy,
            vocabulary_path=args.vocabulary,
            acceptance_repository=acceptance_repository,
            ontology_directory=ontology_directory,
            ontology_config_path=config_path,
            force_refresh=args.refresh,
            force_reextract=args.reextract,
        )
        receipt = builder.check() if args.check else builder.run()
        _json(receipt)
        exit_code = _ontology_build_exit_code(receipt)
        if exit_code:
            raise SystemExit(exit_code)
        return

    if args.command == "discover-crossref":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.domain_index("connector:crossref")
        if not provider_policy.enabled:
            raise ValueError("Crossref is disabled by the research policy")
        connector = CrossrefDiscoveryConnector()
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        base = deterministic_proposal(
            args.question,
            connector_id=connector.manifest.id,
            concept_ids=tuple(args.concept),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "exact_terms": args.term or base.exact_terms,
                "source_classes": [SourceClass.SCHOLARLY],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": args.result_limit,
                "page_limit": min(
                    provider_policy.max_requests_per_run,
                    math.ceil(args.result_limit / 100),
                ),
            }
        )
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=args.approve_budget,
        )
        execution = DiscoveryExecutor().run(plan, connector)
        store = ImmutableStore(args.root)
        store.initialize()
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "query-plan": (store.put_record("query-plan", plan),),
            "connector-manifest": (store.put_record("connector-manifest", connector.manifest),),
            "discovery-run": (store.put_record("discovery-run", execution.discovery_run),),
            "discovery-hit": tuple(
                store.put_record("discovery-hit", hit) for hit in execution.hits
            ),
        }
        _json(
            {
                "query_plan": plan,
                "discovery_run": execution.discovery_run,
                "hits": execution.hits,
                "persistence": {
                    "normalized_metadata": provider_policy.persist_normalized_metadata,
                    "metadata_license": provider_policy.metadata_license,
                    "raw_response_retention_days": (
                        provider_policy.raw_response_retention_days
                    ),
                    "note": "Bibliographic metadata is discovery, not claim evidence.",
                },
                "record_hashes": record_hashes,
            }
        )
        return

    if args.command == "discover-openalex":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.domain_index("connector:openalex")
        if not provider_policy.enabled:
            raise ValueError("OpenAlex is disabled by the research policy")
        _load_allowed_secrets(
            args,
            allowed_names=frozenset({provider_policy.credential_env}),
        )
        run_id = args.run_id or f"openalex:{uuid4()}"
        store = ImmutableStore(args.root)
        store.initialize()
        connector = OpenAlexDiscoveryConnector(
            usage_ledger=UsageLedger(args.root / "usage.sqlite"),
            budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
            run_id=run_id,
            human_approved=args.approve_budget,
            max_calls_per_run=provider_policy.max_requests_per_run,
            daily_cost_ceiling_microusd=math.floor(
                (provider_policy.daily_free_allowance_usd or 0) * 1_000_000
            ),
        )
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        base = deterministic_proposal(
            args.question,
            connector_id=connector.manifest.id,
            concept_ids=tuple(args.concept),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "exact_terms": args.term or base.exact_terms,
                "source_classes": [SourceClass.SCHOLARLY],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": args.result_limit,
                "page_limit": min(
                    provider_policy.max_requests_per_run,
                    math.ceil(args.result_limit / 100),
                ),
            }
        )
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=args.approve_budget,
        )
        execution = DiscoveryExecutor().run(plan, connector)
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "query-plan": (store.put_record("query-plan", plan),),
            "connector-manifest": (store.put_record("connector-manifest", connector.manifest),),
            "discovery-run": (store.put_record("discovery-run", execution.discovery_run),),
        }
        if provider_policy.persist_normalized_metadata:
            record_hashes["discovery-hit"] = tuple(
                store.put_record("discovery-hit", hit) for hit in execution.hits
            )
        _json(
            {
                "run_id": run_id,
                "query_plan": plan,
                "discovery_run": execution.discovery_run,
                "hits": execution.hits,
                "persistence": {
                    "normalized_metadata": provider_policy.persist_normalized_metadata,
                    "metadata_license": provider_policy.metadata_license,
                    "raw_response_retention_days": (
                        provider_policy.raw_response_retention_days
                    ),
                    "note": "OpenAlex metadata is discovery, not claim evidence.",
                },
                "cost_control": {
                    "transactional_ledger": str((args.root / "usage.sqlite").resolve()),
                    "reported_cost_microusd": (
                        execution.discovery_run.reported_cost_microusd
                    ),
                    "daily_ceiling_usd": provider_policy.daily_free_allowance_usd,
                },
                "record_hashes": record_hashes,
            }
        )
        return

    if args.command == "discover-europe-pmc":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.domain_index("connector:europe-pmc")
        if not provider_policy.enabled:
            raise ValueError("Europe PMC is disabled by the research policy")
        connector = EuropePmcDiscoveryConnector()
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        base = deterministic_proposal(
            args.question,
            connector_id=connector.manifest.id,
            concept_ids=tuple(args.concept),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "exact_terms": args.term or base.exact_terms,
                "source_classes": [SourceClass.SCHOLARLY],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": args.result_limit,
                "page_limit": min(
                    provider_policy.max_requests_per_run,
                    math.ceil(args.result_limit / 100),
                ),
            }
        )
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=args.approve_budget,
        )
        execution = DiscoveryExecutor().run(plan, connector)
        store = ImmutableStore(args.root)
        store.initialize()
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "query-plan": (store.put_record("query-plan", plan),),
            "connector-manifest": (store.put_record("connector-manifest", connector.manifest),),
            "discovery-run": (store.put_record("discovery-run", execution.discovery_run),),
        }
        if provider_policy.persist_normalized_metadata:
            record_hashes["discovery-hit"] = tuple(
                store.put_record("discovery-hit", hit) for hit in execution.hits
            )
        _json(
            {
                "query_plan": plan,
                "discovery_run": execution.discovery_run,
                "hits": execution.hits,
                "persistence": {
                    "normalized_metadata": provider_policy.persist_normalized_metadata,
                    "metadata_license": provider_policy.metadata_license,
                    "raw_response_retention_days": (
                        provider_policy.raw_response_retention_days
                    ),
                    "abstracts": False,
                    "full_text": False,
                    "note": "Lite bibliographic metadata is discovery, not evidence.",
                },
                "record_hashes": record_hashes,
            }
        )
        return

    if args.command == "resolve-unpaywall":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.domain_index("connector:unpaywall")
        if not provider_policy.enabled:
            raise ValueError("Unpaywall is disabled by the research policy")
        normalized_dois = tuple(dict.fromkeys(normalize_doi(item) for item in args.doi))
        if len(normalized_dois) > provider_policy.max_requests_per_run:
            raise ValueError("Unpaywall DOI count exceeds the provider run limit")
        _load_allowed_secrets(
            args,
            allowed_names=frozenset({provider_policy.credential_env}),
        )
        resolver = UnpaywallResolver()
        store = ImmutableStore(args.root)
        store.initialize()
        resolutions = []
        constraints = []
        for doi in normalized_dois:
            try:
                resolution = resolver.resolve(doi)
            except UnpaywallError:
                observed_at = datetime.now(UTC)
                fields = {
                    "target_id": f"doi:{doi}",
                    "locator": doi_locator(doi),
                    "connector_id": resolver.connector_id,
                    "observed_at": observed_at,
                }
                constraints.append(
                    AccessConstraint(
                        id=identified("access-constraint", fields),
                        target_id=f"doi:{doi}",
                        locator=doi_locator(doi),
                        reason=AccessConstraintReason.UNAVAILABLE_API,
                        observed_at=observed_at,
                        connector_id=resolver.connector_id,
                        lawful_alternatives=(doi_locator(doi),),
                        human_resolvable=True,
                        detail="Unpaywall lookup failed; upstream content was not retained",
                    )
                )
            else:
                resolutions.append(resolution)
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "connector-manifest": (
                store.put_record("connector-manifest", resolver.manifest),
            ),
            "open-access-resolution": tuple(
                store.put_record("open-access-resolution", item) for item in resolutions
            ),
            "access-constraint": tuple(
                store.put_record("access-constraint", item) for item in constraints
            ),
        }
        _json(
            {
                "resolutions": resolutions,
                "constraints": constraints,
                "persistence": {
                    "normalized_metadata": provider_policy.persist_normalized_metadata,
                    "metadata_license": provider_policy.metadata_license,
                    "raw_response_retention_days": (
                        provider_policy.raw_response_retention_days
                    ),
                    "contact_identity": "transport_only",
                    "note": (
                        "Only locations with a reported license are automatically "
                        "eligible for later acquisition."
                    ),
                },
                "record_hashes": record_hashes,
            }
        )
        return

    if args.command == "parse-document":
        path = args.path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("document path must be a regular file")
        media_type = (
            args.media_type
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        store = ImmutableStore(args.root)
        receipt = ParsedDocumentManager(store=store).ingest(
            path.read_bytes(),
            source_uri=args.source_uri or path.as_uri(),
            media_type=media_type,
            connector_id="connector:operator-file",
            license=args.license,
        )
        _json(receipt)
        return

    if args.command == "derive-structure":
        store = ImmutableStore(args.root)
        store.initialize()
        _json(
            StructuralDocumentManager(store=store).derive_stored(
                args.text_derivation_id
            )
        )
        return

    if args.command == "derive-citations":
        store = ImmutableStore(args.root)
        store.initialize()
        _json(
            CitationDocumentManager(store=store).derive_stored(
                args.structural_derivation_id
            )
        )
        return

    if args.command == "propose-extraction":
        default, providers = load_provider_configs(args.providers)
        name = args.provider or default
        config = providers[name]
        if not 1.0 <= args.timeout <= 86_400.0:
            raise ValueError("model timeout must be between 1 and 86400 seconds")
        if config.api_key_env:
            _load_allowed_secrets(
                args,
                allowed_names=frozenset({config.api_key_env}),
            )
        gate = ModelUseGate(
            ModelUsePolicy.from_yaml(args.model_policy),
            ModelUseContext(
                operation=ModelOperation.ONTOLOGY_EXTRACTION,
                data_class=args.data_class,
                input_kind=InputKind.SOURCE_CONTENT,
                model_route=args.model_route,
                approval_receipt_id=args.approval_receipt_id,
                run_id=args.run_id or f"run:ontology-extraction:{uuid4()}",
            ),
            budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
            usage_ledger=UsageLedger(args.root / "usage.sqlite"),
            approval_registry=ApprovalRegistry(args.root / "usage.sqlite"),
            override_principal=(
                _local_approval_principal(args.root)
                if args.override_external_budget
                else None
            ),
        )
        effective_output_tokens = min(args.max_output_tokens, config.max_output_tokens)
        model_parameters = ModelParameters(
            thinking=args.thinking,
            reasoning_effort=(
                args.reasoning_effort if args.thinking else "none"
            ),
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            min_p=args.min_p,
            seed=args.seed,
            stop=tuple(args.stop),
        )
        required_context = model_parameters.minimum_context_tokens
        if required_context is not None and (
            config.context_window_tokens is None
            or config.context_window_tokens < required_context
        ):
            raise ValueError(
                f"reasoning_effort=max requires at least {required_context} context "
                "tokens; increase the provider/server context window or use high"
            )
        client = ModelClient(
            name,
            config,
            gate=gate,
            timeout=args.timeout,
            parameters=model_parameters,
        )
        store = ImmutableStore(args.root)
        try:
            receipt = AnchorGroundedExtractionManager(
                store=store,
                client=client,
                provider=name,
                model=config.model,
            ).propose(
                question=args.question,
                structural_derivation_id=args.structural_derivation_id,
                anchor_ids=args.anchor,
                allowed_concept_ids=args.concept,
                max_output_tokens=effective_output_tokens,
                model_parameters=model_parameters,
                debug_reasoning=args.debug_reasoning,
                allow_partial_items=args.allow_partial_items,
            )
        except Exception:
            if gate.last_authorization is not None:
                store.put_record("model-authorization", gate.last_authorization)
            if gate.last_settlement is not None:
                store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_approval_receipt is not None:
                store.put_record("approval-receipt", gate.last_approval_receipt)
            raise
        authorization_hash = store.put_record(
            "model-authorization",
            gate.last_authorization,
        )
        settlement_hash = (
            store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_settlement is not None
            else None
        )
        approval_hash = (
            store.put_record("approval-receipt", gate.last_approval_receipt)
            if gate.last_approval_receipt is not None
            else None
        )
        _json(
            {
                "receipt": receipt,
                "authorization": gate.last_authorization,
                "authorization_record_hash": authorization_hash,
                "usage_settlement": gate.last_settlement,
                "usage_settlement_record_hash": settlement_hash,
                "approval_receipt": gate.last_approval_receipt,
                "approval_receipt_record_hash": approval_hash,
            }
        )
        return

    if args.command == "compare-extractions":
        store = ImmutableStore(args.root)
        records = tuple(store.iter_records("extraction-proposal"))
        comparison = compare_proposals(
            find_proposal(records, args.baseline_proposal_id),
            find_proposal(records, args.candidate_proposal_id),
        )
        digest = store.put_record("model-parameter-comparison", comparison)
        _json({"comparison": comparison, "record_digest": digest})
        return

    if args.command == "proposal-slice":
        records = tuple(ImmutableStore(args.root).iter_records("extraction-proposal"))
        proposal = find_proposal(records, args.proposal_id)
        _json(
            slice_proposal(
                proposal,
                args.concept_id,
                include_descendants=args.descendants,
            )
        )
        return

    if args.command == "promotion-stage":
        _json(
            GitPromotionManager(
                store=ImmutableStore(args.root),
                repository=args.workspace,
            ).stage(
                args.proposal_id,
                topic=args.topic,
                topic_concept_id=args.topic_concept_id,
                output=args.output,
                target_ref=args.target_ref,
            )
        )
        return

    if args.command == "promotion-verify":
        manifest, path, commit = GitPromotionManager(
            store=ImmutableStore(args.root),
            repository=args.workspace,
        ).verify_from_ref(
            args.manifest,
            canonical_ref=args.canonical_ref,
        )
        _json({"manifest": manifest, "path": path, "canonical_commit": commit})
        return

    if args.command == "promotion-apply":
        _json(
            GitPromotionManager(
                store=ImmutableStore(args.root),
                repository=args.workspace,
            ).apply(
                args.manifest,
                canonical_ref=args.canonical_ref,
            )
        )
        return

    if args.command == "acquire-open-access":
        doi = normalize_doi(args.doi)
        store = ImmutableStore(args.root)
        store.initialize()
        resolutions = sorted(
            (
                OpenAccessResolution.model_validate(value)
                for value in store.iter_records("open-access-resolution")
                if value.get("doi") == doi
            ),
            key=lambda item: (item.resolved_at, item.id),
        )
        if not resolutions:
            raise ValueError(
                "no stored Unpaywall resolution for DOI; run resolve-unpaywall first"
            )
        _json(
            LicenseGatedAcquirer(store=store).acquire(resolutions[-1])
        )
        return

    if args.command == "truth-snapshot":
        store = ImmutableStore(args.root)
        store.initialize()
        manager = TruthManager(
            workspace_root=args.workspace,
            store_root=store.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        )
        snapshot = manager.capture(
            created_by=args.created_by,
            predecessor=args.predecessor,
        )
        digest = store.put_record("truth-snapshot", snapshot)
        _json(
            {
                "snapshot": snapshot,
                "record_digest": digest,
                "record_path": str(store.record_path("truth-snapshot", digest)),
            }
        )
        return

    if args.command == "truth-check":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        report = TruthManager(
            workspace_root=args.workspace,
            store_root=args.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        ).verify(snapshot)
        _json(report)
        if not report.clean:
            raise SystemExit(2)
        return

    if args.command == "projection-stamp":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        stamp = SQLiteProjectionGuard().stamp(
            args.database,
            snapshot,
            schema_version=args.schema_version,
            builder_version=args.builder_version,
        )
        _json(stamp)
        return

    if args.command == "projection-check":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        truth_report = TruthManager(
            workspace_root=args.workspace,
            store_root=args.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        ).verify(snapshot)
        report = SQLiteProjectionGuard().verify(
            args.database,
            snapshot,
            truth_report=truth_report,
            expected_schema_version=SQLiteKnowledgeProjection.schema_version,
            expected_builder_version=SQLiteKnowledgeProjection.builder_version,
        )
        _json(report)
        if not report.clean:
            raise SystemExit(2)
        return

    if args.command == "knowledge-import":
        store = ImmutableStore(args.root)
        receipt = KnowledgeImporter(store=store).import_pack(
            KnowledgePack.from_yaml(args.pack),
            imported_by=args.imported_by,
        )
        _json(receipt)
        return

    if args.command == "structure-show":
        if args.limit < 1 or args.limit > 100_000:
            raise ValueError("structure limit must be 1..100000")
        store = ImmutableStore(args.root)
        store.initialize()
        anchors = tuple(
            sorted(
                (
                    StructuralAnchor.model_validate(value)
                    for value in store.iter_records("structural-anchor")
                    if value.get("structural_derivation_id")
                    == args.structural_derivation_id
                ),
                key=lambda item: item.ordinal,
            )
        )
        if args.leaf_only:
            containers = {AnchorKind.DOCUMENT, AnchorKind.PAGE, AnchorKind.SECTION}
            anchors = tuple(item for item in anchors if item.kind not in containers)
        truncated = len(anchors) > args.limit
        _json(
            {
                "structural_derivation_id": args.structural_derivation_id,
                "leaf_only": args.leaf_only,
                "total": len(anchors),
                "truncated": truncated,
                "anchors": anchors[: args.limit],
            }
        )
        return

    if args.command == "structure-list":
        if not 1 <= args.limit <= 10_000:
            raise ValueError("structure-list limit must be between 1 and 10000")
        values = tuple(ImmutableStore(args.root).iter_records("structural-derivation"))
        _json(
            {
                "count": min(len(values), args.limit),
                "total": len(values),
                "derivations": values[: args.limit],
            }
        )
        return

    if args.command == "bundle-import":
        _json(
            KnowledgeBundleImporter(store=ImmutableStore(args.root)).import_bundle(
                args.bundle,
                imported_by=args.imported_by,
            )
        )
        return

    if args.command == "library-build":
        ontology_root = _profile_ontology_root(
            args,
            pull_before_read=True,
            ontology=args.manifest,
        )
        manifest_path = (
            resolve_profile_ontology_config(
                args.manifest,
                filename="library.yaml",
                ontology_root=ontology_root,
            )
            if ontology_root is not None
            else args.manifest
        )
        _json(
            SourceLibraryBuilder(store=ImmutableStore(args.root)).build(
                SourceLibraryManifest.from_yaml(manifest_path),
                args.database,
            )
        )
        return

    if args.command == "library-query":
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.SOURCE_LIBRARY,
        )
        _json(
            SourceLibraryQueryEngine(database).query(
                args.question,
                limit=args.limit,
            )
        )
        return

    if args.command == "library-show":
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.SOURCE_LIBRARY,
        )
        _json(SourceLibraryQueryEngine(database).describe())
        return

    if args.command == "library-context":
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.SOURCE_LIBRARY,
        )
        _json(
            SourceLibraryQueryEngine(database).context(
                args.question,
                limit=args.limit,
                max_characters=args.max_characters,
            )
        )
        return

    if args.command == "projection-build":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        store = ImmutableStore(args.root)
        store.initialize()
        manager = TruthManager(
            workspace_root=args.workspace,
            store_root=store.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        )
        result = SQLiteKnowledgeProjection(
            store=store,
            workspace_root=args.workspace,
            vocabulary_path=args.query_vocabulary,
        ).build(
            args.database,
            snapshot=snapshot,
            truth_manager=manager,
        )
        _json(result)
        return

    if args.command == "knowledge-query":
        kinds = tuple(args.kind) if args.kind else tuple(QueryRecordType)
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.KNOWLEDGE_PROJECTION,
        )
        _json(
            KnowledgeQueryEngine(database).query(
                args.question,
                record_types=kinds,
                limit=args.limit,
            )
        )
        return

    if args.command == "knowledge-audit":
        store = ImmutableStore(args.root)
        store.initialize()
        report = DeterministicKnowledgeAuditor().audit(store, as_of=args.as_of)
        finding_hashes = tuple(
            store.put_record("knowledge-audit-finding", item)
            for item in report.findings
        )
        report_hash = store.put_record("knowledge-audit-report", report)
        _json(
            {
                "report": report,
                "record_hashes": {
                    "knowledge-audit-finding": finding_hashes,
                    "knowledge-audit-report": (report_hash,),
                },
            }
        )
        if args.fail_on_error and not report.clean:
            raise SystemExit(2)
        return

    if args.command == "identifier-show":
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.KNOWLEDGE_PROJECTION,
        )
        _json(
            KnowledgeQueryEngine(database).identifier(
                args.kind,
                args.value,
            )
        )
        return

    if args.command == "topic-show":
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.KNOWLEDGE_PROJECTION,
        )
        _json(
            KnowledgeQueryEngine(database).topic(
                args.concept_id,
                as_of=args.as_of,
            )
        )
        return

    if args.command == "topic-export":
        if args.vault_link and args.format != "agent-instructions":
            raise ValueError("--vault-link requires --format agent-instructions")
        database = _resolve_portable_database(
            args,
            args.database,
            role=ArtifactRole.KNOWLEDGE_PROJECTION,
        )
        topic = KnowledgeQueryEngine(database).topic(
            args.concept_id,
            as_of=args.as_of,
        )
        if args.format == "obsidian":
            receipt = write_obsidian_vault(
                render_topic_obsidian(topic),
                args.output,
                force=args.force,
            )
            _json(
                {
                    **receipt,
                    "format": "obsidian",
                    "snapshot_id": topic.projection_snapshot_id,
                    "topic_concept_id": topic.topic_concept_id,
                }
            )
        else:
            rendered = (
                render_agent_instructions(topic, vault_link=args.vault_link)
                if args.format == "agent-instructions"
                else render_topic_markdown(topic)
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
            _json(
                {
                    "output": str(args.output.resolve()),
                    "bytes": len(rendered.encode()),
                    "format": args.format,
                    "snapshot_id": topic.projection_snapshot_id,
                    "topic_concept_id": topic.topic_concept_id,
                }
            )
        return

    if args.command == "projection-benchmark":
        workload = WorkloadPolicy.from_yaml(args.workload_policy)
        configured = next(
            item.claims for item in workload.benchmark_tiers if item.name == args.tier
        )
        claim_count = args.claims or configured
        if args.claims is not None and args.claims > configured:
            raise ValueError("custom benchmark claim count cannot exceed its selected tier")
        _json(
            ProjectionBenchmark(
                workspace_root=args.workspace,
                truth_policy=TruthPolicy.from_yaml(args.truth_policy),
            ).run(tier=args.tier, claim_count=claim_count)
        )
        return

    if args.command == "policy-check":
        observations = [
            ThreatObservation.model_validate_json(path.read_text()) for path in args.observations
        ]
        target = ThreatTarget(source_version=args.source_version)
        decision = PolicyEngine.from_yaml(args.policy).evaluate(
            target=target,
            workflow_id=args.workflow_id,
            stage=args.stage,
            observations=observations,
        )
        _json(decision)
        return

    if args.command == "workflow-transition":
        event = WorkflowEngine().transition(
            workflow_id=args.workflow_id,
            source_version=args.source_version,
            from_state=args.from_state,
            to_state=args.to_state,
            actor_kind=args.actor_kind,
            actor_id=args.actor_id,
            artifact_hashes=tuple(args.artifact_hash),
        )
        _json(event)
        return

    raise AssertionError(f"unhandled command: {args.command}")
