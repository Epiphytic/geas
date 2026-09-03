"""Repository bootstrap receipts and no-effect service protocol."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator

from research_agent.capabilities import _https_url, _ref, _relative_path
from research_agent.models import StrictModel, content_id


class BootstrapPhase(StrEnum):
    PLANNED = "planned"
    VERIFIED = "verified"
    TRUST_COMMITTED = "trust_committed"
    SUBSCRIBED = "subscribed"
    SKILLS_INSTALLED = "skills_installed"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


class ManagedPath(StrictModel):
    version: Literal[1] = 1
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["skill", "manifest", "snapshot", "link", "receipt"]

    @field_validator("path", mode="before")
    @classmethod
    def normalized_relative_path(cls, value: object) -> str:
        return _relative_path(value, label="managed path")


class RepositoryInstallReceipt(StrictModel):
    version: Literal[1] = 1
    repository: str
    ref: str
    commit_sha256: str = Field(pattern=r"^[0-9a-f]{40}$")
    phase: BootstrapPhase
    managed_paths: tuple[ManagedPath, ...] = ()
    created_at: datetime
    recovery_command: str | None = None

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("ref", mode="before")
    @classmethod
    def normalize_ref(cls, value: object) -> str:
        return _ref(value)

    @field_validator("managed_paths")
    @classmethod
    def sorted_unique_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        result = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in result}) != len(result):
            raise ValueError("managed_paths must have unique paths")
        return result

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("repository-install", self.model_dump(mode="json"))


class RepositoryMutationReceipt(StrictModel):
    version: Literal[1] = 1
    install_receipt_id: str
    phase: BootstrapPhase
    action: Literal["install", "update", "remove", "link", "unlink"]
    managed_paths: tuple[ManagedPath, ...] = ()
    recorded_at: datetime
    recovery_command: str | None = None

    @field_validator("managed_paths")
    @classmethod
    def sorted_unique_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        result = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in result}) != len(result):
            raise ValueError("managed_paths must have unique paths")
        return result

    @field_validator("recorded_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("repository-mutation", self.model_dump(mode="json"))


class RepositoryBootstrapService(Protocol):
    def install(self, receipt: RepositoryInstallReceipt) -> RepositoryMutationReceipt: ...

    def update(self, receipt: RepositoryInstallReceipt) -> RepositoryMutationReceipt: ...

    def remove(self, receipt: RepositoryInstallReceipt) -> RepositoryMutationReceipt: ...
