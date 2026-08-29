"""Catalog-bound portable skill export and update contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import research_agent.cli as cli
from research_agent.agent_skills import OntologyIdentity, validate_snapshot
from research_agent.bundles import KnowledgeBundleImporter
from research_agent.geas_update import GeasUpdateReceipt
from research_agent.ontology_artifacts import OntologyArtifact, OntologyArtifactManager
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_subscriptions import (
    OntologyFreshnessConfig,
    OntologySubscription,
)
from research_agent.ontology_trust import TrustRule
from research_agent.projection import SQLiteKnowledgeProjection
from research_agent.repository_catalog import load_catalog, refresh_catalog
from research_agent.store import ImmutableStore
from research_agent.truth import TruthManager, TruthPolicy
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

REPOSITORY_URL = "https://github.com/example/catalog-skill-fixture.git"
ACTIVE_REF = "refs/heads/main"
GEAS_OLD_COMMIT = "a" * 40
GEAS_NEW_COMMIT = "b" * 40
INSTANT = datetime(2026, 8, 29, 12, tzinfo=UTC)


class _MemoryArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.values: dict[tuple[str, str], Path] = {}

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        destination = self.root / artifact.asset_name
        shutil.copyfile(source, destination)
        self.values[(artifact.release_tag, artifact.asset_name)] = destination
        return True

    def available(self, artifact: OntologyArtifact) -> bool:
        return (artifact.release_tag, artifact.asset_name) in self.values

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        shutil.copyfile(self.values[(artifact.release_tag, artifact.asset_name)], destination)


class _FakeGeasUpdater:
    def update_and_reexec(
        self, _argv: tuple[str, ...], *, continuation: str | None
    ) -> GeasUpdateReceipt:
        assert continuation == "catalog-test"
        return GeasUpdateReceipt(
            installer="git-development",
            directory=Path("/trusted/geas"),
            executable=Path("/trusted/geas/bin/geas"),
            old_commit=GEAS_OLD_COMMIT,
            new_commit=GEAS_NEW_COMMIT,
            old_version="0.1.0",
            new_version="0.1.0",
            reinstalled=True,
            reexec_depth=1,
        )


class _MismatchedGeasUpdater(_FakeGeasUpdater):
    def update_and_reexec(
        self, argv: tuple[str, ...], *, continuation: str | None
    ) -> GeasUpdateReceipt:
        receipt = super().update_and_reexec(argv, continuation=continuation)
        return receipt.model_copy(update={"old_commit": "c" * 40})


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Catalog Skill Test",
            "GIT_AUTHOR_EMAIL": "geas-catalog-skill@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Catalog Skill Test",
            "GIT_COMMITTER_EMAIL": "geas-catalog-skill@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _projection(tmp_path: Path) -> Path:
    root = tmp_path / "projection-data"
    store = ImmutableStore(root)
    KnowledgeBundleImporter(store=store).import_bundle(
        Path("ontology/open-source-research-agents/bundle.yaml"),
        imported_by="operator:catalog-skill-test",
    )
    truth = TruthManager(
        workspace_root=Path("."),
        store_root=root,
        policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
        clock=lambda: INSTANT,
    )
    snapshot = truth.capture(created_by="operator:catalog-skill-test")
    database = tmp_path / "query.sqlite"
    SQLiteKnowledgeProjection(store=store, workspace_root=Path(".")).build(
        database,
        snapshot=snapshot,
        truth_manager=truth,
    )
    return database


def _catalog_subscription(tmp_path: Path) -> tuple[UserConfigManager, Path, str, str, str]:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    checkout = manager.root / "subscriptions" / "default" / "samples"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "--initial-branch=main")
    _git(checkout, "remote", "add", "origin", REPOSITORY_URL)

    ontology = checkout / "ontology" / "research-agents"
    ontology.mkdir(parents=True)
    (ontology / "build.yaml").write_text(
        OntologyBuildConfig(
            version=1,
            topic="Open-source research agents",
            topic_concept_id="concept:open-source-research-agents",
            provider="deepseek_local",
            output_directory=Path("data/generated"),
        ).explicit_yaml()
    )
    artifact_store = _MemoryArtifactStore(tmp_path / "artifact-store")
    published = OntologyArtifactManager(ontology).publish(
        store=artifact_store,
        published_by="operator:catalog-skill-test",
        storage_rights_basis="offline deterministic test fixture",
        knowledge_projection=_projection(tmp_path),
    )
    artifact = published.artifacts[0]
    cache = ontology / ".geas-artifacts" / "query.sqlite"
    cache.parent.mkdir()
    shutil.copyfile(
        artifact_store.values[(artifact.release_tag, artifact.asset_name)],
        cache,
    )

    catalog_path = checkout / "geas.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ontologies": [
                    {
                        "name": "research-agents",
                        "description": "Maintained research-agent knowledge.",
                        "path": "ontology/research-agents",
                        "files": [
                            {
                                "path": "artifacts.yaml",
                                "sha256": "0" * 64,
                                "size_bytes": 0,
                            },
                            {
                                "path": "build.yaml",
                                "sha256": "0" * 64,
                                "size_bytes": 0,
                            },
                        ],
                        "bundle_sha256": "0" * 64,
                    }
                ],
            },
            sort_keys=False,
        )
    )
    refresh_catalog(catalog_path)
    _git(checkout, "add", "geas.yaml", "ontology/research-agents/artifacts.yaml")
    _git(checkout, "add", "ontology/research-agents/build.yaml")
    _git(checkout, "commit", "-m", "catalog skill fixture")
    commit = _git(checkout, "rev-parse", "HEAD")
    bundle_sha256 = load_catalog(catalog_path).ontologies[0].bundle_sha256

    subscription = OntologySubscription(
        url=REPOSITORY_URL,
        active_ref=ACTIVE_REF,
        checkout=Path("subscriptions/default/samples"),
        freshness=OntologyFreshnessConfig(check_before_use=False),
    )
    manager.replace(
        GeasUserConfig(
            ontology_freshness=OntologyFreshnessConfig(check_before_use=False),
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"samples": subscription},
                    trust_rules=(
                        TrustRule(
                            decision="allow",
                            repository=REPOSITORY_URL.removesuffix(".git"),
                            created_at=INSTANT,
                            created_via="manual",
                        ),
                    ),
                )
            },
        )
    )
    return manager, ontology, commit, bundle_sha256, artifact.content_sha256


def _run(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def _snapshot_bytes(snapshot: Path) -> dict[str, bytes]:
    return {
        path.relative_to(snapshot).as_posix(): path.read_bytes()
        for path in sorted(snapshot.rglob("*"))
        if path.is_file()
    }


def test_subscription_export_records_complete_catalog_and_artifact_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _ontology, commit, bundle_sha256, artifact_sha256 = _catalog_subscription(
        tmp_path
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", GEAS_OLD_COMMIT))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    _run(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "skill-export",
        "research-agents",
    )

    payload = json.loads(capsys.readouterr().out)
    snapshot = Path(payload["path"])
    manifest = validate_snapshot(snapshot)
    assert manifest.ontology.repository_url == REPOSITORY_URL
    assert manifest.ontology.active_ref == ACTIVE_REF
    assert manifest.ontology.ontology_commit == commit
    assert manifest.ontology.subscription_name == "samples"
    assert manifest.ontology.catalog_path == "geas.yaml"
    assert manifest.ontology.ontology_path == "ontology/research-agents"
    assert manifest.ontology.bundle_sha256 == bundle_sha256
    assert manifest.artifact is not None
    assert manifest.artifact.role == "knowledge-projection"
    assert manifest.artifact.content_sha256 == artifact_sha256
    assert len(manifest.artifact.input_revision) == 64

    entrypoint = (snapshot / "SKILL.md").read_text()
    assert "[reference index](references/index.md)" in entrypoint
    assert REPOSITORY_URL in entrypoint
    assert "geas list" in entrypoint
    assert "geas topic-show" in entrypoint
    assert "geas skill-update /absolute/path/to/directory-containing-this-SKILL" in entrypoint
    assert "geas skill-remove /absolute/path/to/directory-containing-this-SKILL" in entrypoint
    assert "https://github.com/Epiphytic/geas" in entrypoint


def test_catalog_integrity_failure_preserves_previous_complete_skill_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, ontology, commit, _bundle_sha256, _artifact_sha256 = _catalog_subscription(
        tmp_path
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", GEAS_OLD_COMMIT))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "skill-export",
        "research-agents",
    )
    snapshot = Path(json.loads(capsys.readouterr().out)["path"])
    before = _snapshot_bytes(snapshot)

    (ontology / "build.yaml").write_text("version: 1\ntampered: true\n")

    class _NoWriteRepository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def pull(self) -> dict[str, object]:
            return {"commit": commit}

    monkeypatch.setattr(cli, "GeasUpdater", _FakeGeasUpdater)
    monkeypatch.setattr(cli, "OntologyRepositoryManager", _NoWriteRepository)

    with pytest.raises(SystemExit) as failure:
        _run(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "skill-update",
            str(snapshot),
            "--geas-update-continuation",
            "catalog-test",
        )

    captured = capsys.readouterr()
    assert failure.value.code == 1
    assert "mismatch" in captured.err
    assert _snapshot_bytes(snapshot) == before
    assert validate_snapshot(snapshot).ontology.ontology_commit == commit


def test_packaged_geas_skill_documents_catalog_bound_skill_lifecycle() -> None:
    cli_reference = Path(
        "src/research_agent/builtin_skills/geas/references/cli.md"
    ).read_text()
    lifecycle = Path(
        "src/research_agent/builtin_skills/geas/references/skills.md"
    ).read_text()

    for command in (
        "geas list",
        "geas ontology-subscribe",
        "geas ontology-sync",
        "geas ontology-unsubscribe",
    ):
        assert command in cli_reference
    for field in (
        "repository_url",
        "active_ref",
        "ontology_commit",
        "catalog_path",
        "ontology_path",
        "bundle_sha256",
        "content_sha256",
        "input_revision",
    ):
        assert f"`{field}`" in lifecycle
    assert "previous complete snapshot" in lifecycle


def test_catalog_skill_identity_accepts_sha256_git_object_ids() -> None:
    commit = "c" * 64

    identity = OntologyIdentity(
        name="research-agents",
        repository_url=REPOSITORY_URL,
        branch=commit,
        commit=commit,
        active_ref=commit,
        ontology_commit=commit,
        subscription_name="samples",
        catalog_path="geas.yaml",
        ontology_path="ontology/research-agents",
        bundle_sha256="d" * 64,
    )

    assert identity.active_ref == commit
    assert identity.ontology_commit == commit


def test_update_rejects_executing_geas_identity_mismatch_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _ontology, commit, _bundle_sha256, _artifact_sha256 = _catalog_subscription(
        tmp_path
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", GEAS_OLD_COMMIT))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "skill-export",
        "research-agents",
    )
    snapshot = Path(json.loads(capsys.readouterr().out)["path"])
    before = _snapshot_bytes(snapshot)

    class _NoWriteRepository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def pull(self) -> dict[str, object]:
            return {"commit": commit}

    monkeypatch.setattr(cli, "GeasUpdater", _MismatchedGeasUpdater)
    monkeypatch.setattr(cli, "OntologyRepositoryManager", _NoWriteRepository)

    with pytest.raises(SystemExit) as failure:
        _run(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "skill-update",
            str(snapshot),
            "--geas-update-continuation",
            "catalog-test",
        )

    captured = capsys.readouterr()
    assert failure.value.code == 1
    assert "executing Geas identity" in captured.err
    assert _snapshot_bytes(snapshot) == before


def test_update_resolves_the_manifest_subscription_despite_cwd_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _ontology, commit, bundle_sha256, artifact_sha256 = _catalog_subscription(
        tmp_path
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", GEAS_OLD_COMMIT))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "skill-export",
        "research-agents",
    )
    snapshot = Path(json.loads(capsys.readouterr().out)["path"])

    collision = tmp_path / "consumer"
    collision.mkdir()
    _git(collision, "init", "--initial-branch=main")
    local_ontology = collision / "ontology" / "research-agents"
    local_ontology.mkdir(parents=True)
    (local_ontology / "payload.txt").write_text("untrusted local collision\n")
    (collision / "geas.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ontologies": [
                    {
                        "name": "research-agents",
                        "description": "Untrusted local collision.",
                        "path": "ontology/research-agents",
                        "files": [
                            {
                                "path": "payload.txt",
                                "sha256": "0" * 64,
                                "size_bytes": 0,
                            }
                        ],
                        "bundle_sha256": "0" * 64,
                    }
                ],
            },
            sort_keys=False,
        )
    )
    refresh_catalog(collision / "geas.yaml")
    _git(collision, "add", ".")
    _git(collision, "commit", "-m", "colliding local ontology")
    monkeypatch.chdir(collision)

    class _NoWriteRepository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def pull(self) -> dict[str, object]:
            return {"commit": commit}

    monkeypatch.setattr(cli, "GeasUpdater", _FakeGeasUpdater)
    monkeypatch.setattr(cli, "OntologyRepositoryManager", _NoWriteRepository)

    _run(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "skill-update",
        str(snapshot),
        "--geas-update-continuation",
        "catalog-test",
    )

    updated = validate_snapshot(snapshot)
    assert updated.ontology.subscription_name == "samples"
    assert updated.ontology.bundle_sha256 == bundle_sha256
    assert updated.artifact is not None
    assert updated.artifact.content_sha256 == artifact_sha256
    assert updated.geas.commit == GEAS_NEW_COMMIT
