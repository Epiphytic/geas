"""Repository bootstrap receipts and no-effect service protocol."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationInfo, field_validator, model_validator

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


class RepositoryUpdateEffect(StrEnum):
    """Semantic mutations whose receipts make an update phase meaningful."""

    TRUST = "trust"
    SUBSCRIPTION = "subscription"
    ARTIFACTS = "artifacts"
    GENERIC_SKILL = "generic-skill"
    CATALOG_SKILLS = "catalog-skills"
    AGENT_LINKS = "agent-links"
    OBSOLETE_PATHS = "obsolete-paths"


class ManagedPath(StrictModel):
    version: Literal[1] = 1
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["skill", "manifest", "snapshot", "link", "receipt"]

    @field_validator("path", mode="before")
    @classmethod
    def normalized_relative_path(cls, value: object) -> str:
        return _relative_path(value, label="managed path")


_BOOTSTRAP_OPERATION_KEY = (
    r"^repository-bootstrap-(?:operation|update-operation|removal-operation):"
    r"sha256:[0-9a-f]{64}$"
)
_BOOTSTRAP_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class BootstrapConfigMutationReceipt(StrictModel):
    """Exact before/after identity for one scoped user-config mutation."""

    version: Literal[1] = 1
    operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    profile_name: str = Field(pattern=_BOOTSTRAP_NAME)
    bootstrap_name: str = Field(pattern=_BOOTSTRAP_NAME)
    kind: Literal[
        "grant_record",
        "grant_replace",
        "grant_remove",
        "subscription_ensure",
        "subscription_replace",
        "subscription_remove",
    ]
    before_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BootstrapGrantOwnershipReceipt(StrictModel):
    """Active ownership of one exact capability-grant identity."""

    version: Literal[1] = 1
    owner_operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    profile_name: str = Field(pattern=_BOOTSTRAP_NAME)
    bootstrap_name: str = Field(pattern=_BOOTSTRAP_NAME)
    grant_id: str = Field(pattern=r"^capability-grant:sha256:[0-9a-f]{64}$")
    config_mutation: BootstrapConfigMutationReceipt

    @model_validator(mode="after")
    def mutation_matches_ownership(self) -> BootstrapGrantOwnershipReceipt:
        mutation = self.config_mutation
        if (
            mutation.operation_key != self.operation_key
            or mutation.profile_name != self.profile_name
            or mutation.bootstrap_name != self.bootstrap_name
            or mutation.kind not in {"grant_record", "grant_replace"}
        ):
            raise ValueError("grant ownership does not match its config mutation")
        return self


class BootstrapGrantMutationReceipt(StrictModel):
    """Result of one stable-keyed exact grant record, replacement, or removal."""

    version: Literal[1] = 1
    operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    profile_name: str = Field(pattern=_BOOTSTRAP_NAME)
    bootstrap_name: str = Field(pattern=_BOOTSTRAP_NAME)
    action: Literal["record", "replace", "remove"]
    old_grant_id: str | None = Field(
        default=None, pattern=r"^capability-grant:sha256:[0-9a-f]{64}$"
    )
    new_grant_id: str | None = Field(
        default=None, pattern=r"^capability-grant:sha256:[0-9a-f]{64}$"
    )
    config_mutation: BootstrapConfigMutationReceipt
    ownership: BootstrapGrantOwnershipReceipt | None = None

    @model_validator(mode="after")
    def identities_match_action(self) -> BootstrapGrantMutationReceipt:
        mutation = self.config_mutation
        expected_kind = f"grant_{self.action}"
        if (
            mutation.operation_key != self.operation_key
            or mutation.profile_name != self.profile_name
            or mutation.bootstrap_name != self.bootstrap_name
            or mutation.kind != expected_kind
        ):
            raise ValueError("grant mutation operation does not match its config mutation")
        if self.action == "record":
            valid_identity = self.old_grant_id is None and self.new_grant_id is not None
        elif self.action == "replace":
            valid_identity = self.old_grant_id is not None
        else:
            valid_identity = self.old_grant_id is not None and self.new_grant_id is None
        if not valid_identity:
            raise ValueError("grant mutation identities do not match its action")
        if self.new_grant_id is None:
            if self.ownership is not None:
                raise ValueError("removed grant mutation cannot retain ownership")
        elif (
            self.ownership is None
            or self.ownership.operation_key != self.operation_key
            or self.ownership.profile_name != self.profile_name
            or self.ownership.bootstrap_name != self.bootstrap_name
            or self.ownership.grant_id != self.new_grant_id
            or self.ownership.config_mutation != mutation
        ):
            raise ValueError("grant mutation ownership does not match its operation")
        return self


class BootstrapSubscriptionOwnershipReceipt(StrictModel):
    """Exact directory and config ownership for one bootstrap subscription."""

    version: Literal[1] = 1
    owner_operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    profile_name: str = Field(pattern=_BOOTSTRAP_NAME)
    bootstrap_name: str = Field(pattern=_BOOTSTRAP_NAME)
    subscription_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkout: str
    checkout_created: bool
    verified_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    checkout_device: int = Field(ge=0)
    checkout_inode: int = Field(gt=0)
    evidence_path: str
    config_mutation: BootstrapConfigMutationReceipt

    @field_validator("checkout", "evidence_path", mode="before")
    @classmethod
    def paths_are_relative(cls, value: object) -> str:
        return _relative_path(value, label="bootstrap ownership path")

    @model_validator(mode="after")
    def mutation_and_paths_match_ownership(self) -> BootstrapSubscriptionOwnershipReceipt:
        mutation = self.config_mutation
        if (
            mutation.operation_key != self.operation_key
            or mutation.profile_name != self.profile_name
            or mutation.bootstrap_name != self.bootstrap_name
            or mutation.kind not in {"subscription_ensure", "subscription_replace"}
        ):
            raise ValueError("subscription ownership does not match its config mutation")
        expected_checkout = f"subscriptions/{self.profile_name}/{self.bootstrap_name}"
        if self.checkout != expected_checkout:
            raise ValueError("bootstrap subscription must use its fixed checkout")
        operation_digest = self.operation_key.rsplit(":", 1)[-1]
        expected_evidence = (
            "repository-bootstrap/subscription-ownership/"
            f"{self.profile_name}/{self.bootstrap_name}/{operation_digest}.json"
        )
        if self.evidence_path != expected_evidence:
            raise ValueError("subscription ownership evidence path does not match its operation")
        return self


class BootstrapSubscriptionMutationReceipt(StrictModel):
    """Stable-keyed result for one exact bootstrap subscription mutation."""

    version: Literal[1] = 1
    operation_key: str = Field(pattern=_BOOTSTRAP_OPERATION_KEY)
    profile_name: str = Field(pattern=_BOOTSTRAP_NAME)
    bootstrap_name: str = Field(pattern=_BOOTSTRAP_NAME)
    action: Literal["ensure", "replace", "remove"]
    old_subscription_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_subscription_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_mutation: BootstrapConfigMutationReceipt
    ownership: BootstrapSubscriptionOwnershipReceipt | None = None
    managed_paths: tuple[ManagedPath, ...] = ()

    @field_validator("managed_paths")
    @classmethod
    def unique_managed_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in ordered}) != len(ordered):
            raise ValueError("subscription mutation paths must be unique")
        return ordered

    @model_validator(mode="after")
    def identities_and_evidence_match_action(self) -> BootstrapSubscriptionMutationReceipt:
        mutation = self.config_mutation
        expected_kind = f"subscription_{self.action}"
        if (
            mutation.operation_key != self.operation_key
            or mutation.profile_name != self.profile_name
            or mutation.bootstrap_name != self.bootstrap_name
            or mutation.kind != expected_kind
        ):
            raise ValueError("subscription mutation operation does not match config mutation")
        if self.action == "ensure":
            valid_identity = (
                self.old_subscription_sha256 is None
                and self.new_subscription_sha256 is not None
            )
        elif self.action == "replace":
            valid_identity = (
                self.old_subscription_sha256 is not None
                and self.new_subscription_sha256 is not None
            )
        else:
            valid_identity = (
                self.old_subscription_sha256 is not None
                and self.new_subscription_sha256 is None
            )
        if not valid_identity:
            raise ValueError("subscription mutation identities do not match its action")
        if self.new_subscription_sha256 is None:
            if self.ownership is not None or self.managed_paths:
                raise ValueError("removed subscription cannot retain ownership evidence")
            return self
        if (
            self.ownership is None
            or self.ownership.operation_key != self.operation_key
            or self.ownership.profile_name != self.profile_name
            or self.ownership.bootstrap_name != self.bootstrap_name
            or self.ownership.subscription_sha256 != self.new_subscription_sha256
            or self.ownership.config_mutation != mutation
            or len(self.managed_paths) != 1
            or self.managed_paths[0].path != self.ownership.evidence_path
            or self.managed_paths[0].role != "receipt"
        ):
            raise ValueError("subscription mutation requires exact regular receipt evidence")
        return self


class RepositoryUpdateEffectReceipt(StrictModel):
    """Durable result of one stable-keyed update effect."""

    version: Literal[1] = 1
    effect: RepositoryUpdateEffect
    idempotency_key: str = Field(
        pattern=r"^repository-bootstrap-update-operation:sha256:[0-9a-f]{64}$"
    )
    mutation_performed: bool
    affected_paths: tuple[ManagedPath, ...] = ()
    grant_mutation: BootstrapGrantMutationReceipt | None = None
    subscription_mutation: BootstrapSubscriptionMutationReceipt | None = None

    @field_validator("affected_paths")
    @classmethod
    def unique_affected_paths(cls, value: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.path))
        if len({item.path for item in ordered}) != len(ordered):
            raise ValueError("update effect paths must be unique")
        return ordered

    @model_validator(mode="after")
    def state_mutation_matches_effect(self) -> RepositoryUpdateEffectReceipt:
        if self.grant_mutation is not None and (
            self.effect is not RepositoryUpdateEffect.TRUST
            or self.grant_mutation.operation_key != self.idempotency_key
        ):
            raise ValueError("grant mutation does not match its update effect")
        if self.subscription_mutation is not None and (
            self.effect is not RepositoryUpdateEffect.SUBSCRIPTION
            or self.subscription_mutation.operation_key != self.idempotency_key
            or self.subscription_mutation.managed_paths != self.affected_paths
        ):
            raise ValueError("subscription mutation does not match its update effect")
        if self.effect is not RepositoryUpdateEffect.TRUST and self.grant_mutation is not None:
            raise ValueError("only a trust effect may carry a grant mutation")
        if (
            self.effect is not RepositoryUpdateEffect.SUBSCRIPTION
            and self.subscription_mutation is not None
        ):
            raise ValueError("only a subscription effect may carry a subscription mutation")
        return self


_UPDATE_PHASE_EFFECT_COUNT = {
    RepositoryUpdatePhase.VERIFIED: 0,
    RepositoryUpdatePhase.TRUST_PENDING: 0,
    RepositoryUpdatePhase.TRUST_REPLACED: 1,
    RepositoryUpdatePhase.SUBSCRIPTION_PENDING: 1,
    RepositoryUpdatePhase.SUBSCRIPTION_REPLACED: 2,
    RepositoryUpdatePhase.ARTIFACTS_PENDING: 2,
    RepositoryUpdatePhase.ARTIFACTS_HYDRATED: 3,
    RepositoryUpdatePhase.GENERIC_SKILL_PENDING: 3,
    RepositoryUpdatePhase.GENERIC_SKILL_INSTALLED: 4,
    RepositoryUpdatePhase.CATALOG_SKILLS_PENDING: 4,
    RepositoryUpdatePhase.CATALOG_SKILLS_EXPORTED: 5,
    RepositoryUpdatePhase.AGENT_LINKS_PENDING: 5,
    RepositoryUpdatePhase.AGENT_LINKS_INSTALLED: 6,
    RepositoryUpdatePhase.OBSOLETE_PATHS_PENDING: 6,
    RepositoryUpdatePhase.OBSOLETE_PATHS_REMOVED: 7,
    RepositoryUpdatePhase.FINALIZING: 7,
}


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
    def absolute_worktree(
        cls, value: object, info: ValidationInfo
    ) -> Path | str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("current_worktree must be absolute")
        resolved = path.resolve(strict=False)
        return str(resolved) if info.mode == "json" else resolved

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
    def absolute_worktree(
        cls, value: object, info: ValidationInfo
    ) -> Path | str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("current_worktree must be absolute")
        resolved = path.resolve(strict=False)
        return str(resolved) if info.mode == "json" else resolved

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
    grant_ownership: BootstrapGrantOwnershipReceipt | None = None
    subscription_ownership: BootstrapSubscriptionOwnershipReceipt | None = None
    grant_mutation: BootstrapGrantMutationReceipt | None = None
    subscription_mutation: BootstrapSubscriptionMutationReceipt | None = None
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
        if self.grant_ownership is not None and (
            self.trust_grant is None
            or self.grant_ownership.grant_id != self.trust_grant.id
            or self.grant_ownership.bootstrap_name != self.request.name
        ):
            raise ValueError("grant ownership does not match bootstrap receipt")
        if self.subscription_ownership is not None and (
            self.subscription_ownership.bootstrap_name != self.request.name
            or (
                self.verified is not None
                and self.subscription_ownership.verified_commit
                != self.verified.commit_sha256
            )
        ):
            raise ValueError("subscription ownership does not match bootstrap receipt")
        if self.grant_mutation is not None and (
            self.grant_mutation.bootstrap_name != self.request.name
            or self.grant_mutation.ownership != self.grant_ownership
        ):
            raise ValueError("grant mutation does not match bootstrap ownership")
        if self.subscription_mutation is not None and (
            self.subscription_mutation.bootstrap_name != self.request.name
            or self.subscription_mutation.ownership != self.subscription_ownership
        ):
            raise ValueError("subscription mutation does not match bootstrap ownership")
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
    old_grant_ownership: BootstrapGrantOwnershipReceipt | None = None
    old_subscription_ownership: BootstrapSubscriptionOwnershipReceipt | None = None
    candidate_request: RepositoryBootstrapRequest
    candidate_verified: VerifiedRepositoryBootstrap
    candidate_grant: CapabilityGrant | None = None
    candidate_grant_ownership: BootstrapGrantOwnershipReceipt | None = None
    candidate_subscription_ownership: BootstrapSubscriptionOwnershipReceipt | None = None
    candidate_grant_mutation: BootstrapGrantMutationReceipt | None = None
    candidate_subscription_mutation: BootstrapSubscriptionMutationReceipt | None = None
    candidate_managed_paths: tuple[ManagedPath, ...] = ()
    effect_receipts: tuple[RepositoryUpdateEffectReceipt, ...] = ()
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
        if self.old_grant_ownership is not None and (
            self.old_grant is None
            or self.old_grant_ownership.grant_id != self.old_grant.id
            or self.old_grant_ownership.bootstrap_name != self.old_request.name
        ):
            raise ValueError("old grant ownership does not match update journal")
        if self.old_subscription_ownership is not None and (
            self.old_subscription_ownership.bootstrap_name != self.old_request.name
            or self.old_subscription_ownership.verified_commit
            != self.old_request.commit_sha256
        ):
            raise ValueError("old subscription ownership does not match update journal")
        if self.candidate_grant_ownership is not None and (
            self.candidate_grant is None
            or self.candidate_grant_ownership.grant_id != self.candidate_grant.id
            or self.candidate_grant_ownership.bootstrap_name
            != self.candidate_request.name
        ):
            raise ValueError("candidate grant ownership does not match update journal")
        if self.candidate_subscription_ownership is not None and (
            self.candidate_subscription_ownership.bootstrap_name
            != self.candidate_request.name
            or self.candidate_subscription_ownership.verified_commit
            != self.candidate_verified.commit_sha256
        ):
            raise ValueError("candidate subscription ownership does not match update journal")
        expected_effects = tuple(RepositoryUpdateEffect)[
            : _UPDATE_PHASE_EFFECT_COUNT[self.phase]
        ]
        if tuple(receipt.effect for receipt in self.effect_receipts) != expected_effects:
            raise ValueError("update journal effects do not match its phase")
        produced: dict[str, ManagedPath] = {}
        for receipt in self.effect_receipts:
            expected_key = repository_update_operation_id(
                old_receipt_sha256=self.old_receipt_sha256,
                candidate_request=self.candidate_request,
                candidate_verified=self.candidate_verified,
                effect=receipt.effect,
            )
            if receipt.idempotency_key != expected_key:
                raise ValueError("update effect idempotency key does not match its transaction")
            if receipt.effect is RepositoryUpdateEffect.TRUST:
                if receipt.affected_paths:
                    raise ValueError("trust effect cannot own managed paths")
                if receipt.mutation_performed != (self.old_grant != self.candidate_grant):
                    raise ValueError("trust effect does not match the grant replacement")
                if receipt.grant_mutation is not None:
                    expected_old = None if self.old_grant is None else self.old_grant.id
                    expected_new = (
                        None if self.candidate_grant is None else self.candidate_grant.id
                    )
                    if (
                        receipt.grant_mutation.old_grant_id != expected_old
                        or receipt.grant_mutation.new_grant_id != expected_new
                        or receipt.grant_mutation.ownership
                        != self.candidate_grant_ownership
                    ):
                        raise ValueError("grant update mutation has the wrong identities")
            elif receipt.effect is RepositoryUpdateEffect.OBSOLETE_PATHS:
                continue
            elif not receipt.mutation_performed:
                raise ValueError("completed update adapter must record its mutation")
            else:
                for item in receipt.affected_paths:
                    previous = produced.get(item.path)
                    if previous is not None and previous != item:
                        raise ValueError("update effects disagree about a managed path")
                    produced[item.path] = item
            if (
                receipt.effect is RepositoryUpdateEffect.SUBSCRIPTION
                and receipt.subscription_mutation is not None
                and receipt.subscription_mutation.ownership
                != self.candidate_subscription_ownership
            ):
                raise ValueError(
                    "subscription update mutation has the wrong ownership"
                )
        expected_paths = tuple(produced[path] for path in sorted(produced))
        if self.candidate_managed_paths != expected_paths:
            raise ValueError("candidate managed paths are not produced by update effects")
        obsolete_receipts = tuple(
            receipt
            for receipt in self.effect_receipts
            if receipt.effect is RepositoryUpdateEffect.OBSOLETE_PATHS
        )
        if obsolete_receipts:
            obsolete = tuple(
                item for item in self.old_managed_paths if item.path not in produced
            )
            receipt = obsolete_receipts[0]
            if receipt.affected_paths != obsolete:
                raise ValueError("obsolete-path effect does not match old ownership")
            if receipt.mutation_performed != bool(obsolete):
                raise ValueError("obsolete-path effect has an invalid mutation result")
        if self.candidate_grant_mutation is not None and (
            self.candidate_grant_mutation.ownership != self.candidate_grant_ownership
            or not any(
                receipt.grant_mutation == self.candidate_grant_mutation
                for receipt in self.effect_receipts
            )
        ):
            raise ValueError("candidate grant mutation is not recorded by an update effect")
        if self.candidate_subscription_mutation is not None and (
            self.candidate_subscription_mutation.ownership
            != self.candidate_subscription_ownership
            or not any(
                receipt.subscription_mutation
                == self.candidate_subscription_mutation
                for receipt in self.effect_receipts
            )
        ):
            raise ValueError(
                "candidate subscription mutation is not recorded by an update effect"
            )
        return self


def repository_update_operation_id(
    *,
    old_receipt_sha256: str,
    candidate_request: RepositoryBootstrapRequest,
    candidate_verified: VerifiedRepositoryBootstrap,
    effect: RepositoryUpdateEffect,
) -> str:
    """Return the stable semantic idempotency key for one update effect."""
    return content_id(
        "repository-bootstrap-update-operation",
        {
            "old_receipt_sha256": old_receipt_sha256,
            "candidate_request": candidate_request.model_dump(mode="json"),
            "candidate_verified": candidate_verified.id,
            "step": effect.value,
        },
    )


class RepositoryBootstrapService(Protocol):
    def install(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...

    def update(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...

    def remove(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt: ...
