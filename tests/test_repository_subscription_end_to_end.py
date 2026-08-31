"""Offline production-path contract for the maintained repository subscription."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.cli as cli
from research_agent.agent_skills import validate_snapshot
from research_agent.ontology_artifacts import OntologyArtifact, OntologyArtifactManifest
from research_agent.ontology_subscriptions import OntologyFreshnessConfig, SubscriptionManager
from research_agent.ontology_trust import (
    TrustRule,
    authorize_repository_catalog,
    install_snapshot,
    remove_snapshot,
)
from research_agent.repository_catalog import verify_catalog
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/Epiphytic/geas.git"
ONTOLOGY = "open-source-research-agents"
INSTANT = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Offline Subscription Test",
            "GIT_AUTHOR_EMAIL": "geas-offline-subscription@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Offline Subscription Test",
            "GIT_COMMITTER_EMAIL": "geas-offline-subscription@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _local_public_origin_checkout(destination: Path) -> Path:
    """Clone only local objects, then assert the public remote identity Geas trusts."""
    subprocess.run(
        ("git", "clone", "--quiet", "--no-hardlinks", str(REPOSITORY_ROOT), str(destination)),
        text=True,
        capture_output=True,
        check=True,
    )
    _git(destination, "checkout", "-B", "main")
    _git(destination, "remote", "set-url", "origin", REPOSITORY_URL)
    assert _git(destination, "remote", "get-url", "origin") == REPOSITORY_URL
    return destination


class _OfflineRepository:
    """A complete Git operator that cannot fetch, push, or invoke a model."""

    def __init__(self, checkout: Path, *, calls: list[str]) -> None:
        self.checkout = checkout
        self.calls = calls

    def pull(self) -> dict[str, object]:
        self.calls.append("pull")
        assert _git(self.checkout, "remote", "get-url", "origin") == REPOSITORY_URL
        return {"commit": _git(self.checkout, "rev-parse", "HEAD"), "offline": True}

    def push(self) -> dict[str, object]:
        raise AssertionError("offline subscription test must never push")

    def assert_removable(self) -> None:
        self.calls.append("remove")
        assert not _git(self.checkout, "status", "--porcelain")


class _PreseededArtifactStore:
    """Only the locally built projection can satisfy a skill export."""

    def __init__(self, source: Path, artifact: OntologyArtifact) -> None:
        self.source = source
        self.artifact = artifact
        self.downloads = 0

    def available(self, artifact: OntologyArtifact) -> bool:
        return artifact == self.artifact

    def ensure(self, _artifact: OntologyArtifact, _source: Path) -> bool:
        raise AssertionError("offline subscription test must never publish an artifact")

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        assert artifact == self.artifact
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == artifact.content_sha256
        assert self.source.stat().st_size == artifact.size_bytes
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, destination)


def _receipt(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_current_repository_subscription_is_deterministic_and_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches a live fetch/model call, nondurable yolo, or unsafe cleanup regression."""
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    checkout = _local_public_origin_checkout(
        manager.root / "subscriptions" / "default" / "geas-samples"
    )
    manager.replace(
        GeasUserConfig(
            ontology_freshness=OntologyFreshnessConfig(check_before_use=False),
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    trust_rules=(
                        TrustRule(
                            decision="allow",
                            repository="https://github.com/Epiphytic/geas",
                            created_at=INSTANT,
                            created_via="manual",
                        ),
                    ),
                )
            },
        )
    )
    calls: list[str] = []

    def subscription_service(
        args: object,
        *,
        manager: UserConfigManager,
        profile_name: str,
    ) -> SubscriptionManager:
        return SubscriptionManager(
            config_manager=manager,
            profile_name=profile_name,
            catalog_verifier=cli._subscription_catalog,
            authorizer=lambda catalog: authorize_repository_catalog(
                catalog,
                manager=manager,
                profile_name=profile_name,
                yolo=bool(args.yolo),
                prompt=None,
            ),
            repository_factory=lambda path, _subscription: _OfflineRepository(path, calls=calls),
        )

    monkeypatch.setattr(cli, "_subscription_service", subscription_service)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    neutral_cwd = tmp_path / "neutral-cwd"
    neutral_cwd.mkdir()
    monkeypatch.chdir(neutral_cwd)

    snapshot_manager = UserConfigManager(tmp_path / "snapshots" / "config.yaml")
    snapshot_manager.replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    catalog_ontology = verify_catalog(checkout / "geas.yaml")[0]
    snapshot = install_snapshot(
        catalog_ontology,
        manager=snapshot_manager,
        profile_name="default",
    )
    assert (snapshot_manager.root / snapshot.path).is_dir()
    removed_snapshot = remove_snapshot(
        snapshot,
        manager=snapshot_manager,
        profile_name="default",
    )
    assert removed_snapshot.removed is True
    assert not (snapshot_manager.root / snapshot.path).exists()

    monkeypatch.setattr(sys, "argv", [
        "geas", "--geas-config", str(manager.path), "ontology-subscribe", "geas-samples",
        REPOSITORY_URL, "--ref", "refs/heads/main",
    ])
    cli.main()
    subscribed = _receipt(capsys)
    assert subscribed["subscribed"] is True
    assert calls == ["pull"]

    for directory in (checkout, checkout / "ontology" / ONTOLOGY):
        monkeypatch.setattr(sys, "argv", [
            "geas", "--geas-config", str(manager.path), "list", str(directory),
        ])
        cli.main()
        listed = _receipt(capsys)
        assert [item["name"] for item in listed["ontologies"]] == [ONTOLOGY]
        assert listed["ontologies"][0]["trust_status"] == "trusted"

    yolo_manager = UserConfigManager(tmp_path / "yolo" / "config.yaml")
    yolo_manager.replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    yolo_before = yolo_manager.path.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        "geas", "--geas-config", str(yolo_manager.path), "--yolo", "list", str(checkout),
    ])
    cli.main()
    yolo_list = _receipt(capsys)
    assert yolo_list["ontologies"][0]["trust_status"] == "trusted"
    assert yolo_manager.path.read_bytes() == yolo_before

    monkeypatch.setattr(sys, "argv", ["geas", "catalog-verify", str(checkout / "geas.yaml")])
    cli.main()
    verified = _receipt(capsys)
    assert verified["count"] == 1
    demo_root = tmp_path / "demo"
    completed = subprocess.run(
        (str(checkout / "ontology" / ONTOLOGY / "demo.sh"), str(demo_root)),
        cwd=checkout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout)["projection_schema"] == 9
    artifact = OntologyArtifactManifest.from_yaml(
        checkout / "ontology" / ONTOLOGY / "artifacts.yaml"
    ).artifacts[0]
    store = _PreseededArtifactStore(demo_root / "query.sqlite", artifact)
    monkeypatch.setattr(cli, "GitHubReleaseArtifactStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", "a" * 40))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    exports: list[dict[str, object]] = []
    for _ in range(2):
        monkeypatch.setattr(sys, "argv", [
            "geas", "--geas-config", str(manager.path), "skill-export", ONTOLOGY,
        ])
        cli.main()
        exports.append(_receipt(capsys))
    exported = validate_snapshot(Path(str(exports[-1]["path"])))
    assert [item["unchanged"] for item in exports] == [False, True]
    assert exported.ontology.bundle_sha256 == catalog_ontology.bundle_sha256
    assert store.downloads == 1

    monkeypatch.setattr(sys, "argv", [
        "geas", "--geas-config", str(manager.path), "ontology-unsubscribe", "geas-samples",
    ])
    cli.main()
    unsubscribed = _receipt(capsys)
    assert unsubscribed["checkout_removed"] is False
    assert calls == ["pull"]
    assert checkout.is_dir()
