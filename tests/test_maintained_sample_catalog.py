"""Production contracts for the repository's maintained ontology catalog."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import research_agent.cli as cli
from research_agent.agent_skills import validate_snapshot
from research_agent.geas_update import GeasUpdateReceipt
from research_agent.ontology_artifacts import (
    ArtifactRole,
    OntologyArtifact,
    OntologyArtifactManifest,
)
from research_agent.ontology_resolution import resolve_ontology_catalog, select_ontology
from research_agent.ontology_subscriptions import (
    OntologyFreshnessConfig,
    OntologySubscription,
)
from research_agent.ontology_trust import TrustRule
from research_agent.repository_catalog import verify_catalog
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPOSITORY_ROOT / "geas.yaml"
ONTOLOGY_NAME = "open-source-research-agents"
EXPECTED_INVENTORY = (
    "artifacts.yaml",
    "build.yaml",
    "bundle.yaml",
    "demo.sh",
    "generated/alibaba-nlp-deepresearch/bundle.yaml",
    "generated/alibaba-nlp-deepresearch/sources/alibaba-nlp-deepresearch-4b453a820810.md",
    "generated/dzhng-deep-research/bundle.yaml",
    "generated/dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md",
    "library.yaml",
    "model-evaluation.yaml",
    "sources/deepresearchagent.md",
    "sources/deerflow.md",
    "sources/geas.md",
    "sources/gpt-researcher.md",
    "sources/langchain-open-deep-research.md",
    "sources/openresearcher.md",
    "sources/paperqa2.md",
    "sources/poisoned-source-fixture.md",
    "sources/storm.md",
    "tainted-sources.yaml",
)
EXPECTED_SEED_BUNDLES = (
    "bundle.yaml",
    "generated/alibaba-nlp-deepresearch/bundle.yaml",
    "generated/dzhng-deep-research/bundle.yaml",
)
REVISION_INSTANT = datetime(2026, 8, 29, 16, 30, tzinfo=UTC)
AUDIT_INSTANT = datetime(2026, 8, 29, 17, 0, tzinfo=UTC)
GEAS_OLD_COMMIT = "a" * 40
GEAS_NEW_COMMIT = "b" * 40


def test_root_catalog_verifies_the_exact_maintained_sample_inventory() -> None:
    """Fails if canonical inputs are implicit or runtime/cache bytes enter the bundle."""
    verified = verify_catalog(CATALOG)

    assert len(verified) == 1
    sample = verified[0]
    assert sample.name == ONTOLOGY_NAME
    assert sample.ontology_path == REPOSITORY_ROOT / "ontology" / ONTOLOGY_NAME
    assert tuple(item.path.as_posix() for item in sample.files) == EXPECTED_INVENTORY
    assert not any(
        part in {".geas-artifacts", "data", "records", "blobs"}
        or path.endswith((".sqlite", ".jsonl"))
        for path in EXPECTED_INVENTORY
        for part in Path(path).parts
    )


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Maintained Sample Test",
            "GIT_AUTHOR_EMAIL": "geas-sample@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Maintained Sample Test",
            "GIT_COMMITTER_EMAIL": "geas-sample@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _committed_sample_checkout(destination: Path) -> Path:
    subprocess.run(
        ("git", "clone", "--quiet", "--no-hardlinks", str(REPOSITORY_ROOT), str(destination)),
        text=True,
        capture_output=True,
        check=True,
    )
    _git(destination, "checkout", "-B", "main")
    target = destination / "ontology" / ONTOLOGY_NAME
    shutil.rmtree(target)
    shutil.copytree(REPOSITORY_ROOT / "ontology" / ONTOLOGY_NAME, target)
    shutil.copyfile(CATALOG, destination / "geas.yaml")
    shutil.copyfile(
        REPOSITORY_ROOT / "config" / "truth-policy.yaml",
        destination / "config" / "truth-policy.yaml",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "truth.py",
        destination / "src" / "research_agent" / "truth.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "cli.py",
        destination / "src" / "research_agent" / "cli.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "repository_catalog.py",
        destination / "src" / "research_agent" / "repository_catalog.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "ontology_artifacts.py",
        destination / "src" / "research_agent" / "ontology_artifacts.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "credential_scanning.py",
        destination / "src" / "research_agent" / "credential_scanning.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "src" / "research_agent" / "default_config" / "truth-policy.yaml",
        destination / "src" / "research_agent" / "default_config" / "truth-policy.yaml",
    )
    _git(destination, "remote", "set-url", "origin", "https://github.com/Epiphytic/geas.git")
    _git(
        destination,
        "add",
        "geas.yaml",
        f"ontology/{ONTOLOGY_NAME}",
        "config/truth-policy.yaml",
        "src/research_agent/truth.py",
        "src/research_agent/cli.py",
        "src/research_agent/repository_catalog.py",
        "src/research_agent/ontology_artifacts.py",
        "src/research_agent/credential_scanning.py",
        "src/research_agent/default_config/truth-policy.yaml",
    )
    if _git(destination, "status", "--porcelain"):
        _git(destination, "commit", "-m", "maintained sample fixture")
    return destination


def test_named_subscription_selects_the_development_bundle_digest(tmp_path: Path) -> None:
    development_digest = verify_catalog(CATALOG)[0].bundle_sha256
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    checkout = manager.root / "subscriptions" / "default" / "geas-samples"
    _committed_sample_checkout(checkout)
    subscription = OntologySubscription(
        url="https://github.com/Epiphytic/geas.git",
        active_ref="refs/heads/main",
        checkout=Path("subscriptions/default/geas-samples"),
        freshness=OntologyFreshnessConfig(check_before_use=False),
    )
    config = GeasUserConfig(
        ontology_freshness=OntologyFreshnessConfig(check_before_use=False),
        profiles={
            "default": GeasProfile(
                ontology_git=None,
                subscriptions={"geas-samples": subscription},
            )
        },
    )
    manager.replace(config)

    catalog = resolve_ontology_catalog(
        user_config=config,
        manager=manager,
        cwd=tmp_path,
        yolo=True,
        prompt=None,
    )
    selected = select_ontology(ONTOLOGY_NAME, catalog=catalog)

    assert selected.source == "subscription:geas-samples"
    assert selected.bundle_sha256 == development_digest


def test_maintained_projection_artifact_uses_the_current_strict_schema() -> None:
    manifest = OntologyArtifactManifest.from_yaml(
        REPOSITORY_ROOT / "ontology" / ONTOLOGY_NAME / "artifacts.yaml"
    )

    assert manifest.version == 1
    assert manifest.ontology == ONTOLOGY_NAME
    assert len(manifest.artifacts) == 1
    artifact = manifest.artifacts[0]
    assert artifact.role is ArtifactRole.KNOWLEDGE_PROJECTION
    assert artifact.asset_name == f"geas-knowledge-projection-{artifact.content_sha256}.sqlite"
    assert artifact.release_tag == f"geas-artifact-{artifact.content_sha256}"
    assert len(artifact.input_revision) == 64


def test_maintained_bundle_revision_follows_every_source_observation() -> None:
    bundle = yaml.safe_load(
        (REPOSITORY_ROOT / "ontology" / ONTOLOGY_NAME / "bundle.yaml").read_text()
    )
    recorded_at = datetime.fromisoformat(str(bundle["recorded_at"]).replace("Z", "+00:00"))
    acquired_at = tuple(
        datetime.fromisoformat(str(source["acquired_at"]).replace("Z", "+00:00"))
        for source in bundle["sources"]
    )

    assert recorded_at == REVISION_INSTANT
    assert recorded_at >= max(acquired_at)


def test_dzhng_source_is_the_exact_pinned_official_git_blob() -> None:
    source = (
        REPOSITORY_ROOT
        / "ontology"
        / ONTOLOGY_NAME
        / "generated"
        / "dzhng-deep-research"
        / "sources"
        / "dzhng-deep-research-7813045fe377.md"
    ).read_bytes()

    assert hashlib.sha256(source).hexdigest() == (
        "7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259"
    )
    git_blob = b"blob " + str(len(source)).encode("ascii") + b"\0" + source
    assert hashlib.sha1(git_blob, usedforsecurity=False).hexdigest() == (
        "78d7dcaad5524e630b5c106f46bf4782a56b7ce5"
    )
    assert b'FIRECRAWL_KEY="your_firecrawl_key"' in source
    assert b'OPENAI_KEY="your_openai_key"' in source


def test_demo_hydrates_a_preseeded_artifact_and_exports_the_same_skill_twice(
    tmp_path: Path,
) -> None:
    checkout = _committed_sample_checkout(tmp_path / "checkout")
    demo_root = tmp_path / "demo"

    completed = subprocess.run(
        (str(checkout / "ontology" / ONTOLOGY_NAME / "demo.sh"), str(demo_root)),
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    )
    summary = json.loads(completed.stdout)
    first_hydration = json.loads((demo_root / "artifact-hydration-first.json").read_text())
    second_hydration = json.loads((demo_root / "artifact-hydration-second.json").read_text())
    first_skill = json.loads((demo_root / "skill-export-first.json").read_text())
    second_skill = json.loads((demo_root / "skill-export-second.json").read_text())

    assert summary["projection_schema"] == 9
    assert first_hydration["downloaded"] is True
    assert second_hydration["downloaded"] is False
    assert first_hydration["content_sha256"] == second_hydration["content_sha256"]
    assert first_skill["unchanged"] is False
    assert second_skill["unchanged"] is True
    assert first_skill["snapshot_sha256"] == second_skill["snapshot_sha256"]
    assert summary["seed_bundles"] == list(EXPECTED_SEED_BUNDLES)
    assert summary["sources"] == 11
    assert summary["claims"] == 69


def test_independent_demos_match_the_committed_artifact_exactly(tmp_path: Path) -> None:
    checkout = _committed_sample_checkout(tmp_path / "checkout")
    manifest = OntologyArtifactManifest.from_yaml(
        checkout / "ontology" / ONTOLOGY_NAME / "artifacts.yaml"
    )
    committed = manifest.artifacts[0]
    observed: list[tuple[str, str, str, str]] = []

    for index in range(2):
        demo_root = tmp_path / f"demo-{index}"
        completed = subprocess.run(
            (str(checkout / "ontology" / ONTOLOGY_NAME / "demo.sh"), str(demo_root)),
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        )
        json.loads(completed.stdout)
        snapshot = json.loads((demo_root / "snapshot.json").read_text())
        published = json.loads((demo_root / "artifact-publish.json").read_text())[
            "artifacts"
        ][0]
        skill = json.loads((demo_root / "skill-export-second.json").read_text())
        with sqlite3.connect(demo_root / "query.sqlite") as connection:
            stamp = json.loads(
                connection.execute(
                    "SELECT payload FROM _research_projection_metadata WHERE singleton = 1"
                ).fetchone()[0]
            )
        observed.append(
            (
                hashlib.sha256((demo_root / "query.sqlite").read_bytes()).hexdigest(),
                published["input_revision"],
                snapshot["id"],
                skill["projection_snapshot_id"],
            )
        )
        assert snapshot["created_at"] == "2026-08-29T17:00:00Z"
        assert stamp["snapshot_id"] == snapshot["id"]
        assert published["content_sha256"] == observed[-1][0]

    assert observed[0] == observed[1]
    assert committed.content_sha256 == observed[0][0]
    assert committed.input_revision == observed[0][1]


def _build_maintained_projection(checkout: Path, root: Path) -> Path:
    subprocess.run(
        (str(checkout / "ontology" / ONTOLOGY_NAME / "demo.sh"), str(root)),
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    )
    return root / "query.sqlite"


class _PreseededArtifactStore:
    def __init__(
        self,
        source: Path,
        expected: OntologyArtifact,
    ) -> None:
        self.source = source
        self.expected = expected
        self.downloads = 0

    def ensure(self, _artifact: OntologyArtifact, _source: Path) -> bool:
        raise AssertionError("offline hydration must not publish")

    def available(self, artifact: OntologyArtifact) -> bool:
        return artifact == self.expected

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        assert artifact == self.expected
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == artifact.content_sha256
        assert self.source.stat().st_size == artifact.size_bytes
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, destination)


def _run_cli(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def test_committed_artifact_drives_repeatable_catalog_cli_export_and_update_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    checkout = manager.root / "subscriptions" / "default" / "geas-samples"
    _committed_sample_checkout(checkout)
    commit = _git(checkout, "rev-parse", "HEAD")
    catalog_bundle = verify_catalog(checkout / "geas.yaml")[0].bundle_sha256
    artifact_manifest = OntologyArtifactManifest.from_yaml(
        checkout / "ontology" / ONTOLOGY_NAME / "artifacts.yaml"
    )
    artifact = artifact_manifest.artifacts[0]
    projection = _build_maintained_projection(checkout, tmp_path / "projection")
    assert hashlib.sha256(projection.read_bytes()).hexdigest() == artifact.content_sha256

    subscription = OntologySubscription(
        url="https://github.com/Epiphytic/geas.git",
        active_ref="refs/heads/main",
        checkout=Path("subscriptions/default/geas-samples"),
        freshness=OntologyFreshnessConfig(check_before_use=False),
    )
    manager.replace(
        GeasUserConfig(
            ontology_freshness=OntologyFreshnessConfig(check_before_use=False),
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"geas-samples": subscription},
                    trust_rules=(
                        TrustRule(
                            decision="allow",
                            repository="https://github.com/Epiphytic/geas",
                            created_at=AUDIT_INSTANT,
                            created_via="manual",
                        ),
                    ),
                )
            },
        )
    )
    store = _PreseededArtifactStore(projection, artifact)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GitHubReleaseArtifactStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(cli, "_current_geas_identity", lambda: ("0.1.0", GEAS_OLD_COMMIT))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    export_payloads = []
    for _ in range(2):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "--yolo",
            "skill-export",
            ONTOLOGY_NAME,
        )
        export_payloads.append(json.loads(capsys.readouterr().out))
    snapshot = Path(export_payloads[-1]["path"])
    exported = validate_snapshot(snapshot)
    assert tuple(item["unchanged"] for item in export_payloads) == (False, True)
    assert exported.ontology.bundle_sha256 == catalog_bundle
    assert exported.ontology.ontology_commit == commit
    assert exported.ontology.subscription_name == "geas-samples"
    assert exported.ontology.repository_url == subscription.url
    assert exported.ontology.active_ref == subscription.active_ref
    assert exported.ontology.catalog_path == subscription.catalog.as_posix()
    assert exported.artifact is not None
    assert exported.artifact.content_sha256 == artifact.content_sha256
    assert exported.artifact.input_revision == artifact.input_revision
    assert store.downloads == 1

    class _StatefulUpdater:
        calls = 0

        def update_and_reexec(
            self, _argv: tuple[str, ...], *, continuation: str | None
        ) -> GeasUpdateReceipt:
            assert continuation == "maintained-sample-test"
            old = GEAS_OLD_COMMIT if self.calls == 0 else GEAS_NEW_COMMIT
            type(self).calls += 1
            return GeasUpdateReceipt(
                installer="git-development",
                directory=REPOSITORY_ROOT,
                executable=REPOSITORY_ROOT / ".venv" / "bin" / "geas",
                old_commit=old,
                new_commit=GEAS_NEW_COMMIT,
                old_version="0.1.0",
                new_version="0.1.0",
                reinstalled=old != GEAS_NEW_COMMIT,
                reexec_depth=1,
            )

    class _NoWriteRepository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def pull(self) -> dict[str, object]:
            return {"commit": commit}

    monkeypatch.setattr(cli, "GeasUpdater", _StatefulUpdater)
    monkeypatch.setattr(cli, "OntologyRepositoryManager", _NoWriteRepository)
    update_payloads = []
    for _ in range(2):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "--yolo",
            "skill-update",
            str(snapshot),
            "--geas-update-continuation",
            "maintained-sample-test",
        )
        update_payloads.append(json.loads(capsys.readouterr().out))
    updated = validate_snapshot(snapshot)
    assert tuple(item["unchanged"] for item in update_payloads) == (False, True)
    assert updated.ontology.bundle_sha256 == catalog_bundle
    assert updated.artifact == exported.artifact
    assert store.downloads == 1
