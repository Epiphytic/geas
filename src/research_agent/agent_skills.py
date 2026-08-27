"""Strict, portable manifests for generated Geas agent skills."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files as package_files
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel

_GIT_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BRANCH_NAME = re.compile(r"^(?![-/])(?!.*(?://|\.\.|@\{|\.$))[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MANIFEST_NAME = "geas-skill.json"
_BUILTIN_SKILL_NAME = "geas"
_GEAS_PROJECT_URL = "https://github.com/Epiphytic/geas"


@dataclass(frozen=True)
class AgentAdapter:
    """One supported agent's executable probe and skill-parent convention."""

    name: str
    executable: str
    parent: Path


@dataclass(frozen=True)
class AgentDetection:
    """The deterministic result of probing one supported agent."""

    adapter: AgentAdapter
    available: bool
    destination: Path


@dataclass(frozen=True)
class LinkReceipt:
    """One managed agent-link result."""

    path: Path
    target: Path
    unchanged: bool


@dataclass(frozen=True)
class _LinkPlan:
    destination: Path
    expected_target: Path
    signature: tuple[object, ...]
    unchanged: bool


@dataclass(frozen=True)
class SkillExportReceipt:
    """The result of installing a portable snapshot and optional agent links."""

    path: Path
    manifest: SkillManifest
    unchanged: bool
    links: tuple[LinkReceipt, ...] = ()
    cleanup_warning: str | None = None


@dataclass(frozen=True)
class SkillRemovalReceipt:
    """The result of detaching links or deleting a managed snapshot."""

    path: Path
    removed_paths: tuple[Path, ...]
    removed_snapshot: bool
    regeneration_command: str


class BuiltinSkillReceipt(StrictModel):
    """Sorted, non-sensitive results from installing the packaged Geas skill."""

    installed: tuple[Path, ...] = ()
    updated: tuple[Path, ...] = ()
    unchanged: tuple[Path, ...] = ()
    linked: tuple[Path, ...] = ()
    skipped: tuple[Path, ...] = ()
    conflicts: tuple[Path, ...] = ()

    @model_validator(mode="after")
    def sorted_paths(self) -> BuiltinSkillReceipt:
        for name in (
            "installed",
            "updated",
            "unchanged",
            "linked",
            "skipped",
            "conflicts",
        ):
            paths = getattr(self, name)
            if paths != tuple(sorted(paths, key=os.fspath)):
                raise ValueError(f"{name} paths must be sorted")
        return self


class BuiltinSkillState(StrictModel):
    """Local ownership evidence for the generic packaged skill snapshot."""

    version: Literal[1]
    skill_name: Literal["geas"]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


_AGENT_ADAPTERS: tuple[AgentAdapter, ...] = (
    AgentAdapter(name="codex", executable="codex", parent=Path(".agents") / "skills"),
    AgentAdapter(name="claude", executable="claude", parent=Path(".claude") / "skills"),
    AgentAdapter(name="opencode", executable="opencode", parent=Path(".agents") / "skills"),
)


def _validated_name(value: str, field_name: str) -> str:
    if not _SKILL_NAME.fullmatch(value):
        raise ValueError(f"{field_name} must be lowercase hyphenated ASCII")
    return value


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validated_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    host_is_private = False
    if hostname:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            host_is_private = hostname.casefold() == "localhost" or hostname.casefold().endswith(
                ".localhost"
            )
        else:
            host_is_private = not address.is_global
    if (
        _contains_control(value)
        or parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or host_is_private
    ):
        raise ValueError("repository_url must be a public HTTPS URL without credentials")
    return value


def _validated_branch(value: str) -> str:
    if _contains_control(value) or not _BRANCH_NAME.fullmatch(value):
        raise ValueError("branch must be a safe Git branch name")
    return value


def _validated_git_id(value: str, field_name: str) -> str:
    if not _GIT_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a 40-character lowercase Git ID")
    return value


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be a relative normalized POSIX path")
    normalized = path.as_posix()
    if normalized in {".", _MANIFEST_NAME} or normalized != value:
        raise ValueError("path must be a relative normalized POSIX path")
    return normalized


class SkillIdentity(StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _validated_name(value, "name")


class OntologyIdentity(StrictModel):
    name: str
    repository_url: str
    branch: str
    commit: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _validated_name(value, "name")

    @field_validator("repository_url")
    @classmethod
    def valid_repository_url(cls, value: str) -> str:
        return _validated_repository_url(value)

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        return _validated_branch(value)

    @field_validator("commit")
    @classmethod
    def valid_commit(cls, value: str) -> str:
        return _validated_git_id(value, "commit")


class GeasIdentity(StrictModel):
    project_url: str
    version: str
    commit: str | None = None

    @field_validator("project_url", "version")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("commit")
    @classmethod
    def valid_commit(cls, value: str | None) -> str | None:
        if value is not None:
            return _validated_git_id(value, "commit")
        return value


class ProjectionIdentity(StrictModel):
    snapshot_id: str
    topic_concept_id: str

    @field_validator("snapshot_id", "topic_concept_id")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


class SkillFile(StrictModel):
    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _normalized_path(value)

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


def snapshot_digest(files: tuple[SkillFile, ...]) -> str:
    """Hash the canonical ordered manifest inventory."""
    payload = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class SkillManifest(StrictModel):
    format_version: Literal[1]
    skill: SkillIdentity
    ontology: OntologyIdentity
    geas: GeasIdentity
    projection: ProjectionIdentity
    files: tuple[SkillFile, ...]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_inventory(self) -> SkillManifest:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("files inventory must be sorted by encoded path")
        if len(paths) != len(set(paths)):
            raise ValueError("files inventory must not contain duplicate paths")
        if self.snapshot_sha256 != snapshot_digest(self.files):
            raise ValueError("snapshot_sha256 does not match files inventory")
        return self


def canonical_manifest_bytes(manifest: SkillManifest) -> bytes:
    """Serialize a manifest as canonical portable UTF-8 JSON."""
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def validate_snapshot(directory: Path) -> SkillManifest:
    """Validate every regular file in a portable skill snapshot against its manifest."""
    root = directory.expanduser()
    _reject_symlink_ancestry(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill snapshot root must be a non-symlink directory")
    manifest_path = root / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("skill snapshot manifest must be a regular file")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = SkillManifest.model_validate_json(manifest_bytes)
    except Exception as error:
        raise ValueError("skill snapshot manifest is invalid") from error
    if manifest_bytes != canonical_manifest_bytes(manifest):
        raise ValueError("skill snapshot manifest must use canonical JSON encoding")

    actual: list[SkillFile] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("skill snapshot must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("skill snapshot must contain regular files only")
        relative = path.relative_to(root).as_posix()
        if relative == _MANIFEST_NAME:
            continue
        actual.append(
            SkillFile(
                path=relative,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    actual_inventory = tuple(sorted(actual, key=lambda item: item.path.encode("utf-8")))
    if tuple(item.path for item in manifest.files) != tuple(item.path for item in actual_inventory):
        raise ValueError("skill snapshot inventory does not match manifest")
    if manifest.files != actual_inventory:
        raise ValueError("skill snapshot file hash does not match manifest")
    return manifest


def _reject_symlink_ancestry(path: Path) -> None:
    """Reject each lexical component before filesystem resolution can follow it."""
    supplied = path if path.is_absolute() else Path.cwd() / path
    supplied = Path(os.fspath(supplied))
    current = Path(supplied.anchor)
    for part in supplied.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("skill snapshot path must not traverse symbolic links")


def detect_agents(*, home: Path, which: Callable[[str], str | None]) -> tuple[AgentDetection, ...]:
    """Probe supported agents in portable fixed order, without changing the filesystem."""
    root = home.expanduser().resolve(strict=False)
    return tuple(
        AgentDetection(
            adapter=adapter,
            available=which(adapter.executable) is not None,
            destination=root / adapter.parent,
        )
        for adapter in _AGENT_ADAPTERS
    )


def install_snapshot(
    files: Mapping[Path, bytes], target: Path, *, force: bool = False
) -> SkillExportReceipt:
    """Atomically install a complete, manifest-verified snapshot at one exact target."""
    destination = _absolute_path(target)
    _reject_symlink_ancestry(destination.parent)
    manifest = _validate_snapshot_files(files)
    existing: SkillManifest | None = None
    if destination.exists() or destination.is_symlink():
        try:
            existing = validate_snapshot(destination)
        except ValueError:
            if not force:
                raise ValueError("skill snapshot target is unmanaged or modified") from None
        if existing is not None and existing == manifest:
            return SkillExportReceipt(path=destination, manifest=manifest, unchanged=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestry(destination.parent)
    candidate = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        _write_snapshot_candidate(files, candidate)
        if validate_snapshot(candidate) != manifest:
            raise ValueError("skill snapshot candidate does not match its validated files")
        if destination.exists() or destination.is_symlink():
            _replace_snapshot(candidate, destination)
        else:
            os.replace(candidate, destination)
        return SkillExportReceipt(path=destination, manifest=manifest, unchanged=False)
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def export_skill(
    files: Mapping[Path, bytes],
    *,
    config_root: Path,
    home: Path,
    repository: Path | None,
    link: bool,
    force: bool,
    which: Callable[[str], str | None],
) -> SkillExportReceipt:
    """Install a managed snapshot in user configuration or a Git worktree."""
    manifest = _manifest_from_files(files)
    if repository is None:
        target = _absolute_path(config_root) / "skills" / manifest.skill.name
        receipt = install_snapshot(files, target, force=force)
        if not link:
            return receipt
        targets = _user_link_targets(
            manifest.skill.name, home=home, detections=detect_agents(home=home, which=which)
        )
        relative = False
    else:
        worktree = _git_worktree(repository)
        target = _repository_snapshot_path(worktree, manifest.skill.name)
        receipt = install_snapshot(files, target, force=force)
        targets = _repository_link_targets(
            worktree,
            snapshot=target,
            skill_name=manifest.skill.name,
            detections=detect_agents(home=home, which=which),
        )
        relative = True
    links = _install_links(
        targets,
        snapshot=receipt.path,
        root=home if repository is None else worktree,
        relative=relative,
        force=force,
    )
    return SkillExportReceipt(
        path=receipt.path,
        manifest=receipt.manifest,
        unchanged=receipt.unchanged and all(item.unchanged for item in links),
        links=links,
        cleanup_warning=receipt.cleanup_warning,
    )


def install_builtin_geas_skill(
    *,
    config_root: Path,
    home: Path,
    which: Callable[[str], str | None],
) -> BuiltinSkillReceipt:
    """Install the packaged generic skill and repair only exact managed links."""
    files = _builtin_skill_snapshot_files()
    root = _absolute_path(config_root)
    snapshot = root / "skills" / _BUILTIN_SKILL_NAME
    state_path = _builtin_skill_state_path(root)
    state = _load_builtin_skill_state(state_path)
    existed = snapshot.exists() or snapshot.is_symlink()
    installed: list[Path] = []
    updated: list[Path] = []
    unchanged: list[Path] = []
    linked: list[Path] = []
    skipped: list[Path] = []
    conflicts: list[Path] = []

    if existed:
        if state is None:
            return _builtin_receipt(conflicts=(snapshot,))
        try:
            existing_manifest = validate_snapshot(snapshot)
        except ValueError:
            return _builtin_receipt(conflicts=(snapshot,))
        if not _builtin_state_matches_manifest(state, existing_manifest):
            return _builtin_receipt(conflicts=(snapshot,))

    _prepare_builtin_skill_state_parent(state_path)
    try:
        snapshot_receipt = install_snapshot(files, snapshot)
    except ValueError:
        conflicts.append(snapshot)
        return _builtin_receipt(conflicts=conflicts)
    _write_builtin_skill_state(
        state_path,
        _builtin_skill_state_from_manifest(snapshot_receipt.manifest),
    )

    if snapshot_receipt.unchanged:
        unchanged.append(snapshot)
    elif existed:
        updated.append(snapshot)
    else:
        installed.append(snapshot)

    detections = detect_agents(home=home, which=which)
    for destination in _user_link_targets(_BUILTIN_SKILL_NAME, home=home, detections=detections):
        try:
            (link_receipt,) = _install_links(
                (destination,),
                snapshot=snapshot,
                root=home,
                relative=False,
                force=False,
            )
        except ValueError:
            conflicts.append(destination)
            continue
        if link_receipt.unchanged:
            skipped.append(destination)
        else:
            linked.append(destination)
    return _builtin_receipt(
        installed=installed,
        updated=updated,
        unchanged=unchanged,
        linked=linked,
        skipped=skipped,
        conflicts=conflicts,
    )


def resolve_skill_snapshot(path: Path, *, force: bool = False) -> tuple[Path, SkillManifest]:
    """Resolve a snapshot directory or its manifest and validate its managed bytes."""
    snapshot = _snapshot_directory(path)
    manifest = _read_existing_manifest(snapshot, force=force)
    if manifest is None:
        raise ValueError("skill snapshot manifest is missing or invalid")
    return snapshot, manifest


def refresh_skill(
    files: Mapping[Path, bytes],
    path: Path,
    *,
    config_root: Path,
    home: Path,
    force: bool,
    which: Callable[[str], str | None],
) -> SkillExportReceipt:
    """Atomically replace one exact managed snapshot and repair its known links."""
    snapshot, existing = resolve_skill_snapshot(path, force=force)
    candidate = _validate_snapshot_files(files)
    if candidate.skill.name != existing.skill.name:
        raise ValueError("skill update cannot change the managed skill name")
    repository = _containing_worktree(snapshot)
    _assert_managed_snapshot_scope(
        snapshot,
        candidate,
        repository=repository,
        config_root=config_root,
    )
    snapshot_signature = _snapshot_signature(snapshot)
    detections = detect_agents(home=home, which=which)
    if repository is None:
        targets = _user_link_targets(candidate.skill.name, home=home, detections=detections)
        root = home
        relative = False
    else:
        targets = _repository_link_targets(
            repository,
            snapshot=snapshot,
            skill_name=candidate.skill.name,
            detections=detections,
        )
        root = repository
        relative = True
    plans = _plan_links(
        targets,
        snapshot=snapshot,
        root=root,
        relative=relative,
        force=force,
    )
    receipt, links = _replace_snapshot_and_links(
        files,
        snapshot=snapshot,
        manifest=candidate,
        snapshot_signature=snapshot_signature,
        plans=plans,
        root=root,
    )
    return SkillExportReceipt(
        path=receipt.path,
        manifest=receipt.manifest,
        unchanged=receipt.unchanged and all(item.unchanged for item in links),
        links=links,
        cleanup_warning=receipt.cleanup_warning,
    )


def unlink_skill(
    path: Path,
    *,
    home: Path,
    force: bool = False,
    config_root: Path | None = None,
) -> SkillRemovalReceipt:
    """Remove only exact managed agent links, leaving the snapshot untouched."""
    snapshot, manifest = resolve_skill_snapshot(path, force=force)
    repository = _containing_worktree(snapshot)
    if config_root is not None:
        _assert_managed_snapshot_scope(
            snapshot,
            manifest,
            repository=repository,
            config_root=config_root,
        )
    if repository is None:
        targets = _user_link_targets(
            manifest.skill.name,
            home=home,
            detections=detect_agents(home=home, which=lambda _executable: "detached"),
        )
    else:
        targets = _repository_link_targets(
            repository,
            snapshot=snapshot,
            skill_name=manifest.skill.name,
            detections=detect_agents(home=home, which=lambda _executable: "detached"),
        )
    removed = _remove_exact_links(
        targets,
        snapshot=snapshot,
        root=home if repository is None else repository,
        relative=repository is not None,
    )
    return SkillRemovalReceipt(
        path=snapshot,
        removed_paths=removed,
        removed_snapshot=False,
        regeneration_command=_regeneration_command(manifest),
    )


def remove_skill(
    path: Path,
    *,
    home: Path,
    force: bool = False,
    config_root: Path | None = None,
) -> SkillRemovalReceipt:
    """Detach managed links and delete only the exact managed snapshot directory."""
    detached = unlink_skill(path, home=home, force=force, config_root=config_root)
    _remove_directory(detached.path)
    return SkillRemovalReceipt(
        path=detached.path,
        removed_paths=detached.removed_paths,
        removed_snapshot=True,
        regeneration_command=detached.regeneration_command,
    )


def _assert_managed_snapshot_scope(
    snapshot: Path,
    manifest: SkillManifest,
    *,
    repository: Path | None,
    config_root: Path,
) -> None:
    if repository is None:
        expected = _absolute_path(config_root) / "skills" / manifest.skill.name
        if snapshot != expected:
            raise ValueError("user skill snapshot is outside the selected Geas config root")
        return
    relative = snapshot.relative_to(repository)
    expected = {
        Path(".agents") / "skills" / manifest.skill.name,
        Path(".geas") / "skills" / manifest.skill.name,
    }
    if relative not in expected:
        raise ValueError("repository skill snapshot is outside a managed skill path")


def _write_snapshot_candidate(files: Mapping[Path, bytes], candidate: Path) -> None:
    for relative, content in files.items():
        value = relative.as_posix()
        normalized = value if value == _MANIFEST_NAME else _normalized_path(value)
        destination = candidate / normalized
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _validate_snapshot_files(files: Mapping[Path, bytes]) -> SkillManifest:
    """Validate a complete portable snapshot mapping without creating staging files."""
    manifest = _manifest_from_files(files)
    actual: list[SkillFile] = []
    for path, content in files.items():
        if not isinstance(path, Path) or not isinstance(content, bytes):
            raise ValueError("skill snapshot files must map paths to bytes")
        value = path.as_posix()
        if value == _MANIFEST_NAME:
            continue
        normalized = _normalized_path(value)
        actual.append(SkillFile(path=normalized, sha256=hashlib.sha256(content).hexdigest()))
    inventory = tuple(sorted(actual, key=lambda item: item.path.encode("utf-8")))
    if manifest.files != inventory:
        raise ValueError("skill snapshot files do not match manifest inventory")
    return manifest


def _builtin_skill_snapshot_files() -> dict[Path, bytes]:
    source_files = _builtin_skill_source_files()
    inventory = tuple(
        sorted(
            (
                SkillFile(
                    path=path.as_posix(),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
                for path, content in source_files.items()
            ),
            key=lambda item: item.path.encode("utf-8"),
        )
    )
    manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name=_BUILTIN_SKILL_NAME),
        # The generic skill is not ontology knowledge.  These schema-required
        # identifiers describe its fixed Geas project locator only.
        ontology=OntologyIdentity(
            name="geas",
            repository_url=f"{_GEAS_PROJECT_URL}.git",
            branch="main",
            commit="0" * 40,
        ),
        geas=GeasIdentity(
            project_url=_GEAS_PROJECT_URL,
            version=_installed_geas_version(),
        ),
        projection=ProjectionIdentity(
            snapshot_id="builtin:geas",
            topic_concept_id="builtin:geas",
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    return {**source_files, Path(_MANIFEST_NAME): canonical_manifest_bytes(manifest)}


def _builtin_skill_state_path(config_root: Path) -> Path:
    return config_root / "state" / "builtin-skills" / f"{_BUILTIN_SKILL_NAME}.json"


def _load_builtin_skill_state(path: Path) -> BuiltinSkillState | None:
    _prepare_builtin_skill_state_parent(path, create=False)
    if path.is_symlink():
        raise ValueError("builtin skill state must not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("builtin skill state must be a regular file")
    try:
        return BuiltinSkillState.model_validate_json(path.read_bytes())
    except Exception as error:
        raise ValueError("builtin skill state is invalid") from error


def _write_builtin_skill_state(path: Path, state: BuiltinSkillState) -> None:
    _prepare_builtin_skill_state_parent(path)
    if path.is_symlink():
        raise ValueError("builtin skill state must not be a symbolic link")
    payload = (
        json.dumps(
            state.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        dir=path.parent,
        delete=False,
    ) as candidate:
        candidate.write(payload)
        candidate_path = Path(candidate.name)
    try:
        if path.is_symlink():
            raise ValueError("builtin skill state must not be a symbolic link")
        os.replace(candidate_path, path)
    finally:
        candidate_path.unlink(missing_ok=True)


def _prepare_builtin_skill_state_parent(path: Path, *, create: bool = True) -> None:
    """Reject unsafe state ancestors and create the required state directory first."""
    state_root = path.parent.parent
    try:
        _reject_symlink_ancestry(path.parent)
    except ValueError as error:
        raise ValueError("builtin skill state path must not traverse symbolic links") from error
    for directory in (state_root, path.parent):
        if directory.is_symlink():
            raise ValueError("builtin skill state path must not traverse symbolic links")
        if directory.exists() and not directory.is_dir():
            raise ValueError("builtin skill state parent must be a directory")
    if not create:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _reject_symlink_ancestry(path.parent)
    except ValueError as error:
        raise ValueError("builtin skill state path must not traverse symbolic links") from error
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("builtin skill state parent must be a non-symlink directory")


def _builtin_skill_state_from_manifest(manifest: SkillManifest) -> BuiltinSkillState:
    return BuiltinSkillState(
        version=1,
        skill_name=_BUILTIN_SKILL_NAME,
        snapshot_sha256=manifest.snapshot_sha256,
        manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
    )


def _builtin_state_matches_manifest(state: BuiltinSkillState, manifest: SkillManifest) -> bool:
    return (
        manifest.skill.name == _BUILTIN_SKILL_NAME
        and state.snapshot_sha256 == manifest.snapshot_sha256
        and state.manifest_sha256 == hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    )


def _builtin_skill_source_files() -> dict[Path, bytes]:
    """Read only regular, non-symlinked files from the packaged generic skill."""
    root = Path(package_files("research_agent").joinpath("builtin_skills", "geas"))
    if root.is_symlink() or not root.is_dir():
        raise ValueError("packaged Geas skill directory is missing or unsafe")
    result: dict[Path, bytes] = {}
    for source in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if source.is_symlink():
            raise ValueError("packaged Geas skill must not contain symbolic links")
        if source.is_dir():
            continue
        if not source.is_file():
            raise ValueError("packaged Geas skill must contain regular files only")
        result[source.relative_to(root)] = source.read_bytes()
    if Path("SKILL.md") not in result:
        raise ValueError("packaged Geas skill is missing SKILL.md")
    return result


def _installed_geas_version() -> str:
    try:
        return version("geas")
    except PackageNotFoundError:
        return "0.1.0"


def _builtin_receipt(
    *,
    installed: Iterable[Path] = (),
    updated: Iterable[Path] = (),
    unchanged: Iterable[Path] = (),
    linked: Iterable[Path] = (),
    skipped: Iterable[Path] = (),
    conflicts: Iterable[Path] = (),
) -> BuiltinSkillReceipt:
    return BuiltinSkillReceipt(
        installed=tuple(sorted(installed, key=os.fspath)),
        updated=tuple(sorted(updated, key=os.fspath)),
        unchanged=tuple(sorted(unchanged, key=os.fspath)),
        linked=tuple(sorted(linked, key=os.fspath)),
        skipped=tuple(sorted(skipped, key=os.fspath)),
        conflicts=tuple(sorted(conflicts, key=os.fspath)),
    )


def _manifest_from_files(files: Mapping[Path, bytes]) -> SkillManifest:
    raw = files.get(Path(_MANIFEST_NAME))
    if raw is None:
        raise ValueError("skill snapshot files must include geas-skill.json")
    try:
        manifest = SkillManifest.model_validate_json(raw)
    except Exception as error:
        raise ValueError("skill snapshot manifest is invalid") from error
    if raw != canonical_manifest_bytes(manifest):
        raise ValueError("skill snapshot manifest must use canonical JSON encoding")
    return manifest


def _read_existing_manifest(path: Path, *, force: bool) -> SkillManifest | None:
    if path.is_symlink() or not path.is_dir():
        if force:
            return None
        raise ValueError("skill snapshot target is unmanaged or modified")
    try:
        return validate_snapshot(path)
    except ValueError:
        if not force:
            raise ValueError("skill snapshot target is unmanaged or modified") from None
        manifest_path = path / _MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        try:
            return SkillManifest.model_validate_json(manifest_path.read_bytes())
        except Exception:
            return None


def _replace_snapshot(candidate: Path, destination: Path) -> None:
    backup = destination.parent / f".{destination.name}.backup"
    if backup.exists() or backup.is_symlink():
        raise ValueError("skill snapshot backup target already exists")
    os.replace(destination, backup)
    try:
        os.replace(candidate, destination)
    except Exception:
        os.replace(backup, destination)
        raise
    _remove_exact_target(backup)


def _git_worktree(repository: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("repository must be a Git worktree")
    return Path(completed.stdout.strip()).resolve()


def _repository_snapshot_path(worktree: Path, skill_name: str) -> Path:
    preferred = worktree / ".agents" / "skills" / skill_name
    if not _git_ignored(worktree, preferred):
        return preferred
    fallback = worktree / ".geas" / "skills" / skill_name
    if _git_ignored(worktree, fallback):
        raise ValueError("both repository skill paths are ignored by Git")
    return fallback


def _git_ignored(worktree: Path, path: Path) -> bool:
    relative = path.relative_to(worktree).as_posix()
    completed = subprocess.run(
        ["git", "-C", os.fspath(worktree), "check-ignore", "-q", "--", relative],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise ValueError("could not evaluate repository ignore rules")


def _user_link_targets(
    skill_name: str, *, home: Path, detections: tuple[AgentDetection, ...]
) -> tuple[Path, ...]:
    return _deduplicated_paths(
        detection.destination / skill_name for detection in detections if detection.available
    )


def _repository_link_targets(
    worktree: Path,
    *,
    snapshot: Path,
    skill_name: str,
    detections: tuple[AgentDetection, ...],
) -> tuple[Path, ...]:
    targets: list[Path] = []
    for detection in detections:
        if not detection.available:
            continue
        destination = worktree / detection.adapter.parent / skill_name
        if destination != snapshot:
            targets.append(destination)
    return _deduplicated_paths(targets)


def _deduplicated_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=os.fspath))


def _install_links(
    targets: tuple[Path, ...],
    *,
    snapshot: Path,
    root: Path,
    relative: bool,
    force: bool,
) -> tuple[LinkReceipt, ...]:
    receipts: list[LinkReceipt] = []
    for destination in targets:
        _confined_link_parent(destination.parent, root)
        expected_target = _expected_link_target(destination, snapshot=snapshot, relative=relative)
        if destination.is_symlink() and _link_points_to(destination, expected_target):
            receipts.append(LinkReceipt(path=destination, target=snapshot, unchanged=True))
            continue
        if destination.exists() or destination.is_symlink():
            if not force:
                raise ValueError(f"skill link conflict at {destination}")
            _remove_exact_target(destination)
        destination.symlink_to(expected_target, target_is_directory=True)
        receipts.append(LinkReceipt(path=destination, target=snapshot, unchanged=False))
    return tuple(sorted(receipts, key=lambda item: os.fspath(item.path)))


def _plan_links(
    targets: tuple[Path, ...],
    *,
    snapshot: Path,
    root: Path,
    relative: bool,
    force: bool,
) -> tuple[_LinkPlan, ...]:
    plans: list[_LinkPlan] = []
    for destination in targets:
        _confined_link_parent(destination.parent, root, create=False)
        expected = _expected_link_target(destination, snapshot=snapshot, relative=relative)
        unchanged = destination.is_symlink() and _link_points_to(destination, expected)
        exists = destination.exists() or destination.is_symlink()
        if exists and not unchanged and not force:
            raise ValueError(f"skill link conflict at {destination}")
        plans.append(
            _LinkPlan(
                destination=destination,
                expected_target=expected,
                signature=_path_signature(destination),
                unchanged=unchanged,
            )
        )
    return tuple(plans)


def _replace_snapshot_and_links(
    files: Mapping[Path, bytes],
    *,
    snapshot: Path,
    manifest: SkillManifest,
    snapshot_signature: str,
    plans: tuple[_LinkPlan, ...],
    root: Path,
) -> tuple[SkillExportReceipt, tuple[LinkReceipt, ...]]:
    """Commit one visible snapshot/link state or restore every prior target."""
    try:
        snapshot_unchanged = validate_snapshot(snapshot) == manifest
    except ValueError:
        snapshot_unchanged = False
    if snapshot_unchanged and all(plan.unchanged for plan in plans):
        return (
            SkillExportReceipt(path=snapshot, manifest=manifest, unchanged=True),
            tuple(
                LinkReceipt(path=plan.destination, target=snapshot, unchanged=True)
                for plan in plans
            ),
        )

    _reject_symlink_ancestry(snapshot.parent)
    transaction = Path(
        tempfile.mkdtemp(
            prefix=f".{snapshot.name}.geas-transaction-",
            dir=snapshot.parent,
        )
    )
    snapshot_backup = transaction / "snapshot"
    candidate = transaction / "candidate"
    changed_links: list[tuple[_LinkPlan, Path]] = []
    receipts: list[LinkReceipt] = []
    snapshot_moved = False
    committed = False
    try:
        if not snapshot_unchanged:
            candidate.mkdir()
            _write_snapshot_candidate(files, candidate)
            if validate_snapshot(candidate) != manifest:
                raise ValueError("skill snapshot candidate does not match its validated files")
        if _snapshot_signature(snapshot) != snapshot_signature:
            raise ValueError("skill snapshot changed during update")
        if not snapshot_unchanged:
            os.replace(snapshot, snapshot_backup)
            snapshot_moved = True
            os.replace(candidate, snapshot)
        for index, plan in enumerate(plans):
            _confined_link_parent(plan.destination.parent, root)
            if _path_signature(plan.destination) != plan.signature:
                raise ValueError(f"skill link changed during update at {plan.destination}")
            if plan.unchanged:
                receipts.append(LinkReceipt(path=plan.destination, target=snapshot, unchanged=True))
                continue
            backup = transaction / f"link-{index}"
            if plan.destination.exists() or plan.destination.is_symlink():
                os.replace(plan.destination, backup)
            changed_links.append((plan, backup))
            plan.destination.symlink_to(plan.expected_target, target_is_directory=True)
            receipts.append(LinkReceipt(path=plan.destination, target=snapshot, unchanged=False))
        # Commit point: every desired visible snapshot and link now exists.
        committed = True
    except Exception:
        for plan, backup in reversed(changed_links):
            if plan.destination.exists() or plan.destination.is_symlink():
                _remove_exact_target(plan.destination)
            if backup.exists() or backup.is_symlink():
                os.replace(backup, plan.destination)
        if snapshot_moved:
            if snapshot.exists() or snapshot.is_symlink():
                _remove_exact_target(snapshot)
            os.replace(snapshot_backup, snapshot)
        _discard_transaction(transaction)
        raise

    assert committed
    cleanup_warning = None
    if not _discard_transaction(transaction):
        cleanup_warning = "skill transaction cleanup retained"
    return (
        SkillExportReceipt(
            path=snapshot,
            manifest=manifest,
            unchanged=snapshot_unchanged,
            cleanup_warning=cleanup_warning,
        ),
        tuple(sorted(receipts, key=lambda item: os.fspath(item.path))),
    )


def _discard_transaction(path: Path) -> bool:
    try:
        shutil.rmtree(path)
    except Exception:
        return False
    return True


def _path_signature(path: Path) -> tuple[object, ...]:
    if path.is_symlink():
        stat = path.lstat()
        return ("symlink", stat.st_dev, stat.st_ino, os.readlink(path))
    if path.exists():
        stat = path.stat()
        kind = "directory" if path.is_dir() else "file" if path.is_file() else "other"
        return (kind, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return ("absent",)


def _snapshot_signature(snapshot: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(snapshot.rglob("*"), key=lambda item: item.relative_to(snapshot).as_posix()):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError("skill snapshot changed during update")
        relative = path.relative_to(snapshot).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_file():
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        else:
            digest.update(b"directory")
    return digest.hexdigest()


def _expected_link_target(destination: Path, *, snapshot: Path, relative: bool) -> Path:
    if relative:
        return Path(os.path.relpath(snapshot, start=destination.parent))
    return snapshot


def _confined_link_parent(parent: Path, root: Path, *, create: bool = True) -> None:
    root_resolved = root.expanduser().resolve(strict=False)
    lexical_parent = parent.expanduser()
    if not lexical_parent.is_absolute():
        lexical_parent = Path.cwd() / lexical_parent
    try:
        relative = lexical_parent.relative_to(root_resolved)
    except ValueError as error:
        raise ValueError("skill link path escapes its managed root") from error
    lexical = root_resolved
    for part in relative.parts:
        lexical /= part
        if lexical.is_symlink():
            raise ValueError("skill link path must not traverse symbolic links")
    parent_resolved = lexical_parent.resolve(strict=False)
    try:
        parent_resolved.relative_to(root_resolved)
    except ValueError as error:
        raise ValueError("skill link path escapes its managed root") from error
    if create:
        parent_resolved.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestry(parent_resolved)


def _link_points_to(link: Path, expected_target: Path) -> bool:
    """Require the managed link's direct target spelling, without resolving it."""
    try:
        return link.readlink() == expected_target
    except OSError:
        return False


def _snapshot_directory(path: Path) -> Path:
    supplied = _absolute_path(path)
    _reject_symlink_ancestry(supplied)
    if supplied.name == _MANIFEST_NAME:
        return supplied.parent
    return supplied


def _containing_worktree(snapshot: Path) -> Path | None:
    try:
        worktree = _git_worktree(snapshot)
    except ValueError:
        return None
    try:
        snapshot.resolve().relative_to(worktree)
    except ValueError:
        return None
    return worktree


def _remove_exact_links(
    targets: tuple[Path, ...],
    *,
    snapshot: Path,
    root: Path,
    relative: bool,
) -> tuple[Path, ...]:
    removed: list[Path] = []
    for target in targets:
        _confined_link_parent(target.parent, root, create=False)
        expected_target = _expected_link_target(target, snapshot=snapshot, relative=relative)
        if target.is_symlink() and _link_points_to(target, expected_target):
            target.unlink()
            removed.append(target)
    return tuple(sorted(removed, key=os.fspath))


def _remove_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("managed skill target must be a non-symlink directory")
    shutil.rmtree(path)


def _remove_exact_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise ValueError("skill link conflict target is not removable")


def _regeneration_command(manifest: SkillManifest) -> str:
    return f"geas skill-export {manifest.ontology.name} --name {manifest.skill.name}"


def _absolute_path(path: Path) -> Path:
    """Return an absolute lexical path without following a possible target symlink."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))
