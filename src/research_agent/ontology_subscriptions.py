"""Strict named ontology subscriptions and their bounded synchronization service."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from research_agent.bootstrap_models import (
    BootstrapConfigMutationReceipt,
    BootstrapSubscriptionMutationReceipt,
    BootstrapSubscriptionOwnershipReceipt,
    ManagedPath,
)
from research_agent.models import StrictModel, canonical_json
from research_agent.removal_journal import (
    RemovalJournal,
    RemovalPhase,
    confined_removal_path,
    delete_removal_journal,
    load_removal_journals,
    sync_removal_parent,
    verify_directory_identity,
    write_removal_journal,
)
from research_agent.repository_catalog import normalized_repository_identity

if TYPE_CHECKING:
    from research_agent.user_config import GeasProfile, UserConfigManager


_SUBSCRIPTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def validate_subscription_name(value: str) -> str:
    if not _SUBSCRIPTION_NAME.fullmatch(value):
        raise ValueError("subscription name is invalid")
    return value


def normalize_active_ref(value: str) -> str:
    raw = value
    if _OBJECT_ID.fullmatch(raw):
        normalized = raw.lower()
        if raw != normalized:
            raise ValueError("active_ref must exactly match its normalized value")
        return normalized
    if not raw.startswith(("refs/heads/", "refs/tags/")):
        raise ValueError("active_ref must use full branch/tag refs or commit IDs")
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or any(character in raw for character in (" ", "~", "^", ":", "?", "*", "[", "\\"))
        or ".." in raw
        or "@{" in raw
        or "//" in raw
        or raw.endswith(("/", "."))
    ):
        raise ValueError("active_ref is invalid")
    components = raw.split("/")
    if any(
        not component or component.startswith(".") or component.endswith(".lock")
        for component in components
    ):
        raise ValueError("active_ref is invalid")
    return raw


def _relative_path(value: object, *, label: str) -> Path:
    raw = str(value)
    if not raw or "\\" in raw:
        raise ValueError(f"{label} must be a normalized config-relative path")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != raw
    ):
        raise ValueError(f"{label} must be a normalized config-relative path")
    return Path(raw)


class OntologyFreshnessConfig(StrictModel):
    check_before_use: bool = True
    max_age_seconds: int = Field(default=3600, ge=60, le=604_800)
    hydrate_artifacts_before_use: bool = False


class OntologySubscription(StrictModel):
    url: str
    active_ref: str = "refs/heads/main"
    checkout: Path
    catalog: Path = Path("geas.yaml")
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pull_before_update: bool = False
    push_on_update: bool = False
    freshness: OntologyFreshnessConfig = Field(default_factory=OntologyFreshnessConfig)

    @field_validator("url")
    @classmethod
    def url_is_credential_free_remote(cls, value: str) -> str:
        normalized_repository_identity(value)
        return value

    @field_validator("active_ref", mode="before")
    @classmethod
    def ref_is_exact(cls, value: object) -> str:
        return normalize_active_ref(str(value))

    @field_validator("checkout", mode="before")
    @classmethod
    def checkout_is_confined(cls, value: object) -> Path:
        return _relative_path(value, label="subscription checkout")

    @field_validator("catalog", mode="before")
    @classmethod
    def catalog_is_confined(cls, value: object) -> Path:
        path = _relative_path(value, label="subscription catalog")
        if path.name != "geas.yaml":
            raise ValueError("subscription catalog filename must be geas.yaml")
        return path


class NormalizedProfile(StrictModel):
    subscriptions: dict[str, OntologySubscription]

    @field_validator("subscriptions", mode="before")
    @classmethod
    def subscriptions_are_sorted(cls, value: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("subscriptions must be a name-to-subscription mapping")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def names_and_checkouts_are_unique(self) -> NormalizedProfile:
        invalid = sorted(
            name for name in self.subscriptions if not _SUBSCRIPTION_NAME.fullmatch(name)
        )
        if invalid:
            raise ValueError(f"invalid subscription names: {', '.join(invalid)}")
        checkouts = tuple((name, item.checkout) for name, item in self.subscriptions.items())
        for index, (left_name, left) in enumerate(checkouts):
            for right_name, right in checkouts[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError(
                        "subscription checkouts overlap: "
                        f"{left_name!r}={left.as_posix()!r}, "
                        f"{right_name!r}={right.as_posix()!r}"
                    )
        return self


class SubscriptionSyncReceipt(StrictModel):
    name: str
    success: bool
    pull: dict[str, object] | None = None
    push: dict[str, object] | None = None
    error: str | None = None


class SubscriptionMutationReceipt(StrictModel):
    name: str
    checkout: Path
    subscribed: bool = False
    unsubscribed: bool = False
    checkout_created: bool = False
    checkout_removed: bool = False
    pull: dict[str, object] | None = None


class _BootstrapSubscriptionJournal(StrictModel):
    """Private write-ahead record for an exact subscription mutation."""

    version: Literal[1] = 1
    phase: Literal["prepared", "staged", "checkout_swapped", "config_committed", "completed"]
    operation_key: str = Field(
        pattern=(
            r"^repository-bootstrap-(?:operation|update-operation|removal-operation):"
            r"sha256:[0-9a-f]{64}$"
        )
    )
    profile_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    bootstrap_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    action: Literal["ensure", "replace", "remove"]
    owner_operation_key: str = Field(
        pattern=(
            r"^repository-bootstrap-(?:operation|update-operation|removal-operation):"
            r"sha256:[0-9a-f]{64}$"
        )
    )
    old_subscription_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    new_subscription_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    checkout: Path
    staging: Path
    quarantine: Path | None = None
    verified_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    old_checkout_device: int | None = Field(default=None, ge=0)
    old_checkout_inode: int | None = Field(default=None, gt=0)
    new_checkout_device: int | None = Field(default=None, ge=0)
    new_checkout_inode: int | None = Field(default=None, gt=0)
    before_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt: BootstrapSubscriptionMutationReceipt | None = None

    @model_validator(mode="after")
    def receipt_matches_completion(self) -> _BootstrapSubscriptionJournal:
        if (self.old_checkout_device is None) != (self.old_checkout_inode is None):
            raise ValueError("old checkout identity must be complete")
        if (self.new_checkout_device is None) != (self.new_checkout_inode is None):
            raise ValueError("new checkout identity must be complete")
        if self.phase != "prepared" and self.action != "remove" and self.new_checkout_inode is None:
            raise ValueError("staged subscription journal requires checkout identity")
        if self.action == "ensure" and (
            self.old_subscription_sha256 is not None
            or self.old_checkout_inode is not None
            or self.quarantine is not None
        ):
            raise ValueError("subscription ensure journal cannot claim prior ownership")
        if self.action == "replace" and (
            self.old_subscription_sha256 is None
            or self.old_checkout_inode is None
            or self.quarantine is None
        ):
            raise ValueError("subscription replacement journal requires prior ownership")
        if self.action == "remove" and (
            self.old_subscription_sha256 is None
            or self.old_checkout_inode is None
            or self.new_subscription_sha256 is not None
            or self.new_checkout_inode is not None
            or self.quarantine is None
        ):
            raise ValueError("subscription removal journal requires exact prior ownership")
        if (self.phase == "completed") != (self.receipt is not None):
            raise ValueError("subscription journal completion and receipt must agree")
        if self.receipt is not None and (
            self.receipt.operation_key != self.operation_key
            or self.receipt.profile_name != self.profile_name
            or self.receipt.bootstrap_name != self.bootstrap_name
            or self.receipt.action != self.action
            or self.receipt.old_subscription_sha256
            != self.old_subscription_sha256
            or self.receipt.new_subscription_sha256
            != self.new_subscription_sha256
        ):
            raise ValueError("subscription journal receipt does not match its intent")
        return self


def _subscription_identity_sha256(subscription: OntologySubscription) -> str:
    return hashlib.sha256(
        canonical_json(subscription.model_dump(mode="json"))
    ).hexdigest()


def _write_subscription_removal_journal(
    manager: UserConfigManager,
    journal: RemovalJournal,
) -> None:
    if journal.kind != "subscription":
        raise ValueError("subscription removal received the wrong journal kind")
    write_removal_journal(manager.root, journal)


def recover_subscription_removals(config_manager: UserConfigManager) -> None:
    """Restore or finish exact checkout removals from durable journals."""
    for journal in load_removal_journals(
        config_manager.root,
        kind="subscription",
    ):
        config = config_manager.load()
        referenced = False
        for profile in config.profiles.values():
            normalized = profile.normalized_subscriptions(
                freshness=config.ontology_freshness
            )
            for subscription in normalized.values():
                candidate = subscription.checkout
                if (
                    candidate != journal.target
                    and not candidate.is_relative_to(journal.target)
                    and not journal.target.is_relative_to(candidate)
                ):
                    continue
                if candidate != journal.target:
                    raise ValueError(
                        "subscription removal journal overlaps configured checkout"
                    )
                if _subscription_identity_sha256(subscription) != journal.subscription_sha256:
                    raise ValueError(
                        "subscription removal journal conflicts with configured identity"
                    )
                referenced = True

        target = confined_removal_path(config_manager.root, journal.target)
        quarantine = confined_removal_path(config_manager.root, journal.quarantine)
        target_exists = target.exists()
        quarantine_exists = quarantine.exists()
        if target_exists and quarantine_exists:
            raise ValueError("subscription removal target and quarantine both exist")

        if referenced:
            if quarantine_exists:
                verify_directory_identity(quarantine, journal)
                os.replace(quarantine, target)
                sync_removal_parent(config_manager.root, journal.target)
                verify_directory_identity(target, journal)
            elif target_exists:
                verify_directory_identity(target, journal)
            else:
                raise ValueError("registered subscription checkout is missing")
        else:
            if target_exists:
                verify_directory_identity(target, journal)
                os.replace(target, quarantine)
                sync_removal_parent(config_manager.root, journal.quarantine)
                verify_directory_identity(quarantine, journal)
                quarantine_exists = True
            if quarantine_exists:
                verify_directory_identity(quarantine, journal)
                shutil.rmtree(quarantine)
                sync_removal_parent(config_manager.root, journal.quarantine)
        delete_removal_journal(config_manager.root, journal)


class CatalogVerifier(Protocol):
    def __call__(self, catalog_path: Path) -> object: ...


class CatalogAuthorizer(Protocol):
    def __call__(self, verified_catalog: object) -> object: ...


class RepositoryOperator(Protocol):
    def pull(self) -> dict[str, object]: ...

    def assert_removable(self) -> None: ...


class SubscriptionManager:
    """Coordinate named operations without granting configuration trust authority."""

    def __init__(
        self,
        *,
        config_manager: UserConfigManager,
        profile_name: str,
        catalog_verifier: CatalogVerifier,
        authorizer: CatalogAuthorizer,
        repository_factory: (
            Callable[[Path, OntologySubscription], RepositoryOperator] | None
        ) = None,
    ) -> None:
        self.config_manager = config_manager
        if not _SUBSCRIPTION_NAME.fullmatch(profile_name):
            raise ValueError("subscription profile name is invalid")
        self.profile_name = profile_name
        self.catalog_verifier = catalog_verifier
        self.authorizer = authorizer
        self.repository_factory = repository_factory

    def ensure_bootstrap_subscription(
        self,
        name: str,
        subscription: OntologySubscription,
        *,
        operation_key: str,
        verified_commit: str,
    ) -> BootstrapSubscriptionMutationReceipt:
        """Create only an absent fixed bootstrap subscription under a stable key."""
        self._recover_all_removals()
        validate_subscription_name(name)
        self._validate_bootstrap_operation(
            operation_key, name, "subscription_ensure"
        )
        validated = OntologySubscription.model_validate(subscription.model_dump(mode="python"))
        self._validate_bootstrap_checkout(name, validated)
        if not re.fullmatch(r"[0-9a-f]{40}", verified_commit):
            raise ValueError("verified bootstrap commit must be a full SHA-1 object ID")
        journal_path = self._bootstrap_subscription_journal_path(name, operation_key)
        journal = self._load_bootstrap_subscription_journal(journal_path)
        expected_digest = _subscription_identity_sha256(validated)
        if journal is not None:
            self._validate_bootstrap_subscription_journal(
                journal,
                operation_key=operation_key,
                name=name,
                action="ensure",
                owner_operation_key=operation_key,
                old_subscription_sha256=None,
                new_subscription_sha256=expected_digest,
                checkout=validated.checkout,
                verified_commit=verified_commit,
            )
            return self._resume_bootstrap_subscription_ensure(
                journal_path, journal, validated
            )

        config_bytes = self.config_manager.path.read_bytes()
        config = self.config_manager.load()
        _, profile = config.profile(self.profile_name)
        if name in profile.normalized_subscriptions(freshness=config.ontology_freshness):
            raise ValueError("refusing to adopt a pre-existing ontology subscription")
        destination = self.config_manager.subscription_checkout(validated)
        if destination.exists() or destination.is_symlink():
            raise ValueError("refusing to adopt a pre-existing subscription checkout")
        digest = operation_key.rsplit(":", 1)[-1]
        staging = destination.with_name(f".{destination.name}.bootstrap-{digest}.stage")
        if staging.exists() or staging.is_symlink():
            raise ValueError("bootstrap subscription staging path already exists")

        def add_subscription(current: GeasProfile) -> GeasProfile:
            if name in current.normalized_subscriptions(freshness=config.ontology_freshness):
                raise ValueError("refusing to adopt a pre-existing ontology subscription")
            return current.model_copy(
                update={"subscriptions": {**current.subscriptions, name: validated}}
            )

        _updated, after = self.config_manager._profile_mutation_bytes(
            config,
            profile_name=self.profile_name,
            mutate=add_subscription,
            upgrade_version=config.version == 2,
        )
        journal = _BootstrapSubscriptionJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=self.profile_name,
            bootstrap_name=name,
            action="ensure",
            owner_operation_key=operation_key,
            old_subscription_sha256=None,
            new_subscription_sha256=expected_digest,
            checkout=validated.checkout,
            staging=staging.relative_to(self.config_manager.root),
            verified_commit=verified_commit,
            before_config_sha256=hashlib.sha256(config_bytes).hexdigest(),
            after_config_sha256=hashlib.sha256(after).hexdigest(),
        )
        self.config_manager._write_bootstrap_state(journal_path, journal)
        return self._resume_bootstrap_subscription_ensure(journal_path, journal, validated)

    def _resume_bootstrap_subscription_ensure(
        self,
        journal_path: Path,
        journal: _BootstrapSubscriptionJournal,
        subscription: OntologySubscription,
    ) -> BootstrapSubscriptionMutationReceipt:
        if journal.phase == "completed":
            assert journal.receipt is not None
            self._assert_live_subscription_ownership(journal.receipt)
            return journal.receipt
        destination = self.config_manager.subscription_checkout(subscription)
        staging = self.config_manager._confined_state_path(journal.staging)
        current_sha256 = self.config_manager.config_sha256()
        if current_sha256 == journal.after_config_sha256 and (
            journal.before_config_sha256 != journal.after_config_sha256
            or journal.phase == "config_committed"
        ):
            self._assert_named_subscription(
                journal.bootstrap_name, journal.new_subscription_sha256
            )
            self._assert_checkout_identity(
                destination,
                device=journal.new_checkout_device,
                inode=journal.new_checkout_inode,
                label="owned bootstrap subscription checkout",
            )
            mutation = self._subscription_config_receipt(journal, "subscription_ensure")
            return self._finalize_bootstrap_subscription(
                journal_path, journal, mutation, destination
            )
        if current_sha256 != journal.before_config_sha256:
            raise RuntimeError("Geas user config changed during subscription recovery")
        config = self.config_manager.load()
        _, profile = config.profile(self.profile_name)
        if journal.bootstrap_name in profile.normalized_subscriptions(
            freshness=config.ontology_freshness
        ):
            raise ValueError("refusing to adopt a pre-existing ontology subscription")
        if journal.phase == "prepared":
            if destination.exists() or destination.is_symlink():
                raise ValueError("refusing to adopt a pre-existing subscription checkout")
            if staging.exists() or staging.is_symlink():
                raise ValueError("unowned bootstrap subscription staging path exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            repository = self._repository(staging, subscription)
            try:
                pull_receipt = repository.pull()
                if pull_receipt.get("commit") != journal.verified_commit:
                    raise ValueError(
                        "bootstrap checkout commit does not match verified identity"
                    )
                self._verify_repository_identity(
                    staging,
                    subscription,
                    expected_commit=journal.verified_commit,
                )
                identity = staging.stat(follow_symlinks=False)
                journal = journal.model_copy(
                    update={
                        "phase": "staged",
                        "new_checkout_device": identity.st_dev,
                        "new_checkout_inode": identity.st_ino,
                    }
                )
                self.config_manager._write_bootstrap_state(journal_path, journal)
            except BaseException:
                self._remove_exact_checkout(staging)
                raise
        if journal.phase == "staged":
            if destination.exists() or destination.is_symlink():
                if staging.exists() or staging.is_symlink():
                    raise ValueError(
                        "subscription checkout destination appeared during staging"
                    )
                self._assert_checkout_identity(
                    destination,
                    device=journal.new_checkout_device,
                    inode=journal.new_checkout_inode,
                    label="swapped bootstrap subscription checkout",
                )
                self._verify_repository_identity(
                    destination,
                    subscription,
                    expected_commit=journal.verified_commit,
                )
            else:
                self._assert_checkout_identity(
                    staging,
                    device=journal.new_checkout_device,
                    inode=journal.new_checkout_inode,
                    label="staged bootstrap subscription checkout",
                )
                self._verify_repository_identity(
                    staging,
                    subscription,
                    expected_commit=journal.verified_commit,
                )
                os.replace(staging, destination)
                sync_removal_parent(self.config_manager.root, journal.checkout)
            journal = journal.model_copy(update={"phase": "checkout_swapped"})
            self.config_manager._write_bootstrap_state(journal_path, journal)
        if journal.phase == "checkout_swapped":
            self._assert_checkout_identity(
                destination,
                device=journal.new_checkout_device,
                inode=journal.new_checkout_inode,
                label="swapped bootstrap subscription checkout",
            )
            self._verify_repository_identity(
                destination,
                subscription,
                expected_commit=journal.verified_commit,
            )

            def add_subscription(profile: GeasProfile) -> GeasProfile:
                if journal.bootstrap_name in profile.normalized_subscriptions(
                    freshness=config.ontology_freshness
                ):
                    raise ValueError("refusing to adopt a pre-existing ontology subscription")
                return profile.model_copy(
                    update={
                        "subscriptions": {
                            **profile.subscriptions,
                            journal.bootstrap_name: subscription,
                        }
                    }
                )

            try:
                mutation = self.config_manager.mutate_profile_expected(
                    operation_key=journal.operation_key,
                    profile_name=journal.profile_name,
                    bootstrap_name=journal.bootstrap_name,
                    kind="subscription_ensure",
                    expected_config_sha256=journal.before_config_sha256,
                    mutate=add_subscription,
                    upgrade_version=config.version == 2,
                )
                if mutation.after_config_sha256 != journal.after_config_sha256:
                    raise RuntimeError("bootstrap subscription config identity changed")
            except BaseException:
                self._assert_checkout_identity(
                    destination,
                    device=journal.new_checkout_device,
                    inode=journal.new_checkout_inode,
                    label="swapped bootstrap subscription checkout",
                )
                os.replace(destination, staging)
                sync_removal_parent(self.config_manager.root, journal.staging)
                journal = journal.model_copy(update={"phase": "staged"})
                self.config_manager._write_bootstrap_state(journal_path, journal)
                raise
            journal = journal.model_copy(update={"phase": "config_committed"})
            self.config_manager._write_bootstrap_state(journal_path, journal)
        else:
            mutation = self._subscription_config_receipt(
                journal, "subscription_ensure"
            )
        return self._finalize_bootstrap_subscription(
            journal_path, journal, mutation, destination
        )

    def replace_bootstrap_subscription(
        self,
        name: str,
        old_subscription: OntologySubscription,
        candidate_subscription: OntologySubscription,
        *,
        operation_key: str,
        verified_commit: str,
        ownership: BootstrapSubscriptionOwnershipReceipt,
    ) -> BootstrapSubscriptionMutationReceipt:
        """Replace one exact owned subscription with a staged fixed-checkout clone."""
        self._recover_all_removals()
        validate_subscription_name(name)
        self._validate_bootstrap_operation(
            operation_key, name, "subscription_replace"
        )
        old = OntologySubscription.model_validate(
            old_subscription.model_dump(mode="python")
        )
        candidate = OntologySubscription.model_validate(
            candidate_subscription.model_dump(mode="python")
        )
        self._validate_bootstrap_checkout(name, old)
        self._validate_bootstrap_checkout(name, candidate)
        if not re.fullmatch(r"[0-9a-f]{40}", verified_commit):
            raise ValueError("verified bootstrap commit must be a full SHA-1 object ID")
        old_digest = _subscription_identity_sha256(old)
        candidate_digest = _subscription_identity_sha256(candidate)
        if (
            ownership.profile_name != self.profile_name
            or ownership.bootstrap_name != name
            or ownership.subscription_sha256 != old_digest
            or ownership.checkout != old.checkout.as_posix()
        ):
            raise ValueError("bootstrap subscription ownership does not match exact old state")
        journal_path = self._bootstrap_subscription_journal_path(name, operation_key)
        journal = self._load_bootstrap_subscription_journal(journal_path)
        if journal is not None:
            self._validate_bootstrap_subscription_journal(
                journal,
                operation_key=operation_key,
                name=name,
                action="replace",
                owner_operation_key=ownership.owner_operation_key,
                old_subscription_sha256=old_digest,
                new_subscription_sha256=candidate_digest,
                checkout=candidate.checkout,
                verified_commit=verified_commit,
            )
            return self._resume_bootstrap_subscription_replace(
                journal_path,
                journal,
                ownership,
                old,
                candidate,
            )

        self._assert_subscription_ownership(ownership, old)
        destination = self.config_manager.subscription_checkout(old)
        before, config = self.config_manager._validated_config_bytes()

        def replace_subscription(profile: GeasProfile) -> GeasProfile:
            normalized = profile.normalized_subscriptions(
                freshness=config.ontology_freshness
            )
            current = normalized.get(name)
            if current is None or _subscription_identity_sha256(current) != old_digest:
                raise ValueError("owned bootstrap subscription config identity changed")
            return profile.model_copy(
                update={"subscriptions": {**profile.subscriptions, name: candidate}}
            )

        _updated, after = self.config_manager._profile_mutation_bytes(
            config,
            profile_name=self.profile_name,
            mutate=replace_subscription,
            upgrade_version=config.version == 2,
        )
        operation_digest = operation_key.rsplit(":", 1)[-1]
        staging = destination.with_name(
            f".{destination.name}.bootstrap-{operation_digest}.stage"
        )
        quarantine = destination.with_name(
            f".{destination.name}.bootstrap-{operation_digest}.old"
        )
        if (
            staging.exists()
            or staging.is_symlink()
            or quarantine.exists()
            or quarantine.is_symlink()
        ):
            raise ValueError("bootstrap subscription replacement workspace already exists")
        identity = destination.stat(follow_symlinks=False)
        journal = _BootstrapSubscriptionJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=self.profile_name,
            bootstrap_name=name,
            action="replace",
            owner_operation_key=ownership.owner_operation_key,
            old_subscription_sha256=old_digest,
            new_subscription_sha256=candidate_digest,
            checkout=candidate.checkout,
            staging=staging.relative_to(self.config_manager.root),
            quarantine=quarantine.relative_to(self.config_manager.root),
            verified_commit=verified_commit,
            old_checkout_device=identity.st_dev,
            old_checkout_inode=identity.st_ino,
            before_config_sha256=hashlib.sha256(before).hexdigest(),
            after_config_sha256=hashlib.sha256(after).hexdigest(),
        )
        self.config_manager._write_bootstrap_state(journal_path, journal)
        return self._resume_bootstrap_subscription_replace(
            journal_path,
            journal,
            ownership,
            old,
            candidate,
        )

    def _resume_bootstrap_subscription_replace(
        self,
        journal_path: Path,
        journal: _BootstrapSubscriptionJournal,
        prior_ownership: BootstrapSubscriptionOwnershipReceipt,
        old_subscription: OntologySubscription,
        candidate: OntologySubscription,
    ) -> BootstrapSubscriptionMutationReceipt:
        if journal.phase == "completed":
            assert journal.receipt is not None
            self._assert_live_subscription_ownership(journal.receipt)
            return journal.receipt
        destination = self.config_manager.subscription_checkout(candidate)
        staging = self.config_manager._confined_state_path(journal.staging)
        if journal.quarantine is None:
            raise ValueError("bootstrap subscription replacement quarantine is missing")
        quarantine = self.config_manager._confined_state_path(journal.quarantine)
        current_sha256 = self.config_manager.config_sha256()
        if current_sha256 == journal.after_config_sha256 and (
            journal.before_config_sha256 != journal.after_config_sha256
            or journal.phase == "config_committed"
        ):
            self._assert_named_subscription(
                journal.bootstrap_name, journal.new_subscription_sha256
            )
            self._assert_checkout_identity(
                destination,
                device=journal.new_checkout_device,
                inode=journal.new_checkout_inode,
                label="replacement bootstrap subscription checkout",
            )
            self._verify_repository_identity(
                destination,
                candidate,
                expected_commit=journal.verified_commit,
            )
            self._remove_replaced_checkout(
                journal,
                quarantine,
                old_subscription,
                prior_ownership.verified_commit,
            )
            mutation = self._subscription_config_receipt(
                journal, "subscription_replace"
            )
            return self._finalize_bootstrap_subscription(
                journal_path, journal, mutation, destination
            )
        if current_sha256 != journal.before_config_sha256:
            raise RuntimeError("Geas user config changed during subscription replacement")
        self._assert_named_subscription(
            journal.bootstrap_name, journal.old_subscription_sha256
        )
        if journal.phase == "prepared":
            self._assert_subscription_ownership(prior_ownership, old_subscription)
            if (
                staging.exists()
                or staging.is_symlink()
                or quarantine.exists()
                or quarantine.is_symlink()
            ):
                raise ValueError("unowned bootstrap replacement workspace exists")
            repository = self._repository(staging, candidate)
            try:
                pull_receipt = repository.pull()
                if pull_receipt.get("commit") != journal.verified_commit:
                    raise ValueError(
                        "bootstrap checkout commit does not match verified identity"
                    )
                self._verify_repository_identity(
                    staging,
                    candidate,
                    expected_commit=journal.verified_commit,
                )
                identity = staging.stat(follow_symlinks=False)
                journal = journal.model_copy(
                    update={
                        "phase": "staged",
                        "new_checkout_device": identity.st_dev,
                        "new_checkout_inode": identity.st_ino,
                    }
                )
                self.config_manager._write_bootstrap_state(journal_path, journal)
            except BaseException:
                self._remove_exact_checkout(staging)
                raise
        if journal.phase == "staged":
            self._assert_named_subscription(
                journal.bootstrap_name, journal.old_subscription_sha256
            )
            if quarantine.exists() or quarantine.is_symlink():
                self._assert_subscription_ownership(
                    prior_ownership,
                    old_subscription,
                    checkout_override=quarantine,
                )
            else:
                self._assert_subscription_ownership(prior_ownership, old_subscription)
                os.replace(destination, quarantine)
                sync_removal_parent(self.config_manager.root, journal.quarantine)
            if destination.exists() or destination.is_symlink():
                if staging.exists() or staging.is_symlink():
                    raise ValueError("replacement checkout and staging path both exist")
                self._assert_checkout_identity(
                    destination,
                    device=journal.new_checkout_device,
                    inode=journal.new_checkout_inode,
                    label="replacement bootstrap subscription checkout",
                )
                self._verify_repository_identity(
                    destination,
                    candidate,
                    expected_commit=journal.verified_commit,
                )
            else:
                self._assert_checkout_identity(
                    staging,
                    device=journal.new_checkout_device,
                    inode=journal.new_checkout_inode,
                    label="staged replacement bootstrap subscription checkout",
                )
                self._verify_repository_identity(
                    staging,
                    candidate,
                    expected_commit=journal.verified_commit,
                )
                os.replace(staging, destination)
                sync_removal_parent(self.config_manager.root, journal.checkout)
            journal = journal.model_copy(update={"phase": "checkout_swapped"})
            self.config_manager._write_bootstrap_state(journal_path, journal)
        if journal.phase == "checkout_swapped":
            self._assert_checkout_identity(
                destination,
                device=journal.new_checkout_device,
                inode=journal.new_checkout_inode,
                label="replacement bootstrap subscription checkout",
            )
            self._verify_repository_identity(
                destination,
                candidate,
                expected_commit=journal.verified_commit,
            )
            self._assert_checkout_identity(
                quarantine,
                device=journal.old_checkout_device,
                inode=journal.old_checkout_inode,
                label="quarantined old bootstrap subscription checkout",
            )
            self._verify_repository_identity(
                quarantine,
                old_subscription,
                expected_commit=prior_ownership.verified_commit,
            )
            config = self.config_manager.load()

            def replace_subscription(profile: GeasProfile) -> GeasProfile:
                normalized = profile.normalized_subscriptions(
                    freshness=config.ontology_freshness
                )
                current = normalized.get(journal.bootstrap_name)
                if (
                    current is None
                    or _subscription_identity_sha256(current)
                    != journal.old_subscription_sha256
                ):
                    raise ValueError(
                        "owned bootstrap subscription config identity changed"
                    )
                return profile.model_copy(
                    update={
                        "subscriptions": {
                            **profile.subscriptions,
                            journal.bootstrap_name: candidate,
                        }
                    }
                )

            try:
                mutation = self.config_manager.mutate_profile_expected(
                    operation_key=journal.operation_key,
                    profile_name=journal.profile_name,
                    bootstrap_name=journal.bootstrap_name,
                    kind="subscription_replace",
                    expected_config_sha256=journal.before_config_sha256,
                    mutate=replace_subscription,
                    upgrade_version=config.version == 2,
                )
                if mutation.after_config_sha256 != journal.after_config_sha256:
                    raise RuntimeError(
                        "bootstrap subscription replacement config identity changed"
                    )
            except BaseException:
                if self.config_manager.config_sha256() == journal.before_config_sha256:
                    self._rollback_subscription_replacement(
                        journal,
                        destination,
                        staging,
                        quarantine,
                        old_subscription,
                        candidate,
                        prior_ownership.verified_commit,
                    )
                    journal = journal.model_copy(update={"phase": "staged"})
                    self.config_manager._write_bootstrap_state(journal_path, journal)
                raise
            journal = journal.model_copy(update={"phase": "config_committed"})
            self.config_manager._write_bootstrap_state(journal_path, journal)
        else:
            mutation = self._subscription_config_receipt(
                journal, "subscription_replace"
            )
        self._remove_replaced_checkout(
            journal,
            quarantine,
            old_subscription,
            prior_ownership.verified_commit,
        )
        return self._finalize_bootstrap_subscription(
            journal_path, journal, mutation, destination
        )

    def _rollback_subscription_replacement(
        self,
        journal: _BootstrapSubscriptionJournal,
        destination: Path,
        staging: Path,
        quarantine: Path,
        old_subscription: OntologySubscription,
        candidate: OntologySubscription,
        old_verified_commit: str,
    ) -> None:
        self._assert_checkout_identity(
            destination,
            device=journal.new_checkout_device,
            inode=journal.new_checkout_inode,
            label="replacement bootstrap subscription checkout",
        )
        self._verify_repository_identity(
            destination,
            candidate,
            expected_commit=journal.verified_commit,
        )
        self._assert_checkout_identity(
            quarantine,
            device=journal.old_checkout_device,
            inode=journal.old_checkout_inode,
            label="quarantined old bootstrap subscription checkout",
        )
        self._verify_repository_identity(
            quarantine,
            old_subscription,
            expected_commit=old_verified_commit,
        )
        os.replace(destination, staging)
        os.replace(quarantine, destination)
        sync_removal_parent(self.config_manager.root, journal.checkout)

    def _remove_replaced_checkout(
        self,
        journal: _BootstrapSubscriptionJournal,
        quarantine: Path,
        old_subscription: OntologySubscription,
        old_verified_commit: str,
    ) -> None:
        if not (quarantine.exists() or quarantine.is_symlink()):
            return
        self._assert_checkout_identity(
            quarantine,
            device=journal.old_checkout_device,
            inode=journal.old_checkout_inode,
            label="quarantined old bootstrap subscription checkout",
        )
        self._verify_repository_identity(
            quarantine,
            old_subscription,
            expected_commit=old_verified_commit,
        )
        shutil.rmtree(quarantine)
        assert journal.quarantine is not None
        sync_removal_parent(self.config_manager.root, journal.quarantine)

    def remove_bootstrap_subscription(
        self,
        name: str,
        subscription: OntologySubscription,
        *,
        operation_key: str,
        ownership: BootstrapSubscriptionOwnershipReceipt,
    ) -> BootstrapSubscriptionMutationReceipt:
        """Remove only one exact owned declaration, checkout, and receipt leaf."""
        validate_subscription_name(name)
        self._validate_bootstrap_operation(
            operation_key, name, "subscription_remove"
        )
        validated = OntologySubscription.model_validate(
            subscription.model_dump(mode="python")
        )
        self._validate_bootstrap_checkout(name, validated)
        old_digest = _subscription_identity_sha256(validated)
        if (
            ownership.profile_name != self.profile_name
            or ownership.bootstrap_name != name
            or ownership.subscription_sha256 != old_digest
            or ownership.checkout != validated.checkout.as_posix()
        ):
            raise ValueError("bootstrap subscription ownership does not match removal")
        journal_path = self._bootstrap_subscription_journal_path(name, operation_key)
        journal = self._load_bootstrap_subscription_journal(journal_path)
        if journal is not None:
            self._validate_bootstrap_subscription_journal(
                journal,
                operation_key=operation_key,
                name=name,
                action="remove",
                owner_operation_key=ownership.owner_operation_key,
                old_subscription_sha256=old_digest,
                new_subscription_sha256=None,
                checkout=validated.checkout,
                verified_commit=ownership.verified_commit,
            )
            return self._resume_bootstrap_subscription_remove(
                journal_path, journal, ownership, validated
            )

        self._assert_subscription_ownership(ownership, validated)
        destination = self.config_manager.subscription_checkout(validated)
        config_bytes, config = self.config_manager._validated_config_bytes()
        self.config_manager.validate_subscription_removal(
            config,
            profile_name=self.profile_name,
            subscription_name=name,
            expected_checkout=destination,
        )

        def remove_subscription(profile: GeasProfile) -> GeasProfile:
            normalized = profile.normalized_subscriptions(
                freshness=config.ontology_freshness
            )
            current = normalized.get(name)
            if current is None or _subscription_identity_sha256(current) != old_digest:
                raise ValueError("owned bootstrap subscription config identity changed")
            explicit = dict(profile.subscriptions)
            explicit.pop(name, None)
            updates: dict[str, object] = {"subscriptions": explicit}
            if name == "primary":
                updates["ontology_git"] = None
            return profile.model_copy(update=updates)

        _updated, after = self.config_manager._profile_mutation_bytes(
            config,
            profile_name=self.profile_name,
            mutate=remove_subscription,
            upgrade_version=config.version == 2,
        )
        identity = destination.stat(follow_symlinks=False)
        operation_digest = operation_key.rsplit(":", 1)[-1]
        quarantine = destination.with_name(
            f".{destination.name}.remove-bootstrap-{operation_digest}"
        )
        if quarantine.exists() or quarantine.is_symlink():
            raise ValueError("bootstrap subscription removal quarantine already exists")
        journal = _BootstrapSubscriptionJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=self.profile_name,
            bootstrap_name=name,
            action="remove",
            owner_operation_key=ownership.owner_operation_key,
            old_subscription_sha256=old_digest,
            new_subscription_sha256=None,
            checkout=validated.checkout,
            staging=validated.checkout,
            quarantine=quarantine.relative_to(self.config_manager.root),
            verified_commit=ownership.verified_commit,
            old_checkout_device=identity.st_dev,
            old_checkout_inode=identity.st_ino,
            before_config_sha256=hashlib.sha256(config_bytes).hexdigest(),
            after_config_sha256=hashlib.sha256(after).hexdigest(),
        )
        self.config_manager._write_bootstrap_state(journal_path, journal)
        return self._resume_bootstrap_subscription_remove(
            journal_path, journal, ownership, validated
        )

    def _resume_bootstrap_subscription_remove(
        self,
        journal_path: Path,
        journal: _BootstrapSubscriptionJournal,
        ownership: BootstrapSubscriptionOwnershipReceipt,
        subscription: OntologySubscription,
    ) -> BootstrapSubscriptionMutationReceipt:
        destination = self.config_manager.subscription_checkout(subscription)
        if journal.quarantine is None:
            raise ValueError("bootstrap subscription removal quarantine is missing")
        quarantine = self.config_manager._confined_state_path(journal.quarantine)
        evidence_path = self.config_manager._confined_state_path(
            Path(ownership.evidence_path)
        )
        if journal.phase == "completed":
            assert journal.receipt is not None
            self._assert_named_subscription_absent(journal.bootstrap_name)
            if destination.exists() or destination.is_symlink():
                raise ValueError("removed bootstrap subscription checkout reappeared")
            if quarantine.exists() or quarantine.is_symlink():
                raise ValueError("removed bootstrap subscription quarantine reappeared")
            if evidence_path.exists() or evidence_path.is_symlink():
                raise ValueError("removed bootstrap subscription evidence reappeared")
            return journal.receipt
        current_sha256 = self.config_manager.config_sha256()
        if current_sha256 not in {
            journal.before_config_sha256,
            journal.after_config_sha256,
        }:
            raise RuntimeError("Geas user config changed during subscription removal")

        if journal.phase == "prepared":
            target_exists = destination.exists() or destination.is_symlink()
            quarantine_exists = quarantine.exists() or quarantine.is_symlink()
            if target_exists and quarantine_exists:
                raise ValueError(
                    "bootstrap subscription removal target and quarantine both exist"
                )
            if current_sha256 == journal.after_config_sha256:
                if target_exists or not quarantine_exists:
                    raise ValueError(
                        "bootstrap subscription removal state lacks its owned quarantine"
                    )
                self._assert_quarantined_bootstrap_subscription(
                    journal,
                    ownership,
                    subscription,
                    quarantine,
                    configured=False,
                )
                journal = journal.model_copy(update={"phase": "config_committed"})
                self.config_manager._write_bootstrap_state(journal_path, journal)
            else:
                if target_exists:
                    self._assert_subscription_ownership(ownership, subscription)
                    config = self.config_manager.load()
                    self.config_manager.validate_subscription_removal(
                        config,
                        profile_name=journal.profile_name,
                        subscription_name=journal.bootstrap_name,
                        expected_checkout=destination,
                    )
                    self._verify_repository_identity(
                        destination,
                        subscription,
                        expected_commit=ownership.verified_commit,
                    )
                    self._assert_checkout_identity(
                        destination,
                        device=journal.old_checkout_device,
                        inode=journal.old_checkout_inode,
                        label="owned bootstrap subscription checkout",
                    )
                    os.replace(destination, quarantine)
                    sync_removal_parent(self.config_manager.root, journal.quarantine)
                elif quarantine_exists:
                    self._assert_quarantined_bootstrap_subscription(
                        journal,
                        ownership,
                        subscription,
                        quarantine,
                        configured=True,
                    )
                else:
                    raise ValueError("owned bootstrap subscription checkout is missing")
                journal = journal.model_copy(update={"phase": "staged"})
                self.config_manager._write_bootstrap_state(journal_path, journal)

        if journal.phase == "staged":
            current_sha256 = self.config_manager.config_sha256()
            if current_sha256 == journal.before_config_sha256:
                self._assert_quarantined_bootstrap_subscription(
                    journal,
                    ownership,
                    subscription,
                    quarantine,
                    configured=True,
                )
                config = self.config_manager.load()

                def remove_subscription(profile: GeasProfile) -> GeasProfile:
                    normalized = profile.normalized_subscriptions(
                        freshness=config.ontology_freshness
                    )
                    current = normalized.get(journal.bootstrap_name)
                    if (
                        current is None
                        or _subscription_identity_sha256(current)
                        != journal.old_subscription_sha256
                    ):
                        raise ValueError(
                            "owned bootstrap subscription config identity changed"
                        )
                    explicit = dict(profile.subscriptions)
                    explicit.pop(journal.bootstrap_name, None)
                    updates: dict[str, object] = {"subscriptions": explicit}
                    if journal.bootstrap_name == "primary":
                        updates["ontology_git"] = None
                    return profile.model_copy(update=updates)

                try:
                    mutation = self.config_manager.mutate_profile_expected(
                        operation_key=journal.operation_key,
                        profile_name=journal.profile_name,
                        bootstrap_name=journal.bootstrap_name,
                        kind="subscription_remove",
                        expected_config_sha256=journal.before_config_sha256,
                        mutate=remove_subscription,
                        upgrade_version=config.version == 2,
                    )
                    if mutation.after_config_sha256 != journal.after_config_sha256:
                        raise RuntimeError(
                            "bootstrap subscription removal config identity changed"
                        )
                except BaseException:
                    if self.config_manager.config_sha256() == journal.before_config_sha256:
                        self._assert_quarantined_bootstrap_subscription(
                            journal,
                            ownership,
                            subscription,
                            quarantine,
                            configured=True,
                        )
                        if destination.exists() or destination.is_symlink():
                            raise ValueError(
                                "bootstrap subscription removal destination reappeared"
                            ) from None
                        os.replace(quarantine, destination)
                        sync_removal_parent(self.config_manager.root, journal.checkout)
                        journal = journal.model_copy(update={"phase": "prepared"})
                        self.config_manager._write_bootstrap_state(journal_path, journal)
                    raise
            elif current_sha256 == journal.after_config_sha256:
                self._assert_quarantined_bootstrap_subscription(
                    journal,
                    ownership,
                    subscription,
                    quarantine,
                    configured=False,
                )
                mutation = self._subscription_config_receipt(
                    journal, "subscription_remove"
                )
            else:
                raise RuntimeError("Geas user config changed during subscription removal")
            journal = journal.model_copy(update={"phase": "config_committed"})
            self.config_manager._write_bootstrap_state(journal_path, journal)
        else:
            mutation = self._subscription_config_receipt(journal, "subscription_remove")

        if journal.phase != "config_committed":
            raise ValueError("bootstrap subscription removal journal has an invalid phase")
        with self.config_manager._config_lock():
            if self.config_manager.config_sha256() != journal.after_config_sha256:
                raise RuntimeError("Geas user config changed during subscription removal")
            self._assert_named_subscription_absent(journal.bootstrap_name)
            if destination.exists() or destination.is_symlink():
                raise ValueError("bootstrap subscription checkout remains after removal")
            if quarantine.exists() or quarantine.is_symlink():
                self._assert_quarantined_bootstrap_subscription(
                    journal,
                    ownership,
                    subscription,
                    quarantine,
                    configured=False,
                )
                if self.config_manager.config_sha256() != journal.after_config_sha256:
                    raise RuntimeError("Geas user config changed before checkout deletion")
                self._assert_checkout_identity(
                    quarantine,
                    device=journal.old_checkout_device,
                    inode=journal.old_checkout_inode,
                    label="quarantined bootstrap subscription checkout",
                )
                shutil.rmtree(quarantine)
                sync_removal_parent(self.config_manager.root, journal.quarantine)
            evidence = self.config_manager._load_bootstrap_state(evidence_path)
            if evidence is not None:
                expected = canonical_json(ownership.model_dump(mode="json")) + b"\n"
                if evidence != expected:
                    raise ValueError("bootstrap subscription ownership evidence changed")
                self.config_manager._remove_exact_bootstrap_state(
                    evidence_path, ownership
                )
            mutation = self._subscription_config_receipt(
                journal, "subscription_remove"
            )
            receipt = BootstrapSubscriptionMutationReceipt(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                action="remove",
                old_subscription_sha256=journal.old_subscription_sha256,
                new_subscription_sha256=None,
                config_mutation=mutation,
                ownership=None,
                managed_paths=(),
            )
            completed = journal.model_copy(
                update={"phase": "completed", "receipt": receipt}
            )
            self.config_manager._write_bootstrap_state(journal_path, completed)
        return receipt

    def _assert_quarantined_bootstrap_subscription(
        self,
        journal: _BootstrapSubscriptionJournal,
        ownership: BootstrapSubscriptionOwnershipReceipt,
        subscription: OntologySubscription,
        quarantine: Path,
        *,
        configured: bool,
    ) -> None:
        """Rebind a removal quarantine to exact live ownership before mutation."""
        self._assert_checkout_identity(
            quarantine,
            device=journal.old_checkout_device,
            inode=journal.old_checkout_inode,
            label="quarantined bootstrap subscription checkout",
        )
        if (
            journal.old_checkout_device != ownership.checkout_device
            or journal.old_checkout_inode != ownership.checkout_inode
        ):
            raise ValueError("bootstrap subscription removal ownership identity changed")
        if configured:
            self._assert_subscription_ownership(
                ownership,
                subscription,
                checkout_override=quarantine,
            )
        else:
            config = self.config_manager.load()
            self.config_manager.validate_subscription_layout(config)
            self._assert_named_subscription_absent(journal.bootstrap_name)
            destination = self.config_manager.subscription_checkout(subscription)
            for profile in config.profiles.values():
                for current in profile.normalized_subscriptions(
                    freshness=config.ontology_freshness
                ).values():
                    candidate = self.config_manager.subscription_checkout(current)
                    if (
                        candidate in (destination, quarantine)
                        or candidate.is_relative_to(destination)
                        or destination.is_relative_to(candidate)
                        or candidate.is_relative_to(quarantine)
                        or quarantine.is_relative_to(candidate)
                    ):
                        raise ValueError(
                            "bootstrap subscription removal overlaps configured checkout"
                        )
            evidence_path = self.config_manager._confined_state_path(
                Path(ownership.evidence_path)
            )
            expected_evidence_path = self._bootstrap_subscription_evidence_path(
                ownership.bootstrap_name,
                ownership.operation_key,
            )
            if evidence_path != expected_evidence_path:
                raise ValueError("bootstrap subscription ownership evidence path changed")
            expected = canonical_json(ownership.model_dump(mode="json")) + b"\n"
            if self.config_manager._load_bootstrap_state(evidence_path) != expected:
                raise ValueError("bootstrap subscription ownership evidence changed")
        self._verify_repository_identity(
            quarantine,
            subscription,
            expected_commit=ownership.verified_commit,
        )

    def _finalize_bootstrap_subscription(
        self,
        journal_path: Path,
        journal: _BootstrapSubscriptionJournal,
        mutation: BootstrapConfigMutationReceipt,
        destination: Path,
    ) -> BootstrapSubscriptionMutationReceipt:
        self._assert_checkout_identity(
            destination,
            device=journal.new_checkout_device,
            inode=journal.new_checkout_inode,
            label="owned bootstrap subscription checkout",
        )
        configured_subscription = self._assert_named_subscription(
            journal.bootstrap_name, journal.new_subscription_sha256
        )
        self._verify_repository_identity(
            destination,
            configured_subscription,
            expected_commit=journal.verified_commit,
        )
        identity = destination.stat(follow_symlinks=False)
        evidence_path = self._bootstrap_subscription_evidence_path(
            journal.bootstrap_name, journal.operation_key
        )
        ownership = BootstrapSubscriptionOwnershipReceipt(
            owner_operation_key=journal.owner_operation_key,
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            subscription_sha256=journal.new_subscription_sha256,
            checkout=journal.checkout.as_posix(),
            checkout_created=True,
            verified_commit=journal.verified_commit,
            checkout_device=identity.st_dev,
            checkout_inode=identity.st_ino,
            evidence_path=evidence_path.relative_to(self.config_manager.root).as_posix(),
            config_mutation=mutation,
        )
        evidence = canonical_json(ownership.model_dump(mode="json")) + b"\n"
        existing_evidence = self.config_manager._load_bootstrap_state(evidence_path)
        if existing_evidence is None:
            self.config_manager._write_bootstrap_state(evidence_path, ownership)
        elif existing_evidence != evidence:
            raise ValueError("bootstrap subscription ownership evidence already exists")
        managed = ManagedPath(
            path=ownership.evidence_path,
            sha256=hashlib.sha256(evidence).hexdigest(),
            role="receipt",
        )
        receipt = BootstrapSubscriptionMutationReceipt(
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            action=journal.action,
            old_subscription_sha256=journal.old_subscription_sha256,
            new_subscription_sha256=journal.new_subscription_sha256,
            config_mutation=mutation,
            ownership=ownership,
            managed_paths=(managed,),
        )
        completed = journal.model_copy(update={"phase": "completed", "receipt": receipt})
        self.config_manager._write_bootstrap_state(journal_path, completed)
        return receipt

    def _validate_bootstrap_checkout(
        self,
        name: str,
        subscription: OntologySubscription,
    ) -> None:
        expected = Path("subscriptions") / self.profile_name / name
        if subscription.checkout != expected:
            raise ValueError("bootstrap subscription must use its fixed checkout")
        self.config_manager.subscription_checkout(subscription)

    def _validate_bootstrap_operation(
        self,
        operation_key: str,
        name: str,
        kind: Literal[
            "subscription_ensure", "subscription_replace", "subscription_remove"
        ],
    ) -> None:
        BootstrapConfigMutationReceipt(
            operation_key=operation_key,
            profile_name=self.profile_name,
            bootstrap_name=name,
            kind=kind,
            before_config_sha256="0" * 64,
            after_config_sha256="0" * 64,
        )

    def _bootstrap_subscription_journal_path(
        self,
        name: str,
        operation_key: str,
    ) -> Path:
        digest = operation_key.rsplit(":", 1)[-1]
        return self.config_manager._confined_state_path(
            Path("repository-bootstrap")
            / "subscription-mutations"
            / self.profile_name
            / name
            / f"{digest}.json"
        )

    def _bootstrap_subscription_evidence_path(
        self,
        name: str,
        operation_key: str,
    ) -> Path:
        digest = operation_key.rsplit(":", 1)[-1]
        return self.config_manager._confined_state_path(
            Path("repository-bootstrap")
            / "subscription-ownership"
            / self.profile_name
            / name
            / f"{digest}.json"
        )

    def _load_bootstrap_subscription_journal(
        self,
        path: Path,
    ) -> _BootstrapSubscriptionJournal | None:
        value = self.config_manager._load_bootstrap_state(path)
        if value is None:
            return None
        try:
            return _BootstrapSubscriptionJournal.model_validate_json(value)
        except ValueError as error:
            raise ValueError("bootstrap subscription mutation journal is invalid") from error

    def _validate_bootstrap_subscription_journal(
        self,
        journal: _BootstrapSubscriptionJournal,
        *,
        operation_key: str,
        name: str,
        action: Literal["ensure", "replace", "remove"],
        owner_operation_key: str,
        old_subscription_sha256: str | None,
        new_subscription_sha256: str | None,
        checkout: Path,
        verified_commit: str,
    ) -> None:
        operation_digest = operation_key.rsplit(":", 1)[-1]
        destination = self.config_manager._confined_state_path(checkout)
        expected_staging = destination.with_name(
            f".{destination.name}.bootstrap-{operation_digest}.stage"
        ).relative_to(self.config_manager.root)
        expected_quarantine = (
            destination.with_name(
                f".{destination.name}.bootstrap-{operation_digest}.old"
            ).relative_to(self.config_manager.root)
            if action == "replace"
            else destination.with_name(
                f".{destination.name}.remove-bootstrap-{operation_digest}"
            ).relative_to(self.config_manager.root)
            if action == "remove"
            else None
        )
        if action == "remove":
            expected_staging = checkout
        if (
            journal.operation_key != operation_key
            or journal.profile_name != self.profile_name
            or journal.bootstrap_name != name
            or journal.action != action
            or journal.owner_operation_key != owner_operation_key
            or journal.old_subscription_sha256 != old_subscription_sha256
            or journal.new_subscription_sha256 != new_subscription_sha256
            or journal.checkout != checkout
            or journal.staging != expected_staging
            or journal.quarantine != expected_quarantine
            or journal.verified_commit != verified_commit
        ):
            raise ValueError("bootstrap subscription operation conflicts with its journal")

    def _assert_named_subscription(
        self,
        name: str,
        expected_sha256: str | None,
    ) -> OntologySubscription:
        if expected_sha256 is None:
            raise ValueError("owned bootstrap subscription identity is missing")
        config = self.config_manager.load()
        _, profile = config.profile(self.profile_name)
        try:
            subscription = profile.normalized_subscriptions(
                freshness=config.ontology_freshness
            )[name]
        except KeyError:
            raise ValueError("owned bootstrap subscription is missing") from None
        if _subscription_identity_sha256(subscription) != expected_sha256:
            raise ValueError("owned bootstrap subscription config identity changed")
        return subscription

    def _assert_named_subscription_absent(self, name: str) -> None:
        config = self.config_manager.load()
        _, profile = config.profile(self.profile_name)
        if name in profile.normalized_subscriptions(
            freshness=config.ontology_freshness
        ):
            raise ValueError("removed bootstrap subscription is still configured")

    @staticmethod
    def _assert_checkout_identity(
        path: Path,
        *,
        device: int | None,
        inode: int | None,
        label: str,
    ) -> None:
        if device is None or inode is None:
            raise ValueError(f"{label} identity is missing")
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"{label} is missing or unsafe")
        identity = path.stat(follow_symlinks=False)
        if identity.st_dev != device or identity.st_ino != inode:
            raise ValueError(f"{label} identity changed")

    @staticmethod
    def _subscription_config_receipt(
        journal: _BootstrapSubscriptionJournal,
        kind: Literal[
            "subscription_ensure", "subscription_replace", "subscription_remove"
        ],
    ) -> BootstrapConfigMutationReceipt:
        return BootstrapConfigMutationReceipt(
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            kind=kind,
            before_config_sha256=journal.before_config_sha256,
            after_config_sha256=journal.after_config_sha256,
        )

    def _assert_live_subscription_ownership(
        self,
        receipt: BootstrapSubscriptionMutationReceipt,
    ) -> None:
        ownership = receipt.ownership
        if ownership is None:
            raise ValueError("bootstrap subscription ownership receipt is missing")
        self._assert_subscription_ownership(ownership)
        expected = canonical_json(ownership.model_dump(mode="json")) + b"\n"
        if (
            len(receipt.managed_paths) != 1
            or receipt.managed_paths[0].path != ownership.evidence_path
            or receipt.managed_paths[0].sha256 != hashlib.sha256(expected).hexdigest()
        ):
            raise ValueError("bootstrap subscription managed evidence changed")

    def _assert_subscription_ownership(
        self,
        ownership: BootstrapSubscriptionOwnershipReceipt,
        expected_subscription: OntologySubscription | None = None,
        *,
        checkout_override: Path | None = None,
    ) -> None:
        if (
            ownership.profile_name != self.profile_name
            or ownership.checkout
            != (Path("subscriptions") / self.profile_name / ownership.bootstrap_name).as_posix()
        ):
            raise ValueError("bootstrap subscription ownership selector changed")
        if expected_subscription is not None and (
            ownership.subscription_sha256
            != _subscription_identity_sha256(expected_subscription)
            or ownership.checkout != expected_subscription.checkout.as_posix()
        ):
            raise ValueError("bootstrap subscription ownership does not match exact old state")
        configured_subscription = self._assert_named_subscription(
            ownership.bootstrap_name, ownership.subscription_sha256
        )
        config = self.config_manager.load()
        self.config_manager.validate_subscription_layout(config)
        checkout = (
            checkout_override
            if checkout_override is not None
            else self.config_manager.subscription_checkout(configured_subscription)
        )
        if checkout.is_symlink() or not checkout.is_dir():
            raise ValueError("owned bootstrap subscription checkout is missing or unsafe")
        identity = checkout.stat(follow_symlinks=False)
        if (
            identity.st_dev != ownership.checkout_device
            or identity.st_ino != ownership.checkout_inode
        ):
            raise ValueError("owned bootstrap subscription checkout identity changed")
        evidence_path = self.config_manager._confined_state_path(
            Path(ownership.evidence_path)
        )
        expected_evidence_path = self._bootstrap_subscription_evidence_path(
            ownership.bootstrap_name,
            ownership.operation_key,
        )
        if evidence_path != expected_evidence_path:
            raise ValueError("bootstrap subscription ownership evidence path changed")
        evidence = self.config_manager._load_bootstrap_state(evidence_path)
        expected = canonical_json(ownership.model_dump(mode="json")) + b"\n"
        if evidence != expected:
            raise ValueError("bootstrap subscription ownership evidence changed")
        self._verify_repository_identity(
            checkout,
            configured_subscription,
            expected_commit=ownership.verified_commit,
        )

    def _verify_repository_identity(
        self,
        checkout: Path,
        subscription: OntologySubscription,
        *,
        expected_commit: str,
    ) -> None:
        """Recheck the exact clean synchronized repository and catalog identity."""
        if checkout.is_symlink() or not checkout.is_dir():
            raise ValueError("bootstrap subscription checkout is missing or unsafe")
        git_directory = checkout / ".git"
        if git_directory.is_symlink():
            raise ValueError("bootstrap subscription Git metadata contains a symbolic link")
        catalog = checkout / subscription.catalog
        current = checkout
        for component in subscription.catalog.parts:
            current /= component
            if current.is_symlink():
                raise ValueError("bootstrap subscription catalog contains a symbolic link")
        if catalog.is_symlink() or not catalog.is_file():
            raise ValueError("bootstrap subscription catalog is missing or unsafe")
        repository = self._repository(checkout, subscription)
        repository.assert_removable()
        assert_verified_commit = getattr(repository, "assert_verified_commit", None)
        if callable(assert_verified_commit):
            assert_verified_commit(expected_commit)
        else:
            result = subprocess.run(
                ("git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"),
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            if result.returncode != 0 or result.stdout.strip() != expected_commit:
                raise ValueError(
                    "bootstrap subscription checkout does not match the exact verified commit"
                )
        verified = self.catalog_verifier(catalog)
        self.authorizer(verified)

    def subscribe(
        self,
        name: str,
        subscription: OntologySubscription,
    ) -> SubscriptionMutationReceipt:
        """Verify and authorize a checkout before atomically recording it."""
        self._recover_all_removals()
        validate_subscription_name(name)
        validated = OntologySubscription.model_validate(subscription.model_dump(mode="python"))
        original = self.config_manager.load()
        _, profile = original.profile(self.profile_name)
        prospective_profile = profile.model_copy(
            update={"subscriptions": {**profile.subscriptions, name: validated}}
        )
        prospective = original.model_copy(
            update={
                "profiles": {
                    **original.profiles,
                    self.profile_name: prospective_profile,
                }
            }
        )
        self.config_manager.validate_subscription_layout(prospective)
        destination = self.config_manager.subscription_checkout(validated)
        before = self.config_manager.path.read_bytes()
        created = not destination.exists()
        staging = (
            destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
            if created
            else destination
        )
        installed = False
        try:
            if created:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if staging.exists() or staging.is_symlink():
                    raise ValueError("temporary subscription checkout already exists")
            repository = self._repository(staging, validated)
            pull_receipt = repository.pull()
            verified = self.catalog_verifier(staging / validated.catalog)
            self.authorizer(verified)

            current = self.config_manager.load()
            _, current_profile = current.profile(self.profile_name)
            updated_subscriptions = {
                **current_profile.subscriptions,
                name: validated,
            }
            updated_profile = current_profile.model_copy(
                update={"subscriptions": updated_subscriptions}
            )
            updated = current.model_copy(
                update={
                    "profiles": {
                        **current.profiles,
                        self.profile_name: updated_profile,
                    }
                }
            )
            if created:
                if destination.exists() or destination.is_symlink():
                    raise ValueError("subscription checkout destination appeared during staging")
                os.replace(staging, destination)
                installed = True
            self.config_manager.replace(updated)
            return SubscriptionMutationReceipt(
                name=name,
                checkout=destination,
                subscribed=True,
                checkout_created=created,
                pull=pull_receipt,
            )
        except BaseException:
            if (
                not self.config_manager.path.exists()
                or self.config_manager.path.read_bytes() != before
            ):
                self.config_manager.restore_bytes(before)
            if created:
                self._remove_exact_checkout(destination if installed else staging)
            raise

    def unsubscribe(
        self,
        name: str,
        *,
        remove_checkout: bool = False,
    ) -> SubscriptionMutationReceipt:
        """Remove one declaration, preserving checkout bytes unless explicitly requested."""
        self._recover_all_removals()
        validate_subscription_name(name)
        config = self.config_manager.load()
        original_config_bytes = self.config_manager.path.read_bytes()
        _, profile = config.profile(self.profile_name)
        try:
            subscription = profile.normalized_subscriptions(freshness=config.ontology_freshness)[
                name
            ]
        except KeyError:
            raise ValueError(f"unknown ontology subscription: {name}") from None
        checkout = self.config_manager.subscription_checkout(subscription)
        explicit = dict(profile.subscriptions)
        explicit.pop(name, None)
        updates: dict[str, object] = {"subscriptions": explicit}
        if name == "primary":
            updates["ontology_git"] = None
        updated_profile = profile.model_copy(update=updates)
        updated = config.model_copy(
            update={
                "profiles": {
                    **config.profiles,
                    self.profile_name: updated_profile,
                }
            }
        )
        if not remove_checkout or not checkout.exists():
            if config.version == 2:
                self.config_manager.replace(updated, upgrade_version=True)
            else:
                self.config_manager.replace(updated)
            return SubscriptionMutationReceipt(
                name=name,
                checkout=checkout,
                unsubscribed=True,
            )

        if checkout.is_symlink():
            raise RuntimeError("subscription checkout cannot be a symbolic link")
        self.config_manager.validate_subscription_removal(
            config,
            profile_name=self.profile_name,
            subscription_name=name,
            expected_checkout=checkout,
        )
        repository = self._repository(checkout, subscription)
        repository.assert_removable()
        if self.config_manager.path.read_bytes() != original_config_bytes:
            raise RuntimeError("Geas user config changed during subscription removal")
        current_config = self.config_manager.load()
        if current_config != config:
            raise RuntimeError("Geas user config changed during subscription removal")
        self.config_manager.validate_subscription_removal(
            current_config,
            profile_name=self.profile_name,
            subscription_name=name,
            expected_checkout=checkout,
        )
        rechecked = self.config_manager.subscription_checkout(subscription)
        if rechecked != checkout:
            raise RuntimeError("subscription checkout identity changed before removal")
        verified_identity = checkout.stat(follow_symlinks=False)
        transaction_id = uuid4().hex
        quarantine = checkout.with_name(f".{checkout.name}.remove-{transaction_id}")
        if quarantine.exists() or quarantine.is_symlink():
            raise RuntimeError("subscription removal quarantine already exists")
        relative_quarantine = subscription.checkout.with_name(quarantine.name)
        journal = RemovalJournal(
            kind="subscription",
            transaction_id=transaction_id,
            phase=RemovalPhase.VALIDATED,
            profile_name=self.profile_name,
            target=subscription.checkout,
            quarantine=relative_quarantine,
            device=verified_identity.st_dev,
            inode=verified_identity.st_ino,
            name=name,
            subscription_sha256=_subscription_identity_sha256(subscription),
        )
        _write_subscription_removal_journal(self.config_manager, journal)
        try:
            verify_directory_identity(checkout, journal)
            os.replace(checkout, quarantine)
            sync_removal_parent(self.config_manager.root, relative_quarantine)
            journal = journal.model_copy(update={"phase": RemovalPhase.QUARANTINED})
            _write_subscription_removal_journal(self.config_manager, journal)
            if config.version == 2:
                self.config_manager.replace(updated, upgrade_version=True)
            else:
                self.config_manager.replace(updated)
            journal = journal.model_copy(update={"phase": RemovalPhase.CONFIG_COMMITTED})
            _write_subscription_removal_journal(self.config_manager, journal)
            verify_directory_identity(quarantine, journal)
            shutil.rmtree(quarantine)
            sync_removal_parent(self.config_manager.root, relative_quarantine)
            delete_removal_journal(self.config_manager.root, journal)
        except BaseException:
            with suppress(BaseException):
                self.recover_removals()
            raise
        return SubscriptionMutationReceipt(
            name=name,
            checkout=checkout,
            unsubscribed=True,
            checkout_removed=True,
        )

    def recover_removals(self) -> None:
        """Restore or finish exact checkout removals from durable journals."""
        recover_subscription_removals(self.config_manager)

    def _recover_all_removals(self) -> None:
        from research_agent.ontology_recovery import recover_managed_removals

        recover_managed_removals(self.config_manager)

    def _repository(self, checkout: Path, subscription: OntologySubscription) -> RepositoryOperator:
        if self.repository_factory is not None:
            return self.repository_factory(checkout, subscription)
        from research_agent.ontology_sync import OntologyRepositoryManager

        return OntologyRepositoryManager(checkout=checkout, config=subscription)

    @staticmethod
    def _remove_exact_checkout(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def sync(
        self,
        names: tuple[str, ...] = (),
        *,
        pull: bool = True,
        push: bool = False,
    ) -> tuple[SubscriptionSyncReceipt, ...]:
        if push:
            raise ValueError("ontology subscription sync does not authorize repository push")
        self._recover_all_removals()
        config = self.config_manager.load()
        profile = config.profile(self.profile_name)[1]
        configured = profile.normalized_subscriptions(freshness=config.ontology_freshness)
        selected = tuple(sorted(set(names))) if names else tuple(configured)
        receipts: list[SubscriptionSyncReceipt] = []
        for name in selected:
            try:
                validate_subscription_name(name)
                subscription = configured[name]
                checkout = self.config_manager.subscription_checkout(subscription)
                repository = self._repository(checkout, subscription)
                pull_receipt = repository.pull() if pull else None
                verified = self.catalog_verifier(checkout / subscription.catalog)
                self.authorizer(verified)
                receipts.append(
                    SubscriptionSyncReceipt(
                        name=name,
                        success=True,
                        pull=pull_receipt,
                        push=None,
                    )
                )
            except Exception as error:
                receipts.append(SubscriptionSyncReceipt(name=name, success=False, error=str(error)))
        return tuple(receipts)
