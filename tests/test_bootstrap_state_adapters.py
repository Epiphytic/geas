"""Exact-owned bootstrap configuration and subscription state tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.bootstrap_models import (
    BootstrapConfigMutationReceipt,
    BootstrapGrantMutationReceipt,
    BootstrapGrantOwnershipReceipt,
    BootstrapPhase,
    BootstrapSubscriptionMutationReceipt,
    BootstrapSubscriptionOwnershipReceipt,
    ManagedPath,
    RepositoryBootstrapRequest,
    VerifiedRepositoryBootstrap,
)
from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResources,
    CapabilitySubject,
)
from research_agent.ontology_subscriptions import (
    OntologySubscription,
    SubscriptionManager,
)
from research_agent.ontology_trust import TrustRule
from research_agent.removal_journal import (
    RemovalJournal,
    RemovalPhase,
    removal_journal_path,
    write_removal_journal,
)
from research_agent.repository_bootstrap import (
    BootstrapOperation,
    RepositoryBootstrapManager,
    remove_obsolete_paths,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

_INSTALL_KEY = f"repository-bootstrap-operation:sha256:{'1' * 64}"
_UPDATE_KEY = f"repository-bootstrap-update-operation:sha256:{'2' * 64}"
_REMOVE_KEY = f"repository-bootstrap-removal-operation:sha256:{'3' * 64}"
_OTHER_KEY = f"repository-bootstrap-operation:sha256:{'9' * 64}"
_NOW = datetime(2026, 9, 3, tzinfo=UTC)


def _grant(
    *,
    repository: str = "https://example.test/gold",
    created_at: datetime = _NOW,
    created_via: str = "repository_install",
) -> CapabilityGrant:
    return CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=repository,
            refs=("refs/heads/main",),
            paths=("ontology/gold",),
            bundle_sha256=("4" * 64,),
        ),
        capabilities=(Capability.REPOSITORY_READ,),
        delegable_capabilities=(),
        resources=CapabilityResources(),
        max_delegation_depth=0,
        expires_at=None,
        created_at=created_at,
        created_via=created_via,
    )


def _config_manager(tmp_path: Path) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(ontology_git=None),
                "other": GeasProfile(ontology_git=None, ontology_directory=Path("kept")),
            }
        )
    )
    return manager


def _subscription(*, active_ref: str = "refs/heads/main") -> OntologySubscription:
    return OntologySubscription(
        url="https://example.test/gold.git",
        active_ref=active_ref,
        checkout=Path("subscriptions/default/gold"),
    )


class _BootstrapRepository:
    pulls = 0

    def __init__(self, checkout: Path, subscription: OntologySubscription) -> None:
        self.checkout = checkout
        self.subscription = subscription

    def pull(self) -> dict[str, object]:
        type(self).pulls += 1
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").mkdir()
        (self.checkout / "geas.yaml").write_text("version: 1\nontologies: []\n")
        commit = "d" * 40 if self.subscription.active_ref == "refs/heads/main" else "e" * 40
        (self.checkout / ".bootstrap-commit").write_text(commit)
        return {"commit": commit}

    def push(self) -> dict[str, object]:
        raise AssertionError("bootstrap subscription must not push")

    def assert_removable(self) -> None:
        return None

    def assert_verified_commit(self, expected_commit: str) -> None:
        if (self.checkout / ".bootstrap-commit").read_text() != expected_commit:
            raise ValueError("checkout does not match the exact verified commit")


def _stop_before_config_mutation(**kwargs: object) -> BootstrapConfigMutationReceipt:
    raise RuntimeError("stop after prepared journal")


def test_record_grant_retry_cannot_claim_matching_bytes_from_another_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches PREPARED record recovery treating matching config bytes as its write."""
    manager = _config_manager(tmp_path)
    grant = _grant()
    preparing = UserConfigManager(manager.path)
    monkeypatch.setattr(
        preparing, "mutate_profile_expected", _stop_before_config_mutation
    )
    with pytest.raises(RuntimeError, match="prepared journal"):
        preparing.record_bootstrap_grant(
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            grant=grant,
        )

    manager.mutate_profile_expected(
        operation_key=_OTHER_KEY,
        profile_name="default",
        bootstrap_name="other-writer",
        kind="grant_record",
        expected_config_sha256=manager.config_sha256(),
        mutate=lambda profile: profile.model_copy(
            update={"capability_grants": (*profile.capability_grants, grant)}
        ),
        upgrade_version=True,
    )

    with pytest.raises(RuntimeError, match="applied marker"):
        UserConfigManager(manager.path).record_bootstrap_grant(
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            grant=grant,
        )


def test_replace_grant_retry_cannot_claim_matching_bytes_from_another_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches PREPARED replacement recovery adopting another writer's result."""
    manager = _config_manager(tmp_path)
    old = _grant()
    recorded = manager.record_bootstrap_grant(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant=old,
    )
    assert recorded.ownership is not None
    new = _grant(created_at=datetime(2026, 9, 4, tzinfo=UTC))
    preparing = UserConfigManager(manager.path)
    monkeypatch.setattr(
        preparing, "mutate_profile_expected", _stop_before_config_mutation
    )
    with pytest.raises(RuntimeError, match="prepared journal"):
        preparing.replace_bootstrap_grant(
            operation_key=_UPDATE_KEY,
            profile_name="default",
            bootstrap_name="gold",
            ownership=recorded.ownership,
            old_grant=old,
            new_grant=new,
        )

    manager.mutate_profile_expected(
        operation_key=_OTHER_KEY,
        profile_name="default",
        bootstrap_name="other-writer",
        kind="grant_replace",
        expected_config_sha256=manager.config_sha256(),
        mutate=lambda profile: profile.model_copy(
            update={
                "capability_grants": tuple(
                    item for item in profile.capability_grants if item.id != old.id
                )
                + (new,)
            }
        ),
        upgrade_version=True,
    )

    with pytest.raises(RuntimeError, match="applied marker"):
        UserConfigManager(manager.path).replace_bootstrap_grant(
            operation_key=_UPDATE_KEY,
            profile_name="default",
            bootstrap_name="gold",
            ownership=recorded.ownership,
            old_grant=old,
            new_grant=new,
        )


def test_remove_grant_retry_cannot_claim_matching_bytes_from_another_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches PREPARED removal recovery adopting another writer's deletion."""
    manager = _config_manager(tmp_path)
    grant = _grant()
    recorded = manager.record_bootstrap_grant(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant=grant,
    )
    assert recorded.ownership is not None
    preparing = UserConfigManager(manager.path)
    monkeypatch.setattr(
        preparing, "mutate_profile_expected", _stop_before_config_mutation
    )
    with pytest.raises(RuntimeError, match="prepared journal"):
        preparing.remove_bootstrap_grant(
            operation_key=_REMOVE_KEY,
            profile_name="default",
            bootstrap_name="gold",
            ownership=recorded.ownership,
            grant=grant,
        )

    manager.mutate_profile_expected(
        operation_key=_OTHER_KEY,
        profile_name="default",
        bootstrap_name="other-writer",
        kind="grant_remove",
        expected_config_sha256=manager.config_sha256(),
        mutate=lambda profile: profile.model_copy(
            update={
                "capability_grants": tuple(
                    item for item in profile.capability_grants if item.id != grant.id
                )
            }
        ),
        upgrade_version=True,
    )

    with pytest.raises(RuntimeError, match="applied marker"):
        UserConfigManager(manager.path).remove_bootstrap_grant(
            operation_key=_REMOVE_KEY,
            profile_name="default",
            bootstrap_name="gold",
            ownership=recorded.ownership,
            grant=grant,
        )


def test_config_mutation_receipt_rejects_an_unbound_extra_field() -> None:
    """Catches mutation evidence accepting caller data outside its strict contract."""
    with pytest.raises(ValidationError, match="extra"):
        BootstrapConfigMutationReceipt(
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            kind="grant_record",
            before_config_sha256="a" * 64,
            after_config_sha256="b" * 64,
            unbound="must fail",
        )


def test_grant_mutation_receipt_rejects_ownership_from_another_operation() -> None:
    """Catches a grant mutation adopting ownership evidence from another key."""
    config = BootstrapConfigMutationReceipt(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        kind="grant_record",
        before_config_sha256="a" * 64,
        after_config_sha256="b" * 64,
    )
    ownership = BootstrapGrantOwnershipReceipt(
        owner_operation_key=_INSTALL_KEY,
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant_id=f"capability-grant:sha256:{'c' * 64}",
        config_mutation=config,
    )

    with pytest.raises(ValidationError, match="operation"):
        BootstrapGrantMutationReceipt(
            operation_key=f"repository-bootstrap-update-operation:sha256:{'2' * 64}",
            profile_name="default",
            bootstrap_name="gold",
            action="record",
            old_grant_id=None,
            new_grant_id=ownership.grant_id,
            config_mutation=config,
            ownership=ownership,
        )


def test_subscription_ownership_rejects_a_nonfixed_checkout() -> None:
    """Catches bootstrap ownership claiming a caller-selected checkout directory."""
    config = BootstrapConfigMutationReceipt(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        kind="subscription_ensure",
        before_config_sha256="a" * 64,
        after_config_sha256="b" * 64,
    )
    evidence_path = (
        "repository-bootstrap/subscription-ownership/default/gold/"
        f"{'1' * 64}.json"
    )

    with pytest.raises(ValidationError, match="fixed checkout"):
        BootstrapSubscriptionOwnershipReceipt(
            owner_operation_key=_INSTALL_KEY,
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            subscription_sha256="c" * 64,
            checkout="subscriptions/gold",
            checkout_created=True,
            verified_commit="d" * 40,
            checkout_device=1,
            checkout_inode=2,
            evidence_path=evidence_path,
            config_mutation=config,
        )


def test_subscription_mutation_requires_regular_receipt_evidence() -> None:
    """Catches checkout directory ownership being returned as a managed path."""
    config = BootstrapConfigMutationReceipt(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        kind="subscription_ensure",
        before_config_sha256="a" * 64,
        after_config_sha256="b" * 64,
    )
    evidence_path = (
        "repository-bootstrap/subscription-ownership/default/gold/"
        f"{'1' * 64}.json"
    )
    ownership = BootstrapSubscriptionOwnershipReceipt(
        owner_operation_key=_INSTALL_KEY,
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        subscription_sha256="c" * 64,
        checkout="subscriptions/default/gold",
        checkout_created=True,
        verified_commit="d" * 40,
        checkout_device=1,
        checkout_inode=2,
        evidence_path=evidence_path,
        config_mutation=config,
    )

    with pytest.raises(ValidationError, match="receipt evidence"):
        BootstrapSubscriptionMutationReceipt(
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            action="ensure",
            old_subscription_sha256=None,
            new_subscription_sha256="c" * 64,
            config_mutation=config,
            ownership=ownership,
            managed_paths=(
                ManagedPath(
                    path="subscriptions/default/gold",
                    sha256="e" * 64,
                    role="receipt",
                ),
            ),
        )


def test_profile_config_mutation_rejects_a_stale_compare_and_swap(
    tmp_path: Path,
) -> None:
    """Catches a scoped mutation overwriting a concurrent operator config update."""
    manager = _config_manager(tmp_path)
    stale = manager.config_sha256()
    concurrent = manager.load().model_copy(update={"default_profile": "other"})
    manager.replace(concurrent)
    before = manager.path.read_bytes()

    with pytest.raises(RuntimeError, match="changed"):
        manager.mutate_profile_expected(
            operation_key=_INSTALL_KEY,
            profile_name="default",
            bootstrap_name="gold",
            kind="grant_record",
            expected_config_sha256=stale,
            mutate=lambda profile: profile.model_copy(
                update={"ontology_directory": Path("must-not-write")}
            ),
        )

    assert manager.path.read_bytes() == before


def test_public_config_replace_rejects_a_stale_loaded_snapshot(tmp_path: Path) -> None:
    """Catches an ordinary writer resurrecting bytes replaced by another manager."""
    manager = _config_manager(tmp_path)
    stale = manager.load()
    contender = UserConfigManager(manager.path)
    concurrent = contender.load().model_copy(update={"default_profile": "other"})
    contender.replace(concurrent)
    concurrent_bytes = manager.path.read_bytes()

    with pytest.raises(RuntimeError, match="changed.*replacement"):
        manager.replace(stale)

    assert manager.path.read_bytes() == concurrent_bytes


def test_public_config_restore_rejects_a_stale_expected_identity(tmp_path: Path) -> None:
    """A rollback helper cannot overwrite a later operator-owned config state."""
    manager = _config_manager(tmp_path)
    original = manager.path.read_bytes()
    original_sha256 = manager.config_sha256()
    concurrent = manager.load().model_copy(update={"default_profile": "other"})
    manager.replace(concurrent)
    concurrent_bytes = manager.path.read_bytes()

    with pytest.raises(RuntimeError, match="changed.*restoration"):
        manager.restore_bytes(
            original,
            expected_config_sha256=original_sha256,
        )

    assert manager.path.read_bytes() == concurrent_bytes


def test_normal_subscribe_cannot_stale_write_after_checkout_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race may fail, but it may never leave config true and checkout false."""
    manager = _config_manager(tmp_path)
    subscription = _subscription()
    destination = manager.subscription_checkout(subscription)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    import research_agent.ontology_subscriptions as subscriptions

    original_replace = subscriptions.os.replace
    raced = False

    def remove_after_checkout_install(source: object, target: object) -> None:
        nonlocal raced
        original_replace(source, target)
        if Path(target) != destination or raced:
            return
        raced = True
        contender = UserConfigManager(manager.path)
        concurrent = contender.load().model_copy(update={"default_profile": "other"})
        contender.replace(concurrent)
        shutil.rmtree(destination)

    monkeypatch.setattr(subscriptions.os, "replace", remove_after_checkout_install)

    with pytest.raises(RuntimeError, match="changed.*replacement"):
        service.subscribe("gold", subscription)

    current = manager.load()
    assert raced
    assert current.default_profile == "other"
    assert "gold" not in current.profiles["default"].subscriptions
    assert not destination.exists()


def test_record_bootstrap_grant_migrates_only_intended_v1_state(
    tmp_path: Path,
) -> None:
    """Catches grant recording dropping legacy authority or unrelated profile data."""
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    legacy = TrustRule(
        decision="allow",
        repository="https://example.test/manual",
        refs=("refs/heads/main",),
        paths="*",
        bundle_sha256="*",
        created_at=_NOW,
        created_via="manual",
    )
    manager.replace(
        GeasUserConfig(
            default_profile="other",
            profiles={
                "default": GeasProfile(ontology_git=None, trust_rules=(legacy,)),
                "other": GeasProfile(ontology_git=None, ontology_directory=Path("kept")),
            },
        )
    )
    before = manager.config_sha256()

    receipt = manager.record_bootstrap_grant(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant=_grant(),
    )

    loaded = manager.load()
    assert loaded.version == 2
    assert loaded.default_profile == "other"
    assert loaded.profiles["other"].ontology_directory == Path("kept")
    assert tuple(
        item.subject.repository for item in loaded.profiles["default"].capability_grants
    ) == ("https://example.test/manual", "https://example.test/gold")
    assert loaded.profiles["default"].capability_grants[0].created_via == "manual"
    assert receipt.config_mutation.before_config_sha256 == before
    assert receipt.config_mutation.after_config_sha256 == manager.config_sha256()
    assert receipt.ownership is not None
    assert receipt.ownership.grant_id == _grant().id


def test_replace_bootstrap_grant_uses_exact_old_ownership_and_preserves_concurrent_data(
    tmp_path: Path,
) -> None:
    """Catches replace removing by selector or overwriting unrelated config state."""
    manager = _config_manager(tmp_path)
    old = _grant()
    recorded = manager.record_bootstrap_grant(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant=old,
    )
    assert recorded.ownership is not None
    unrelated = _grant(repository="https://example.test/unrelated")
    current = manager.load()
    default = current.profiles["default"].model_copy(
        update={
            "capability_grants": (
                *current.profiles["default"].capability_grants,
                unrelated,
            )
        }
    )
    manager.replace(
        current.model_copy(
            update={
                "default_profile": "other",
                "profiles": {**current.profiles, "default": default},
            }
        ),
        upgrade_version=True,
    )
    replacement = _grant(created_at=datetime(2026, 9, 4, tzinfo=UTC))

    receipt = manager.replace_bootstrap_grant(
        operation_key=_UPDATE_KEY,
        profile_name="default",
        bootstrap_name="gold",
        ownership=recorded.ownership,
        old_grant=old,
        new_grant=replacement,
    )

    loaded = manager.load()
    assert loaded.default_profile == "other"
    assert loaded.profiles["other"].ontology_directory == Path("kept")
    assert tuple(item.id for item in loaded.profiles["default"].capability_grants) == (
        unrelated.id,
        replacement.id,
    )
    assert receipt.old_grant_id == old.id
    assert receipt.new_grant_id == replacement.id
    assert receipt.ownership is not None
    assert receipt.ownership.owner_operation_key == _INSTALL_KEY
    assert receipt.ownership.operation_key == _UPDATE_KEY


def test_remove_bootstrap_grant_deletes_only_the_exact_owned_id_and_replays(
    tmp_path: Path,
) -> None:
    """Catches grant removal deleting equivalent manual or unrelated grants."""
    manager = _config_manager(tmp_path)
    owned = _grant()
    recorded = manager.record_bootstrap_grant(
        operation_key=_INSTALL_KEY,
        profile_name="default",
        bootstrap_name="gold",
        grant=owned,
    )
    assert recorded.ownership is not None
    unrelated = _grant(repository="https://example.test/unrelated")
    current = manager.load()
    profile = current.profiles["default"].model_copy(
        update={
            "capability_grants": (
                *current.profiles["default"].capability_grants,
                unrelated,
            )
        }
    )
    manager.replace(
        current.model_copy(update={"profiles": {**current.profiles, "default": profile}}),
        upgrade_version=True,
    )

    removed = manager.remove_bootstrap_grant(
        operation_key=_REMOVE_KEY,
        profile_name="default",
        bootstrap_name="gold",
        ownership=recorded.ownership,
        grant=owned,
    )
    replayed = UserConfigManager(manager.path).remove_bootstrap_grant(
        operation_key=_REMOVE_KEY,
        profile_name="default",
        bootstrap_name="gold",
        ownership=recorded.ownership,
        grant=owned,
    )

    assert removed == replayed
    assert removed.ownership is None
    assert tuple(
        item.id for item in manager.load().profiles["default"].capability_grants
    ) == (unrelated.id,)


def test_ensure_bootstrap_subscription_replays_without_a_second_checkout_mutation(
    tmp_path: Path,
) -> None:
    """Catches install retry adopting or re-pulling an already owned checkout."""
    manager = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0

    def service() -> SubscriptionManager:
        return SubscriptionManager(
            config_manager=UserConfigManager(manager.path),
            profile_name="default",
            catalog_verifier=lambda path: path,
            authorizer=lambda verified: verified,
            repository_factory=_BootstrapRepository,
        )

    first = service().ensure_bootstrap_subscription(
        "gold",
        _subscription(),
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    replayed = service().ensure_bootstrap_subscription(
        "gold",
        _subscription(),
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )

    assert first == replayed
    assert _BootstrapRepository.pulls == 1
    assert manager.load().profiles["default"].subscriptions["gold"] == _subscription()
    assert first.ownership is not None
    assert first.ownership.checkout == "subscriptions/default/gold"
    assert first.ownership.verified_commit == "d" * 40
    assert len(first.managed_paths) == 1
    evidence = manager.root / first.managed_paths[0].path
    assert evidence.is_file() and not evidence.is_symlink()
    assert first.managed_paths[0].path != first.ownership.checkout


@pytest.mark.parametrize("active_ref", ["refs/heads/main", "refs/heads/other"])
def test_ensure_bootstrap_subscription_refuses_matching_or_conflicting_operator_state(
    tmp_path: Path,
    active_ref: str,
) -> None:
    """Catches bootstrap install adopting a same-named operator subscription."""
    manager = _config_manager(tmp_path)
    existing = _subscription(active_ref=active_ref)
    config = manager.load()
    profile = config.profiles["default"].model_copy(
        update={"subscriptions": {"gold": existing}}
    )
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, "default": profile}})
    )
    checkout = manager.subscription_checkout(existing)
    checkout.mkdir(parents=True)
    before = manager.path.read_bytes()
    _BootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )

    with pytest.raises(ValueError, match="adopt"):
        service.ensure_bootstrap_subscription(
            "gold",
            _subscription(),
            operation_key=_INSTALL_KEY,
            verified_commit="d" * 40,
        )

    assert manager.path.read_bytes() == before
    assert _BootstrapRepository.pulls == 0


def test_replace_bootstrap_subscription_requires_exact_old_state_and_replays(
    tmp_path: Path,
) -> None:
    """Catches update changing checkout/name or replaying a completed pull."""
    manager = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    old_subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        old_subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    candidate = _subscription(active_ref="refs/heads/stable")

    replaced = service.replace_bootstrap_subscription(
        "gold",
        old_subscription,
        candidate,
        operation_key=_UPDATE_KEY,
        verified_commit="e" * 40,
        ownership=installed.ownership,
    )
    replayed = SubscriptionManager(
        config_manager=UserConfigManager(manager.path),
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    ).replace_bootstrap_subscription(
        "gold",
        old_subscription,
        candidate,
        operation_key=_UPDATE_KEY,
        verified_commit="e" * 40,
        ownership=installed.ownership,
    )

    assert replaced == replayed
    assert _BootstrapRepository.pulls == 2
    assert manager.load().profiles["default"].subscriptions["gold"] == candidate
    assert replaced.old_subscription_sha256 == installed.ownership.subscription_sha256
    assert replaced.ownership is not None
    assert replaced.ownership.owner_operation_key == _INSTALL_KEY
    assert replaced.ownership.operation_key == _UPDATE_KEY
    assert replaced.ownership.checkout == installed.ownership.checkout


def test_replace_bootstrap_subscription_rejects_changed_old_config_before_pull(
    tmp_path: Path,
) -> None:
    """Catches update selecting a same-name record after operator modification."""
    manager = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    old_subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        old_subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    config = manager.load()
    changed = _subscription(active_ref="refs/heads/operator")
    profile = config.profiles["default"].model_copy(
        update={"subscriptions": {"gold": changed}}
    )
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, "default": profile}})
    )
    before_pulls = _BootstrapRepository.pulls

    with pytest.raises(ValueError, match="config identity"):
        service.replace_bootstrap_subscription(
            "gold",
            old_subscription,
            _subscription(active_ref="refs/heads/stable"),
            operation_key=_UPDATE_KEY,
            verified_commit="e" * 40,
            ownership=installed.ownership,
        )

    assert _BootstrapRepository.pulls == before_pulls


def test_remove_bootstrap_subscription_deletes_exact_owned_checkout_and_replays(
    tmp_path: Path,
) -> None:
    """Catches removal selecting a checkout parent or treating absence as ownership."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    checkout = manager.subscription_checkout(subscription)
    sibling = checkout.parent / "operator-kept"
    sibling.mkdir()
    (sibling / "note").write_text("keep")

    removed = service.remove_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_REMOVE_KEY,
        ownership=installed.ownership,
    )
    replayed = SubscriptionManager(
        config_manager=UserConfigManager(manager.path),
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    ).remove_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_REMOVE_KEY,
        ownership=installed.ownership,
    )

    assert removed == replayed
    assert "gold" not in manager.load().profiles["default"].subscriptions
    assert not checkout.exists()
    assert (sibling / "note").read_text() == "keep"
    assert not (manager.root / installed.ownership.evidence_path).exists()


def test_remove_bootstrap_subscription_rejects_replaced_checkout_inode(
    tmp_path: Path,
) -> None:
    """Catches ownership by path after the exact checkout directory was replaced."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    checkout = manager.subscription_checkout(subscription)
    moved = checkout.with_name("operator-old")
    checkout.rename(moved)
    checkout.mkdir()
    before = manager.path.read_bytes()

    with pytest.raises(ValueError, match="identity changed"):
        service.remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )

    assert checkout.is_dir()
    assert moved.is_dir()
    assert manager.path.read_bytes() == before


class _DirtyBootstrapRepository(_BootstrapRepository):
    def assert_removable(self) -> None:
        raise RuntimeError("checkout has local changes")


class _LifecycleBootstrapRepository(_BootstrapRepository):
    pulls = 0

    def pull(self) -> dict[str, object]:
        type(self).pulls += 1
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").mkdir()
        (self.checkout / "geas.yaml").write_text("version: 1\nontologies: []\n")
        commit = ("d" if type(self).pulls == 1 else "e") * 40
        (self.checkout / ".bootstrap-commit").write_text(commit)
        return {"commit": commit}


def test_remove_bootstrap_subscription_retains_dirty_owned_checkout(
    tmp_path: Path,
) -> None:
    """Catches bootstrap removal bypassing the repository removability gate."""
    manager = _config_manager(tmp_path)
    install_service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = install_service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    before = manager.path.read_bytes()

    with pytest.raises(RuntimeError, match="local changes"):
        SubscriptionManager(
            config_manager=manager,
            profile_name="default",
            catalog_verifier=lambda path: path,
            authorizer=lambda verified: verified,
            repository_factory=_DirtyBootstrapRepository,
        ).remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )

    assert manager.subscription_checkout(subscription).is_dir()
    assert manager.path.read_bytes() == before


def test_remove_subscription_rejects_checkout_advanced_beyond_owned_commit(
    tmp_path: Path,
) -> None:
    """Catches an old ownership receipt deleting a later synchronized checkout."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    checkout = manager.subscription_checkout(subscription)
    (checkout / ".bootstrap-commit").write_text("f" * 40)

    with pytest.raises(ValueError, match="verified commit"):
        service.remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )

    assert checkout.is_dir()
    assert "gold" in manager.load().profiles["default"].subscriptions


def test_remove_subscription_rejects_catalog_symlink_drift(tmp_path: Path) -> None:
    """Catches removal trusting a catalog path redirected after installation."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    checkout = manager.subscription_checkout(subscription)
    catalog = checkout / "geas.yaml"
    catalog.unlink()
    outside = tmp_path / "outside-geas.yaml"
    outside.write_text("version: 1\nontologies: []\n")
    catalog.symlink_to(outside)

    with pytest.raises(ValueError, match="catalog"):
        service.remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )

    assert checkout.is_dir()
    assert outside.is_file()


@pytest.mark.parametrize(
    "tamper",
    ("commit", "removability", "catalog-symlink", "authorization"),
)
def test_remove_subscription_fresh_retry_reverifies_quarantine_after_config_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """Catches generic recovery deleting a drifted owned checkout before exact replay."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None

    import research_agent.ontology_subscriptions as subscriptions

    original_rmtree = subscriptions.shutil.rmtree

    def stop_after_config_commit(path: object) -> None:
        if ".remove-" in Path(path).name:
            raise OSError("stop after subscription config commit")
        original_rmtree(path)

    monkeypatch.setattr(subscriptions.shutil, "rmtree", stop_after_config_commit)
    with pytest.raises(OSError, match="config commit"):
        service.remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )
    monkeypatch.setattr(subscriptions.shutil, "rmtree", original_rmtree)

    assert "gold" not in manager.load().profiles["default"].subscriptions
    quarantine = next((manager.root / "subscriptions/default").glob(".gold.remove-*"))
    removal_journal = (
        manager.root
        / "repository-bootstrap/subscription-mutations/default/gold"
        / f"{_REMOVE_KEY.rsplit(':', 1)[-1]}.json"
    )
    assert removal_journal.is_file()
    journal_bytes = removal_journal.read_bytes()
    evidence = manager.root / installed.ownership.evidence_path
    evidence_bytes = evidence.read_bytes()
    authorizer_calls: list[object] = []
    if tamper == "commit":
        (quarantine / ".bootstrap-commit").write_text("f" * 40)

        def authorize(verified: object) -> object:
            authorizer_calls.append(verified)
            return verified

        expected = "verified commit"
        repository_factory = _BootstrapRepository
    elif tamper == "removability":

        def authorize(verified: object) -> object:
            authorizer_calls.append(verified)
            return verified

        expected = "local changes"
        repository_factory = _DirtyBootstrapRepository
    elif tamper == "catalog-symlink":
        catalog = quarantine / subscription.catalog
        catalog.unlink()
        outside = tmp_path / "outside-geas.yaml"
        outside.write_text("version: 1\nontologies: []\n")
        catalog.symlink_to(outside)

        def authorize(verified: object) -> object:
            authorizer_calls.append(verified)
            return verified

        expected = "catalog"
        repository_factory = _BootstrapRepository
    else:

        def authorize(verified: object) -> object:
            authorizer_calls.append(verified)
            raise PermissionError("catalog authorization was revoked")

        expected = "authorization was revoked"
        repository_factory = _BootstrapRepository

    fresh = SubscriptionManager(
        config_manager=UserConfigManager(manager.path),
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=authorize,
        repository_factory=repository_factory,
    )

    with pytest.raises((ValueError, PermissionError, RuntimeError), match=expected):
        fresh.remove_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_REMOVE_KEY,
            ownership=installed.ownership,
        )

    assert quarantine.is_dir()
    assert removal_journal.read_bytes() == journal_bytes
    assert evidence.read_bytes() == evidence_bytes
    assert not (manager.root / "state/removal-transactions/subscriptions").exists()
    if tamper == "authorization":
        assert authorizer_calls


def test_remove_subscription_serializes_final_delete_against_config_reregistration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches config being re-registered after validation but before rmtree."""
    manager = _config_manager(tmp_path)
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    subscription = _subscription()
    installed = service.ensure_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_INSTALL_KEY,
        verified_commit="d" * 40,
    )
    assert installed.ownership is not None
    registered_config = manager.path.read_bytes()

    import research_agent.ontology_subscriptions as subscriptions

    original_rmtree = subscriptions.shutil.rmtree
    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    writes: list[str] = []
    writer: threading.Thread | None = None

    def race_config_writer(path: object) -> None:
        nonlocal writer
        quarantine = Path(path)

        def write_if_checkout_is_still_owned() -> None:
            contender = UserConfigManager(manager.path)
            writer_started.set()
            try:
                with contender._config_lock():
                    if quarantine.is_dir():
                        contender.path.write_bytes(registered_config)
                        writes.append("registered")
            except BaseException as error:
                writer_errors.append(error)
            finally:
                writer_finished.set()

        writer = threading.Thread(target=write_if_checkout_is_still_owned)
        writer.start()
        assert writer_started.wait(timeout=5)
        writer_finished.wait(timeout=0.25)
        original_rmtree(quarantine)

    monkeypatch.setattr(subscriptions.shutil, "rmtree", race_config_writer)
    removed = service.remove_bootstrap_subscription(
        "gold",
        subscription,
        operation_key=_REMOVE_KEY,
        ownership=installed.ownership,
    )
    assert removed.action == "remove"
    assert writer is not None
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert writer_errors == []
    assert writes == []
    assert "gold" not in manager.load().profiles["default"].subscriptions
    assert not manager.subscription_checkout(subscription).exists()


def test_subscription_staged_replay_reverifies_exact_commit_before_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches staged recovery swapping a checkout whose verified identity drifted."""
    manager = _config_manager(tmp_path)
    subscription = _subscription()
    destination = manager.subscription_checkout(subscription)
    digest = _INSTALL_KEY.rsplit(":", 1)[-1]
    staging = destination.with_name(
        f".{destination.name}.bootstrap-{digest}.stage"
    )
    original_replace = os.replace
    interrupted = False

    def stop_before_checkout_swap(source: object, target: object) -> None:
        nonlocal interrupted
        if not interrupted and Path(source) == staging and Path(target) == destination:
            interrupted = True
            raise RuntimeError("stop before checkout swap")
        original_replace(source, target)

    monkeypatch.setattr(
        "research_agent.ontology_subscriptions.os.replace",
        stop_before_checkout_swap,
    )
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )

    with pytest.raises(RuntimeError, match="stop before checkout swap"):
        service.ensure_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_INSTALL_KEY,
            verified_commit="d" * 40,
        )

    assert staging.is_dir()
    (staging / ".bootstrap-commit").write_text("f" * 40)
    before = manager.path.read_bytes()
    with pytest.raises(ValueError, match="verified commit"):
        service.ensure_bootstrap_subscription(
            "gold",
            subscription,
            operation_key=_INSTALL_KEY,
            verified_commit="d" * 40,
        )

    assert staging.is_dir()
    assert not destination.exists()
    assert manager.path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_name", "other"),
        ("staging", "subscriptions/default/.wrong.stage"),
    ],
)
def test_subscription_ensure_rejects_misdirected_journal_before_repository_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    """Catches a valid-but-misdirected journal selecting a sibling state path."""
    manager = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0
    preparing = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )

    def stop_after_journal(*args: object) -> BootstrapSubscriptionMutationReceipt:
        raise RuntimeError("stop after subscription journal")

    monkeypatch.setattr(
        preparing, "_resume_bootstrap_subscription_ensure", stop_after_journal
    )
    with pytest.raises(RuntimeError, match="subscription journal"):
        preparing.ensure_bootstrap_subscription(
            "gold",
            _subscription(),
            operation_key=_INSTALL_KEY,
            verified_commit="d" * 40,
        )
    digest = _INSTALL_KEY.rsplit(":", 1)[-1]
    journal_path = (
        manager.root
        / "repository-bootstrap/subscription-mutations/default/gold"
        / f"{digest}.json"
    )
    payload = json.loads(journal_path.read_text())
    payload[field] = value
    journal_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(ValueError, match="journal"):
        SubscriptionManager(
            config_manager=manager,
            profile_name="default",
            catalog_verifier=lambda path: path,
            authorizer=lambda verified: verified,
            repository_factory=_BootstrapRepository,
        ).ensure_bootstrap_subscription(
            "gold",
            _subscription(),
            operation_key=_INSTALL_KEY,
            verified_commit="d" * 40,
        )

    assert _BootstrapRepository.pulls == 0


def test_subscription_replace_rejects_misdirected_quarantine_before_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches replacement replay moving an old checkout to a sibling path."""
    manager = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0
    preparing = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    old = _subscription()
    installed = preparing.ensure_bootstrap_subscription(
        "gold", old, operation_key=_INSTALL_KEY, verified_commit="d" * 40
    )
    assert installed.ownership is not None

    def stop_after_journal(*args: object) -> BootstrapSubscriptionMutationReceipt:
        raise RuntimeError("stop after replacement journal")

    monkeypatch.setattr(
        preparing, "_resume_bootstrap_subscription_replace", stop_after_journal
    )
    candidate = _subscription(active_ref="refs/heads/stable")
    with pytest.raises(RuntimeError, match="replacement journal"):
        preparing.replace_bootstrap_subscription(
            "gold",
            old,
            candidate,
            operation_key=_UPDATE_KEY,
            verified_commit="e" * 40,
            ownership=installed.ownership,
        )
    digest = _UPDATE_KEY.rsplit(":", 1)[-1]
    journal_path = (
        manager.root
        / "repository-bootstrap/subscription-mutations/default/gold"
        / f"{digest}.json"
    )
    payload = json.loads(journal_path.read_text())
    payload["quarantine"] = "subscriptions/default/.operator-owned.old"
    journal_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(ValueError, match="journal"):
        SubscriptionManager(
            config_manager=manager,
            profile_name="default",
            catalog_verifier=lambda path: path,
            authorizer=lambda verified: verified,
            repository_factory=_BootstrapRepository,
        ).replace_bootstrap_subscription(
            "gold",
            old,
            candidate,
            operation_key=_UPDATE_KEY,
            verified_commit="e" * 40,
            ownership=installed.ownership,
        )

    assert _BootstrapRepository.pulls == 1


def _obsolete_operation(path: ManagedPath) -> BootstrapOperation:
    request = RepositoryBootstrapRequest(
        name="gold",
        repository="https://example.test/gold.git",
        ref="refs/heads/main",
        commit_sha256="d" * 40,
    )
    verified = VerifiedRepositoryBootstrap(
        repository=request.repository,
        ref=request.ref,
        catalog=request.catalog,
        commit_sha256=request.commit_sha256,
        ontology_paths=("ontology/gold",),
        bundle_sha256=("4" * 64,),
    )
    return BootstrapOperation(
        request=request,
        verified=verified,
        phase=BootstrapPhase.COMPLETED,
        idempotency_key=_UPDATE_KEY,
        owned_paths=(path,),
    )


def test_remove_obsolete_paths_unlinks_only_the_exact_regular_leaf(tmp_path: Path) -> None:
    """Catches update cleanup recursively deleting an owned leaf's parent."""
    root = tmp_path / "state"
    parent = root / "receipts"
    parent.mkdir(parents=True)
    target = parent / "old.json"
    target.write_bytes(b"owned")
    sibling = parent / "operator.txt"
    sibling.write_bytes(b"keep")
    managed = ManagedPath(
        path="receipts/old.json",
        sha256="f5e6d024c05c9cc2746a3e127408b91a8b7a7f2a30da0c259bc54265502ddef4",
        role="receipt",
    )

    remove_obsolete_paths(root, _obsolete_operation(managed))

    assert not target.exists()
    assert sibling.read_bytes() == b"keep"
    assert parent.is_dir()


def test_remove_obsolete_receipt_uses_state_root_not_managed_root(
    tmp_path: Path,
) -> None:
    """Catches split-root cleanup selecting a same-relative repository leaf."""
    managed_root = tmp_path / "managed"
    state_root = tmp_path / "state"
    relative = "repository-bootstrap/subscription-ownership/default/gold/old.json"
    state_target = state_root / relative
    state_target.parent.mkdir(parents=True)
    state_target.write_bytes(b"owned")
    repository_sibling = managed_root / relative
    repository_sibling.parent.mkdir(parents=True)
    repository_sibling.write_bytes(b"operator")
    receipt = ManagedPath(
        path=relative,
        sha256=hashlib.sha256(b"owned").hexdigest(),
        role="receipt",
    )

    remove_obsolete_paths(
        managed_root,
        _obsolete_operation(receipt),
        state_root=state_root,
    )

    assert not state_target.exists()
    assert repository_sibling.read_bytes() == b"operator"


def test_remove_obsolete_paths_rejects_modified_or_symlinked_leaf(tmp_path: Path) -> None:
    """Catches stale ownership deleting operator-modified or redirected data."""
    root = tmp_path / "state"
    parent = root / "receipts"
    parent.mkdir(parents=True)
    target = parent / "old.json"
    target.write_bytes(b"changed")
    managed = ManagedPath(
        path="receipts/old.json",
        sha256="f5e6d024c05c9cc2746a3e127408b91a8b7a7f2a30da0c259bc54265502ddef4",
        role="receipt",
    )

    with pytest.raises(ValueError, match="modified"):
        remove_obsolete_paths(root, _obsolete_operation(managed))
    assert target.read_bytes() == b"changed"

    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="missing or unsafe"):
        remove_obsolete_paths(root, _obsolete_operation(managed))
    assert outside.read_bytes() == b"outside"


def test_subscription_sync_push_is_explicitly_disabled_before_repository_work(
    tmp_path: Path,
) -> None:
    """Catches the legacy subscription service reaching repository.push()."""
    manager = _config_manager(tmp_path)
    subscription = _subscription()
    config = manager.load()
    profile = config.profiles["default"].model_copy(
        update={"subscriptions": {"gold": subscription}}
    )
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, "default": profile}})
    )
    _BootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )

    with pytest.raises(ValueError, match="does not authorize repository push"):
        service.sync(push=True)

    assert _BootstrapRepository.pulls == 0


def test_subscription_push_denial_precedes_pending_removal_recovery(
    tmp_path: Path,
) -> None:
    """Catches denied push mutating a pending quarantine before returning denial."""
    manager = _config_manager(tmp_path)
    subscription = _subscription()
    config = manager.load()
    profile = config.profiles["default"].model_copy(
        update={"subscriptions": {"gold": subscription}}
    )
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, "default": profile}})
    )
    transaction = "7" * 32
    quarantine_relative = subscription.checkout.with_name(
        f".{subscription.checkout.name}.remove-{transaction}"
    )
    quarantine = manager.root / quarantine_relative
    quarantine.mkdir(parents=True)
    marker = quarantine / "operator-marker"
    marker.write_bytes(b"unchanged")
    identity = quarantine.stat(follow_symlinks=False)
    subscription_sha256 = hashlib.sha256(
        json.dumps(
            subscription.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    journal = RemovalJournal(
        kind="subscription",
        transaction_id=transaction,
        phase=RemovalPhase.QUARANTINED,
        profile_name="default",
        target=subscription.checkout,
        quarantine=quarantine_relative,
        device=identity.st_dev,
        inode=identity.st_ino,
        name="gold",
        subscription_sha256=subscription_sha256,
    )
    write_removal_journal(manager.root, journal)
    journal_path = removal_journal_path(manager.root, journal)
    journal_bytes = journal_path.read_bytes()
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )

    with pytest.raises(ValueError, match="does not authorize repository push"):
        service.sync(push=True)

    assert quarantine.is_dir()
    assert marker.read_bytes() == b"unchanged"
    assert not manager.subscription_checkout(subscription).exists()
    assert journal_path.read_bytes() == journal_bytes


def _bootstrap_request(
    *,
    ref: str = "refs/heads/main",
    commit: str = "d" * 40,
) -> RepositoryBootstrapRequest:
    return RepositoryBootstrapRequest(
        name="gold",
        repository="https://example.test/gold.git",
        ref=ref,
        commit_sha256=commit,
        trust="read_only",
    )


def _verified_request(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
    return VerifiedRepositoryBootstrap(
        repository=request.repository,
        ref=request.ref,
        catalog=request.catalog,
        commit_sha256=request.commit_sha256,
        ontology_paths=("ontology/gold",),
        bundle_sha256=("4" * 64,),
    )


def _initialize_managed_repository(
    repository: Path,
    *,
    tracked: tuple[tuple[str, bytes], ...] = (),
) -> str:
    repository.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "fixture@example.test"),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Fixture"), cwd=repository, check=True
    )
    (repository / "geas.yaml").write_text("version: 1\nontologies: []\n")
    for relative, value in tracked:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "fixture"), cwd=repository, check=True)
    subprocess.run(
        ("git", "remote", "add", "origin", "https://example.test/gold.git"),
        cwd=repository,
        check=True,
    )
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _operation_subscription(operation: BootstrapOperation) -> OntologySubscription:
    return OntologySubscription(
        url=operation.verified.repository,
        active_ref=operation.verified.ref,
        checkout=Path("subscriptions/default") / operation.request.name,
        catalog=Path(operation.verified.catalog),
    )


@pytest.mark.parametrize("interrupted_step", ["trust", "subscription"])
def test_coordinator_retry_recovers_exact_state_adapter_mutation_once(
    tmp_path: Path,
    interrupted_step: str,
) -> None:
    """Catches coordinator replay duplicating a completed external state mutation."""
    config = _config_manager(tmp_path)
    _BootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=config,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_BootstrapRepository,
    )
    request = _bootstrap_request()
    interrupt = {interrupted_step}
    grant_calls = 0
    subscription_calls = 0

    def record_trust(operation: BootstrapOperation, grant: CapabilityGrant):
        nonlocal grant_calls
        grant_calls += 1
        result = config.record_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            grant=grant,
        )
        if "trust" in interrupt:
            interrupt.remove("trust")
            raise RuntimeError("crash after trust mutation")
        return result

    def subscribe(operation: BootstrapOperation):
        nonlocal subscription_calls
        subscription_calls += 1
        result = service.ensure_bootstrap_subscription(
            operation.request.name,
            _operation_subscription(operation),
            operation_key=operation.idempotency_key,
            verified_commit=operation.verified.commit_sha256,
        )
        if "subscription" in interrupt:
            interrupt.remove("subscription")
            raise RuntimeError("crash after subscription mutation")
        return result

    def coordinator() -> RepositoryBootstrapManager:
        return RepositoryBootstrapManager(
            root=config.root,
            announce=lambda message: None,
            now=lambda: _NOW,
            verify=_verified_request,
            record_trust=record_trust,
            subscribe=subscribe,
            hydrate_artifacts=lambda operation: (),
            install_generic_skill=lambda operation: (),
            export_catalog_skills=lambda operation: (),
            link_agents=lambda operation: (),
        )

    with pytest.raises(RuntimeError, match="crash after"):
        coordinator().install(request)
    completed = coordinator().install(request)

    profile = config.load().profiles["default"]
    assert len(profile.capability_grants) == 1
    assert tuple(profile.subscriptions) == ("gold",)
    assert _BootstrapRepository.pulls == 1
    assert grant_calls == (2 if interrupted_step == "trust" else 1)
    assert subscription_calls == (2 if interrupted_step == "subscription" else 1)
    assert completed.grant_ownership is not None
    assert completed.subscription_ownership is not None
    assert completed.grant_mutation is not None
    assert completed.subscription_mutation is not None
    assert completed.managed_paths == completed.subscription_mutation.managed_paths


def test_coordinator_update_and_remove_forward_exact_ownership_receipts(
    tmp_path: Path,
) -> None:
    """Catches update/removal callbacks losing original grant or checkout ownership."""
    config = _config_manager(tmp_path)
    _LifecycleBootstrapRepository.pulls = 0
    service = SubscriptionManager(
        config_manager=config,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_LifecycleBootstrapRepository,
    )

    def record_trust(operation: BootstrapOperation, grant: CapabilityGrant):
        return config.record_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            grant=grant,
        )

    def replace_trust(
        operation: BootstrapOperation,
        old_grant: CapabilityGrant | None,
        new_grant: CapabilityGrant | None,
    ):
        assert operation.grant_ownership is not None
        assert old_grant is not None
        return config.replace_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            ownership=operation.grant_ownership,
            old_grant=old_grant,
            new_grant=new_grant,
        )

    def subscribe(operation: BootstrapOperation):
        return service.ensure_bootstrap_subscription(
            operation.request.name,
            _operation_subscription(operation),
            operation_key=operation.idempotency_key,
            verified_commit=operation.verified.commit_sha256,
        )

    def replace_subscription(
        old_operation: BootstrapOperation,
        candidate_operation: BootstrapOperation,
    ):
        assert old_operation.subscription_ownership is not None
        return service.replace_bootstrap_subscription(
            candidate_operation.request.name,
            _operation_subscription(old_operation),
            _operation_subscription(candidate_operation),
            operation_key=candidate_operation.idempotency_key,
            verified_commit=candidate_operation.verified.commit_sha256,
            ownership=old_operation.subscription_ownership,
        )

    def unsubscribe(operation: BootstrapOperation):
        assert operation.subscription_ownership is not None
        return service.remove_bootstrap_subscription(
            operation.request.name,
            _operation_subscription(operation),
            operation_key=operation.idempotency_key,
            ownership=operation.subscription_ownership,
        )

    def remove_trust(operation: BootstrapOperation, grant: CapabilityGrant):
        assert operation.grant_ownership is not None
        return config.remove_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            ownership=operation.grant_ownership,
            grant=grant,
        )

    def coordinator() -> RepositoryBootstrapManager:
        return RepositoryBootstrapManager(
            root=config.root,
            announce=lambda message: None,
            now=lambda: _NOW,
            verify=_verified_request,
            record_trust=record_trust,
            replace_trust=replace_trust,
            subscribe=subscribe,
            replace_subscription=replace_subscription,
            hydrate_artifacts=lambda operation: (),
            install_generic_skill=lambda operation: (),
            export_catalog_skills=lambda operation: (),
            link_agents=lambda operation: (),
            remove_trust=remove_trust,
            unsubscribe=unsubscribe,
            remove_skills=lambda operation: None,
            remove_obsolete_paths=lambda operation: remove_obsolete_paths(
                config.root, operation
            ),
            verify_software_provenance=lambda: None,
        )

    initial_request = _bootstrap_request()
    installed = coordinator().install(initial_request)
    candidate_request = _bootstrap_request(
        commit="e" * 40,
    )
    updated = coordinator().update(candidate_request)

    assert updated.grant_ownership is not None
    assert updated.subscription_ownership is not None
    assert updated.grant_ownership.owner_operation_key == (
        installed.grant_ownership.owner_operation_key
        if installed.grant_ownership is not None
        else None
    )
    assert updated.subscription_ownership.owner_operation_key == (
        installed.subscription_ownership.owner_operation_key
        if installed.subscription_ownership is not None
        else None
    )
    assert updated.subscription_ownership.verified_commit == "e" * 40
    assert updated.subscription_ownership.checkout == "subscriptions/default/gold"
    assert _LifecycleBootstrapRepository.pulls == 2

    removed = coordinator().remove(candidate_request)

    profile = config.load().profiles["default"]
    assert profile.capability_grants == ()
    assert profile.subscriptions == {}
    assert removed.removed is True
    assert removed.grant_ownership is None
    assert removed.subscription_ownership is None
    assert removed.grant_mutation is not None
    assert removed.grant_mutation.action == "remove"
    assert removed.subscription_mutation is not None
    assert removed.subscription_mutation.action == "remove"


def test_bootstrap_manager_separates_operational_state_from_managed_repository(
    tmp_path: Path,
) -> None:
    """Catches receipts polluting the repository whose managed skills are publishable."""
    repository = tmp_path / "repository"
    commit = _initialize_managed_repository(repository)
    state_root = tmp_path / "state"
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": repository.resolve()}
    )

    def install_skill(operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        relative = Path(".agents/skills/gold/SKILL.md")
        destination = repository / relative
        destination.parent.mkdir(parents=True)
        value = b"# Gold\n"
        destination.write_bytes(value)
        return (
            ManagedPath(
                path=relative.as_posix(),
                sha256=hashlib.sha256(value).hexdigest(),
                role="skill",
            ),
        )

    receipt = RepositoryBootstrapManager(
        managed_root=repository,
        state_root=state_root,
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=install_skill,
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    ).install(request)

    assert (state_root / "repository-bootstrap/gold.json").is_file()
    assert not (repository / "repository-bootstrap").exists()
    assert receipt.managed_paths[0].path == ".agents/skills/gold/SKILL.md"
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout.splitlines()
    assert status == ["?? .agents/"]


@pytest.mark.parametrize("repository_state", ("non-git", "dirty"))
def test_explicit_managed_root_requires_exact_clean_verified_git_worktree_before_mutation(
    tmp_path: Path,
    repository_state: str,
) -> None:
    """Catches bootstrap state or callbacks mutating an unverified managed root."""
    repository = tmp_path / "repository"
    repository.mkdir()
    commit = "d" * 40
    if repository_state == "dirty":
        commit = _initialize_managed_repository(repository)
        (repository / "operator-dirty.txt").write_text("preserve\n")
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": repository.resolve()}
    )
    effects: list[str] = []
    manager = RepositoryBootstrapManager(
        managed_root=repository,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: effects.append("trust"),
        subscribe=lambda operation: effects.append("subscription") or (),
        hydrate_artifacts=lambda operation: effects.append("artifacts") or (),
        install_generic_skill=lambda operation: effects.append("generic-skill") or (),
        export_catalog_skills=lambda operation: effects.append("catalog-skills") or (),
        link_agents=lambda operation: effects.append("links") or (),
    )

    with pytest.raises(ValueError, match="Git worktree|local changes"):
        manager.install(request)

    assert effects == []
    assert not (tmp_path / "state" / "repository-bootstrap").exists()


def test_explicit_managed_root_must_equal_verified_worktree(tmp_path: Path) -> None:
    """Catches a verified checkout being used to authorize writes in a sibling root."""
    managed = tmp_path / "managed"
    verified_root = tmp_path / "verified"
    managed.mkdir()
    verified_root.mkdir()
    request = _bootstrap_request()
    verified = _verified_request(request).model_copy(
        update={"current_worktree": verified_root.resolve()}
    )
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    with pytest.raises(ValueError, match="verified.*worktree"):
        manager.install(request)

    assert not (tmp_path / "state" / "repository-bootstrap").exists()


def test_explicit_managed_root_rejects_repository_environment_spoof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches sibling Git metadata authorizing a non-Git managed directory."""
    sibling = tmp_path / "sibling"
    commit = _initialize_managed_repository(sibling)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "geas.yaml").write_text("version: 1\nontologies: []\n")
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    monkeypatch.setenv("GIT_DIR", str(sibling / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(managed))
    effects: list[str] = []
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: effects.append("trust"),
        subscribe=lambda operation: effects.append("subscription") or (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    with pytest.raises(ValueError, match="local Git metadata|Git worktree"):
        manager.install(request)

    assert effects == []
    assert not (tmp_path / "state" / "repository-bootstrap").exists()


def test_explicit_managed_root_rejects_gitfile_pointing_at_a_sibling_repository(
    tmp_path: Path,
) -> None:
    """A local gitfile is not proof that its administrative directory owns this root."""
    sibling = tmp_path / "sibling"
    commit = _initialize_managed_repository(sibling)
    managed = tmp_path / "managed"
    managed.mkdir()
    shutil.copyfile(sibling / "geas.yaml", managed / "geas.yaml")
    (managed / ".git").write_text(f"gitdir: {(sibling / '.git').resolve()}\n")
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    effects: list[str] = []
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: effects.append("trust"),
        subscribe=lambda operation: effects.append("subscription") or (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    with pytest.raises(ValueError, match="linked Git metadata|administrative"):
        manager.install(request)

    assert effects == []
    assert not (tmp_path / "state" / "repository-bootstrap").exists()


def test_explicit_managed_root_accepts_bound_linked_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit = _initialize_managed_repository(repository)
    managed = tmp_path / "linked"
    subprocess.run(
        ("git", "worktree", "add", "-q", "-b", "fixture-linked", str(managed)),
        cwd=repository,
        check=True,
    )
    request = _bootstrap_request(ref="refs/heads/fixture-linked", commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    receipt = manager.install(request)

    assert receipt.completed_phases[-1] is BootstrapPhase.COMPLETED
    assert (managed / ".git").is_file()


def test_explicit_worktree_receipt_round_trips_for_fresh_resume_and_removal(
    tmp_path: Path,
) -> None:
    """Durable receipt JSON must preserve its strict absolute worktree identity."""
    managed = tmp_path / "managed"
    commit = _initialize_managed_repository(managed)
    state = tmp_path / "state"
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )

    def coordinator() -> RepositoryBootstrapManager:
        return RepositoryBootstrapManager(
            managed_root=managed,
            state_root=state,
            announce=lambda message: None,
            now=lambda: _NOW,
            verify=lambda candidate: verified,
            record_trust=lambda operation, grant: None,
            subscribe=lambda operation: (),
            hydrate_artifacts=lambda operation: (),
            install_generic_skill=lambda operation: (),
            export_catalog_skills=lambda operation: (),
            link_agents=lambda operation: (),
            remove_trust=lambda operation, grant: None,
            unsubscribe=lambda operation: None,
            remove_skills=lambda operation: None,
        )

    installed = coordinator().install(request)
    reloaded = coordinator().install(request)
    removed = coordinator().remove(request)

    assert reloaded == installed
    assert reloaded.verified is not None
    assert reloaded.verified.current_worktree == managed.resolve()
    assert removed.removed is True


@pytest.mark.parametrize("drift", ("head", "ref", "remote"))
def test_explicit_managed_root_rechecks_exact_git_identity_before_mutation(
    tmp_path: Path,
    drift: str,
) -> None:
    """Catches a stale verification result authorizing a changed clean worktree."""
    managed = tmp_path / "managed"
    commit = _initialize_managed_repository(managed)
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    if drift == "head":
        (managed / "second.txt").write_text("second\n")
        subprocess.run(("git", "add", "second.txt"), cwd=managed, check=True)
        subprocess.run(("git", "commit", "-q", "-m", "second"), cwd=managed, check=True)
        expected = "HEAD differs"
    elif drift == "ref":
        subprocess.run(("git", "checkout", "-q", "-b", "other"), cwd=managed, check=True)
        expected = "wrong branch"
    else:
        subprocess.run(
            ("git", "remote", "set-url", "origin", "https://example.test/other.git"),
            cwd=managed,
            check=True,
        )
        expected = "remote differs"
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=tmp_path / "state",
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    with pytest.raises(ValueError, match=expected):
        manager.install(request)

    assert not (tmp_path / "state" / "repository-bootstrap").exists()


@pytest.mark.parametrize("relationship", ("equal", "state-below-managed"))
def test_explicit_state_root_cannot_live_inside_managed_repository(
    tmp_path: Path,
    relationship: str,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    state = managed if relationship == "equal" else managed / ".geas-state"

    with pytest.raises(ValueError, match="state root.*managed root"):
        RepositoryBootstrapManager(
            managed_root=managed,
            state_root=state,
            announce=lambda message: None,
        )

    assert not (managed / "repository-bootstrap").exists()
    assert not (managed / ".geas-state").exists()


def test_legacy_root_with_explicit_nested_state_root_uses_split_root_guards(
    tmp_path: Path,
) -> None:
    """Legacy spelling cannot bypass explicit state-root separation and Git binding."""
    managed = tmp_path / "managed"
    managed.mkdir()

    with pytest.raises(ValueError, match="state root.*managed root"):
        RepositoryBootstrapManager(
            root=managed,
            state_root=managed / ".geas-state",
            announce=lambda message: None,
        )

    assert not (managed / ".geas-state").exists()


def test_explicit_managed_root_may_live_below_state_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    managed = state / "subscriptions/default/gold"
    commit = _initialize_managed_repository(managed)
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=state,
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: (),
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: (),
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    receipt = manager.install(request)

    assert receipt.completed_phases[-1] is BootstrapPhase.COMPLETED
    assert (state / "repository-bootstrap/gold.json").is_file()
    assert not (managed / "repository-bootstrap").exists()


@pytest.mark.parametrize("linked_root", ["managed", "state"])
def test_bootstrap_manager_rejects_symlinked_managed_or_state_root(
    tmp_path: Path,
    linked_root: str,
) -> None:
    """Catches either authority root traversing a symlink into another tree."""
    managed = tmp_path / "managed"
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_root == "managed":
        managed.symlink_to(outside, target_is_directory=True)
        state.mkdir()
    else:
        managed.mkdir()
        state.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="root.*symbolic link"):
        RepositoryBootstrapManager(
            managed_root=managed,
            state_root=state,
            announce=lambda message: None,
        )


@pytest.mark.parametrize(
    ("role", "relative"),
    [
        ("skill", ".agents/skills/gold/SKILL.md"),
        (
            "receipt",
            "repository-bootstrap/subscription-ownership/default/gold/"
            f"{'1' * 64}.json",
        ),
    ],
)
def test_bootstrap_manager_never_resolves_owned_evidence_from_the_other_root(
    tmp_path: Path,
    role: str,
    relative: str,
) -> None:
    """Catches repository and state evidence being adopted across authority roots."""
    managed = tmp_path / "managed"
    state = tmp_path / "state"
    managed.mkdir()
    state.mkdir()
    wrong_root = state if role == "skill" else managed
    value = b"owned only in the wrong root\n"
    tracked = ((relative, value),) if role == "receipt" else ()
    if role == "skill":
        wrong_path = wrong_root / relative
        wrong_path.parent.mkdir(parents=True)
        wrong_path.write_bytes(value)
    commit = _initialize_managed_repository(managed, tracked=tracked)
    wrong_path = wrong_root / relative
    request = _bootstrap_request(commit=commit)
    verified = _verified_request(request).model_copy(
        update={"current_worktree": managed.resolve()}
    )
    evidence = ManagedPath(
        path=relative,
        sha256=hashlib.sha256(value).hexdigest(),
        role=role,
    )
    subscription_result = (evidence,) if role == "receipt" else ()
    skill_result = (evidence,) if role == "skill" else ()
    manager = RepositoryBootstrapManager(
        managed_root=managed,
        state_root=state,
        announce=lambda message: None,
        now=lambda: _NOW,
        verify=lambda candidate: verified,
        record_trust=lambda operation, grant: None,
        subscribe=lambda operation: subscription_result,
        hydrate_artifacts=lambda operation: (),
        install_generic_skill=lambda operation: skill_result,
        export_catalog_skills=lambda operation: (),
        link_agents=lambda operation: (),
    )

    with pytest.raises(ValueError, match="missing or unsafe"):
        manager.install(request)

    assert wrong_path.read_bytes() == value
