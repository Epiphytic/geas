"""Strict publication boundary contracts, with no forge implementation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import _https_url, _ref, _relative_path
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
