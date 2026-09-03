"""Strict publication boundary contracts and deterministic path roles."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    _https_url,
    _ref,
    _relative_path,
)
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


class PublicationProducer(StrEnum):
    GENERIC_SKILL = "generic_skill"
    EXPORTED_SKILL = "exported_skill"
    GENERATED_PROJECTION = "generated_projection"
    EXTRACTION_PROPOSAL = "extraction_proposal"
    KNOWLEDGE_PROMOTION = "knowledge_promotion"
    ACCEPTED_KNOWLEDGE = "accepted_knowledge"
    SOURCE_CARD = "source_card"
    POLICY = "policy"


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
    producer: PublicationProducer
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

    @model_validator(mode="after")
    def producer_owns_exact_role_and_paths(self) -> PublicationManifest:
        expected_role = _PRODUCER_ROLES[self.producer]
        for item in self.paths:
            if item.role is not expected_role or not _producer_path_allowed(
                self.producer, item.path
            ):
                raise ValueError("publication producer path or role is invalid")
        return self


class ProducerReceiptVerifier(Protocol):
    """Verify a producer receipt and its complete exact publication manifest."""

    def verify(self, manifest: PublicationManifest) -> None: ...


_PRODUCER_ROLES = {
    PublicationProducer.GENERIC_SKILL: PathRole.GENERIC_SKILL,
    PublicationProducer.EXPORTED_SKILL: PathRole.EXPORTED_SKILL,
    PublicationProducer.GENERATED_PROJECTION: PathRole.GENERATED_PROJECTION,
    PublicationProducer.EXTRACTION_PROPOSAL: PathRole.EXTRACTION_PROPOSAL,
    PublicationProducer.KNOWLEDGE_PROMOTION: PathRole.CANONICAL_KNOWLEDGE,
    PublicationProducer.ACCEPTED_KNOWLEDGE: PathRole.CANONICAL_KNOWLEDGE,
    PublicationProducer.SOURCE_CARD: PathRole.CANONICAL_KNOWLEDGE,
    PublicationProducer.POLICY: PathRole.CANONICAL_KNOWLEDGE,
}
_ONTOLOGY_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_POLICY_PATHS = frozenset(
    {
        "config/budget-policy.yaml",
        "config/deposit-policy.yaml",
        "config/model-policy.yaml",
        "config/providers.toml",
        "config/query-vocabulary.yaml",
        "config/research-policy.yaml",
        "config/source-policy.yaml",
        "config/truth-policy.yaml",
        "config/workload-policy.yaml",
    }
)


def _producer_path_allowed(producer: PublicationProducer, value: str) -> bool:
    parts = PurePosixPath(value).parts
    if producer is PublicationProducer.GENERIC_SKILL:
        return len(parts) > 3 and parts[:3] == (".agents", "skills", "geas")
    if producer is PublicationProducer.EXPORTED_SKILL:
        return (
            len(parts) > 3
            and parts[:2] == (".agents", "skills")
            and parts[2] != "geas"
            and _ONTOLOGY_NAME.fullmatch(parts[2]) is not None
        )
    if producer in {
        PublicationProducer.GENERATED_PROJECTION,
        PublicationProducer.EXTRACTION_PROPOSAL,
        PublicationProducer.KNOWLEDGE_PROMOTION,
        PublicationProducer.ACCEPTED_KNOWLEDGE,
        PublicationProducer.SOURCE_CARD,
    }:
        if (
            producer is PublicationProducer.ACCEPTED_KNOWLEDGE
            and value == "ontology/research-knowledge.yaml"
        ):
            return True
        if len(parts) < 3 or parts[0] != "ontology" or not _ONTOLOGY_NAME.fullmatch(parts[1]):
            return False
        if producer is PublicationProducer.GENERATED_PROJECTION:
            return len(parts) > 3 and parts[2] == "generated"
        if producer is PublicationProducer.EXTRACTION_PROPOSAL:
            return len(parts) > 3 and parts[2] == "candidates"
        if producer is PublicationProducer.KNOWLEDGE_PROMOTION:
            return len(parts) > 3 and parts[2] == "promotions"
        if producer is PublicationProducer.ACCEPTED_KNOWLEDGE:
            return (len(parts) == 3 and parts[2] == "bundle.yaml") or (
                len(parts) > 3 and parts[2] == "accepted"
            )
        return len(parts) > 3 and parts[2] == "sources"
    return value in _POLICY_PATHS or value == "intelligence/sources.yaml"


def capability_decision_set_sha256(decisions: Sequence[CapabilityDecision]) -> str:
    """Bind one exact capability decision to every path in a publication."""
    ordered = tuple(sorted(decisions, key=lambda item: item.request.path.encode("utf-8")))
    if not ordered or len({item.request.path for item in ordered}) != len(ordered):
        raise ValueError("publication capability decisions must cover unique paths")
    if len(ordered) == 1:
        return ordered[0].sha256
    payload = [
        {"path": decision.request.path, "decision_sha256": decision.sha256}
        for decision in ordered
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
    if role is PathRole.CANONICAL_KNOWLEDGE and mode is not PublishMode.PULL_REQUEST and (
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
