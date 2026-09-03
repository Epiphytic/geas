"""Exact-owned bootstrap configuration and subscription state tests."""

from __future__ import annotations

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
from research_agent.repository_bootstrap import (
    BootstrapOperation,
    RepositoryBootstrapManager,
    remove_obsolete_paths,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

_INSTALL_KEY = f"repository-bootstrap-operation:sha256:{'1' * 64}"
_UPDATE_KEY = f"repository-bootstrap-update-operation:sha256:{'2' * 64}"
_REMOVE_KEY = f"repository-bootstrap-removal-operation:sha256:{'3' * 64}"
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
        return {"commit": commit}

    def push(self) -> dict[str, object]:
        raise AssertionError("bootstrap subscription must not push")

    def assert_removable(self) -> None:
        return None


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
        return {"commit": ("d" if type(self).pulls == 1 else "e") * 40}


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
