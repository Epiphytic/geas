"""Deterministic authorization and immutable snapshots for repository ontologies."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from pydantic import Field, field_validator

from research_agent.models import StrictModel, canonical_json, utc_now
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    ResolvedRepositoryCatalog,
    VerifiedCatalogOntology,
    _verify_transitive_inputs,
    ontology_bundle_sha256,
    resolve_repository_catalog,
    verify_catalog,
)

if TYPE_CHECKING:
    from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager


_DIGEST = re.compile(r"[0-9a-f]{64}")
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _normalized_ref(value: str) -> str:
    raw = value.strip()
    if _OBJECT_ID.fullmatch(raw):
        return raw.lower()
    if raw.startswith("tags/"):
        raw = f"refs/{raw}"
    elif not raw.startswith("refs/"):
        raw = f"refs/heads/{raw}"
    if not raw.startswith(("refs/heads/", "refs/tags/")):
        raise ValueError("trust refs must be full branch/tag refs or commit IDs")
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or "\\" in raw
        or ".." in raw
        or "@{" in raw
        or "//" in raw
        or raw.endswith(("/", "."))
    ):
        raise ValueError("trust ref is invalid")
    return raw


def _normalized_relative_path(value: object, *, label: str) -> Path:
    raw = str(value)
    if not raw or "\\" in raw:
        raise ValueError(f"{label} must be a normalized relative path")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != raw
    ):
        raise ValueError(f"{label} must be a normalized relative path")
    return Path(raw)


def _unique_sorted(values: Sequence[object], *, label: str) -> tuple[object, ...]:
    if not values:
        raise ValueError(f"{label} selectors must be non-empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} selectors contain a duplicate")
    return tuple(sorted(values, key=lambda item: str(item).encode("utf-8")))


class TrustRule(StrictModel):
    decision: Literal["allow", "deny"]
    repository: str = Field(min_length=1)
    refs: Literal["*"] | tuple[str, ...] = "*"
    paths: Literal["*"] | tuple[Path, ...] = "*"
    bundle_sha256: Literal["*"] | tuple[str, ...] = "*"
    created_at: datetime
    created_via: Literal["interactive", "manual"]

    @field_validator("repository")
    @classmethod
    def repository_is_inert_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValueError("trust repository identity is invalid")
        return normalized

    @field_validator("refs", mode="before")
    @classmethod
    def refs_are_normalized(cls, value: object) -> object:
        if value == "*":
            return value
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("trust refs must be '*' or a non-empty sequence")
        normalized = tuple(_normalized_ref(str(item)) for item in value)
        return _unique_sorted(normalized, label="ref")

    @field_validator("paths", mode="before")
    @classmethod
    def paths_are_normalized(cls, value: object) -> object:
        if value == "*":
            return value
        if isinstance(value, (str, Path)) or not isinstance(value, Sequence):
            raise ValueError("trust paths must be '*' or a non-empty sequence")
        normalized = tuple(_normalized_relative_path(item, label="trust path") for item in value)
        return _unique_sorted(normalized, label="path")

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def digests_are_normalized(cls, value: object) -> object:
        if value == "*":
            return value
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("bundle digests must be '*' or a non-empty sequence")
        normalized = tuple(str(item).lower() for item in value)
        if any(not _DIGEST.fullmatch(item) for item in normalized):
            raise ValueError("bundle digest must be a lowercase SHA-256")
        return _unique_sorted(normalized, label="bundle digest")

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trust rule created_at must include a timezone")
        return value


class TrustContext(StrictModel):
    repository: str = Field(min_length=1)
    ref: str
    path: Path
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dirty: bool = False

    @field_validator("ref", mode="before")
    @classmethod
    def ref_is_normalized(cls, value: object) -> str:
        return _normalized_ref(str(value))

    @field_validator("path", mode="before")
    @classmethod
    def path_is_normalized(cls, value: object) -> Path:
        return _normalized_relative_path(value, label="trust context path")


class TrustDecision(StrictModel):
    matched: bool
    allowed: bool
    specificity: int | None = Field(default=None, ge=0, le=7)
    rule: TrustRule | None = None


class InstalledOntologySnapshot(StrictModel):
    name: str
    description: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path
    files: tuple[CatalogFile, ...]

    @field_validator("path", mode="before")
    @classmethod
    def path_is_config_relative(cls, value: object) -> Path:
        return _normalized_relative_path(value, label="installed snapshot path")


class AuthorizedOntology(StrictModel):
    ontology: VerifiedCatalogOntology
    authorization: Literal["rule", "interactive", "snapshot", "yolo"]
    snapshot: InstalledOntologySnapshot | None = None


class SnapshotRemovalReceipt(StrictModel):
    name: str
    bundle_sha256: str
    path: Path
    removed: bool


class _StagedSnapshot(StrictModel):
    snapshot: InstalledOntologySnapshot
    ontology: VerifiedCatalogOntology
    destination: Path
    created: bool


class _SnapshotRemovalPhase(StrEnum):
    VALIDATED = "validated"
    QUARANTINED = "quarantined"
    CONFIG_COMMITTED = "config_committed"


class TrustPrompt(Protocol):
    """Pure injected I/O for decisions; trusted state remains manager-owned."""

    def choose_action(self, catalog: ResolvedRepositoryCatalog) -> Literal["1", "2", "3", "4"]: ...

    def select_ontology(
        self, ontology: VerifiedCatalogOntology, *, action: Literal["2", "3"]
    ) -> bool: ...


def _specificity(rule: TrustRule) -> int:
    return (
        (0 if rule.bundle_sha256 == "*" else 4)
        + (0 if rule.paths == "*" else 2)
        + (0 if rule.refs == "*" else 1)
    )


def _matches(rule: TrustRule, context: TrustContext) -> bool:
    if rule.repository != context.repository:
        return False
    if rule.refs != "*" and context.ref not in rule.refs:
        return False
    if rule.paths != "*" and context.path not in rule.paths:
        return False
    if rule.bundle_sha256 != "*" and context.bundle_sha256 not in rule.bundle_sha256:
        return False
    return not (
        context.dirty
        and rule.decision == "allow"
        and rule.refs != "*"
        and rule.paths == "*"
        and rule.bundle_sha256 == "*"
    )


def evaluate_trust(context: TrustContext, rules: Sequence[TrustRule]) -> TrustDecision:
    """Apply highest selector specificity, resolving equal scores to deny."""
    matches = tuple(rule for rule in rules if _matches(rule, context))
    if not matches:
        return TrustDecision(matched=False, allowed=False)
    winner = max(
        matches,
        key=lambda rule: (
            _specificity(rule),
            rule.decision == "deny",
            canonical_json(rule.model_dump(mode="json")),
        ),
    )
    return TrustDecision(
        matched=True,
        allowed=winner.decision == "allow",
        specificity=_specificity(winner),
        rule=winner,
    )


def _context(catalog: ResolvedRepositoryCatalog, ontology: VerifiedCatalogOntology) -> TrustContext:
    if catalog.repository_root is None or catalog.repository_identity is None:
        raise ValueError("repository catalog has no trust identity")
    if catalog.active_ref is None:
        raise ValueError("repository catalog has no active Git ref")
    try:
        relative = ontology.ontology_path.relative_to(catalog.repository_root)
    except ValueError as error:
        raise ValueError("catalog ontology path escapes repository") from error
    return TrustContext(
        repository=catalog.repository_identity,
        ref=catalog.active_ref,
        path=relative,
        bundle_sha256=ontology.bundle_sha256,
        dirty=ontology.dirty,
    )


def _fresh_catalog(catalog: ResolvedRepositoryCatalog) -> ResolvedRepositoryCatalog:
    """Repeat integrity and repository metadata resolution before authorization."""
    if catalog.repository_root is None:
        raise ValueError("repository catalog has no repository root")
    if catalog.discovery_start is None:
        raise ValueError("repository catalog has no discovery start")
    fresh = resolve_repository_catalog(catalog.discovery_start)
    if fresh != catalog:
        raise ValueError("repository catalog changed after integrity verification")
    return fresh


def _profile_update(
    manager: UserConfigManager,
    profile_name: str,
    profile: GeasProfile,
) -> None:
    config = manager.load()
    if profile_name not in config.profiles:
        raise ValueError(f"unknown Geas profile: {profile_name}")
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, profile_name: profile}})
    )


def _append_rules(
    manager: UserConfigManager,
    profile_name: str,
    rules: Sequence[TrustRule],
) -> None:
    config = manager.load()
    try:
        profile = config.profiles[profile_name]
    except KeyError:
        raise ValueError(f"unknown Geas profile: {profile_name}") from None
    updated = profile.model_copy(update={"trust_rules": (*profile.trust_rules, *rules)})
    _profile_update(manager, profile_name, updated)


def _interactive_rule(
    catalog: ResolvedRepositoryCatalog,
    *,
    decision: Literal["allow", "deny"],
    refs: Literal["*"] | tuple[str, ...] = "*",
    paths: Literal["*"] | tuple[Path, ...] = "*",
    digests: Literal["*"] | tuple[str, ...] = "*",
) -> TrustRule:
    if catalog.repository_identity is None:
        raise ValueError("repository catalog has no trust identity")
    return TrustRule(
        decision=decision,
        repository=catalog.repository_identity,
        refs=refs,
        paths=paths,
        bundle_sha256=digests,
        created_at=utc_now(),
        created_via="interactive",
    )


def _profile_with_effective_ref_denial(
    profile: GeasProfile,
    catalog: ResolvedRepositoryCatalog,
) -> GeasProfile:
    if catalog.active_ref is None:
        raise ValueError("repository catalog has no active Git ref")
    denial = _interactive_rule(
        catalog,
        decision="deny",
        refs=(catalog.active_ref,),
    )
    denial_selectors = (
        denial.repository,
        denial.refs,
        denial.paths,
        denial.bundle_sha256,
    )
    retained: list[TrustRule] = []
    for rule in profile.trust_rules:
        selectors = (
            rule.repository,
            rule.refs,
            rule.paths,
            rule.bundle_sha256,
        )
        if selectors == denial_selectors:
            continue
        if rule.decision == "allow" and rule.repository == denial.repository:
            if rule.refs == "*":
                if rule.paths != "*" or rule.bundle_sha256 != "*":
                    continue
            elif catalog.active_ref in rule.refs:
                remaining_refs = tuple(ref for ref in rule.refs if ref != catalog.active_ref)
                if not remaining_refs:
                    continue
                rule = rule.model_copy(update={"refs": remaining_refs})
        retained.append(rule)
    return profile.model_copy(update={"trust_rules": _normalized_trust_rules((*retained, denial))})


def _normalized_trust_rules(rules: Sequence[TrustRule]) -> tuple[TrustRule, ...]:
    """Return one deterministic, fail-closed rule per effective selector.

    Deny wins any decision collision.  Otherwise the earliest audit timestamp
    wins, followed by creation method and canonical JSON as stable tie-breakers.
    """
    grouped: dict[
        tuple[
            str,
            Literal["*"] | tuple[str, ...],
            Literal["*"] | tuple[Path, ...],
            Literal["*"] | tuple[str, ...],
        ],
        list[TrustRule],
    ] = {}
    for rule in rules:
        selector = (
            rule.repository,
            rule.refs,
            rule.paths,
            rule.bundle_sha256,
        )
        grouped.setdefault(selector, []).append(rule)

    normalized: list[TrustRule] = []
    for candidates in grouped.values():
        denied = [rule for rule in candidates if rule.decision == "deny"]
        eligible = denied or candidates
        normalized.append(
            min(
                eligible,
                key=lambda rule: (
                    rule.created_at,
                    rule.created_via,
                    canonical_json(rule.model_dump(mode="json")),
                ),
            )
        )
    return tuple(
        sorted(
            normalized,
            key=lambda rule: canonical_json(rule.model_dump(mode="json")),
        )
    )


def authorize_repository_catalog(
    catalog: ResolvedRepositoryCatalog,
    *,
    manager: UserConfigManager,
    profile_name: str,
    yolo: bool,
    prompt: TrustPrompt | None,
) -> tuple[AuthorizedOntology, ...]:
    """Authorize a freshly re-verified catalog through trusted profile state."""
    catalog = _fresh_catalog(catalog)
    config = manager.load()
    try:
        profile = config.profiles[profile_name]
    except KeyError:
        raise ValueError(f"unknown Geas profile: {profile_name}") from None

    if yolo:
        return tuple(
            AuthorizedOntology(ontology=ontology, authorization="yolo")
            for ontology in catalog.ontologies
        )

    authorized: dict[str, AuthorizedOntology] = {}
    unresolved: list[VerifiedCatalogOntology] = []
    for ontology in catalog.ontologies:
        decision = evaluate_trust(_context(catalog, ontology), profile.trust_rules)
        if decision.matched:
            if decision.allowed:
                authorized[ontology.name] = AuthorizedOntology(
                    ontology=ontology, authorization="rule"
                )
        else:
            unresolved.append(ontology)
    if not unresolved:
        return tuple(
            authorized[item.name] for item in catalog.ontologies if item.name in authorized
        )
    if prompt is None:
        raise ValueError(
            "repository ontology is not trusted and non-interactive authorization "
            "cannot ask for a decision"
        )

    action = prompt.choose_action(catalog)
    if action == "1":
        _append_rules(manager, profile_name, (_interactive_rule(catalog, decision="allow"),))
        for ontology in unresolved:
            authorized[ontology.name] = AuthorizedOntology(
                ontology=ontology, authorization="interactive"
            )
    elif action == "2":
        if catalog.active_ref is None:
            raise ValueError("repository catalog has no active Git ref")
        rules: list[TrustRule] = []
        for ontology in unresolved:
            selected = prompt.select_ontology(ontology, action="2")
            context = _context(catalog, ontology)
            rules.append(
                _interactive_rule(
                    catalog,
                    decision="allow" if selected else "deny",
                    refs=(context.ref,),
                    paths=(context.path,),
                    digests=(context.bundle_sha256,),
                )
            )
            if selected:
                authorized[ontology.name] = AuthorizedOntology(
                    ontology=ontology, authorization="interactive"
                )
        _append_rules(manager, profile_name, rules)
    elif action == "3":
        selected = tuple(
            ontology for ontology in unresolved if prompt.select_ontology(ontology, action="3")
        )
        staged: list[_StagedSnapshot] = []
        try:
            for ontology in selected:
                staged.append(_stage_snapshot(ontology, manager=manager))
            registrations = list(profile.installed_ontologies)
            for item in staged:
                existing = next(
                    (
                        registered
                        for registered in registrations
                        if (registered.name, registered.bundle_sha256)
                        == (item.snapshot.name, item.snapshot.bundle_sha256)
                    ),
                    None,
                )
                if existing is not None:
                    if existing != item.snapshot:
                        raise ValueError("registered ontology snapshot metadata mismatch")
                    continue
                registrations.append(item.snapshot)
            registered_profile = profile.model_copy(
                update={"installed_ontologies": tuple(registrations)}
            )
            _profile_update(
                manager,
                profile_name,
                _profile_with_effective_ref_denial(registered_profile, catalog),
            )
        except BaseException:
            _rollback_staged_snapshots(staged)
            raise
        authorized = {
            item.ontology.name: AuthorizedOntology(
                ontology=item.ontology,
                authorization="snapshot",
                snapshot=item.snapshot,
            )
            for item in staged
        }
    elif action == "4":
        _profile_update(
            manager,
            profile_name,
            _profile_with_effective_ref_denial(profile, catalog),
        )
        authorized.clear()
    else:  # Defensive for runtime implementations not checked by static typing.
        raise ValueError("invalid repository trust prompt choice")

    return tuple(authorized[item.name] for item in catalog.ontologies if item.name in authorized)


def _reject_symlink_ancestry(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError(f"symbolic link is not allowed in snapshot path: {current}")


def _verified_source(ontology: VerifiedCatalogOntology) -> VerifiedCatalogOntology:
    matches = verify_catalog(ontology.catalog_path, names=(ontology.name,))
    if len(matches) != 1:
        raise ValueError("verified ontology is absent from its catalog")
    fresh = matches[0].model_copy(update={"dirty": ontology.dirty})
    if fresh != ontology:
        raise ValueError("ontology changed after integrity verification")
    return ontology


def _verify_snapshot_directory(directory: Path, ontology: VerifiedCatalogOntology) -> None:
    entry = CatalogOntology(
        name=ontology.name,
        description=ontology.description,
        path=Path("snapshot"),
        files=ontology.files,
        bundle_sha256=ontology.bundle_sha256,
    )
    if ontology_bundle_sha256(entry) != ontology.bundle_sha256:
        raise ValueError("snapshot bundle digest mismatch")
    expected_files = {item.path.as_posix() for item in ontology.files}
    expected_directories = {
        parent.as_posix()
        for item in ontology.files
        for parent in item.path.parents
        if parent != Path(".")
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    pending = [directory]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for item in entries:
                candidate = Path(item.path)
                relative = candidate.relative_to(directory).as_posix()
                if item.is_symlink():
                    raise ValueError(
                        f"symbolic link is not allowed in snapshot inventory: {relative}"
                    )
                if item.is_dir(follow_symlinks=False):
                    actual_directories.add(relative)
                    pending.append(candidate)
                elif item.is_file(follow_symlinks=False):
                    actual_files.add(relative)
                else:
                    raise ValueError(f"unsupported snapshot inventory entry: {relative}")
    undeclared_files = sorted(actual_files.difference(expected_files))
    if undeclared_files:
        raise ValueError(f"undeclared snapshot inventory file: {undeclared_files[0]}")
    undeclared_directories = sorted(actual_directories.difference(expected_directories))
    if undeclared_directories:
        raise ValueError(f"undeclared snapshot inventory directory: {undeclared_directories[0]}")
    for item in ontology.files:
        candidate = directory / item.path
        _reject_symlink_ancestry(candidate)
        if not candidate.is_file():
            raise ValueError(f"snapshot inventory file is missing: {item.path}")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(directory.resolve(strict=True)):
            raise ValueError("snapshot inventory file escapes destination")
        content = resolved.read_bytes()
        if len(content) != item.size_bytes:
            raise ValueError(f"snapshot inventory size mismatch: {item.path}")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError(f"snapshot inventory sha256 mismatch: {item.path}")
    _verify_transitive_inputs(directory, ontology.files, workspace=directory)


def _remove_exact_directory(path: Path) -> None:
    _reject_symlink_ancestry(path)
    if path.exists():
        if not path.is_dir():
            raise ValueError("managed snapshot path must be a directory")
        shutil.rmtree(path)


def _remove_empty_snapshot_parents(destination: Path) -> None:
    for empty_parent in (destination.parent, destination.parent.parent):
        try:
            empty_parent.rmdir()
        except OSError:
            break


def _rollback_staged_snapshots(staged: Sequence[_StagedSnapshot]) -> None:
    for item in reversed(staged):
        if item.created:
            _remove_exact_directory(item.destination)
            _remove_empty_snapshot_parents(item.destination)


def _physical_snapshot_identity(
    snapshot: InstalledOntologySnapshot,
    *,
    manager: UserConfigManager,
) -> tuple[str, str, Path]:
    expected = Path("snapshots") / snapshot.name / snapshot.bundle_sha256
    if snapshot.path != expected:
        raise ValueError("registered ontology snapshot destination is invalid")
    destination = Path(os.path.abspath(manager.root / snapshot.path))
    if not destination.is_relative_to(manager.root):
        raise ValueError("registered ontology snapshot destination escapes config root")
    return snapshot.name, snapshot.bundle_sha256, destination


def _config_references_physical_snapshot(
    config: GeasUserConfig,
    identity: tuple[str, str, Path],
    *,
    manager: UserConfigManager,
) -> bool:
    for profile in config.profiles.values():
        for candidate in profile.installed_ontologies:
            try:
                candidate_identity = _physical_snapshot_identity(
                    candidate,
                    manager=manager,
                )
            except ValueError:
                continue
            if candidate_identity == identity:
                return True
    return False


def _validated_snapshot_quarantine(
    quarantine: Path,
    *,
    snapshot: InstalledOntologySnapshot,
    manager: UserConfigManager,
) -> Path:
    _, _, destination = _physical_snapshot_identity(snapshot, manager=manager)
    candidate = Path(os.path.abspath(quarantine))
    expected_name = re.compile(rf"\.{re.escape(destination.name)}\.remove-[0-9a-f]{{32}}")
    if candidate.parent != destination.parent or not expected_name.fullmatch(candidate.name):
        raise ValueError("snapshot quarantine path is invalid")
    _reject_symlink_ancestry(candidate)
    return candidate


def _cleanup_snapshot_quarantine(
    quarantine: Path,
    *,
    snapshot: InstalledOntologySnapshot,
    manager: UserConfigManager,
) -> bool:
    """Confine and idempotently collect an unregistered snapshot quarantine."""
    candidate = _validated_snapshot_quarantine(
        quarantine,
        snapshot=snapshot,
        manager=manager,
    )
    identity = _physical_snapshot_identity(snapshot, manager=manager)
    if _config_references_physical_snapshot(manager.load(), identity, manager=manager):
        raise ValueError("registered snapshot quarantine cannot be removed")
    if not candidate.exists():
        return False
    _remove_exact_directory(candidate)
    return True


def _stage_snapshot(
    ontology: VerifiedCatalogOntology,
    *,
    manager: UserConfigManager,
) -> _StagedSnapshot:
    ontology = _verified_source(ontology)
    relative = Path("snapshots") / ontology.name / ontology.bundle_sha256
    destination = manager.root / relative
    _reject_symlink_ancestry(destination)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestry(parent)
    created = False
    temporary = parent / f".{ontology.bundle_sha256}.tmp-{uuid4().hex}"
    try:
        if destination.exists():
            _verify_snapshot_directory(destination, ontology)
        else:
            temporary.mkdir()
            for item in ontology.files:
                source = ontology.ontology_path / item.path
                _reject_symlink_ancestry(source)
                if not source.is_file():
                    raise ValueError(f"declared inventory file is missing: {item.path}")
                resolved = source.resolve(strict=True)
                if not resolved.is_relative_to(ontology.ontology_path):
                    raise ValueError("declared inventory file escapes ontology directory")
                target = temporary / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resolved, target)
            _verify_snapshot_directory(temporary, ontology)
            os.replace(temporary, destination)
            created = True

        snapshot = InstalledOntologySnapshot(
            name=ontology.name,
            description=ontology.description,
            bundle_sha256=ontology.bundle_sha256,
            path=relative,
            files=ontology.files,
        )
        installed = ontology.model_copy(
            update={
                "ontology_path": destination,
                "dirty": False,
            }
        )
        return _StagedSnapshot(
            snapshot=snapshot,
            ontology=installed,
            destination=destination,
            created=created,
        )
    except BaseException:
        if created:
            _remove_exact_directory(destination)
        _remove_exact_directory(temporary)
        _remove_empty_snapshot_parents(destination)
        raise
    finally:
        _remove_exact_directory(temporary)


def install_snapshot(
    ontology: VerifiedCatalogOntology,
    *,
    manager: UserConfigManager,
    profile_name: str,
) -> InstalledOntologySnapshot:
    """Copy and register one exact verified inventory transactionally."""
    config = manager.load()
    try:
        profile = config.profiles[profile_name]
    except KeyError:
        raise ValueError(f"unknown Geas profile: {profile_name}") from None
    staged = _stage_snapshot(ontology, manager=manager)
    try:
        existing = next(
            (
                item
                for item in profile.installed_ontologies
                if (item.name, item.bundle_sha256)
                == (staged.snapshot.name, staged.snapshot.bundle_sha256)
            ),
            None,
        )
        if existing is not None:
            if existing != staged.snapshot:
                raise ValueError("registered ontology snapshot metadata mismatch")
            return existing
        updated = profile.model_copy(
            update={
                "installed_ontologies": (
                    *profile.installed_ontologies,
                    staged.snapshot,
                )
            }
        )
        _profile_update(manager, profile_name, updated)
        return staged.snapshot
    except BaseException:
        _rollback_staged_snapshots((staged,))
        raise


def remove_snapshot(
    snapshot: InstalledOntologySnapshot,
    *,
    manager: UserConfigManager,
    profile_name: str,
) -> SnapshotRemovalReceipt:
    """Atomically unregister and remove one exact managed digest directory."""
    config = manager.load()
    try:
        profile = config.profiles[profile_name]
    except KeyError:
        raise ValueError(f"unknown Geas profile: {profile_name}") from None
    if snapshot not in profile.installed_ontologies:
        raise ValueError("ontology snapshot is not registered in the selected profile")
    physical_identity = _physical_snapshot_identity(snapshot, manager=manager)
    destination = physical_identity[2]
    _reject_symlink_ancestry(destination)
    if not destination.is_dir():
        raise ValueError("registered ontology snapshot directory is missing")
    remaining = tuple(item for item in profile.installed_ontologies if item != snapshot)
    updated_profile = profile.model_copy(update={"installed_ontologies": remaining})
    updated_config = config.model_copy(
        update={"profiles": {**config.profiles, profile_name: updated_profile}}
    )
    if _config_references_physical_snapshot(
        updated_config,
        physical_identity,
        manager=manager,
    ):
        manager.replace(updated_config)
        return SnapshotRemovalReceipt(
            name=snapshot.name,
            bundle_sha256=snapshot.bundle_sha256,
            path=snapshot.path,
            removed=False,
        )

    moved = destination.with_name(f".{destination.name}.remove-{uuid4().hex}")
    phase = _SnapshotRemovalPhase.VALIDATED
    try:
        os.replace(destination, moved)
        phase = _SnapshotRemovalPhase.QUARANTINED
        manager.replace(updated_config)
        phase = _SnapshotRemovalPhase.CONFIG_COMMITTED
    except BaseException:
        if phase is _SnapshotRemovalPhase.VALIDATED and moved.exists():
            phase = _SnapshotRemovalPhase.QUARANTINED
        if phase is _SnapshotRemovalPhase.QUARANTINED:
            try:
                committed = not _config_references_physical_snapshot(
                    manager.load(),
                    physical_identity,
                    manager=manager,
                )
            except BaseException:
                committed = False
            if committed:
                phase = _SnapshotRemovalPhase.CONFIG_COMMITTED
                with suppress(BaseException):
                    _cleanup_snapshot_quarantine(
                        moved,
                        snapshot=snapshot,
                        manager=manager,
                    )
            elif moved.exists() and not destination.exists():
                os.replace(moved, destination)
        raise
    try:
        _cleanup_snapshot_quarantine(
            moved,
            snapshot=snapshot,
            manager=manager,
        )
    except BaseException:
        # Configuration is authoritative now; quarantine cleanup is only GC.
        raise
    _remove_empty_snapshot_parents(destination)
    return SnapshotRemovalReceipt(
        name=snapshot.name,
        bundle_sha256=snapshot.bundle_sha256,
        path=snapshot.path,
        removed=True,
    )
