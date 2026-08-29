from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_agent.ontology_subscriptions import (
    OntologySubscription,
    SubscriptionManager,
)
from research_agent.user_config import (
    GeasProfile,
    GeasUserConfig,
    OntologyGitConfig,
    UserConfigManager,
)

URL = "https://example.invalid/ontologies.git"


def _git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Test",
            "GIT_AUTHOR_EMAIL": "geas-test@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Test",
            "GIT_COMMITTER_EMAIL": "geas-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )


def test_legacy_git_profile_normalizes_to_primary_subscription() -> None:
    profile = GeasProfile(ontology_git=OntologyGitConfig(url=URL, branch="release/v1"))

    subscription = profile.normalized_subscriptions()["primary"]

    assert subscription.active_ref == "refs/heads/release/v1"
    assert subscription.checkout == Path("ontologies")
    assert subscription.catalog == Path("geas.yaml")


def test_explicit_subscriptions_are_strict_sorted_and_override_legacy_primary() -> None:
    primary = OntologySubscription(
        url=URL,
        active_ref="refs/tags/v1.0.0",
        checkout=Path("subscriptions/primary"),
    )
    zeta = primary.model_copy(update={"checkout": Path("subscriptions/zeta")})
    profile = GeasProfile(
        ontology_git=OntologyGitConfig(url="https://legacy.example.invalid/ontology.git"),
        subscriptions={"zeta": zeta, "primary": primary},
    )

    normalized = profile.normalized_subscriptions()

    assert tuple(normalized) == ("primary", "zeta")
    assert normalized["primary"] == primary


@pytest.mark.parametrize(
    "values, message",
    [
        ({"active_ref": "main"}, "full branch/tag refs or commit IDs"),
        ({"active_ref": "refs/pull/1/head"}, "full branch/tag refs or commit IDs"),
        ({"checkout": Path("../outside")}, "config-relative"),
        ({"catalog": Path("nested/../geas.yaml")}, "normalized"),
        ({"url": "https://token@example.invalid/repo.git"}, "embed credentials"),
    ],
)
def test_subscription_rejects_unsafe_authority_and_paths(
    values: dict[str, object], message: str
) -> None:
    candidate: dict[str, object] = {
        "url": URL,
        "checkout": Path("subscriptions/example"),
    }
    candidate.update(values)

    with pytest.raises(ValueError, match=message):
        OntologySubscription.model_validate(candidate)


def test_subscription_accepts_sha1_and_sha256_commit_ids() -> None:
    for object_id in ("a" * 40, "b" * 64):
        subscription = OntologySubscription(
            url=URL,
            active_ref=object_id,
            checkout=Path("subscriptions/example"),
        )
        assert subscription.active_ref == object_id


def test_sync_processes_requested_subscriptions_in_sorted_order_and_keeps_successes(
    tmp_path: Path,
) -> None:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    profile = GeasProfile(
        ontology_git=None,
        subscriptions={
            "zeta": OntologySubscription(
                url="https://zeta.example.invalid/repo.git", checkout=Path("zeta")
            ),
            "alpha": OntologySubscription(
                url="https://alpha.example.invalid/repo.git", checkout=Path("alpha")
            ),
        },
    )
    manager.replace(GeasUserConfig(profiles={"default": profile}))

    class FakeRepository:
        def __init__(self, checkout: Path, subscription: OntologySubscription) -> None:
            self.checkout = checkout
            self.subscription = subscription

        def pull(self) -> dict[str, object]:
            if self.subscription.url.startswith("https://zeta"):
                raise RuntimeError("injected fetch failure")
            (self.checkout / ".git").mkdir(parents=True)
            return {"commit": "a" * 40}

        def push(self) -> dict[str, object]:
            return {"pushed": False}

        def assert_removable(self) -> None:
            return None

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
        repository_factory=FakeRepository,
    )

    receipts = subscriptions.sync()

    assert tuple(item.name for item in receipts) == ("alpha", "zeta")
    assert receipts[0].success is True
    assert receipts[1].success is False
    assert receipts[1].error is not None
    assert (manager.root / "alpha" / ".git").is_dir()


def _configured_manager(tmp_path: Path) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.replace(GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)}))
    return manager


class _StagingRepository:
    def __init__(
        self,
        checkout: Path,
        subscription: OntologySubscription,
        *,
        failure: str | None = None,
    ) -> None:
        self.checkout = checkout
        self.subscription = subscription
        self.failure = failure

    def pull(self) -> dict[str, object]:
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").mkdir()
        (self.checkout / "geas.yaml").write_text("version: 1\nontologies: []\n")
        if self.failure == "fetch":
            raise RuntimeError("injected fetch failure")
        return {"commit": "a" * 40}

    def push(self) -> dict[str, object]:
        return {"pushed": False}

    def assert_removable(self) -> None:
        return None


def _subscription(*, checkout: str = "subscriptions/sample") -> OntologySubscription:
    return OntologySubscription(
        url="https://example.invalid/repository.git",
        checkout=Path(checkout),
    )


def test_subscribe_validates_constructed_input_before_any_write(tmp_path: Path) -> None:
    manager = _configured_manager(tmp_path)
    before = manager.path.read_bytes()
    calls: list[str] = []
    invalid = OntologySubscription.model_construct(
        url="https://example.invalid/repository.git",
        active_ref="refs/heads/main",
        checkout=Path("../outside"),
        catalog=Path("geas.yaml"),
        remote="origin",
        pull_before_update=False,
        push_on_update=False,
    )
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: calls.append("verify"),
        authorizer=lambda verified: calls.append("authorize"),
        repository_factory=lambda checkout, subscription: calls.append("repository"),
    )

    with pytest.raises(ValueError, match="config-relative"):
        subscriptions.subscribe("sample", invalid)

    assert manager.path.read_bytes() == before
    assert calls == []
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("failure", ("fetch", "catalog", "trust"))
def test_subscribe_failure_restores_config_and_removes_only_temporary_checkout(
    tmp_path: Path, failure: str
) -> None:
    manager = _configured_manager(tmp_path)
    # Preserve non-semantic operator formatting to prove byte-exact rollback.
    manager.path.write_bytes(b"# operator comment\n" + manager.path.read_bytes())
    before = manager.path.read_bytes()

    def verify(path: Path) -> object:
        assert path.name == "geas.yaml"
        if failure == "catalog":
            raise ValueError("injected catalog failure")
        return ("verified", path)

    def authorize(verified: object) -> object:
        if failure == "trust":
            current = manager.load()
            manager.replace(
                current.model_copy(
                    update={
                        "profiles": {
                            **current.profiles,
                            "temporary": GeasProfile(ontology_git=None),
                        }
                    }
                )
            )
            raise ValueError("injected trust failure")
        return verified

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=verify,
        authorizer=authorize,
        repository_factory=lambda checkout, subscription: _StagingRepository(
            checkout, subscription, failure=failure
        ),
    )

    with pytest.raises((RuntimeError, ValueError), match="injected"):
        subscriptions.subscribe("sample", _subscription())

    assert manager.path.read_bytes() == before
    assert not (manager.root / "subscriptions" / "sample").exists()
    assert not tuple((manager.root / "subscriptions").glob(".sample.tmp-*"))


def test_subscribe_installs_verified_checkout_then_records_subscription(tmp_path: Path) -> None:
    manager = _configured_manager(tmp_path)
    events: list[tuple[str, Any]] = []

    def verify(path: Path) -> object:
        assert ".sample.tmp-" in path.parent.name
        events.append(("verify", path))
        return {"catalog": str(path)}

    def authorize(verified: object) -> object:
        events.append(("authorize", verified))
        return verified

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=verify,
        authorizer=authorize,
        repository_factory=_StagingRepository,
    )

    receipt = subscriptions.subscribe("sample", _subscription())

    recorded = manager.load().profiles["default"].subscriptions["sample"]
    assert recorded == _subscription()
    assert receipt.name == "sample"
    assert receipt.checkout == manager.root / "subscriptions" / "sample"
    assert receipt.subscribed is True
    assert receipt.checkout_created is True
    assert (receipt.checkout / ".git").is_dir()
    assert [event[0] for event in events] == ["verify", "authorize"]


def test_subscribe_update_uses_new_checkout_and_preserves_previous_checkout(
    tmp_path: Path,
) -> None:
    manager = _configured_manager(tmp_path)
    previous = _subscription(checkout="subscriptions/previous")
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"sample": previous},
                )
            }
        )
    )
    previous_checkout = manager.subscription_checkout(previous)
    previous_checkout.mkdir(parents=True)
    (previous_checkout / "operator.txt").write_text("preserve me\n")
    replacement = _subscription(checkout="subscriptions/replacement")
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=_StagingRepository,
    )

    receipt = subscriptions.subscribe("sample", replacement)

    assert manager.load().profiles["default"].subscriptions["sample"] == replacement
    assert receipt.checkout == manager.subscription_checkout(replacement)
    assert (previous_checkout / "operator.txt").read_text() == "preserve me\n"


def _removable_checkout(manager: UserConfigManager, subscription: OntologySubscription) -> Path:
    checkout = manager.subscription_checkout(subscription)
    checkout.mkdir(parents=True)
    _git("init", "--initial-branch=main", cwd=checkout)
    (checkout / "ontology.yaml").write_text("version: 1\n")
    _git("add", "ontology.yaml", cwd=checkout)
    _git("commit", "-m", "seed", cwd=checkout)
    _git("remote", "add", "origin", subscription.url, cwd=checkout)
    branch = subscription.active_ref.removeprefix("refs/heads/")
    _git("update-ref", f"refs/geas-sync/{branch}", "HEAD", cwd=checkout)
    return checkout


def _manager_with_subscription(
    tmp_path: Path,
) -> tuple[UserConfigManager, OntologySubscription, Path]:
    manager = _configured_manager(tmp_path)
    subscription = _subscription()
    profile = GeasProfile(ontology_git=None, subscriptions={"sample": subscription})
    manager.replace(GeasUserConfig(profiles={"default": profile}))
    checkout = _removable_checkout(manager, subscription)
    return manager, subscription, checkout


def test_unsubscribe_preserves_checkout_by_default(tmp_path: Path) -> None:
    manager, _, checkout = _manager_with_subscription(tmp_path)
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
    )

    receipt = subscriptions.unsubscribe("sample")

    assert "sample" not in manager.load().profiles["default"].subscriptions
    assert checkout.is_dir()
    assert receipt.checkout_removed is False


def test_unsubscribe_removes_only_exact_clean_identity_matched_checkout(
    tmp_path: Path,
) -> None:
    manager, _, checkout = _manager_with_subscription(tmp_path)
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
    )

    receipt = subscriptions.unsubscribe("sample", remove_checkout=True)

    assert "sample" not in manager.load().profiles["default"].subscriptions
    assert not checkout.exists()
    assert receipt.checkout_removed is True


@pytest.mark.parametrize("problem", ("dirty", "dirty_gitignore", "origin"))
def test_unsubscribe_preserves_config_and_checkout_when_removal_is_unsafe(
    tmp_path: Path, problem: str
) -> None:
    manager, _, checkout = _manager_with_subscription(tmp_path)
    before = manager.path.read_bytes()
    if problem == "dirty":
        (checkout / "uncommitted.yaml").write_text("dirty: true\n")
    elif problem == "dirty_gitignore":
        (checkout / ".gitignore").write_text("operator content\n")
    else:
        _git("remote", "set-url", "origin", "https://wrong.example.invalid/repo.git", cwd=checkout)
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
    )

    with pytest.raises(RuntimeError, match="local changes|remote.*does not match"):
        subscriptions.unsubscribe("sample", remove_checkout=True)

    assert manager.path.read_bytes() == before
    assert checkout.is_dir()
