"""Catalog-aware ontology discovery with authorization kept separate from integrity."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field

from research_agent.models import StrictModel
from research_agent.ontology_recovery import recover_managed_removals
from research_agent.ontology_subscriptions import OntologySubscription
from research_agent.ontology_trust import (
    InstalledOntologySnapshot,
    TrustContext,
    TrustPrompt,
    authorize_repository_catalog,
    evaluate_trust,
)
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    ResolvedRepositoryCatalog,
    VerifiedCatalogOntology,
    ontology_bundle_sha256,
    resolve_repository_catalog,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager


class OntologyCandidate(StrictModel):
    """Inert ontology metadata; the ontology directory has not been parsed."""

    name: str
    source: str
    source_kind: Literal["legacy_profile", "repository", "subscription", "snapshot"]
    ontology_directory: Path
    verified_ontology_directory: Path | None = None
    description: str | None = None
    repository_identity: str | None = None
    repository_root: Path | None = None
    verified_repository_root: Path | None = None
    identity_kind: Literal["remote", "machine_local"] | None = None
    active_ref: str | None = None
    commit: str | None = None
    catalog_path: Path | None = None
    repository_path: Path | None = None
    bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files: tuple[CatalogFile, ...] | None = None
    trust_status: Literal["trusted", "untrusted", "denied"]
    authorization: Literal["profile", "rule", "interactive", "snapshot", "yolo"] | None = None
    subscription_name: str | None = None
    subscription: OntologySubscription | None = None


class OntologySelection(OntologyCandidate):
    """One unambiguous candidate that is authorized for operational use."""


class OntologyCatalog(StrictModel):
    version: Literal[1] = 1
    profile: str
    cwd: Path
    candidates: tuple[OntologyCandidate, ...]


def resolve_ontology_catalog(
    *,
    user_config: GeasUserConfig,
    manager: UserConfigManager,
    cwd: Path,
    yolo: bool,
    prompt: TrustPrompt | None,
) -> OntologyCatalog:
    """Merge profile and repository candidates without parsing ontology inputs."""
    recover_managed_removals(manager)
    profile_name, profile = user_config.profile()
    candidates: list[OntologyCandidate] = []

    normalized = profile.normalized_subscriptions(freshness=user_config.ontology_freshness)
    catalog_directories: set[Path] = set()
    for subscription_name, subscription in normalized.items():
        checkout = manager.subscription_checkout(subscription)
        catalog_path = checkout / subscription.catalog
        explicit = subscription_name in profile.subscriptions
        if not catalog_path.exists() and not catalog_path.is_symlink():
            if explicit:
                raise ValueError(f"subscription catalog is missing: {catalog_path}")
            continue
        resolved = resolve_repository_catalog(catalog_path.parent)
        if catalog_path.resolve() not in resolved.catalog_paths:
            raise ValueError(f"configured subscription catalog was not discovered: {catalog_path}")
        catalog_directories.update(item.ontology_path for item in resolved.ontologies)
        candidates.extend(
            _repository_candidates(
                resolved,
                profile=profile,
                manager=manager,
                profile_name=profile_name,
                yolo=yolo,
                prompt=prompt,
                source_kind="subscription",
                source=f"subscription:{subscription_name}",
                subscription_name=subscription_name,
                subscription=subscription,
            )
        )

    local = resolve_repository_catalog(cwd)
    if local.repository_root is not None:
        candidates.extend(
            _repository_candidates(
                local,
                profile=profile,
                manager=manager,
                profile_name=profile_name,
                yolo=yolo,
                prompt=prompt,
                source_kind="repository",
                source=None,
            )
        )

    # Authorization may atomically add snapshots or trust rules, so use fresh
    # trusted profile state for the remaining candidate construction.
    if manager.path.exists():
        current = manager.load()
        profile = current.profile(profile_name)[1]
    candidates.extend(_snapshot_candidates(profile, manager=manager))
    candidates.extend(
        _legacy_candidates(
            manager.ontology_root(profile),
            profile_name=profile_name,
            excluded=catalog_directories,
        )
    )

    return OntologyCatalog(
        profile=profile_name,
        cwd=cwd.expanduser().resolve(),
        candidates=tuple(sorted(candidates, key=lambda item: (item.name, item.source))),
    )


def select_ontology(name: str, *, catalog: OntologyCatalog) -> OntologySelection:
    """Select one authorized candidate, rejecting source collisions explicitly."""
    matches = tuple(item for item in catalog.candidates if item.name == name)
    if not matches:
        raise ValueError(f"unknown ontology: {name}")
    if len(matches) != 1:
        sources = ", ".join(item.source for item in matches)
        raise ValueError(f"ambiguous ontology {name!r}; candidates: {sources}")
    candidate = matches[0]
    if candidate.trust_status != "trusted":
        raise ValueError(
            f"ontology {name!r} is not trusted for operational use ({candidate.trust_status})"
        )
    return OntologySelection.model_validate(candidate.model_dump(mode="python"))


def legacy_directory_candidates(
    root: Path,
    *,
    profile_name: str = "provided_directory",
) -> tuple[OntologyCandidate, ...]:
    """Expose legacy direct children for the compatibility listing path."""
    return tuple(_legacy_candidates(root, profile_name=profile_name, excluded=set()))


def _legacy_candidates(
    root: Path,
    *,
    profile_name: str,
    excluded: set[Path],
) -> list[OntologyCandidate]:
    if root.is_symlink():
        raise ValueError("ontology inventory root cannot be a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("ontology inventory root must be a directory")
    candidates: list[OntologyCandidate] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if directory.name.startswith("."):
            continue
        if directory.is_symlink():
            raise ValueError(f"ontology directory cannot be a symbolic link: {directory.name}")
        if not directory.is_dir() or directory.resolve() in excluded:
            continue
        build = directory / "build.yaml"
        library = directory / "library.yaml"
        if not build.exists() and not library.exists():
            continue
        if build.is_symlink() or library.is_symlink():
            raise ValueError(f"ontology config cannot be a symbolic link: {directory.name}")
        candidates.append(
            OntologyCandidate(
                name=directory.name,
                source=f"profile:{profile_name}",
                source_kind="legacy_profile",
                ontology_directory=directory.resolve(),
                trust_status="trusted",
                authorization="profile",
            )
        )
    return candidates


def _snapshot_candidates(
    profile: GeasProfile,
    *,
    manager: UserConfigManager,
) -> list[OntologyCandidate]:
    candidates: list[OntologyCandidate] = []
    for snapshot in profile.installed_ontologies:
        raw_directory = manager.root / snapshot.path
        current = manager.root
        for component in snapshot.path.parts:
            current /= component
            if current.is_symlink():
                raise ValueError("installed ontology snapshot path contains a symbolic link")
        directory = raw_directory.resolve()
        if not directory.is_relative_to(manager.root):
            raise ValueError("installed ontology snapshot path escapes its managed root")
        _verify_snapshot(directory, snapshot)
        candidates.append(
            OntologyCandidate(
                name=snapshot.name,
                description=snapshot.description,
                source=f"snapshot:{snapshot.bundle_sha256}",
                source_kind="snapshot",
                ontology_directory=directory,
                verified_ontology_directory=directory,
                verified_repository_root=manager.root,
                bundle_sha256=snapshot.bundle_sha256,
                files=snapshot.files,
                trust_status="trusted",
                authorization="snapshot",
            )
        )
    return candidates


def _verify_snapshot(directory: Path, snapshot: InstalledOntologySnapshot) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("installed ontology snapshot directory is missing or symbolic")
    declared = {item.path.as_posix(): item for item in snapshot.files}
    actual: set[str] = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("installed ontology snapshot contains a symbolic link")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(declared):
        raise ValueError("installed ontology snapshot inventory does not match its registration")
    for relative, item in declared.items():
        content = (directory / relative).read_bytes()
        if len(content) != item.size_bytes or hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError(f"installed ontology snapshot file mismatch: {relative}")
    entry = CatalogOntology(
        name=snapshot.name,
        description=snapshot.description,
        path=Path(snapshot.name),
        files=snapshot.files,
        bundle_sha256=snapshot.bundle_sha256,
    )
    if ontology_bundle_sha256(entry) != snapshot.bundle_sha256:
        raise ValueError("installed ontology snapshot bundle digest mismatch")


def _repository_candidates(
    catalog: ResolvedRepositoryCatalog,
    *,
    profile: GeasProfile,
    manager: UserConfigManager,
    profile_name: str,
    yolo: bool,
    prompt: TrustPrompt | None,
    source_kind: Literal["repository", "subscription"],
    source: str | None,
    subscription_name: str | None = None,
    subscription: OntologySubscription | None = None,
) -> list[OntologyCandidate]:
    authorization: dict[tuple[str, Path], str] = {}
    installed_names: set[str] = set()
    if yolo:
        if catalog.discovery_start is None:
            raise ValueError("repository catalog has no discovery start")
        fresh = resolve_repository_catalog(catalog.discovery_start)
        if fresh != catalog:
            raise ValueError("repository catalog changed after integrity verification")
        authorization = {
            (ontology.name, ontology.ontology_path): "yolo"
            for ontology in catalog.ontologies
        }
    elif prompt is not None:
        authorized = authorize_repository_catalog(
            catalog,
            manager=manager,
            profile_name=profile_name,
            yolo=False,
            prompt=prompt,
        )
        authorization = {
            (item.ontology.name, item.ontology.ontology_path): item.authorization
            for item in authorized
            if item.authorization != "snapshot"
        }
        installed_names = {
            item.ontology.name for item in authorized if item.authorization == "snapshot"
        }
        profile = manager.load().profile(profile_name)[1]

    candidates: list[OntologyCandidate] = []
    for ontology in catalog.ontologies:
        if ontology.name in installed_names:
            continue
        status, authorized_via = _trust_status(catalog, ontology, profile=profile)
        explicit_authorization = authorization.get((ontology.name, ontology.ontology_path))
        if explicit_authorization is not None:
            status = "trusted"
            authorized_via = explicit_authorization
        relative = _repository_relative_path(catalog, ontology)
        candidates.append(
            OntologyCandidate(
                name=ontology.name,
                description=ontology.description,
                source=source or f"repository:{ontology.catalog_path}",
                source_kind=source_kind,
                ontology_directory=ontology.ontology_path,
                verified_ontology_directory=ontology.ontology_path,
                repository_identity=catalog.repository_identity,
                repository_root=catalog.repository_root,
                verified_repository_root=catalog.repository_root,
                identity_kind=catalog.identity_kind,
                active_ref=catalog.active_ref,
                commit=catalog.commit,
                catalog_path=ontology.catalog_path,
                repository_path=relative,
                bundle_sha256=ontology.bundle_sha256,
                files=ontology.files,
                trust_status=status,
                authorization=authorized_via,
                subscription_name=subscription_name,
                subscription=subscription,
            )
        )
    return candidates


def _trust_status(
    catalog: ResolvedRepositoryCatalog,
    ontology: VerifiedCatalogOntology,
    *,
    profile: GeasProfile,
) -> tuple[Literal["trusted", "untrusted", "denied"], str | None]:
    if catalog.repository_identity is None or catalog.active_ref is None:
        raise ValueError("repository catalog has incomplete trust metadata")
    decision = evaluate_trust(
        TrustContext(
            repository=catalog.repository_identity,
            ref=catalog.active_ref,
            path=_repository_relative_path(catalog, ontology),
            bundle_sha256=ontology.bundle_sha256,
            dirty=ontology.dirty,
        ),
        profile.trust_rules,
    )
    if not decision.matched:
        return "untrusted", None
    if decision.allowed:
        return "trusted", "rule"
    return "denied", None


def _repository_relative_path(
    catalog: ResolvedRepositoryCatalog,
    ontology: VerifiedCatalogOntology,
) -> Path:
    if catalog.repository_root is None:
        raise ValueError("repository catalog has no repository root")
    try:
        return ontology.ontology_path.relative_to(catalog.repository_root)
    except ValueError as error:
        raise ValueError("catalog ontology path escapes repository") from error
