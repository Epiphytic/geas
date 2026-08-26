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
class SkillExportReceipt:
    """The result of installing a portable snapshot and optional agent links."""

    path: Path
    manifest: SkillManifest
    unchanged: bool
    links: tuple[LinkReceipt, ...] = ()


@dataclass(frozen=True)
class SkillRemovalReceipt:
    """The result of detaching links or deleting a managed snapshot."""

    path: Path
    removed_paths: tuple[Path, ...]
    removed_snapshot: bool
    regeneration_command: str


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


def detect_agents(
    *, home: Path, which: Callable[[str], str | None]
) -> tuple[AgentDetection, ...]:
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
        target = config_root.expanduser().resolve(strict=False) / "skills" / manifest.skill.name
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
            worktree, snapshot=target, skill_name=manifest.skill.name, detections=detect_agents(
                home=home, which=which
            )
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
    )


def unlink_skill(path: Path, *, home: Path, force: bool = False) -> SkillRemovalReceipt:
    """Remove only exact managed agent links, leaving the snapshot untouched."""
    snapshot = _snapshot_directory(path)
    manifest = _read_existing_manifest(snapshot, force=force)
    if manifest is None:
        raise ValueError("skill snapshot must be a directory")
    repository = _containing_worktree(snapshot)
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


def remove_skill(path: Path, *, home: Path, force: bool = False) -> SkillRemovalReceipt:
    """Detach managed links and delete only the exact managed snapshot directory."""
    detached = unlink_skill(path, home=home, force=force)
    _remove_directory(detached.path)
    return SkillRemovalReceipt(
        path=detached.path,
        removed_paths=detached.removed_paths,
        removed_snapshot=True,
        regeneration_command=detached.regeneration_command,
    )


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
