"""Strict, confined ontology catalogs declared by repository ``geas.yaml`` files."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from yaml.events import (
    MappingEndEvent,
    MappingStartEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from research_agent.capabilities import DelegationManifest
from research_agent.git_environment import confined_git_environment
from research_agent.models import StrictModel, canonical_json

_CATALOG_NAME = "geas.yaml"
_ONTOLOGY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}")
_SCP_REMOTE = re.compile(
    r"git@(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):"
    r"(?P<path>[^/?#][^?#]*)"
)
_REFERENCE_KEYS = frozenset(
    {
        "artifact_manifest",
        "artifacts_manifest",
        "build",
        "build_path",
        "bundle",
        "bundle_path",
        "library",
        "library_path",
        "manifest_path",
        "source_card",
        "source_card_path",
        "source_cards",
        "source_cards_path",
        "threat_index",
        "threat_index_path",
    }
)
_PATH_COLLECTION_KEYS = frozenset({"artifacts", "sources"})
_MAX_YAML_FILE_BYTES = 1024 * 1024
_MAX_YAML_DEPTH = 64
_MAX_YAML_NODES = 100_000


def validate_ontology_name(value: str) -> str:
    """Accept only the shared safe ontology identifier grammar."""
    if not isinstance(value, str) or not _ONTOLOGY_NAME.fullmatch(value):
        raise ValueError("ontology name is invalid")
    return value


def validate_bundle_sha256(value: str) -> str:
    """Accept only a canonical lowercase SHA-256 bundle identifier."""
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("bundle SHA-256 is invalid")
    return value


def normalized_repository_identity(value: str) -> str:
    """Canonicalize every accepted remote syntax without expanding authority."""
    raw = _validate_remote_url(value)
    scp = _SCP_REMOTE.fullmatch(raw)
    if scp is not None:
        host = scp.group("host").lower()
        path = scp.group("path").rstrip("/").removesuffix(".git")
        if host == "github.com":
            return f"https://github.com/{path}"
        # SCP syntax is relative to the remote user's home; an explicit SSH
        # URL beginning with '/' names an absolute remote path and therefore
        # has different authority.
        return f"ssh://git@{host}/~/{path}"

    parsed = urlsplit(raw)
    assert parsed.hostname is not None  # Established by _validate_remote_url.
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/").removesuffix(".git")
    port_number = parsed.port
    if (
        host == "github.com"
        and port_number is None
        and (parsed.scheme == "https" or parsed.username == "git")
    ):
        return f"https://github.com/{path.lstrip('/')}"
    rendered_host = f"[{host}]" if ":" in host else host
    port = f":{port_number}" if port_number is not None else ""
    username = "git@" if parsed.username == "git" else ""
    return f"{parsed.scheme.lower()}://{username}{rendered_host}{port}{path}"


def _validate_remote_url(value: str) -> str:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("ontology Git URL contains control characters or is empty")
    scp = _SCP_REMOTE.fullmatch(value)
    if scp is not None:
        _validate_url_path(scp.group("path"))
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"}:
        raise ValueError("ontology Git URL uses an unsupported remote transport")
    if parsed.password is not None or (
        parsed.username is not None and not (parsed.scheme == "ssh" and parsed.username == "git")
    ):
        raise ValueError("ontology Git URLs cannot embed credentials")
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("ontology Git URL must be a credential-free remote URL")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("ontology Git URL port is invalid") from error
    _validate_url_path(parsed.path)
    return value


def _validate_url_path(value: str) -> None:
    decoded = unquote(value)
    if not decoded or any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ValueError("ontology Git remote path is invalid")
    pure = PurePosixPath(decoded)
    parts = pure.parts
    if any(part in {"", ".", ".."} for part in parts) or pure.as_posix() != decoded:
        raise ValueError("ontology Git remote path contains traversal")


def _relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{label} must be a relative path")
    raw = str(value)
    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(f"{label} contains a control character or is empty")
    if "\\" in raw:
        raise ValueError(f"{label} must use normalized POSIX separators")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ValueError(f"{label} must be relative")
    if any(part in {"", ".", ".."} for part in pure.parts) or pure.as_posix() != raw:
        raise ValueError(f"{label} must be normalized and cannot contain parent paths")
    return Path(raw)


class CatalogFile(StrictModel):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("path", mode="before")
    @classmethod
    def path_is_confined(cls, value: object) -> Path:
        return _relative_path(value, label="catalog file path")

    @field_validator("sha256", mode="before")
    @classmethod
    def sha256_is_canonical(cls, value: str) -> str:
        return validate_bundle_sha256(value)


class CatalogOntology(StrictModel):
    name: str
    description: str = Field(min_length=1)
    path: Path
    files: tuple[CatalogFile, ...]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name", mode="before")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        return validate_ontology_name(value)

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def bundle_sha256_is_canonical(cls, value: str) -> str:
        return validate_bundle_sha256(value)

    @field_validator("path", mode="before")
    @classmethod
    def path_is_confined(cls, value: object) -> Path:
        return _relative_path(value, label="ontology path")

    @model_validator(mode="after")
    def inventory_is_canonical(self) -> CatalogOntology:
        if not self.files:
            raise ValueError("ontology file inventory must not be empty")
        paths = tuple(item.path.as_posix() for item in self.files)
        if len(paths) != len(set(paths)):
            raise ValueError("ontology file inventory paths must be unique")
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("ontology file inventory must be in ascending UTF-8 path order")
        return self


class RepositoryCatalog(StrictModel):
    version: Literal[1] = 1
    ontologies: tuple[CatalogOntology, ...]
    delegations: CatalogFile | None = None

    @model_validator(mode="after")
    def ontologies_are_unique(self) -> RepositoryCatalog:
        names = tuple(item.name for item in self.ontologies)
        if len(names) != len(set(names)):
            raise ValueError("ontology names must be unique")
        if (
            self.delegations is not None
            and self.delegations.path.as_posix() != "geas-delegations.yaml"
        ):
            raise ValueError("delegations path must be geas-delegations.yaml")
        return self


class VerifiedCatalogOntology(StrictModel):
    """A catalog entry whose declared bytes and portable bundle identity match."""

    name: str
    description: str
    catalog_path: Path
    ontology_path: Path
    workspace_path: PurePosixPath
    files: tuple[CatalogFile, ...]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dirty: bool = False

    @field_validator("workspace_path", mode="before")
    @classmethod
    def workspace_path_is_portable(cls, value: object) -> PurePosixPath:
        return _portable_relative_path(
            value,
            label="workspace ontology path",
            allow_root=True,
        )

    @field_validator("name", mode="before")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        return validate_ontology_name(value)

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def bundle_sha256_is_canonical(cls, value: str) -> str:
        return validate_bundle_sha256(value)


class ResolvedRepositoryCatalog(StrictModel):
    """Verified effective entries from the direct Git-root-to-cwd catalog chain."""

    repository_root: Path | None = None
    discovery_start: Path | None = None
    repository_identity: str | None = None
    identity_kind: Literal["remote", "machine_local"] | None = None
    active_ref: str | None = None
    commit: str | None = None
    catalog_paths: tuple[Path, ...] = ()
    ontologies: tuple[VerifiedCatalogOntology, ...] = ()
    delegation_manifest_path: Path | None = None
    delegation_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    delegation_manifest_size_bytes: int | None = Field(default=None, ge=0)
    delegation_manifest: DelegationManifest | None = None

    @model_validator(mode="after")
    def discovery_scope_is_confined(self) -> ResolvedRepositoryCatalog:
        metadata = (
            self.delegation_manifest_path,
            self.delegation_manifest_sha256,
            self.delegation_manifest_size_bytes,
            self.delegation_manifest,
        )
        if any(item is not None for item in metadata) and not all(
            item is not None for item in metadata
        ):
            raise ValueError("verified delegation metadata must be complete")
        if self.delegation_manifest is not None and (
            self.repository_identity is None or self.commit is None
        ):
            raise ValueError(
                "verified delegation metadata requires repository identity and catalog commit"
            )
        if self.repository_root is None:
            if self.discovery_start is not None or any(
                item is not None for item in metadata
            ):
                raise ValueError("catalog discovery start requires a repository root")
            return self
        if self.discovery_start is None:
            raise ValueError("repository catalog must record its discovery start")
        if not self.discovery_start.is_relative_to(self.repository_root):
            raise ValueError("catalog discovery start escapes repository root")
        if (
            self.delegation_manifest_path is not None
            and not self.delegation_manifest_path.is_relative_to(self.repository_root)
        ):
            raise ValueError("delegation manifest escapes repository root")
        return self

    def by_name(self, name: str) -> VerifiedCatalogOntology:
        for ontology in self.ontologies:
            if ontology.name == name:
                return ontology
        raise ValueError(f"unknown catalog ontology: {name}")


def ontology_bundle_sha256(entry: CatalogOntology) -> str:
    payload = {
        "description": entry.description,
        "files": [item.model_dump(mode="json") for item in entry.files],
        "format": "geas-ontology-bundle/1",
        "name": entry.name,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def load_catalog(path: Path) -> RepositoryCatalog:
    """Load one strict catalog without resolving or trusting any ontology input."""
    catalog_path = _catalog_path(path)
    return _parse_catalog_value(
        _load_bounded_yaml(catalog_path, label=f"catalog: {catalog_path}")
    )


def _parse_catalog_bytes(encoded: bytes, *, label: str) -> RepositoryCatalog:
    return _parse_catalog_value(_load_bounded_yaml_bytes(encoded, label=label))


def _parse_catalog_value(value: object) -> RepositoryCatalog:
    try:
        return RepositoryCatalog.model_validate(value)
    except ValidationError as error:
        raise ValueError(str(error)) from error


def load_delegation_manifest(
    catalog_path: Path,
    declaration: CatalogFile | None,
) -> DelegationManifest:
    """Verify catalog-pinned bytes before parsing one strict delegation manifest."""
    if declaration is None:
        raise ValueError("catalog does not declare a delegation manifest")
    if declaration.path.as_posix() != "geas-delegations.yaml":
        raise ValueError("delegations path must be geas-delegations.yaml")
    catalog = _catalog_path(catalog_path)
    manifest_path = catalog.parent / declaration.path
    _reject_symlink_ancestry(manifest_path)
    if not manifest_path.exists():
        raise ValueError("delegation manifest is missing")
    if not manifest_path.is_file():
        raise ValueError("delegation manifest must be a regular file")
    encoded = manifest_path.read_bytes()
    if len(encoded) != declaration.size_bytes:
        raise ValueError("delegation manifest size mismatch")
    if hashlib.sha256(encoded).hexdigest() != declaration.sha256:
        raise ValueError("delegation manifest sha256 mismatch")
    return _parse_delegation_manifest_bytes(
        encoded,
        label=f"delegation manifest: {manifest_path}",
    )


def _parse_delegation_manifest_bytes(encoded: bytes, *, label: str) -> DelegationManifest:
    return _parse_delegation_manifest_value(_load_bounded_yaml_bytes(encoded, label=label))


def _parse_delegation_manifest_value(value: object) -> DelegationManifest:
    try:
        return DelegationManifest.model_validate(value)
    except ValidationError as error:
        raise ValueError(str(error)) from error


def verify_catalog(path: Path, *, names: Sequence[str] = ()) -> tuple[VerifiedCatalogOntology, ...]:
    """Verify declared files, closed-world YAML inputs, and portable digests."""
    catalog_path = _catalog_path(path)
    catalog = load_catalog(catalog_path)
    if catalog.delegations is not None:
        load_delegation_manifest(catalog_path, catalog.delegations)
    selected = _selected_entries(catalog, names)
    workspace = _catalog_workspace(catalog_path)
    return tuple(_verify_entry(catalog_path, entry, workspace=workspace) for entry in selected)


def refresh_catalog(path: Path, *, names: Sequence[str] = ()) -> RepositoryCatalog:
    """Atomically refresh hashes for already declared inventory files only."""
    catalog_path = _catalog_path(path)
    catalog = load_catalog(catalog_path)
    workspace = _catalog_workspace(catalog_path)
    selected_names = {entry.name for entry in _selected_entries(catalog, names)}
    refreshed: list[CatalogOntology] = []
    for entry in catalog.ontologies:
        if entry.name not in selected_names:
            refreshed.append(entry)
            continue
        ontology_path = _ontology_directory(catalog_path, entry)
        files = tuple(
            CatalogFile(
                path=item.path,
                sha256=hashlib.sha256(
                    _regular_file(ontology_path, item.path).read_bytes()
                ).hexdigest(),
                size_bytes=_regular_file(ontology_path, item.path).stat().st_size,
            )
            for item in entry.files
        )
        candidate = entry.model_copy(update={"files": files, "bundle_sha256": "0" * 64})
        candidate = candidate.model_copy(
            update={"bundle_sha256": ontology_bundle_sha256(candidate)}
        )
        _verify_transitive_inputs(
            ontology_path,
            candidate.files,
            workspace=workspace,
            workspace_path=_workspace_ontology_path(ontology_path, workspace),
        )
        refreshed.append(candidate)
    delegations = catalog.delegations
    if delegations is not None:
        manifest_path = catalog_path.parent / delegations.path
        _reject_symlink_ancestry(manifest_path)
        if not manifest_path.is_file():
            raise ValueError("delegation manifest is missing or not a regular file")
        encoded = manifest_path.read_bytes()
        delegations = CatalogFile(
            path=delegations.path,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
        )
        load_delegation_manifest(
            catalog_path,
            delegations,
        )
    result = RepositoryCatalog(
        version=1,
        ontologies=tuple(refreshed),
        delegations=delegations,
    )
    _atomic_yaml_replace(catalog_path, result)
    return result


def discover_catalogs(start: Path) -> tuple[Path, ...]:
    """Return present ``geas.yaml`` files only on Git root-to-start ancestors."""
    worktree = _git_worktree(start)
    if worktree is None:
        return ()
    current = _start_directory(start)
    try:
        relative = current.relative_to(worktree)
    except ValueError as error:  # pragma: no cover - Git itself should prevent this.
        raise ValueError("catalog start escapes Git worktree") from error
    directories = (
        worktree,
        *(
            worktree.joinpath(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ),
    )
    return tuple(
        candidate
        for directory in directories
        if (candidate := directory / _CATALOG_NAME).exists() or candidate.is_symlink()
    )


def resolve_repository_catalog(
    start: Path,
    *,
    verified_commit: str | None = None,
) -> ResolvedRepositoryCatalog:
    """Merge catalogs whose worktree and index bytes match one named commit."""
    worktree = _git_worktree(start)
    if worktree is None:
        return ResolvedRepositoryCatalog()
    discovery_start = _start_directory(start)
    requested_commit = verified_commit or "HEAD"
    commit = _git(worktree, "rev-parse", "--verify", f"{requested_commit}^{{commit}}")
    if not _GIT_OBJECT_ID.fullmatch(commit):
        raise ValueError("verified catalog commit is not a full Git object ID")
    bound_catalogs: list[tuple[Path, bytes]] = []
    for path in _catalog_candidates(discovery_start, worktree=worktree):
        encoded = _git_bound_authority_bytes(
            worktree,
            commit=commit,
            path=path,
            label="repository catalog",
        )
        if encoded is not None:
            bound_catalogs.append((path.resolve(strict=True), encoded))
    catalog_paths = tuple(path for path, _ in bound_catalogs)
    entries: dict[str, tuple[Path, CatalogOntology]] = {}
    delegation_path: Path | None = None
    delegation_declaration: CatalogFile | None = None
    delegation_manifest: DelegationManifest | None = None
    for catalog_path, catalog_bytes in bound_catalogs:
        loaded = _parse_catalog_bytes(
            catalog_bytes,
            label=f"catalog at verified commit: {catalog_path}",
        )
        for entry in loaded.ontologies:
            entries[entry.name] = (catalog_path, entry)
        if loaded.delegations is not None:
            manifest_path = catalog_path.parent / loaded.delegations.path
            manifest_bytes = _git_bound_authority_bytes(
                worktree,
                commit=commit,
                path=manifest_path,
                label="delegation manifest",
            )
            if manifest_bytes is None:
                raise ValueError("delegation manifest is missing from the verified commit")
            if len(manifest_bytes) != loaded.delegations.size_bytes:
                raise ValueError("delegation manifest size mismatch")
            if hashlib.sha256(manifest_bytes).hexdigest() != loaded.delegations.sha256:
                raise ValueError("delegation manifest sha256 mismatch")
            delegation_manifest = _parse_delegation_manifest_bytes(
                manifest_bytes,
                label=f"delegation manifest at verified commit: {manifest_path}",
            )
            delegation_path = manifest_path
            delegation_declaration = loaded.delegations
    verified = tuple(
        _verified_with_dirtiness(_verify_entry(catalog_path, entry, workspace=worktree), worktree)
        for _, (catalog_path, entry) in sorted(entries.items())
    )
    origin = _git(worktree, "remote", "get-url", "origin", required=False)
    if origin:
        identity = normalized_repository_identity(origin)
        identity_kind: Literal["remote", "machine_local"] = "remote"
    else:
        identity = str(worktree)
        identity_kind = "machine_local"
    active_ref = (
        commit
        if verified_commit is not None
        else _git(worktree, "symbolic-ref", "-q", "HEAD", required=False) or commit
    )
    return ResolvedRepositoryCatalog(
        repository_root=worktree,
        discovery_start=discovery_start,
        repository_identity=identity,
        identity_kind=identity_kind,
        active_ref=active_ref,
        commit=commit,
        catalog_paths=catalog_paths,
        ontologies=verified,
        delegation_manifest_path=delegation_path,
        delegation_manifest_sha256=(
            delegation_declaration.sha256 if delegation_declaration is not None else None
        ),
        delegation_manifest_size_bytes=(
            delegation_declaration.size_bytes
            if delegation_declaration is not None
            else None
        ),
        delegation_manifest=delegation_manifest,
    )


def _catalog_candidates(start: Path, *, worktree: Path) -> tuple[Path, ...]:
    try:
        relative = start.relative_to(worktree)
    except ValueError as error:
        raise ValueError("catalog start escapes Git worktree") from error
    directories = (
        worktree,
        *(
            worktree.joinpath(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ),
    )
    return tuple(directory / _CATALOG_NAME for directory in directories)


def _git_bound_authority_bytes(
    worktree: Path,
    *,
    commit: str,
    path: Path,
    label: str,
) -> bytes | None:
    """Return exact commit bytes after matching the index and regular worktree file."""
    try:
        relative = path.relative_to(worktree).as_posix()
    except ValueError as error:
        raise ValueError(f"{label} escapes Git worktree") from error
    committed = _git_file_state(worktree, commit=commit, relative=relative)
    indexed = _git_file_state(worktree, commit=None, relative=relative)
    exists = path.exists() or path.is_symlink()
    if committed is None and indexed is None and not exists:
        return None
    if committed is None:
        raise ValueError(f"{label} is untracked at the verified commit")
    if indexed is None:
        raise ValueError(f"{label} is missing from the Git index")
    _reject_symlink_ancestry(path)
    if not path.is_file():
        raise ValueError(f"{label} tracked at the verified commit is missing")
    committed_mode, committed_bytes = committed
    indexed_mode, indexed_bytes = indexed
    worktree_bytes = path.read_bytes()
    worktree_mode = "100755" if path.stat().st_mode & 0o111 else "100644"
    if committed_mode != indexed_mode or committed_mode != worktree_mode:
        raise ValueError(f"{label} mode differs from the verified commit or index")
    if indexed_bytes != committed_bytes:
        raise ValueError(f"{label} index bytes differ from the verified commit")
    if worktree_bytes != committed_bytes:
        raise ValueError(f"{label} worktree bytes differ from the verified commit")
    return committed_bytes


def _git_file_state(
    worktree: Path,
    *,
    commit: str | None,
    relative: str,
) -> tuple[str, bytes] | None:
    if commit is None:
        listing = subprocess.run(
            ("git", "-C", str(worktree), "ls-files", "--stage", "-z", "--", relative),
            env=confined_git_environment(),
            check=False,
            capture_output=True,
        )
        spec = f":{relative}"
    else:
        listing = subprocess.run(
            ("git", "-C", str(worktree), "ls-tree", "-z", commit, "--", relative),
            env=confined_git_environment(),
            check=False,
            capture_output=True,
        )
        spec = f"{commit}:{relative}"
    if listing.returncode:
        raise ValueError("Git failed while verifying an authority file")
    entries = tuple(item for item in listing.stdout.split(b"\0") if item)
    if not entries:
        return None
    if len(entries) != 1:
        raise ValueError("Git returned an ambiguous authority file identity")
    metadata, separator, listed_path = entries[0].partition(b"\t")
    fields = metadata.split()
    if not separator or listed_path.decode("utf-8", errors="strict") != relative:
        raise ValueError("Git returned a mismatched authority file path")
    mode = fields[0].decode("ascii")
    if mode not in {"100644", "100755"}:
        raise ValueError("authority file must be a tracked regular Git blob")
    content = subprocess.run(
        ("git", "-C", str(worktree), "cat-file", "blob", spec),
        env=confined_git_environment(),
        check=False,
        capture_output=True,
    )
    if content.returncode:
        raise ValueError("Git failed to read a verified authority file blob")
    return mode, content.stdout


def _catalog_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.name != _CATALOG_NAME:
        raise ValueError(f"catalog filename must be {_CATALOG_NAME}")
    _reject_symlink_ancestry(expanded)
    if not expanded.exists():
        raise ValueError(f"catalog is missing: {expanded}")
    if not expanded.is_file():
        raise ValueError("catalog must be a regular file")
    return expanded.resolve(strict=True)


def _ontology_directory(catalog_path: Path, entry: CatalogOntology) -> Path:
    root = catalog_path.parent
    candidate = root / entry.path
    _reject_symlink_ancestry(candidate)
    if not candidate.exists():
        raise ValueError(f"ontology directory is missing: {entry.path.as_posix()}")
    if not candidate.is_dir():
        raise ValueError("ontology path must be a directory")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("ontology directory escapes catalog root")
    return resolved


def _regular_file(ontology_path: Path, relative: Path) -> Path:
    candidate = ontology_path / relative
    _reject_symlink_ancestry(candidate)
    if not candidate.exists():
        raise ValueError(f"declared inventory file is missing: {relative.as_posix()}")
    if not candidate.is_file():
        raise ValueError(f"declared inventory path must be a regular file: {relative.as_posix()}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(ontology_path):
        raise ValueError("declared inventory file escapes ontology directory")
    return resolved


def _verify_entry(
    catalog_path: Path, entry: CatalogOntology, *, workspace: Path
) -> VerifiedCatalogOntology:
    ontology_path = _ontology_directory(catalog_path, entry)
    workspace_path = _workspace_ontology_path(ontology_path, workspace)
    for item in entry.files:
        file_path = _regular_file(ontology_path, item.path)
        content = file_path.read_bytes()
        if len(content) != item.size_bytes:
            raise ValueError(f"declared inventory size mismatch: {item.path.as_posix()}")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError(f"declared inventory sha256 mismatch: {item.path.as_posix()}")
    _verify_transitive_inputs(
        ontology_path,
        entry.files,
        workspace=workspace,
        workspace_path=workspace_path,
    )
    digest = ontology_bundle_sha256(entry)
    if digest != entry.bundle_sha256:
        raise ValueError("catalog bundle digest mismatch")
    return VerifiedCatalogOntology(
        name=entry.name,
        description=entry.description,
        catalog_path=catalog_path,
        ontology_path=ontology_path,
        workspace_path=workspace_path,
        files=entry.files,
        bundle_sha256=digest,
    )


def _verify_transitive_inputs(
    ontology_path: Path,
    files: Sequence[CatalogFile],
    *,
    workspace: Path | None,
    workspace_path: PurePosixPath,
) -> None:
    declared = {item.path.as_posix() for item in files}
    for item in files:
        yaml_path = _regular_file(ontology_path, item.path)
        if yaml_path.suffix not in {".yaml", ".yml"}:
            continue
        value = _load_bounded_yaml(
            yaml_path,
            label=f"declared YAML input: {item.path.as_posix()}",
        )
        for reference in _yaml_references(value):
            _require_declared_input(
                yaml_path.parent / _relative_path(reference, label="transitive YAML input path"),
                ontology_path=ontology_path,
                declared=declared,
                label="transitive YAML input",
                required=False,
            )
        if not isinstance(value, dict):
            continue
        for reference in _path_strings(value.get("seed_bundles")):
            relative = _workspace_input_path(reference, workspace_path=workspace_path)
            _require_declared_input(
                (workspace / _relative_path(reference, label="workspace seed bundle path"))
                if workspace is not None
                else ontology_path / relative,
                ontology_path=ontology_path,
                declared=declared,
                label="workspace seed bundle",
                required=True,
            )
        for pattern in _path_strings(value.get("seed_bundle_globs")):
            _workspace_glob_path(pattern, workspace_path=workspace_path)
            bundle_paths = (
                _seed_glob_paths(workspace, pattern)
                if workspace is not None
                else _relocated_seed_glob_paths(
                    ontology_path,
                    pattern,
                    workspace_path=workspace_path,
                )
            )
            for bundle_path in bundle_paths:
                _require_declared_input(
                    bundle_path,
                    ontology_path=ontology_path,
                    declared=declared,
                    label="workspace seed bundle",
                    required=True,
                )


def _workspace_ontology_path(ontology_path: Path, workspace: Path) -> PurePosixPath:
    resolved_workspace = workspace.resolve(strict=True)
    if not ontology_path.is_relative_to(resolved_workspace):
        raise ValueError("ontology directory escapes workspace")
    relative = ontology_path.relative_to(resolved_workspace)
    return _portable_relative_path(
        relative,
        label="workspace ontology path",
        allow_root=True,
    )


def _portable_relative_path(
    value: object,
    *,
    label: str,
    allow_root: bool = False,
) -> PurePosixPath:
    if not isinstance(value, (str, PurePath)):
        raise ValueError(f"{label} must be a string or path")
    if isinstance(value, PurePath):
        if value.drive or value.root:
            raise ValueError(f"{label} must not be drive-qualified or rooted")
        raw = value.as_posix()
    else:
        raw = value
    windows = PureWindowsPath(raw)
    if windows.drive or windows.root:
        raise ValueError(f"{label} must not be drive-qualified or rooted")
    if allow_root and raw == ".":
        return PurePosixPath(".")
    validated = _relative_path(raw, label=label)
    return PurePosixPath(validated.as_posix())


def _workspace_input_path(reference: str, *, workspace_path: PurePosixPath) -> Path:
    reference_path = _portable_relative_path(
        reference,
        label="workspace seed bundle path",
    )
    try:
        relative = reference_path.relative_to(workspace_path)
    except ValueError as error:
        raise ValueError("workspace seed bundle escapes ontology directory") from error
    return relative


def _relocated_seed_glob_paths(
    ontology_path: Path,
    pattern: str,
    *,
    workspace_path: PurePosixPath,
) -> tuple[Path, ...]:
    relative_pattern = _workspace_glob_path(pattern, workspace_path=workspace_path)
    try:
        matches = tuple(ontology_path.glob(relative_pattern.as_posix()))
    except ValueError as error:
        raise ValueError("workspace seed bundle glob is invalid") from error
    return tuple(
        sorted(
            (path for path in matches if path.is_file()),
            key=lambda path: path.relative_to(ontology_path).as_posix().encode("utf-8"),
        )
    )


def _workspace_glob_path(
    pattern: str,
    *,
    workspace_path: PurePosixPath,
) -> PurePosixPath:
    workspace_pattern = _portable_relative_path(
        pattern,
        label="workspace seed bundle glob",
    )
    try:
        return workspace_pattern.relative_to(workspace_path)
    except ValueError as error:
        raise ValueError("workspace seed bundle glob escapes ontology directory") from error


def _require_declared_input(
    candidate: Path,
    *,
    ontology_path: Path,
    declared: set[str],
    label: str,
    required: bool,
) -> None:
    _reject_symlink_ancestry(candidate)
    if not candidate.exists():
        if required:
            raise ValueError(f"{label} is missing")
        return
    if not candidate.is_file():
        raise ValueError(f"{label} must be a regular file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(ontology_path):
        raise ValueError(f"{label} escapes ontology directory")
    contained = resolved.relative_to(ontology_path).as_posix()
    if contained not in declared:
        raise ValueError(f"undeclared {label}: {contained}")


def _seed_glob_paths(workspace: Path, pattern: str) -> tuple[Path, ...]:
    relative_pattern = _relative_path(pattern, label="workspace seed bundle glob")
    completed = subprocess.run(
        ("git", "-C", str(workspace), "ls-tree", "-r", "--name-only", "-z", "HEAD", "--"),
        env=confined_git_environment(),
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise ValueError("seed_bundle_globs require an accessible Git HEAD")
    tracked = frozenset(
        item.decode("utf-8", errors="strict") for item in completed.stdout.split(b"\0") if item
    )
    try:
        matches = tuple(workspace.glob(relative_pattern.as_posix()))
    except ValueError as error:
        raise ValueError("workspace seed bundle glob is invalid") from error
    return tuple(
        sorted(
            (
                path
                for path in matches
                if path.is_file() and path.relative_to(workspace).as_posix() in tracked
            ),
            key=lambda path: path.relative_to(workspace).as_posix().encode("utf-8"),
        )
    )


def _yaml_references(value: object, *, parent_key: str | None = None) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            if key in _REFERENCE_KEYS or key == "path" and parent_key in _PATH_COLLECTION_KEYS:
                yield from _path_strings(nested)
            yield from _yaml_references(nested, parent_key=key)
    elif isinstance(value, list):
        for nested in value:
            yield from _yaml_references(nested, parent_key=parent_key)


def _load_bounded_yaml(path: Path, *, label: str) -> object:
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise ValueError(f"invalid {label}") from error
    return _load_bounded_yaml_bytes(encoded, label=label)


def _load_bounded_yaml_bytes(encoded: bytes, *, label: str) -> object:
    if len(encoded) > _MAX_YAML_FILE_BYTES:
        raise ValueError(f"invalid {label}: YAML file size exceeds the limit")
    try:
        rendered = encoded.decode("utf-8")
        _validate_yaml_events(rendered, label=label)
        value = yaml.safe_load(rendered)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"invalid {label}: bounded safe YAML parsing failed") from error
    _validate_yaml_graph(value, label=label)
    return value


def _validate_yaml_events(rendered: str, *, label: str) -> None:
    depth = 0
    for events, event in enumerate(
        yaml.parse(rendered, Loader=yaml.SafeLoader),
        start=1,
    ):
        if events > _MAX_YAML_NODES:
            raise ValueError(f"invalid {label}: YAML node limit exceeded")
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise ValueError(f"invalid {label}: YAML depth limit exceeded")
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1


def _validate_yaml_graph(value: object, *, label: str) -> None:
    nodes = 0
    active: set[int] = set()

    def visit(candidate: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_YAML_NODES:
            raise ValueError(f"invalid {label}: YAML node expansion limit exceeded")
        if depth > _MAX_YAML_DEPTH:
            raise ValueError(f"invalid {label}: YAML depth limit exceeded")
        if not isinstance(candidate, (dict, list, tuple, set)):
            return
        identity = id(candidate)
        if identity in active:
            raise ValueError(f"invalid {label}: YAML alias cycle is not allowed")
        active.add(identity)
        try:
            nested_values: Iterable[object]
            if isinstance(candidate, dict):
                nested_values = (
                    nested
                    for item in candidate.items()
                    for nested in item
                )
            else:
                nested_values = candidate
            for nested in nested_values:
                visit(nested, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)


def _path_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for nested in value:
            yield from _path_strings(nested)


def _selected_entries(
    catalog: RepositoryCatalog, names: Sequence[str]
) -> tuple[CatalogOntology, ...]:
    if not names:
        return catalog.ontologies
    requested = tuple(names)
    if len(requested) != len(set(requested)):
        raise ValueError("requested catalog ontology names must be unique")
    by_name = {entry.name: entry for entry in catalog.ontologies}
    missing = sorted(set(requested).difference(by_name))
    if missing:
        raise ValueError(f"unknown ontology in catalog: {', '.join(missing)}")
    return tuple(by_name[name] for name in requested)


def _atomic_yaml_replace(path: Path, catalog: RepositoryCatalog) -> None:
    rendered = yaml.safe_dump(
        catalog.model_dump(mode="json"), sort_keys=False, allow_unicode=True
    ).encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _reject_symlink_ancestry(path: Path) -> None:
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError(f"symbolic link is not allowed in catalog path: {current}")


def _start_directory(start: Path) -> Path:
    expanded = start.expanduser()
    if expanded.is_file():
        expanded = expanded.parent
    return expanded.resolve()


def _git_worktree(start: Path) -> Path | None:
    output = _git(_start_directory(start), "rev-parse", "--show-toplevel", required=False)
    return Path(output).resolve() if output else None


def _catalog_workspace(catalog_path: Path) -> Path:
    """Match OntologyBuilder's workspace-relative seed path interpretation."""
    return _git_worktree(catalog_path.parent) or catalog_path.parent


def _git(directory: Path, *arguments: str, required: bool = True) -> str:
    completed = subprocess.run(
        ("git", "-C", str(directory), *arguments),
        env=confined_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        if required:
            raise ValueError("Git command failed while resolving repository catalog")
        return ""
    return completed.stdout.strip()


def _verified_with_dirtiness(
    ontology: VerifiedCatalogOntology, worktree: Path
) -> VerifiedCatalogOntology:
    paths = [
        ontology.catalog_path,
        *(ontology.ontology_path / item.path for item in ontology.files),
    ]
    relative = [str(path.relative_to(worktree)) for path in paths]
    dirty = bool(_git(worktree, "status", "--porcelain", "--untracked-files=all", "--", *relative))
    return ontology.model_copy(update={"dirty": dirty})
