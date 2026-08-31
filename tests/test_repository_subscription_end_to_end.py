"""Offline production-path contract for the maintained repository subscription."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.cli as cli
from research_agent.agent_skills import validate_snapshot
from research_agent.geas_update import GeasUpdater
from research_agent.ontology_artifacts import OntologyArtifact, OntologyArtifactManifest
from research_agent.ontology_subscriptions import OntologyFreshnessConfig, SubscriptionManager
from research_agent.ontology_trust import (
    TrustRule,
    authorize_repository_catalog,
    install_snapshot,
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


def _inspected_checkout_provenance(checkout: Path) -> tuple[str, str]:
    """Use the real Geas provenance verifier over the committed local checkout."""
    executable = checkout / "bin" / "geas"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    _git(checkout, "add", "bin/geas")
    _git(checkout, "commit", "-m", "offline test Geas entrypoint")
    provenance = GeasUpdater(
        source_directory=checkout / "src" / "research_agent",
        executable=executable,
        module_file=checkout / "src" / "research_agent" / "geas_update.py",
        installed_version=cli._installed_geas_version,
    ).inspect()
    return provenance.version, provenance.commit


def _offline_subprocess_env(tmp_path: Path, checkout: Path) -> dict[str, str]:
    """Deny Python socket use while keeping the demo's local interpreter usable."""
    environment = tmp_path / "demo-venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    site = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site.mkdir(parents=True, exist_ok=True)
    source_packages = Path(sysconfig.get_paths()["purelib"])
    (site / "geas-offline-test.pth").write_text(
        f"{REPOSITORY_ROOT / 'src'}\n{source_packages}\n"
    )
    (environment / "bin" / "geas").write_text(
        f"#!/bin/sh\nexec {environment / 'bin' / 'python'} -m research_agent \"$@\"\n"
    )
    (environment / "bin" / "geas").chmod(0o700)
    (site / "sitecustomize.py").write_text(
        """import os
import socket
from pathlib import Path

Path(os.environ["GEAS_TEST_NETWORK_SENTINEL"]).write_text("socket denial active\\n")

def _deny(*_args, **_kwargs):
    raise AssertionError(\"offline integration test forbids subprocess network access\")

class _DeniedSocket(socket.socket):
    def connect(self, *_args, **_kwargs):
        _deny()

socket.socket = _DeniedSocket
socket.create_connection = _deny
"""
    )
    home = tmp_path / "subprocess-home"
    config = tmp_path / "subprocess-config"
    command_bin = tmp_path / "offline-command-bin"
    sentinel = tmp_path / "network-sentinel"
    temporary = tmp_path / "subprocess-tmp"
    home.mkdir()
    command_bin.mkdir()
    config.mkdir()
    temporary.mkdir()
    (command_bin / "uv").write_text(
        """#!/bin/sh
if [ "$1" != "run" ]; then
  echo "offline demo only permits uv run" >&2
  exit 2
fi
shift
case "$1" in
  python) shift; exec "$GEAS_TEST_DEMO_PYTHON" "$@" ;;
  geas) shift; exec "$GEAS_TEST_DEMO_PYTHON" -m research_agent "$@" ;;
  *) echo "offline demo rejected command: $1" >&2; exit 2 ;;
esac
"""
    )
    (command_bin / "uv").chmod(0o700)
    return {
        "GEAS_CONFIG_HOME": str(config),
        "GEAS_TEST_NETWORK_SENTINEL": str(sentinel),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "GEAS_TEST_DEMO_PYTHON": str(environment / "bin" / "python"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{command_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "TMPDIR": str(temporary),
        "UV_NO_SYNC": "1",
        "UV_OFFLINE": "1",
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "XDG_CONFIG_HOME": str(config),
    }


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
    real_home = Path.home()
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    checkout = _local_public_origin_checkout(
        manager.root / "subscriptions" / "default" / "geas-samples"
    )
    geas_version, geas_commit = _inspected_checkout_provenance(checkout)
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
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "geas",
            "--geas-config",
            str(snapshot_manager.path),
            "ontology-snapshot-remove",
            ONTOLOGY,
            snapshot.bundle_sha256,
        ],
    )
    cli.main()
    removed_snapshot = _receipt(capsys)
    assert removed_snapshot == {
        "bundle_sha256": snapshot.bundle_sha256,
        "name": ONTOLOGY,
        "path": str(snapshot.path),
        "removed": True,
    }
    assert not (snapshot_manager.root / snapshot.path).exists()

    monkeypatch.setattr(sys, "argv", [
        "geas", "--geas-config", str(manager.path), "ontology-subscribe", "geas-samples",
        REPOSITORY_URL, "--ref", "refs/heads/main",
    ])
    cli.main()
    subscribed = _receipt(capsys)
    assert subscribed["subscribed"] is True
    assert calls == ["pull"]

    config_before_list = manager.path.read_bytes()
    for directory in (checkout, checkout / "ontology" / ONTOLOGY):
        listings: list[dict[str, object]] = []
        for _ in range(2):
            monkeypatch.chdir(directory)
            monkeypatch.setattr(
                sys,
                "argv",
                ["geas", "--geas-config", str(manager.path), "list"],
            )
            cli.main()
            listings.append(_receipt(capsys))
        assert listings[0] == listings[1]
        listed = listings[0]
        assert listed["location"] == "selected_profile"
        assert [item["name"] for item in listed["ontologies"]] == [ONTOLOGY, ONTOLOGY]
        assert {
            (item["source_kind"], item["source"])
            for item in listed["ontologies"]
        } == {
            ("repository", f"repository:{checkout / 'geas.yaml'}"),
            ("subscription", "subscription:geas-samples"),
        }
        assert all(item["trust_status"] == "trusted" for item in listed["ontologies"])
        assert all(item["commit"] == geas_commit for item in listed["ontologies"])
    assert manager.path.read_bytes() == config_before_list

    monkeypatch.chdir(neutral_cwd)

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

    verify_receipts: list[dict[str, object]] = []
    for _ in range(2):
        monkeypatch.setattr(
            sys,
            "argv",
            ["geas", "catalog-verify", str(checkout / "geas.yaml")],
        )
        cli.main()
        verify_receipts.append(_receipt(capsys))
    assert verify_receipts[0] == verify_receipts[1]
    assert verify_receipts[0]["count"] == 1

    config_before_sync = manager.path.read_bytes()
    sync_receipts: list[dict[str, object]] = []
    for _ in range(2):
        monkeypatch.setattr(
            sys,
            "argv",
            ["geas", "--geas-config", str(manager.path), "ontology-sync", "geas-samples"],
        )
        cli.main()
        sync_receipts.append(_receipt(capsys))
    assert sync_receipts[0] == sync_receipts[1]
    assert sync_receipts[0]["subscriptions"] == [
        {
            "error": None,
            "name": "geas-samples",
            "pull": {"commit": geas_commit, "offline": True},
            "push": None,
            "success": True,
        }
    ]
    assert manager.path.read_bytes() == config_before_sync
    assert calls == ["pull", "pull", "pull"]
    demo_root = tmp_path / "demo"
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEAS_PROVIDER", "GEAS_MODEL"):
        monkeypatch.setenv(name, "must-not-reach-the-offline-demo")
    subprocess_environment = _offline_subprocess_env(tmp_path, checkout)
    assert real_home != Path(subprocess_environment["HOME"])
    assert all(
        str(real_home) not in subprocess_environment[name]
        for name in ("HOME", "XDG_CONFIG_HOME", "GEAS_CONFIG_HOME", "TMPDIR")
    )
    assert not {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEAS_PROVIDER",
        "GEAS_MODEL",
    }.intersection(subprocess_environment)
    completed = subprocess.run(
        (str(checkout / "ontology" / ONTOLOGY / "demo.sh"), str(demo_root)),
        cwd=checkout,
        env=subprocess_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(subprocess_environment["GEAS_TEST_NETWORK_SENTINEL"]).read_text() == (
        "socket denial active\n"
    )
    assert json.loads(completed.stdout)["projection_schema"] == 9
    artifact = OntologyArtifactManifest.from_yaml(
        checkout / "ontology" / ONTOLOGY / "artifacts.yaml"
    ).artifacts[0]
    store = _PreseededArtifactStore(demo_root / "query.sqlite", artifact)
    monkeypatch.setattr(cli, "GitHubReleaseArtifactStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: (geas_version, geas_commit))
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
    assert exports[0]["snapshot_sha256"] == exports[1]["snapshot_sha256"]
    assert exported.ontology.name == ONTOLOGY
    assert exported.ontology.repository_url == REPOSITORY_URL
    assert exported.ontology.active_ref == "refs/heads/main"
    assert exported.ontology.branch == "main"
    assert exported.ontology.commit == geas_commit
    assert exported.ontology.ontology_commit == geas_commit
    assert exported.ontology.subscription_name == "geas-samples"
    assert exported.ontology.catalog_path == "geas.yaml"
    assert exported.ontology.ontology_path == f"ontology/{ONTOLOGY}"
    assert exported.ontology.bundle_sha256 == catalog_ontology.bundle_sha256
    assert exported.geas.version == geas_version
    assert exported.geas.commit == geas_commit
    assert exported.geas.project_url == "https://github.com/Epiphytic/geas"
    assert exported.artifact is not None
    assert exported.artifact.role == "knowledge-projection"
    assert exported.artifact.content_sha256 == artifact.content_sha256
    assert exported.artifact.input_revision == artifact.input_revision
    assert exported.projection.snapshot_id == json.loads(
        (demo_root / "snapshot.json").read_text()
    )["id"]
    assert exported.projection.topic_concept_id == "concept:open-source-research-agents"
    assert store.downloads == 1

    monkeypatch.setattr(sys, "argv", [
        "geas", "--geas-config", str(manager.path), "ontology-unsubscribe", "geas-samples",
    ])
    cli.main()
    unsubscribed = _receipt(capsys)
    assert unsubscribed["checkout_removed"] is False
    assert calls == ["pull", "pull", "pull"]
    assert checkout.is_dir()
