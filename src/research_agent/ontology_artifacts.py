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
    contains_fixed_credential,
    contains_possible_credential,
)
from research_agent.models import StrictModel, canonical_json, utc_now
from research_agent.truth import ProjectionStamp, SQLiteProjectionGuard

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
                        source.resolve(),
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
        manifest = self.load()
        selected = set(roles) if roles else {item.role for item in manifest.artifacts}
        unknown = selected - {item.role for item in manifest.artifacts}
        if unknown:
            raise OntologyArtifactError(
                "artifact manifest does not provide roles: "
                + ", ".join(sorted(item.value for item in unknown))
            )
        hydrated: list[ArtifactHydrationItem] = []
        for artifact in manifest.artifacts:
            if artifact.role not in selected:
                continue
            destination = self._cache_path(artifact)
            downloaded = not _cached_artifact_is_valid(destination, artifact)
            if downloaded:
                store.download(artifact, destination)
            _verify_file(destination, artifact)
            if artifact.format is ArtifactFormat.SQLITE:
                _sqlite_input_revision(destination, artifact.role)
            else:
                _extract_generated_zip(destination, self.cache / "generated")
            hydrated.append(
                ArtifactHydrationItem(
                    role=artifact.role,
                    path=str(
                        self.cache / "generated"
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
        source = source.resolve()
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
        if not path.resolve().is_relative_to(self.ontology_directory):
            raise OntologyArtifactError("ontology artifact cache escapes its ontology")
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
            elif role is ArtifactRole.KNOWLEDGE_PROJECTION:
                row = connection.execute(
                    "SELECT payload FROM _research_projection_metadata WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    raise OntologyArtifactError("knowledge projection is unstamped")
                stamp = ProjectionStamp.model_validate_json(row[0])
                if verify_contents:
                    actual = SQLiteProjectionGuard().logical_digest(connection)
                    if actual != stamp.projection_digest:
                        raise OntologyArtifactError(
                            "knowledge projection logical digest does not match its stamp"
                        )
                inputs = {
                    "truth_state_digest": stamp.truth_state_digest,
                    "schema_version": stamp.schema_version,
                    "builder_version": stamp.builder_version,
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


def _extract_generated_zip(archive_path: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
    previous = destination.with_name(f".{destination.name}.previous-{uuid4().hex}")
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
    with path.open("rb") as handle, mmap.mmap(
        handle.fileno(),
        0,
        access=mmap.ACCESS_READ,
    ) as content:
        sqlite_database = content[:16] == b"SQLite format 3\x00"
        sensitive = (
            contains_fixed_credential(content)
            if sqlite_database
            else contains_possible_credential(content)
        )
        if sensitive:
            raise OntologyArtifactError(
                f"possible credential detected in ontology artifact: {path}"
            )
    if sqlite_database:
        _scan_sqlite_values(path)


def _scan_sqlite_values(path: Path) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            schema_values = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL "
                "ORDER BY type, name, tbl_name, rootpage, sql"
            )
            for (value,) in schema_values:
                _raise_for_sqlite_credential(path, value)
                for fragment in _sqlite_quoted_fragments(value):
                    _raise_for_sqlite_credential(path, fragment)
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
    if contains_possible_credential(content):
        raise OntologyArtifactError(
            f"possible credential detected in ontology artifact: {path}"
        )


def _sqlite_quoted_fragments(value: str) -> tuple[str, ...]:
    """Return inert SQLite string and quoted-identifier contents."""
    fragments: list[str] = []
    index = 0
    while index < len(value):
        opener = value[index]
        if opener not in {'"', "'", "`", "["}:
            index += 1
            continue
        closer = "]" if opener == "[" else opener
        index += 1
        parts: list[str] = []
        start = index
        while index < len(value):
            if value[index] != closer:
                index += 1
                continue
            parts.append(value[start:index])
            if opener != "[" and index + 1 < len(value) and value[index + 1] == closer:
                parts.append(closer)
                index += 2
                start = index
                continue
            fragments.append("".join(parts))
            index += 1
            break
        else:
            raise ValueError("unterminated quoted SQLite schema text")
    return tuple(fragments)


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
