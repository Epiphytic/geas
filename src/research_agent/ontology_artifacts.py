from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from uuid import uuid4

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.credential_scanning import (
    contains_binary_credential_residue,
    contains_credential_assignment_marker,
    contains_fixed_credential,
    contains_possible_credential,
)
from research_agent.models import StrictModel, canonical_json, utc_now
from research_agent.truth import SQLiteProjectionGuard

_ONTOLOGY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ASSET_NAME = re.compile(r"geas-[a-z-]+-[0-9a-f]{64}\.(?:sqlite|zip)")
_RELEASE_TAG = re.compile(r"geas-artifact-[0-9a-f]{64}")
class OntologyArtifactError(RuntimeError):
    pass


class ArtifactRole(StrEnum):
    SOURCE_LIBRARY = "source-library"
    KNOWLEDGE_PROJECTION = "knowledge-projection"
    GENERATED_CONTENT = "generated-content"


class ArtifactFormat(StrEnum):
    SQLITE = "sqlite"
    ZIP = "zip"


class OntologyArtifact(StrictModel):
    role: ArtifactRole
    format: ArtifactFormat
    input_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    asset_name: str
    release_tag: str
    contains_source_text: bool = True
    storage_rights_basis: str = Field(min_length=1, max_length=500)

    @field_validator("asset_name")
    @classmethod
    def asset_name_is_safe(cls, value: str) -> str:
        if not _ASSET_NAME.fullmatch(value):
            raise ValueError("ontology artifact asset name is invalid")
        return value

    @field_validator("release_tag")
    @classmethod
    def release_tag_is_safe(cls, value: str) -> str:
        if not _RELEASE_TAG.fullmatch(value):
            raise ValueError("ontology artifact release tag is invalid")
        return value


class OntologyArtifactManifest(StrictModel):
    version: Literal[1] = 1
    ontology: str
    published_at: datetime
    published_by: str = Field(min_length=1, max_length=200)
    artifacts: tuple[OntologyArtifact, ...]

    @field_validator("ontology")
    @classmethod
    def ontology_name_is_safe(cls, value: str) -> str:
        if not _ONTOLOGY_NAME.fullmatch(value):
            raise ValueError("ontology artifact manifest name is invalid")
        return value

    @model_validator(mode="after")
    def roles_are_unique(self) -> OntologyArtifactManifest:
        roles = tuple(item.role for item in self.artifacts)
        if len(roles) != len(set(roles)):
            raise ValueError("ontology artifact roles must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> OntologyArtifactManifest:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def explicit_yaml(self) -> str:
        header = (
            "# Rebuildable, non-canonical ontology artifacts.\n"
            "# Verify hashes and embedded SQLite projection stamps before use.\n"
        )
        return header + yaml.safe_dump(
            self.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )


class ArtifactPublishReceipt(StrictModel):
    ontology: str
    manifest: str
    changed: bool
    published: tuple[ArtifactRole, ...]
    reused: tuple[ArtifactRole, ...]
    artifacts: tuple[OntologyArtifact, ...]


class ArtifactHydrationItem(StrictModel):
    role: ArtifactRole
    path: str
    downloaded: bool
    content_sha256: str
    input_revision: str


class ArtifactHydrationReceipt(StrictModel):
    ontology: str
    manifest: str
    hydrated: tuple[ArtifactHydrationItem, ...]


class ArtifactStore(Protocol):
    def available(self, artifact: OntologyArtifact) -> bool: ...

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool: ...

    def download(self, artifact: OntologyArtifact, destination: Path) -> None: ...


class GitHubReleaseArtifactStore:
    """Content-addressed release assets associated with a GitHub repository."""

    def __init__(self, repository_url: str, *, branch: str = "main") -> None:
        self.repository = _github_repository_slug(repository_url)
        self.branch = branch

    def available(self, artifact: OntologyArtifact) -> bool:
        release = self._release(artifact.release_tag)
        if release is None:
            return False
        assets = {item.get("name"): item for item in release.get("assets", [])}
        existing = assets.get(artifact.asset_name)
        if existing is None:
            return False
        expected_digest = f"sha256:{artifact.content_sha256}"
        if (
            existing.get("digest") != expected_digest
            or existing.get("size") != artifact.size_bytes
            or existing.get("state") != "uploaded"
        ):
            raise OntologyArtifactError(
                "existing GitHub release asset does not match its content address"
            )
        return True

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        release = self._release(artifact.release_tag)
        if release is None:
            self._run(
                (
                    "gh",
                    "release",
                    "create",
                    artifact.release_tag,
                    "--repo",
                    self.repository,
                    "--target",
                    self.branch,
                    "--title",
                    artifact.release_tag,
                    "--notes",
                    "Geas content-addressed rebuildable ontology artifact.",
                    "--prerelease",
                )
            )
            release = self._release(artifact.release_tag)
            if release is None:
                raise OntologyArtifactError("GitHub release was not visible after creation")
        if self.available(artifact):
            return False
        with tempfile.TemporaryDirectory(prefix="geas-artifact-upload-") as temporary:
            upload = Path(temporary) / artifact.asset_name
            shutil.copyfile(source, upload)
            self._run(
                (
                    "gh",
                    "release",
                    "upload",
                    artifact.release_tag,
                    str(upload),
                    "--repo",
                    self.repository,
                )
            )
        verified = self._release(artifact.release_tag)
        if verified is None:
            raise OntologyArtifactError("GitHub release disappeared after artifact upload")
        uploaded = {
            item.get("name"): item for item in verified.get("assets", [])
        }.get(artifact.asset_name)
        if uploaded is None or uploaded.get("digest") != f"sha256:{artifact.content_sha256}":
            raise OntologyArtifactError("GitHub did not report the expected artifact digest")
        return True

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="geas-artifact-download-") as temporary:
            self._run(
                (
                    "gh",
                    "release",
                    "download",
                    artifact.release_tag,
                    "--repo",
                    self.repository,
                    "--pattern",
                    artifact.asset_name,
                    "--dir",
                    temporary,
                )
            )
            downloaded = Path(temporary) / artifact.asset_name
            if not downloaded.is_file():
                raise OntologyArtifactError("GitHub release did not provide the requested asset")
            _verify_file(downloaded, artifact)
            temporary_destination = destination.with_name(
                f".{destination.name}.tmp-{uuid4().hex}"
            )
            try:
                shutil.copyfile(downloaded, temporary_destination)
                os.replace(temporary_destination, destination)
            finally:
                temporary_destination.unlink(missing_ok=True)

    def _release(self, tag: str) -> dict[str, object] | None:
        result = self._run(
            ("gh", "api", f"repos/{self.repository}/releases/tags/{tag}"),
            check=False,
        )
        if result.returncode == 1 and "HTTP 404" in result.stderr:
            return None
        if result.returncode != 0:
            raise OntologyArtifactError(_command_error("GitHub release lookup failed", result))
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise OntologyArtifactError("GitHub returned invalid release metadata") from error
        if not isinstance(value, dict):
            raise OntologyArtifactError("GitHub returned invalid release metadata")
        return value

    @staticmethod
    def _run(
        command: tuple[str, ...],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            completed = subprocess.run(
                command,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise OntologyArtifactError(
                "GitHub artifact sync requires the authenticated gh executable"
            ) from error
        if check and completed.returncode != 0:
            raise OntologyArtifactError(_command_error("GitHub artifact command failed", completed))
        return completed


class OntologyArtifactManager:
    manifest_name = "artifacts.yaml"
    cache_name = ".geas-artifacts"

    def __init__(self, ontology_directory: Path) -> None:
        if ontology_directory.is_symlink():
            raise OntologyArtifactError("ontology artifact directory cannot be a symbolic link")
        self.ontology_directory = ontology_directory.expanduser().resolve()
        self.ontology = self.ontology_directory.name
        if not _ONTOLOGY_NAME.fullmatch(self.ontology):
            raise OntologyArtifactError("ontology directory name is invalid")
        self.manifest_path = self.ontology_directory / self.manifest_name
        self.cache = self.ontology_directory / self.cache_name

    def load(self) -> OntologyArtifactManifest:
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise OntologyArtifactError(
                f"ontology artifact manifest is missing: {self.manifest_path}"
            )
        manifest = OntologyArtifactManifest.from_yaml(self.manifest_path)
        if manifest.ontology != self.ontology:
            raise OntologyArtifactError("ontology artifact manifest belongs to another ontology")
        return manifest

    def publish(
        self,
        *,
        store: ArtifactStore,
        published_by: str,
        storage_rights_basis: str,
        source_library: Path | None = None,
        knowledge_projection: Path | None = None,
        generated_content: Path | None = None,
    ) -> ArtifactPublishReceipt:
        requested = {
            ArtifactRole.SOURCE_LIBRARY: source_library,
            ArtifactRole.KNOWLEDGE_PROJECTION: knowledge_projection,
            ArtifactRole.GENERATED_CONTENT: generated_content,
        }
        if not any(requested.values()):
            raise OntologyArtifactError("at least one ontology artifact must be selected")
        basis = storage_rights_basis.strip()
        if not basis:
            raise OntologyArtifactError("artifact publication requires a storage-rights basis")
        existing = self.load() if self.manifest_path.exists() else None
        by_role = {item.role: item for item in existing.artifacts} if existing else {}
        published: list[ArtifactRole] = []
        reused: list[ArtifactRole] = []
        temporary_root = Path(tempfile.mkdtemp(prefix="geas-artifact-prepare-"))
        try:
            for role, path in requested.items():
                if path is None:
                    continue
                previous = by_role.get(role)
                if role is not ArtifactRole.GENERATED_CONTENT:
                    source = path.expanduser()
                    if source.is_symlink() or not source.is_file():
                        raise OntologyArtifactError(
                            f"SQLite ontology artifact is missing or unsafe: {path}"
                        )
                    quick_revision = _sqlite_input_revision(
                        source.absolute(),
                        role,
                        verify_contents=False,
                    )
                    if (
                        previous is not None
                        and previous.input_revision == quick_revision
                        and store.available(previous)
                    ):
                        by_role[role] = previous
                        reused.append(role)
                        continue
                prepared = self._prepare(
                    role,
                    path,
                    temporary_root=temporary_root,
                    storage_rights_basis=basis,
                )
                if (
                    previous is not None
                    and previous.input_revision == prepared[0].input_revision
                    and store.available(previous)
                ):
                    by_role[role] = previous
                    reused.append(role)
                    continue
                artifact, asset_path = prepared
                uploaded = store.ensure(artifact, asset_path)
                by_role[role] = artifact
                (published if uploaded else reused).append(role)
            artifacts = tuple(sorted(by_role.values(), key=lambda item: item.role.value))
            next_manifest = OntologyArtifactManifest(
                ontology=self.ontology,
                published_at=utc_now(),
                published_by=published_by,
                artifacts=artifacts,
            )
            changed = existing is None or existing.artifacts != artifacts
            if changed:
                self.ontology_directory.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(self.manifest_path, next_manifest.explicit_yaml())
            return ArtifactPublishReceipt(
                ontology=self.ontology,
                manifest=str(self.manifest_path),
                changed=changed,
                published=tuple(published),
                reused=tuple(reused),
                artifacts=artifacts,
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    def hydrate(
        self,
        *,
        store: ArtifactStore,
        roles: tuple[ArtifactRole, ...] = (),
    ) -> ArtifactHydrationReceipt:
        _validate_cache_path(
            self.ontology_directory,
            ontology_directory=self.ontology_directory,
            expected_kind="directory",
        )
        manifest = self.load()
        selected = set(roles) if roles else {item.role for item in manifest.artifacts}
        unknown = selected - {item.role for item in manifest.artifacts}
        if unknown:
            raise OntologyArtifactError(
                "artifact manifest does not provide roles: "
                + ", ".join(sorted(item.value for item in unknown))
            )
        selected_artifacts = tuple(
            artifact for artifact in manifest.artifacts if artifact.role in selected
        )
        destinations = tuple(
            (artifact, self._cache_path(artifact)) for artifact in selected_artifacts
        )
        generated_output = self.cache / "generated"
        if any(
            artifact.role is ArtifactRole.GENERATED_CONTENT
            for artifact in selected_artifacts
        ):
            _validate_cache_path(
                generated_output,
                ontology_directory=self.ontology_directory,
                expected_kind="directory",
            )
        _validate_cache_path(
            self.cache,
            ontology_directory=self.ontology_directory,
            expected_kind="directory",
        )
        for _, destination in destinations:
            _validate_cache_path(
                destination,
                ontology_directory=self.ontology_directory,
                expected_kind="file",
            )
        self.cache.mkdir(exist_ok=True)
        _validate_cache_path(
            self.cache,
            ontology_directory=self.ontology_directory,
            expected_kind="directory",
        )
        if any(
            artifact.role is ArtifactRole.GENERATED_CONTENT
            for artifact in selected_artifacts
        ):
            _validate_cache_path(
                generated_output,
                ontology_directory=self.ontology_directory,
                expected_kind="directory",
            )
        hydrated: list[ArtifactHydrationItem] = []
        for artifact, destination in destinations:
            downloaded = not _cached_artifact_is_valid(destination, artifact)
            if downloaded:
                store.download(artifact, destination)
            _validate_cache_path(
                destination,
                ontology_directory=self.ontology_directory,
                expected_kind="file",
            )
            _verify_file(destination, artifact)
            if artifact.format is ArtifactFormat.SQLITE:
                actual_input_revision = _sqlite_input_revision(
                    destination,
                    artifact.role,
                )
                if actual_input_revision != artifact.input_revision:
                    raise OntologyArtifactError(
                        "SQLite ontology artifact input revision does not match "
                        "its manifest"
                    )
            else:
                _extract_generated_zip(
                    destination,
                    generated_output,
                    ontology_directory=self.ontology_directory,
                )
            hydrated.append(
                ArtifactHydrationItem(
                    role=artifact.role,
                    path=str(
                        generated_output
                        if artifact.role is ArtifactRole.GENERATED_CONTENT
                        else destination
                    ),
                    downloaded=downloaded,
                    content_sha256=artifact.content_sha256,
                    input_revision=artifact.input_revision,
                )
            )
        return ArtifactHydrationReceipt(
            ontology=self.ontology,
            manifest=str(self.manifest_path),
            hydrated=tuple(hydrated),
        )

    def _prepare(
        self,
        role: ArtifactRole,
        path: Path,
        *,
        temporary_root: Path,
        storage_rights_basis: str,
    ) -> tuple[OntologyArtifact, Path]:
        source = path.expanduser()
        if source.is_symlink():
            raise OntologyArtifactError(f"ontology artifact cannot be a symbolic link: {path}")
        source = source.absolute()
        if role is ArtifactRole.GENERATED_CONTENT:
            if not source.is_dir() and not source.is_file():
                raise OntologyArtifactError(
                    "generated-content artifact must be a file or directory"
                )
            prepared = temporary_root / "generated-content.zip"
            _write_deterministic_zip(source, prepared)
            format = ArtifactFormat.ZIP
            input_revision = _sha256_file(prepared)
        else:
            if not source.is_file():
                raise OntologyArtifactError(f"SQLite ontology artifact is missing: {path}")
            input_revision = _sqlite_input_revision(source, role)
            prepared = source
            format = ArtifactFormat.SQLITE
        _scan_sensitive_content(prepared)
        digest = _sha256_file(prepared)
        extension = format.value
        asset_name = f"geas-{role.value}-{digest}.{extension}"
        return (
            OntologyArtifact(
                role=role,
                format=format,
                input_revision=input_revision,
                content_sha256=digest,
                size_bytes=prepared.stat().st_size,
                asset_name=asset_name,
                release_tag=f"geas-artifact-{digest}",
                storage_rights_basis=storage_rights_basis,
            ),
            prepared,
        )

    def _cache_path(self, artifact: OntologyArtifact) -> Path:
        names = {
            ArtifactRole.SOURCE_LIBRARY: "library.sqlite",
            ArtifactRole.KNOWLEDGE_PROJECTION: "query.sqlite",
            ArtifactRole.GENERATED_CONTENT: "generated.zip",
        }
        path = self.cache / names[artifact.role]
        _validate_cache_path(
            path,
            ontology_directory=self.ontology_directory,
            expected_kind="file",
        )
        return path


def _github_repository_slug(value: str) -> str:
    normalized = value.rstrip("/").removesuffix(".git")
    prefix = "https://github.com/"
    if not normalized.startswith(prefix):
        raise OntologyArtifactError(
            "GitHub release artifacts require an https://github.com/OWNER/REPO Git URL"
        )
    slug = normalized.removeprefix(prefix)
    parts = slug.split("/")
    if len(parts) != 2 or not all(_ONTOLOGY_NAME.fullmatch(item) for item in parts):
        raise OntologyArtifactError("GitHub ontology repository URL is invalid")
    return slug


def _sqlite_input_revision(
    path: Path,
    role: ArtifactRole,
    *,
    verify_contents: bool = True,
) -> str:
    try:
        if role is ArtifactRole.KNOWLEDGE_PROJECTION:
            stamp = SQLiteProjectionGuard().validated_stamp(path)
            inputs = {
                "truth_state_digest": stamp.truth_state_digest,
                "schema_version": stamp.schema_version,
                "builder_version": stamp.builder_version,
            }
            return hashlib.sha256(canonical_json(inputs)).hexdigest()
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            if verify_contents:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise OntologyArtifactError(
                        f"SQLite artifact failed integrity check: {path}"
                    )
            if role is ArtifactRole.SOURCE_LIBRARY:
                row = connection.execute(
                    "SELECT payload FROM library_metadata WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    raise OntologyArtifactError("source-library projection is unstamped")
                metadata = json.loads(row[0])
                snapshot = metadata["snapshot"]
                inputs = {
                    "schema_version": metadata["schema_version"],
                    "builder_version": metadata["builder_version"],
                    "library_id": snapshot["library_id"],
                    "manifest_sha256": snapshot["manifest_sha256"],
                    "source_version_ids": snapshot["source_version_ids"],
                    "text_derivation_ids": snapshot["text_derivation_ids"],
                    "repository_snapshot_ids": snapshot["repository_snapshot_ids"],
                }
            else:
                raise OntologyArtifactError("generated content is not a SQLite artifact")
    except (json.JSONDecodeError, KeyError, sqlite3.Error, ValueError) as error:
        if isinstance(error, OntologyArtifactError):
            raise
        raise OntologyArtifactError(f"invalid SQLite ontology artifact: {path}") from error
    return hashlib.sha256(canonical_json(inputs)).hexdigest()


def _write_deterministic_zip(source: Path, destination: Path) -> None:
    if source.is_file():
        _scan_sensitive_content(source)
        files = ((source, source.name),)
    else:
        files = tuple(
            (path, path.relative_to(source).as_posix())
            for path in _safe_generated_files(source)
        )
    if not files:
        raise OntologyArtifactError("generated-content directory contains no files")
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path, relative in files:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def _safe_generated_files(root: Path) -> Iterable[Path]:
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise OntologyArtifactError("generated content cannot contain symbolic links")
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > 100_000_000:
            raise OntologyArtifactError("generated-content file exceeds 100 MB")
        total += size
        if total > 1_000_000_000:
            raise OntologyArtifactError("generated-content artifact exceeds 1 GB")
        _scan_sensitive_content(path)
        yield path


def _extract_generated_zip(
    archive_path: Path,
    destination: Path,
    *,
    ontology_directory: Path,
) -> None:
    _validate_cache_path(
        archive_path,
        ontology_directory=ontology_directory,
        expected_kind="file",
    )
    _validate_cache_path(
        destination,
        ontology_directory=ontology_directory,
        expected_kind="directory",
    )
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
    previous = destination.with_name(f".{destination.name}.previous-{uuid4().hex}")
    _validate_cache_path(
        temporary,
        ontology_directory=ontology_directory,
        expected_kind="directory",
    )
    _validate_cache_path(
        previous,
        ontology_directory=ontology_directory,
        expected_kind="directory",
    )
    temporary.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                relative = PurePosixPath(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if relative.is_absolute() or ".." in relative.parts or mode == 0o120000:
                    raise OntologyArtifactError("generated-content archive contains unsafe paths")
                target = temporary.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(temporary.resolve()):
                    raise OntologyArtifactError("generated-content archive escapes its cache")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise OntologyArtifactError("generated-content cache target is unsafe")
            os.replace(destination, previous)
        os.replace(temporary, destination)
        shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        if previous.exists() and not destination.exists():
            os.replace(previous, destination)


def _validate_cache_path(
    path: Path,
    *,
    ontology_directory: Path,
    expected_kind: Literal["file", "directory"] | None = None,
) -> None:
    """Reject lexical cache aliases before any cache I/O can follow them."""
    root = Path(os.path.abspath(ontology_directory))
    if root.is_symlink() or not root.is_dir():
        raise OntologyArtifactError("ontology artifact ontology root is unsafe")
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise OntologyArtifactError("ontology artifact cache escapes its ontology") from error
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise OntologyArtifactError(
                f"ontology artifact cache contains a symbolic link: {current}"
            )
        if current != candidate and current.exists() and not current.is_dir():
            raise OntologyArtifactError(
                f"ontology artifact cache ancestor is not a directory: {current}"
            )
    if not candidate.exists() or expected_kind is None:
        return
    if expected_kind == "file" and not candidate.is_file():
        raise OntologyArtifactError(
            f"ontology artifact cache file target is unsafe: {candidate}"
        )
    if expected_kind == "directory" and not candidate.is_dir():
        raise OntologyArtifactError(
            f"ontology artifact cache directory target is unsafe: {candidate}"
        )


def _cached_artifact_is_valid(path: Path, artifact: OntologyArtifact) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        _verify_file(path, artifact)
    except OntologyArtifactError:
        return False
    return True


def _verify_file(path: Path, artifact: OntologyArtifact) -> None:
    if path.is_symlink() or not path.is_file():
        raise OntologyArtifactError(f"ontology artifact is missing or unsafe: {path}")
    if path.stat().st_size != artifact.size_bytes:
        raise OntologyArtifactError("ontology artifact size does not match its manifest")
    if _sha256_file(path) != artifact.content_sha256:
        raise OntologyArtifactError("ontology artifact hash does not match its manifest")


def _scan_sensitive_content(path: Path) -> None:
    if path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        sqlite_database = handle.read(16) == b"SQLite format 3\x00"
    if sqlite_database:
        _scan_sqlite_values(path)
    access = mmap.ACCESS_COPY if sqlite_database else mmap.ACCESS_READ
    with path.open("rb") as handle, mmap.mmap(
        handle.fileno(),
        0,
        access=access,
    ) as content:
        if sqlite_database:
            _mask_live_sqlite_cells(path, content)
            sensitive = contains_binary_credential_residue(content)
        else:
            sensitive = contains_possible_credential(content)
        if sensitive:
            raise OntologyArtifactError(
                f"possible credential detected in ontology artifact: {path}"
            )


def _mask_live_sqlite_cells(path: Path, content: mmap.mmap) -> None:
    """Mask only reachable live B-tree structures in a private scan view."""
    if len(content) < 100 or bytes(content[:16]) != b"SQLite format 3\x00":
        raise OntologyArtifactError(f"invalid SQLite ontology artifact: {path}")
    encoded_page_size = int.from_bytes(content[16:18], "big")
    page_size = 65536 if encoded_page_size == 1 else encoded_page_size
    reserved = content[20]
    if (
        page_size < 512
        or page_size > 65536
        or page_size & (page_size - 1)
        or reserved >= page_size
        or len(content) % page_size
    ):
        raise OntologyArtifactError(f"invalid SQLite page layout: {path}")
    usable_size = page_size - reserved
    page_count = len(content) // page_size
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            roots = {
                1,
                *(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT rootpage FROM sqlite_schema WHERE rootpage > 0"
                    )
                ),
            }
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise OntologyArtifactError(
            f"could not inventory live SQLite pages: {path}"
        ) from error

    pending = sorted(roots, reverse=True)
    visited: set[int] = set()
    while pending:
        page_number = pending.pop()
        if page_number in visited:
            continue
        _validate_sqlite_page_number(page_number, page_count)
        visited.add(page_number)
        page_start = (page_number - 1) * page_size
        header_start = page_start + (100 if page_number == 1 else 0)
        page_type = content[header_start]
        if page_type not in {0x02, 0x05, 0x0A, 0x0D}:
            raise OntologyArtifactError("SQLite root traversal reached a non-B-tree page")
        header_size = 12 if page_type in {0x02, 0x05} else 8
        cell_count = int.from_bytes(content[header_start + 3 : header_start + 5], "big")
        pointer_start = header_start + header_size
        pointer_end = pointer_start + 2 * cell_count
        page_usable_end = page_start + usable_size
        if pointer_end > page_usable_end:
            raise OntologyArtifactError("SQLite B-tree cell pointer array is invalid")
        children: list[int] = []
        if page_type in {0x02, 0x05}:
            children.append(
                int.from_bytes(content[header_start + 8 : header_start + 12], "big")
            )
        cell_offsets = tuple(
            int.from_bytes(content[offset : offset + 2], "big")
            for offset in range(pointer_start, pointer_end, 2)
        )
        if len(set(cell_offsets)) != len(cell_offsets):
            raise OntologyArtifactError("duplicate SQLite B-tree cell pointer")
        cells: list[
            tuple[int, int, int | None, int, int, tuple[int, int] | None]
        ] = []
        for relative_offset in cell_offsets:
            if relative_offset == 0 and page_size == 65536:
                relative_offset = 65536
            cell_start = page_start + relative_offset
            if not pointer_end <= cell_start < page_usable_end:
                raise OntologyArtifactError("SQLite B-tree cell offset is invalid")
            cell_end, child, payload_start, local_size, overflow = (
                _sqlite_live_cell_extent(
                    content,
                    cell_start=cell_start,
                    page_type=page_type,
                    page_usable_end=page_usable_end,
                    usable_size=usable_size,
                )
            )
            if child is not None:
                children.append(child)
            cells.append(
                (cell_start, cell_end, child, payload_start, local_size, overflow)
            )
        previous_end = pointer_end
        for cell_start, cell_end, *_rest in sorted(cells):
            if cell_start < previous_end:
                raise OntologyArtifactError("overlapping SQLite B-tree cells")
            previous_end = cell_end

        content[header_start:pointer_end] = b"\x00" * (pointer_end - header_start)
        if page_number == 1:
            content[:100] = b"\x00" * 100
        for cell_start, cell_end, _child, payload_start, local_size, overflow in cells:
            _scan_and_mask_sqlite_payload(
                path,
                content,
                payload_start=payload_start,
                local_size=local_size,
                overflow=overflow,
                scan_payload=page_type in {0x02, 0x0A},
                page_size=page_size,
                usable_size=usable_size,
                page_count=page_count,
                visited_btree=visited,
            )
            content[cell_start:cell_end] = b"\x00" * (cell_end - cell_start)
        for child in sorted(children, reverse=True):
            _validate_sqlite_page_number(child, page_count)
            if child not in visited:
                pending.append(child)


def _sqlite_live_cell_extent(
    content: mmap.mmap,
    *,
    cell_start: int,
    page_type: int,
    page_usable_end: int,
    usable_size: int,
) -> tuple[int, int | None, int, int, tuple[int, int] | None]:
    cursor = cell_start
    child: int | None = None
    if page_type in {0x02, 0x05}:
        if cursor + 4 > page_usable_end:
            raise OntologyArtifactError("SQLite interior cell is truncated")
        child = int.from_bytes(content[cursor : cursor + 4], "big")
        cursor += 4
    if page_type == 0x05:
        _rowid, cursor = _read_sqlite_varint(content, cursor, page_usable_end)
        return cursor, child, cursor, 0, None

    payload_size, cursor = _read_sqlite_varint(content, cursor, page_usable_end)
    if page_type == 0x0D:
        _rowid, cursor = _read_sqlite_varint(content, cursor, page_usable_end)
    local_size = _sqlite_local_payload_size(
        payload_size,
        usable_size=usable_size,
        table_leaf=page_type == 0x0D,
    )
    payload_end = cursor + local_size
    if payload_end > page_usable_end:
        raise OntologyArtifactError("SQLite B-tree payload is truncated")
    if local_size == payload_size:
        return payload_end, child, cursor, local_size, None
    if payload_end + 4 > page_usable_end:
        raise OntologyArtifactError("SQLite overflow pointer is truncated")
    overflow_page = int.from_bytes(content[payload_end : payload_end + 4], "big")
    return (
        payload_end + 4,
        child,
        cursor,
        local_size,
        (overflow_page, payload_size - local_size),
    )


def _read_sqlite_varint(
    content: mmap.mmap,
    offset: int,
    limit: int,
) -> tuple[int, int]:
    value = 0
    for index in range(9):
        if offset >= limit:
            raise OntologyArtifactError("SQLite varint is truncated")
        byte = content[offset]
        offset += 1
        if index == 8:
            return (value << 8) | byte, offset
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset
    raise AssertionError("SQLite varint loop did not terminate")


def _sqlite_local_payload_size(
    payload_size: int,
    *,
    usable_size: int,
    table_leaf: bool,
) -> int:
    minimum = ((usable_size - 12) * 32 // 255) - 23
    maximum = (
        usable_size - 35
        if table_leaf
        else ((usable_size - 12) * 64 // 255) - 23
    )
    if minimum < 0 or maximum < minimum:
        raise OntologyArtifactError("SQLite usable page size is invalid")
    if payload_size <= maximum:
        return payload_size
    local = minimum + (payload_size - minimum) % (usable_size - 4)
    return minimum if local > maximum else local


def _scan_and_mask_sqlite_payload(
    path: Path,
    content: mmap.mmap,
    *,
    payload_start: int,
    local_size: int,
    overflow: tuple[int, int] | None,
    scan_payload: bool,
    page_size: int,
    usable_size: int,
    page_count: int,
    visited_btree: set[int],
) -> None:
    """Scan unprojected index record bodies before masking their live bytes.

    Table values are decoded and scanned by ``_scan_sqlite_values``. Index
    records can contain derived expression values which SQLite does not expose
    through that inventory. Their record header contains only SQLite serial
    types, so scan the exact contiguous record body without evaluating SQL or
    treating a separate physical layout as authority.
    """
    overflow_segments = _sqlite_overflow_segments(
        content,
        overflow=overflow,
        page_size=page_size,
        usable_size=usable_size,
        page_count=page_count,
        visited_btree=visited_btree,
    )
    segments = (
        ((payload_start, local_size),) if local_size else ()
    ) + tuple((start, size) for _page_start, start, size in overflow_segments)
    payload_size = sum(size for _start, size in segments)
    expected_size = local_size + (overflow[1] if overflow is not None else 0)
    if payload_size != expected_size:
        raise OntologyArtifactError("SQLite record payload extent is invalid")
    if payload_size == 0:
        if scan_payload:
            raise OntologyArtifactError("SQLite index record payload is empty")
        return
    header_size = (
        _validate_sqlite_record(content, segments, payload_size)
        if scan_payload
        else 0
    )

    # Binary assignment classification materializes at most 4 KiB. Keeping
    # twice that much overlap makes every cross-page candidate visible while
    # bounding each scan window to one SQLite page plus 8 KiB.
    overlap_size = 8192
    carry = b""
    logical_offset = 0

    def scan_segment(start: int, size: int) -> None:
        nonlocal carry, logical_offset
        if not scan_payload:
            return
        segment_end = logical_offset + size
        body_offset = max(header_size - logical_offset, 0)
        logical_offset = segment_end
        if body_offset >= size:
            return
        segment = bytes(content[start + body_offset : start + size])
        window = carry + segment
        if contains_binary_credential_residue(window):
            raise OntologyArtifactError(
                f"possible credential detected in ontology artifact: {path}"
            )
        carry = window[-overlap_size:]

    scan_segment(payload_start, local_size)
    for page_start, segment_start, segment_size in overflow_segments:
        scan_segment(segment_start, segment_size)
        mask_end = segment_start + segment_size
        content[page_start:mask_end] = b"\x00" * (mask_end - page_start)


def _sqlite_overflow_segments(
    content: mmap.mmap,
    *,
    overflow: tuple[int, int] | None,
    page_size: int,
    usable_size: int,
    page_count: int,
    visited_btree: set[int],
) -> tuple[tuple[int, int, int], ...]:
    if overflow is None:
        return ()
    page_number, remaining = overflow
    visited: set[int] = set()
    segments: list[tuple[int, int, int]] = []
    while remaining:
        _validate_sqlite_page_number(page_number, page_count)
        if page_number in visited or page_number in visited_btree:
            raise OntologyArtifactError("SQLite overflow page cycle is invalid")
        visited.add(page_number)
        page_start = (page_number - 1) * page_size
        next_page = int.from_bytes(content[page_start : page_start + 4], "big")
        segment_size = min(remaining, usable_size - 4)
        segments.append((page_start, page_start + 4, segment_size))
        remaining -= segment_size
        if remaining and next_page == 0:
            raise OntologyArtifactError("SQLite overflow chain ended early")
        if not remaining and next_page != 0:
            raise OntologyArtifactError("SQLite overflow chain exceeds its payload")
        page_number = next_page
    return tuple(segments)


def _validate_sqlite_record(
    content: mmap.mmap,
    segments: tuple[tuple[int, int], ...],
    payload_size: int,
) -> int:
    """Validate one complete SQLite record header and its declared body extent."""
    segment_index = 0
    segment_offset = 0
    logical_offset = 0

    def read_varint(limit: int) -> int:
        nonlocal segment_index, segment_offset, logical_offset
        value = 0
        start = logical_offset
        for index in range(9):
            if logical_offset >= limit or segment_index >= len(segments):
                raise OntologyArtifactError("SQLite record varint is truncated")
            segment_start, segment_size = segments[segment_index]
            byte = content[segment_start + segment_offset]
            segment_offset += 1
            logical_offset += 1
            if segment_offset == segment_size:
                segment_index += 1
                segment_offset = 0
            if index == 8:
                value = (value << 8) | byte
                break
            value = (value << 7) | (byte & 0x7F)
            if byte < 0x80:
                break
        consumed = logical_offset - start
        if consumed != _sqlite_varint_size(value):
            raise OntologyArtifactError("SQLite record varint is overlong")
        return value

    header_size = read_varint(payload_size)
    if not logical_offset <= header_size <= payload_size:
        raise OntologyArtifactError("SQLite record header size is invalid")
    body_size = 0
    available_body = payload_size - header_size
    while logical_offset < header_size:
        serial_type = read_varint(header_size)
        body_size += _sqlite_serial_type_size(serial_type)
        if body_size > available_body:
            raise OntologyArtifactError("SQLite record body extent is invalid")
    if logical_offset != header_size or body_size != available_body:
        raise OntologyArtifactError("SQLite record body extent is invalid")
    return header_size


def _sqlite_varint_size(value: int) -> int:
    if value > 0x00FFFFFFFFFFFFFF:
        return 9
    return max(1, (value.bit_length() + 6) // 7)


def _sqlite_serial_type_size(serial_type: int) -> int:
    fixed_sizes = (0, 1, 2, 3, 4, 6, 8, 8, 0, 0)
    if serial_type < len(fixed_sizes):
        return fixed_sizes[serial_type]
    if serial_type in {10, 11}:
        raise OntologyArtifactError("SQLite record serial type is reserved")
    return (serial_type - 12) // 2


def _validate_sqlite_page_number(page_number: int, page_count: int) -> None:
    if not 1 <= page_number <= page_count:
        raise OntologyArtifactError("SQLite page number is outside the database")


def _scan_sqlite_values(path: Path) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            schema_values = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL "
                "ORDER BY type, name, tbl_name, rootpage, sql"
            )
            for (value,) in schema_values:
                _raise_for_sqlite_schema_credential(path, value)
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
                )
            )
            for table in tables:
                quoted_table = _quoted_sqlite_identifier(table)
                columns = tuple(
                    row[1]
                    for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
                )
                for column in columns:
                    quoted_column = _quoted_sqlite_identifier(column)
                    values = connection.execute(
                        f"SELECT {quoted_column} FROM {quoted_table} "
                        f"WHERE typeof({quoted_column}) IN ('text', 'blob')"
                    )
                    for (value,) in values:
                        _raise_for_sqlite_credential(path, value)
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as error:
        raise OntologyArtifactError(
            f"could not scan SQLite ontology artifact for credentials: {path}"
        ) from error


def _raise_for_sqlite_credential(path: Path, value: str | bytes) -> None:
    content = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if contains_possible_credential(content) or contains_binary_credential_residue(
        content
    ):
        raise OntologyArtifactError(
            f"possible credential detected in ontology artifact: {path}"
        )


def _raise_for_sqlite_schema_credential(path: Path, value: str) -> None:
    content = value.encode("utf-8")
    if contains_fixed_credential(content) or contains_credential_assignment_marker(
        content
    ):
        raise OntologyArtifactError(
            f"possible credential detected in ontology artifact: {path}"
        )


def _quoted_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    if path.is_symlink():
        raise OntologyArtifactError("ontology artifact manifest cannot be a symbolic link")
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        temporary.write_text(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _command_error(
    prefix: str,
    completed: subprocess.CompletedProcess[str],
) -> str:
    detail = completed.stderr.strip()[-2000:]
    return f"{prefix}: {detail}" if detail else prefix
