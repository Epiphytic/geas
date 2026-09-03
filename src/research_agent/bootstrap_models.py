"""Repository bootstrap receipts and no-effect service protocol."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import (
    CapabilityGrant,
    _https_url,
    _ref,
    _relative_path,
)
from research_agent.models import StrictModel, content_id


class BootstrapPhase(StrEnum):
    PLANNED = "planned"
    VERIFIED = "verified"
    TRUST_COMMITTED = "trust_committed"
    SUBSCRIBED = "subscribed"
    SKILLS_INSTALLED = "skills_installed"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


class RepositoryUpdatePhase(StrEnum):
    """Durable before/after markers for every update-side mutation."""

    VERIFIED = "verified"
    TRUST_PENDING = "trust_pending"
    TRUST_REPLACED = "trust_replaced"
    SUBSCRIPTION_PENDING = "subscription_pending"
    SUBSCRIPTION_REPLACED = "subscription_replaced"
    ARTIFACTS_PENDING = "artifacts_pending"
    ARTIFACTS_HYDRATED = "artifacts_hydrated"
    GENERIC_SKILL_PENDING = "generic_skill_pending"
    GENERIC_SKILL_INSTALLED = "generic_skill_installed"
    CATALOG_SKILLS_PENDING = "catalog_skills_pending"
    CATALOG_SKILLS_EXPORTED = "catalog_skills_exported"
    AGENT_LINKS_PENDING = "agent_links_pending"
    AGENT_LINKS_INSTALLED = "agent_links_installed"
    OBSOLETE_PATHS_PENDING = "obsolete_paths_pending"
    OBSOLETE_PATHS_REMOVED = "obsolete_paths_removed"
    FINALIZING = "finalizing"


class RepositoryRemovalPhase(StrEnum):
    """Durable removal progress over the original ownership receipt."""

    PENDING = "pending"
    SKILLS_REMOVED = "skills_removed"
    SUBSCRIPTION_REMOVED = "subscription_removed"
    TRUST_REMOVED = "trust_removed"


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


class RepositoryBootstrapRequest(StrictModel):
    """Explicit operator input for one repository-local bootstrap transaction."""

    version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    repository: str
    ref: str
    catalog: str = "geas.yaml"
    commit_sha256: str = Field(pattern=r"^[0-9a-f]{40}$")
    trust: Literal["none", "read_only", "trust_repository"] = "none"
    delegate_depth: int = Field(default=1, ge=0, le=32)
    ontology_paths: tuple[str, ...] = ()
    bundle_sha256: tuple[str, ...] = ()
    source_hosts: tuple[str, ...] = ()
    source_path_prefixes: tuple[str, ...] = ()
    source_connectors: tuple[str, ...] = ()
    delegated_repositories: tuple[str, ...] = ()
    current_worktree: Path | None = None

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("ref", mode="before")
    @classmethod
    def normalize_ref(cls, value: object) -> str:
        return _ref(value)

    @field_validator("catalog", mode="before")
    @classmethod
    def normalize_catalog(cls, value: object) -> str:
        path = _relative_path(value, label="catalog")
        if path.rsplit("/", 1)[-1] != "geas.yaml":
            raise ValueError("catalog must name geas.yaml")
        return path

    @field_validator("ontology_paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_relative_path(item, label="ontology path") for item in value}))  # type: ignore[arg-type]

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def normalize_digests(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _sha256

        return tuple(sorted({_sha256(item, label="bundle_sha256") for item in value}))  # type: ignore[arg-type]

    @field_validator("source_hosts", mode="before")
    @classmethod
    def normalize_hosts(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _host

        return tuple(sorted({_host(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("source_path_prefixes", mode="before")
    @classmethod
    def normalize_prefixes(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _path_prefix

        return tuple(sorted({_path_prefix(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("source_connectors", mode="before")
    @classmethod
    def normalize_connectors(cls, value: object) -> tuple[str, ...]:
        values = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if any(not item or item.strip() != item for item in values):
            raise ValueError("source connectors must be normalized non-empty strings")
        return tuple(sorted(set(values)))

    @field_validator("delegated_repositories", mode="before")
    @classmethod
    def normalize_delegated_repositories(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_https_url(item, label="delegated repository") for item in value}))  # type: ignore[arg-type]

    @field_validator("current_worktree", mode="before")
    @classmethod
    def absolute_worktree(cls, value: object) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("current_worktree must be absolute")
        return path.resolve(strict=False)

    @model_validator(mode="after")
    def invocation_scope_requires_trust_repository(self) -> RepositoryBootstrapRequest:
        scoped = (
            self.delegate_depth != 1,
            self.ontology_paths,
            self.bundle_sha256,
            self.source_hosts,
            self.source_path_prefixes,
            self.source_connectors,
            self.delegated_repositories,
        )
        if self.trust != "trust_repository" and any(scoped):
            raise ValueError("delegation and source scopes require trust_repository")
        return self


class VerifiedRepositoryBootstrap(StrictModel):
    """Read-only exact catalog/checkout verification input for a bootstrap transaction."""

    version: Literal[1] = 1
    repository: str
    ref: str
    catalog: str
    commit_sha256: str = Field(pattern=r"^[0-9a-f]{40}$")
    ontology_paths: tuple[str, ...] = Field(min_length=1)
    bundle_sha256: tuple[str, ...] = Field(min_length=1)
    source_hosts: tuple[str, ...] = ()
    source_path_prefixes: tuple[str, ...] = ()
    source_connectors: tuple[str, ...] = ()
    delegated_repositories: tuple[str, ...] = ()
    current_worktree: Path | None = None

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("ref", mode="before")
    @classmethod
    def normalize_ref(cls, value: object) -> str:
        return _ref(value)

    @field_validator("catalog", mode="before")
    @classmethod
    def normalize_catalog(cls, value: object) -> str:
        path = _relative_path(value, label="catalog")
        if path.rsplit("/", 1)[-1] != "geas.yaml":
            raise ValueError("catalog must name geas.yaml")
        return path

    @field_validator("ontology_paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_relative_path(item, label="ontology path") for item in value}))  # type: ignore[arg-type]

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def normalize_digests(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _sha256

        return tuple(sorted({_sha256(item, label="bundle_sha256") for item in value}))  # type: ignore[arg-type]

    @field_validator("source_hosts", mode="before")
    @classmethod
    def normalize_hosts(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _host

        return tuple(sorted({_host(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("source_path_prefixes", mode="before")
    @classmethod
    def normalize_prefixes(cls, value: object) -> tuple[str, ...]:
        from research_agent.capabilities import _path_prefix

        return tuple(sorted({_path_prefix(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("source_connectors", mode="before")
    @classmethod
    def normalize_connectors(cls, value: object) -> tuple[str, ...]:
        values = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if any(not item or item.strip() != item for item in values):
            raise ValueError("source connectors must be normalized non-empty strings")
        return tuple(sorted(set(values)))

    @field_validator("delegated_repositories", mode="before")
    @classmethod
    def normalize_delegated_repositories(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_https_url(item, label="delegated repository") for item in value}))  # type: ignore[arg-type]

    @field_validator("current_worktree", mode="before")
    @classmethod
    def absolute_worktree(cls, value: object) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("current_worktree must be absolute")
        return path.resolve(strict=False)

    @property
    def id(self) -> str:
        return content_id("verified-repository-bootstrap", self.model_dump(mode="json"))


class RepositoryBootstrapReceipt(StrictModel):
    """Durable ownership evidence for a completed or resumable bootstrap."""

    version: Literal[1] = 1
    request: RepositoryBootstrapRequest
    verified: VerifiedRepositoryBootstrap | None = None
    completed_phases: tuple[BootstrapPhase, ...] = ()
    pending_phase: BootstrapPhase | None = None
    update_candidate: VerifiedRepositoryBootstrap | None = None
    removal_pending: bool = False
    removal_phase: RepositoryRemovalPhase | None = None
    removed: bool = False
    trust_grant: CapabilityGrant | None = None
    managed_paths: tuple[ManagedPath, ...] = ()
    created_at: datetime
    updated_at: datetime
    removal_guidance: str = "uv tool uninstall geas"

    @field_validator("completed_phases")
    @classmethod
    def ordered_unique_phases(cls, value: tuple[BootstrapPhase, ...]) -> tuple[BootstrapPhase, ...]:
        legal = (
            BootstrapPhase.VERIFIED,
            BootstrapPhase.TRUST_COMMITTED,
            BootstrapPhase.SUBSCRIBED,
            BootstrapPhase.SKILLS_INSTALLED,
            BootstrapPhase.COMPLETED,
        )
        if value != legal[: len(value)]:
            raise ValueError("completed_phases must be a legal ordered prefix")
        return value

    @model_validator(mode="after")
    def pending_phase_is_legal(self) -> RepositoryBootstrapReceipt:
        legal = (
            BootstrapPhase.VERIFIED,
            BootstrapPhase.TRUST_COMMITTED,
            BootstrapPhase.SUBSCRIBED,
            BootstrapPhase.SKILLS_INSTALLED,
            BootstrapPhase.COMPLETED,
        )
        if self.pending_phase is not None and (
            len(self.completed_phases) == len(legal)
            or self.pending_phase != legal[len(self.completed_phases)]
        ):
            raise ValueError("pending_phase must be the next legal phase")
        if self.removed and (
            self.pending_phase is not None
            or self.removal_pending
            or self.removal_phase is not None
            or self.update_candidate is not None
        ):
            raise ValueError("removed receipt cannot retain a pending operation")
        if self.removal_pending != (self.removal_phase is not None):
            raise ValueError("removal_pending and removal_phase must agree")
        return self

    @field_validator("managed_paths")
    @classmethod
    def unique_managed_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in ordered}) != len(ordered):
            raise ValueError("managed_paths must have unique paths")
        return ordered

    @field_validator("created_at", "updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("repository-bootstrap", self.model_dump(mode="json"))


class RepositoryUpdateJournal(StrictModel):
    """Validated candidate transaction retained beside an unchanged old receipt."""

    version: Literal[1] = 1
    old_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_request: RepositoryBootstrapRequest
    old_managed_paths: tuple[ManagedPath, ...]
    old_grant: CapabilityGrant | None = None
    candidate_request: RepositoryBootstrapRequest
    candidate_verified: VerifiedRepositoryBootstrap
    candidate_grant: CapabilityGrant | None = None
    candidate_managed_paths: tuple[ManagedPath, ...] = ()
    phase: RepositoryUpdatePhase
    created_at: datetime
    updated_at: datetime

    @field_validator("old_managed_paths", "candidate_managed_paths")
    @classmethod
    def unique_managed_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in ordered}) != len(ordered):
            raise ValueError("update journal managed paths must be unique")
        return ordered

    @field_validator("created_at", "updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def candidate_identity_matches_request(self) -> RepositoryUpdateJournal:
        values = ("repository", "ref", "catalog", "commit_sha256", "current_worktree")
        if any(
            getattr(self.candidate_request, field) != getattr(self.candidate_verified, field)
            for field in values
        ):
            raise ValueError("candidate verified identity does not match candidate request")
        if self.old_request.name != self.candidate_request.name:
            raise ValueError("update journal bootstrap names must match")
        return self


class RepositoryBootstrapService(Protocol):
    def install(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...

    def update(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...

    def remove(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...
