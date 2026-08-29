"""Production contracts for the repository's maintained ontology catalog."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from research_agent.ontology_artifacts import (
    ArtifactRole,
    OntologyArtifactManifest,
)
from research_agent.ontology_resolution import resolve_ontology_catalog, select_ontology
from research_agent.ontology_subscriptions import (
    OntologyFreshnessConfig,
    OntologySubscription,
)
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
    "generated/dzhng-deep-research/sources/dzhng-deep-research-e157f80f5866.md",
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
    target = destination / "ontology" / ONTOLOGY_NAME
    shutil.rmtree(target)
    shutil.copytree(REPOSITORY_ROOT / "ontology" / ONTOLOGY_NAME, target)
    shutil.copyfile(CATALOG, destination / "geas.yaml")
    _git(destination, "remote", "set-url", "origin", "https://github.com/Epiphytic/geas.git")
    _git(destination, "add", "geas.yaml", f"ontology/{ONTOLOGY_NAME}")
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
