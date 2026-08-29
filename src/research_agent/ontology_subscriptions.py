"""Strict named ontology subscriptions and their bounded synchronization service."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel

if TYPE_CHECKING:
    from research_agent.user_config import UserConfigManager


_SUBSCRIPTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def validate_subscription_name(value: str) -> str:
    if not _SUBSCRIPTION_NAME.fullmatch(value):
        raise ValueError("subscription name is invalid")
    return value


def normalize_active_ref(value: str) -> str:
    raw = value.strip()
    if _OBJECT_ID.fullmatch(raw):
        return raw.lower()
    if not raw.startswith(("refs/heads/", "refs/tags/")):
        raise ValueError("active_ref must use full branch/tag refs or commit IDs")
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or "\\" in raw
        or ".." in raw
        or "@{" in raw
        or "//" in raw
        or raw.endswith(("/", "."))
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


def _validate_remote_url(value: str) -> str:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("ontology Git URL contains control characters or is empty")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ontology Git URLs cannot embed credentials")
    if value.startswith(("file:", "/", "./", "../")):
        raise ValueError("ontology Git URL must be a remote repository")
    return value


class OntologySubscription(StrictModel):
    url: str
    active_ref: str = "refs/heads/main"
    checkout: Path
    catalog: Path = Path("geas.yaml")
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pull_before_update: bool = False
    push_on_update: bool = False

    @field_validator("url")
    @classmethod
    def url_is_credential_free_remote(cls, value: str) -> str:
        return _validate_remote_url(value)

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
        checkouts = tuple(item.checkout for item in self.subscriptions.values())
        if len(checkouts) != len(set(checkouts)):
            raise ValueError("subscription checkouts must be unique")
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
        validate_subscription_name(name)
        validated = OntologySubscription.model_validate(subscription.model_dump(mode="python"))
        original = self.config_manager.load()
        _, profile = original.profile(self.profile_name)
        destination = self.config_manager.subscription_checkout(validated)
        for sibling_name, sibling in profile.normalized_subscriptions().items():
            if sibling_name != name and sibling.checkout == validated.checkout:
                raise ValueError(f"subscription checkout is already used by {sibling_name!r}")
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
        validate_subscription_name(name)
        config = self.config_manager.load()
        _, profile = config.profile(self.profile_name)
        try:
            subscription = profile.normalized_subscriptions()[name]
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
        for sibling_name, sibling in updated_profile.normalized_subscriptions().items():
            if sibling.checkout == subscription.checkout:
                raise RuntimeError(f"subscription checkout is still used by {sibling_name!r}")
        repository = self._repository(checkout, subscription)
        repository.assert_removable()
        before = self.config_manager.path.read_bytes()
        quarantine = checkout.with_name(f".{checkout.name}.remove-{uuid4().hex}")
        moved = False
        try:
            os.replace(checkout, quarantine)
            moved = True
            self.config_manager.replace(updated)
            shutil.rmtree(quarantine)
            moved = False
        except BaseException:
            if (
                not self.config_manager.path.exists()
                or self.config_manager.path.read_bytes() != before
            ):
                self.config_manager.restore_bytes(before)
            if moved and quarantine.exists() and not checkout.exists():
                os.replace(quarantine, checkout)
            raise
        return SubscriptionMutationReceipt(
            name=name,
            checkout=checkout,
            unsubscribed=True,
            checkout_removed=True,
        )

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
        profile = self.config_manager.load().profile(self.profile_name)[1]
        configured = profile.normalized_subscriptions()
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
            except (KeyError, OSError, ValueError, RuntimeError) as error:
                receipts.append(SubscriptionSyncReceipt(name=name, success=False, error=str(error)))
        return tuple(receipts)
