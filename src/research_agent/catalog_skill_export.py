"""Shared catalog-aware ontology skill export service."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from research_agent.agent_skills import (
    OntologyIdentity,
    PortableArtifactIdentity,
    bind_catalog_skill_provenance,
)
from research_agent.ontology_artifacts import (
    ArtifactHydrationReceipt,
    ArtifactRole,
    ArtifactStore,
    OntologyArtifactManager,
    _sqlite_input_revision,
)
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_config import OntologyBuildDefaults
from research_agent.ontology_resolution import OntologySelection
from research_agent.ontology_subscriptions import OntologySubscription
from research_agent.paths import resolve_selected_ontology_config
from research_agent.projection import KnowledgeQueryEngine
from research_agent.render import render_ontology_skill
from research_agent.repository_catalog import verify_catalog


@dataclass(frozen=True)
class CatalogSkillExport:
    files: dict[Path, bytes]
    artifact: ArtifactHydrationReceipt
    topic_concept_id: str


def selection_from_repository_catalog(
    catalog_path: Path,
    *,
    ontology_name: str,
    subscription_name: str,
    subscription: OntologySubscription,
    commit: str,
) -> OntologySelection:
    """Create the same verified selection shape used by configured subscriptions."""
    catalog_path = catalog_path.resolve()
    repository_root = catalog_path.parent
    if subscription.catalog.as_posix() != catalog_path.relative_to(repository_root).as_posix():
        raise ValueError("catalog path does not match the declaring subscription")
    verified = verify_catalog(catalog_path, names=(ontology_name,))[0]
    repository_path = verified.ontology_path.relative_to(repository_root)
    return OntologySelection(
        name=verified.name,
        description=verified.description,
        source=f"subscription:{subscription_name}",
        source_kind="subscription",
        ontology_directory=verified.ontology_path,
        verified_ontology_directory=verified.ontology_path,
        repository_identity=subscription.url.removesuffix(".git"),
        repository_root=repository_root,
        verified_repository_root=repository_root,
        identity_kind="remote",
        active_ref=subscription.active_ref,
        commit=commit,
        catalog_path=catalog_path,
        repository_path=repository_path,
        bundle_sha256=verified.bundle_sha256,
        files=verified.files,
        trust_status="trusted",
        authorization="profile",
        subscription_name=subscription_name,
        subscription=subscription,
    )


def export_catalog_skill(
    selection: OntologySelection,
    *,
    artifact_store: ArtifactStore,
    skill_name: str,
    geas_version: str,
    geas_commit: str | None,
    defaults: OntologyBuildDefaults | None = None,
    artifact_workspace: Path | None = None,
) -> CatalogSkillExport:
    """Resolve declared config, hydrate one projection, and render bound skill bytes."""
    if geas_commit is None:
        raise ValueError("catalog skill export requires an exact executing Geas commit")
    build_path = resolve_selected_ontology_config(
        Path(selection.name),
        filename="build.yaml",
        selection=selection,
    )
    topic_concept_id = OntologyBuildConfig.from_yaml(
        build_path,
        defaults=defaults,
    ).topic_concept_id
    artifact_manifest = resolve_selected_ontology_config(
        Path(selection.name),
        filename="artifacts.yaml",
        selection=selection,
    )
    ontology_directory = selection.ontology_directory
    if artifact_workspace is not None:
        workspace = artifact_workspace.resolve()
        if workspace.exists() or workspace.is_symlink():
            raise ValueError("artifact workspace must not already exist")
        ontology_directory = workspace / selection.name
        ontology_directory.mkdir(parents=True)
        shutil.copyfile(artifact_manifest, ontology_directory / "artifacts.yaml")
    manager = OntologyArtifactManager(ontology_directory)
    hydration = manager.hydrate(
        store=artifact_store,
        roles=(ArtifactRole.KNOWLEDGE_PROJECTION,),
    )
    identity = _artifact_identity(hydration)
    hydrated = hydration.hydrated[0]
    database = Path(hydrated.path)
    if _sqlite_input_revision(database, hydrated.role) != hydrated.input_revision:
        raise ValueError(
            "knowledge-projection artifact input revision does not match its verified "
            "SQLite projection stamp"
        )
    ontology = _catalog_ontology_identity(selection)
    topic = KnowledgeQueryEngine(database).topic(topic_concept_id)
    rendered = render_ontology_skill(
        topic,
        skill_name=skill_name,
        ontology_name=selection.name,
        repository_url=ontology.repository_url,
        branch=ontology.branch,
        ontology_commit=ontology.commit,
        geas_version=geas_version,
        geas_commit=geas_commit,
    )
    return CatalogSkillExport(
        files=bind_catalog_skill_provenance(
            rendered,
            ontology=ontology,
            artifact=identity,
        ),
        artifact=hydration,
        topic_concept_id=topic_concept_id,
    )


def _artifact_identity(receipt: ArtifactHydrationReceipt) -> PortableArtifactIdentity:
    projections = tuple(
        item for item in receipt.hydrated if item.role is ArtifactRole.KNOWLEDGE_PROJECTION
    )
    if len(projections) != 1:
        raise ValueError("verified artifact receipt does not identify one knowledge projection")
    item = projections[0]
    return PortableArtifactIdentity(
        role="knowledge-projection",
        content_sha256=item.content_sha256,
        input_revision=item.input_revision,
    )


def _catalog_ontology_identity(selection: OntologySelection) -> OntologyIdentity:
    subscription = selection.subscription
    if (
        subscription is None
        or selection.subscription_name is None
        or selection.active_ref is None
        or selection.commit is None
        or selection.catalog_path is None
        or selection.repository_path is None
        or selection.bundle_sha256 is None
        or selection.repository_root is None
    ):
        raise ValueError("catalog selection has incomplete subscription provenance")
    try:
        catalog_path = selection.catalog_path.relative_to(selection.repository_root).as_posix()
    except ValueError as error:
        raise ValueError("catalog path escapes its declaring repository") from error
    if catalog_path != subscription.catalog.as_posix():
        raise ValueError("selected catalog does not match its declaring subscription")
    branch = selection.active_ref.removeprefix("refs/heads/")
    return OntologyIdentity(
        name=selection.name,
        repository_url=subscription.url,
        branch=branch,
        commit=selection.commit,
        active_ref=selection.active_ref,
        ontology_commit=selection.commit,
        subscription_name=selection.subscription_name,
        catalog_path=catalog_path,
        ontology_path=selection.repository_path.as_posix(),
        bundle_sha256=selection.bundle_sha256,
    )
