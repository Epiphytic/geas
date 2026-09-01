"""Strict named ontology subscriptions and their bounded synchronization service."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

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
    from research_agent.user_config import UserConfigManager


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

    def push(self) -> dict[str, object]: ...

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
        self.profile_name = profile_name
        self.catalog_verifier = catalog_verifier
        self.authorizer = authorizer
        self.repository_factory = repository_factory

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
                push_receipt = repository.push() if push else None
                receipts.append(
                    SubscriptionSyncReceipt(
                        name=name,
                        success=True,
                        pull=pull_receipt,
                        push=push_receipt,
                    )
                )
            except Exception as error:
                receipts.append(SubscriptionSyncReceipt(name=name, success=False, error=str(error)))
        return tuple(receipts)
