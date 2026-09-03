"""Strict publication boundary contracts and deterministic path roles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import Capability, _https_url, _ref, _relative_path
from research_agent.models import StrictModel, content_id


class PathRole(StrEnum):
    GENERIC_SKILL = "generic_skill"
    EXPORTED_SKILL = "exported_skill"
    GENERATED_PROJECTION = "generated_projection"
    EXTRACTION_PROPOSAL = "extraction_proposal"
    CANONICAL_KNOWLEDGE = "canonical_knowledge"
    RUNTIME_STORE = "runtime_store"
    UNCLASSIFIED = "unclassified"


class PublishMode(StrEnum):
    NONE = "none"
    PULL_REQUEST = "pull_request"
    DIRECT_PUSH = "direct_push"
    AUTO_MERGE = "auto_merge"


class PublishPath(StrictModel):
    version: Literal[1] = 1
    path: str
    role: PathRole

    @field_validator("path", mode="before")
    @classmethod
    def normalized_relative_path(cls, value: object) -> str:
        return _relative_path(value, label="publish path")


class PublicationManifestPath(PublishPath):
    """One content-addressed file in a producer-owned publication manifest."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationManifest(StrictModel):
    """Closed, canonical inventory used for role classification, not authority."""

    version: Literal[1] = 1
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paths: tuple[PublicationManifestPath, ...] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def sorted_unique_paths(
        cls, value: tuple[PublicationManifestPath, ...]
    ) -> tuple[PublicationManifestPath, ...]:
        result = tuple(sorted(value, key=lambda item: item.path.encode("utf-8")))
        if len({item.path for item in result}) != len(result):
            raise ValueError("publication manifest paths must be unique")
        return result


_RUNTIME_ROOTS = frozenset({".geas", "data", "logs"})
_RUNTIME_NAMES = frozenset({".env", "credentials", "credentials.json"})


def classify_managed_path(
    path: str,
    *,
    manifests: Sequence[PublicationManifest] = (),
) -> PathRole:
    """Classify one normalized repository path from fixed rules and exact manifests."""
    normalized = _relative_path(path, label="managed publication path")
    parts = PurePosixPath(normalized).parts
    name = parts[-1]
    if (
        parts[0] in _RUNTIME_ROOTS
        or name in _RUNTIME_NAMES
        or name.startswith(".env.")
        or name.endswith((".sqlite", ".sqlite-shm", ".sqlite-wal"))
    ):
        return PathRole.RUNTIME_STORE
    generic_root = (".agents", "skills", "geas")
    if parts[: len(generic_root)] == generic_root and len(parts) > len(generic_root):
        return PathRole.GENERIC_SKILL
    matches = {
        item.role for manifest in manifests for item in manifest.paths if item.path == normalized
    }
    if len(matches) > 1:
        raise ValueError("publication manifests assign conflicting roles to a path")
    return next(iter(matches), PathRole.UNCLASSIFIED)


def required_capabilities(
    role: PathRole,
    mode: PublishMode,
    *,
    canonical_target: bool,
) -> frozenset[Capability] | None:
    """Return the literal path-role publication matrix; ``None`` means forbidden."""
    if mode is PublishMode.NONE:
        return frozenset()
    if role in {PathRole.RUNTIME_STORE, PathRole.UNCLASSIFIED}:
        return None
    if role is PathRole.EXTRACTION_PROPOSAL and (
        canonical_target or mode is PublishMode.AUTO_MERGE
    ):
        return None
    if mode is PublishMode.PULL_REQUEST:
        capabilities = {Capability.GIT_PULL_REQUEST}
    elif mode is PublishMode.DIRECT_PUSH:
        capabilities = {Capability.GIT_DIRECT_PUSH}
    elif mode is PublishMode.AUTO_MERGE:
        capabilities = {Capability.GIT_AUTO_MERGE}
    else:  # pragma: no cover - strict enum closes this branch
        raise ValueError("unsupported publication mode")
    if role is PathRole.CANONICAL_KNOWLEDGE and (
        canonical_target or mode is PublishMode.AUTO_MERGE
    ):
        capabilities.add(Capability.KNOWLEDGE_AUTO_PROMOTE)
    return frozenset(capabilities)


class PublishRequest(StrictModel):
    version: Literal[1] = 1
    repository: str
    target_ref: str
    mode: PublishMode = PublishMode.PULL_REQUEST
    paths: tuple[PublishPath, ...] = Field(min_length=1)
    capability_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("target_ref", mode="before")
    @classmethod
    def normalize_ref(cls, value: object) -> str:
        return _ref(value)

    @field_validator("paths")
    @classmethod
    def sorted_unique_paths(cls, value: tuple[PublishPath, ...]) -> tuple[PublishPath, ...]:
        result = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in result}) != len(result):
            raise ValueError("publish paths must be unique")
        return result

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def remote_publish_only_has_classified_paths(self) -> PublishRequest:
        if self.mode is not PublishMode.NONE:
            forbidden = {PathRole.RUNTIME_STORE, PathRole.UNCLASSIFIED}
            if any(path.role in forbidden for path in self.paths):
                raise ValueError("unclassified or runtime-store paths cannot be published remotely")
        return self

    @property
    def id(self) -> str:
        return content_id("publish-request", self.model_dump(mode="json"))


class PublishResult(StrictModel):
    version: Literal[1] = 1
    request_id: str
    published: bool
    branch: str | None = None
    commit_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    pull_request_url: str | None = None
    reason: str
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("publish-result", self.model_dump(mode="json"))


class RepositoryPublisher(Protocol):
    def publish(self, request: PublishRequest) -> PublishResult: ...
