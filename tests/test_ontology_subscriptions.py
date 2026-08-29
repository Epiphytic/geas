from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_agent.ontology_subscriptions import (
    OntologyFreshnessConfig,
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
        ({"checkout": "subscriptions//alias"}, "normalized"),
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


@pytest.mark.parametrize(
    "url",
    (
        "https://example.invalid/repository.git",
        "ssh://git@example.invalid/owner/repository.git",
        "git@example.invalid:owner/repository.git",
    ),
)
def test_subscription_accepts_explicit_supported_remote_transports(url: str) -> None:
    assert (
        OntologySubscription(
            url=url,
            checkout=Path("subscriptions/example"),
        ).url
        == url
    )


@pytest.mark.parametrize(
    "active_ref",
    (
        "refs/heads/topic.lock",
        "refs/heads/has space",
        "refs/heads/control\x01",
        "refs/heads/a..b",
        "refs/heads/a@{b",
        "refs/heads/trailing.",
        "refs/heads/trailing/",
        "refs/heads/.hidden",
        "refs/tags/question?mark",
    ),
)
def test_subscribe_revalidates_git_refs_before_any_write(tmp_path: Path, active_ref: str) -> None:
    manager = _configured_manager(tmp_path)
    before = manager.path.read_bytes()
    calls: list[str] = []
    invalid = OntologySubscription.model_construct(
        url=URL,
        active_ref=active_ref,
        checkout=Path("subscriptions/invalid"),
        catalog=Path("geas.yaml"),
        remote="origin",
        pull_before_update=False,
        push_on_update=False,
        freshness=OntologyFreshnessConfig(),
    )
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: calls.append("verify"),
        authorizer=lambda verified: calls.append("authorize"),
        repository_factory=lambda checkout, subscription: calls.append("repository"),
    )

    with pytest.raises(ValueError, match="active_ref"):
        subscriptions.subscribe("invalid", invalid)

    assert manager.path.read_bytes() == before
    assert calls == []
    assert not (manager.root / "subscriptions").exists()


@pytest.mark.parametrize(
    "url",
    (
        "http://example.invalid/repository.git",
        "file:///tmp/repository.git",
        "ftp://example.invalid/repository.git",
        "../repository.git",
        "repository.git",
        "https://token@example.invalid/repository.git",
        "https://example.invalid/repository.git?token=secret",
        "https://example.invalid/repository.git#token=secret",
        "https://example.invalid/a/../repository.git",
        "https://example.invalid/a/%2e%2e/repository.git",
    ),
)
def test_subscribe_revalidates_supported_credential_free_remote_before_writes(
    tmp_path: Path, url: str
) -> None:
    manager = _configured_manager(tmp_path)
    before = manager.path.read_bytes()
    calls: list[str] = []
    invalid = OntologySubscription.model_construct(
        url=url,
        active_ref="refs/heads/main",
        checkout=Path("subscriptions/invalid"),
        catalog=Path("geas.yaml"),
        remote="origin",
        pull_before_update=False,
        push_on_update=False,
        freshness=OntologyFreshnessConfig(),
    )
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: calls.append("verify"),
        authorizer=lambda verified: calls.append("authorize"),
        repository_factory=lambda checkout, subscription: calls.append("repository"),
    )

    with pytest.raises(ValueError, match="URL|remote|transport|credentials"):
        subscriptions.subscribe("invalid", invalid)

    assert manager.path.read_bytes() == before
    assert calls == []
    assert not (manager.root / "subscriptions").exists()


def test_profile_rejects_ancestor_and_descendant_subscription_checkouts() -> None:
    with pytest.raises(ValueError, match="overlap"):
        GeasProfile(
            ontology_git=None,
            subscriptions={
                "outer": _subscription(checkout="repositories/outer"),
                "ignored-nested": _subscription(checkout="repositories/outer/vendor/nested"),
            },
        )


def test_subscribe_rejects_new_nested_checkout_before_repository_work(
    tmp_path: Path,
) -> None:
    manager = _configured_manager(tmp_path)
    profile = GeasProfile(
        ontology_git=None,
        subscriptions={"outer": _subscription(checkout="repositories/outer")},
    )
    manager.replace(GeasUserConfig(profiles={"default": profile}))
    before = manager.path.read_bytes()
    calls: list[str] = []
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: calls.append("verify"),
        authorizer=lambda verified: calls.append("authorize"),
        repository_factory=lambda checkout, configured: calls.append("repository"),
    )

    with pytest.raises(ValueError, match="overlap"):
        subscriptions.subscribe(
            "nested",
            _subscription(checkout="repositories/outer/vendor/nested"),
        )

    assert calls == []
    assert manager.path.read_bytes() == before
    assert not (manager.root / "repositories").exists()


def test_config_rejects_in_root_symlink_alias_without_rewriting_config(
    tmp_path: Path,
) -> None:
    manager = _configured_manager(tmp_path)
    before = manager.path.read_bytes()
    actual = manager.root / "repositories" / "actual"
    actual.mkdir(parents=True)
    alias = manager.root / "repositories" / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    candidate = GeasUserConfig(
        profiles={
            "default": GeasProfile(
                ontology_git=None,
                subscriptions={
                    "actual": _subscription(checkout="repositories/actual"),
                    "alias": _subscription(checkout="repositories/alias"),
                },
            )
        }
    )

    with pytest.raises(ValueError, match="symbolic link|same checkout"):
        manager.replace(candidate)

    assert manager.path.read_bytes() == before
    assert alias.is_symlink()
    assert actual.is_dir()


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


def test_sync_catches_arbitrary_exception_and_continues_to_later_sibling(
    tmp_path: Path,
) -> None:
    manager = _configured_manager(tmp_path)
    profile = GeasProfile(
        ontology_git=None,
        subscriptions={
            "alpha": _subscription(checkout="alpha"),
            "beta": _subscription(checkout="beta"),
        },
    )
    manager.replace(GeasUserConfig(profiles={"default": profile}))

    class Repository:
        def __init__(self, checkout: Path, subscription: OntologySubscription) -> None:
            self.checkout = checkout

        def pull(self) -> dict[str, object]:
            self.checkout.mkdir()
            return {"commit": "a" * 40}

        def push(self) -> dict[str, object]:
            return {"pushed": False}

        def assert_removable(self) -> None:
            return None

    def verify(path: Path) -> object:
        if path.parent.name == "alpha":
            raise ArithmeticError("arbitrary verifier failure")
        return path

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=verify,
        authorizer=lambda verified: verified,
        repository_factory=Repository,
    )

    receipts = subscriptions.sync()

    assert [(item.name, item.success) for item in receipts] == [
        ("alpha", False),
        ("beta", True),
    ]
    assert receipts[0].error == "arbitrary verifier failure"


def test_sync_propagates_process_control_base_exception(tmp_path: Path) -> None:
    manager = _configured_manager(tmp_path)
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"sample": _subscription(checkout="sample")},
                )
            }
        )
    )

    class Repository:
        def pull(self) -> dict[str, object]:
            raise KeyboardInterrupt

        def push(self) -> dict[str, object]:
            return {}

        def assert_removable(self) -> None:
            return None

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: path,
        authorizer=lambda verified: verified,
        repository_factory=lambda checkout, configured: Repository(),
    )

    with pytest.raises(KeyboardInterrupt):
        subscriptions.sync()


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


def test_unsubscribe_missing_remote_is_read_only_and_preserves_everything(
    tmp_path: Path,
) -> None:
    manager, _, checkout = _manager_with_subscription(tmp_path)
    _git("remote", "remove", "origin", cwd=checkout)
    before_config = manager.path.read_bytes()
    before_git_config = (checkout / ".git" / "config").read_bytes()
    before_remotes = _git("remote", cwd=checkout).stdout
    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
    )

    with pytest.raises(RuntimeError, match="remote.*identity|missing"):
        subscriptions.unsubscribe("sample", remove_checkout=True)

    assert manager.path.read_bytes() == before_config
    assert (checkout / ".git" / "config").read_bytes() == before_git_config
    assert _git("remote", cwd=checkout).stdout == before_remotes
    assert checkout.is_dir()


def test_unsubscribe_rechecks_symlink_race_before_quarantine_or_config_write(
    tmp_path: Path,
) -> None:
    manager, subscription, checkout = _manager_with_subscription(tmp_path)
    before = manager.path.read_bytes()
    original = checkout.with_name("original-preserved")
    target = checkout.with_name("race-target")
    target.mkdir()
    (target / "must-remain.txt").write_text("preserve\n")
    replace_calls: list[str] = []

    class RacingRepository:
        def pull(self) -> dict[str, object]:
            return {}

        def push(self) -> dict[str, object]:
            return {}

        def assert_removable(self) -> None:
            checkout.rename(original)
            checkout.symlink_to(target, target_is_directory=True)

    subscriptions = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda verified: verified,
        repository_factory=lambda path, configured: RacingRepository(),
    )
    real_replace = manager.replace

    def record_replace(config: GeasUserConfig) -> None:
        replace_calls.append("config")
        real_replace(config)

    manager.replace = record_replace  # type: ignore[method-assign]

    with pytest.raises((RuntimeError, ValueError), match="symbolic link"):
        subscriptions.unsubscribe("sample", remove_checkout=True)

    assert replace_calls == []
    assert manager.path.read_bytes() == before
    assert checkout.is_symlink()
    assert original.is_dir()
    assert (target / "must-remain.txt").read_text() == "preserve\n"
    assert subscription.checkout == Path("subscriptions/sample")


def test_legacy_primary_inherits_global_freshness_without_moving_checkout() -> None:
    freshness = OntologyFreshnessConfig(
        check_before_use=False,
        max_age_seconds=7200,
        hydrate_artifacts_before_use=True,
    )
    profile = GeasProfile(ontology_git=OntologyGitConfig(url=URL))
    config = GeasUserConfig(
        ontology_freshness=freshness,
        profiles={"default": profile},
    )

    primary = config.normalized_profile().subscriptions["primary"]

    assert primary.freshness == freshness
    assert primary.checkout == Path("ontologies")


def test_explicit_subscription_serializes_strict_freshness(tmp_path: Path) -> None:
    manager = _configured_manager(tmp_path)
    subscription = _subscription().model_copy(
        update={
            "freshness": OntologyFreshnessConfig(
                check_before_use=False,
                max_age_seconds=900,
                hydrate_artifacts_before_use=True,
            )
        }
    )
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"sample": subscription},
                )
            }
        )
    )

    serialized = __import__("yaml").safe_load(manager.path.read_text())

    assert serialized["profiles"]["default"]["subscriptions"]["sample"]["freshness"] == {
        "check_before_use": False,
        "max_age_seconds": 900,
        "hydrate_artifacts_before_use": True,
    }
    with pytest.raises(ValueError, match="greater than or equal to 60"):
        OntologySubscription(
            url=URL,
            checkout=Path("subscriptions/invalid"),
            freshness={"max_age_seconds": 1},
        )
