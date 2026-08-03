import hashlib
from pathlib import Path

import pytest
import yaml

from research_agent.bundles import KnowledgeBundleImporter
from research_agent.projection import KnowledgeQueryEngine, SQLiteKnowledgeProjection
from research_agent.store import ImmutableStore
from research_agent.truth import TruthManager, TruthPolicy


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    source = root / "source.md"
    content = (
        "# Research agent\n\n"
        "The project provides a deterministic research workflow.\n\n"
        "References https://github.com/example/research-agent.\n"
    )
    source.write_text(content)
    bundle = {
        "version": 1,
        "topic": "Research agents",
        "topic_concept_id": "concept:research-agents",
        "recorded_at": "2026-08-03T12:00:00Z",
        "sources": [
            {
                "key": "project",
                "path": "source.md",
                "expected_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "acquired_at": "2026-08-03T11:00:00Z",
                "original_locator": "https://github.com/example/research-agent",
                "title": "Example research agent",
                "authors": ["Example maintainers"],
                "publisher": "Example",
                "license": "Apache-2.0",
                "usage_conditions": ["Maintained source brief"],
                "rights_basis": "Project-authored summary",
                "provenance_note": "Checked against the official repository.",
            }
        ],
        "concepts": [
            {
                "id": "concept:research-agents",
                "label": "Research agents",
                "description": "Systems that perform multi-step research.",
                "recorded_at": "2026-08-03T12:00:00Z",
                "recorded_by": "operator:test",
            }
        ],
        "evidence": [
            {
                "key": "workflow",
                "source_key": "project",
                "exact": "The project provides a deterministic research workflow.",
            }
        ],
        "claims": [
            {
                "key": "workflow",
                "subject": "concept:research-agents",
                "predicate": "ep:has_capability",
                "object": "deterministic research workflow",
                "stance": "asserts",
                "epistemic_status": "observed",
                "asserted_by": "source:project",
                "evidence_keys": ["workflow"],
            }
        ],
        "gaps": [
            {
                "topic_concept_id": "concept:research-agents",
                "question": "How is this evaluated?",
                "rationale": "No benchmark is recorded.",
                "priority": 70,
            }
        ],
    }
    path = root / "bundle.yaml"
    path.write_text(yaml.safe_dump(bundle, sort_keys=False))
    return path


def test_bundle_import_is_confined_reproducible_and_queryable(tmp_path: Path) -> None:
    path = _bundle(tmp_path)
    store = ImmutableStore(tmp_path / "data")
    importer = KnowledgeBundleImporter(store=store)

    first = importer.import_bundle(path, imported_by="operator:test")
    second = importer.import_bundle(path, imported_by="operator:test")

    assert second == first
    assert len(first.parse_receipts) == 1
    assert len(first.source_metadata_ids) == 1
    assert len(first.knowledge_receipt.claim_ids) == 1
    metadata = tuple(store.iter_records("source-metadata"))
    assert metadata[0]["authors"] == ["Example maintainers"]
    assert metadata[0]["authorship_status"] == "declared"
    assert metadata[0]["license_status"] == "declared"

    manager = TruthManager(
        workspace_root=Path("."),
        store_root=store.root,
        policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
    )
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    build = SQLiteKnowledgeProjection(store=store, workspace_root=Path(".")).build(
        database,
        snapshot=snapshot,
        truth_manager=manager,
    )
    topic = KnowledgeQueryEngine(database).topic("concept:research-agents")

    assert build.counts["source_metadata"] == 1
    assert build.counts["bibliographic_references"] == 1
    assert topic.sources[0]["title"] == "Example research agent"
    assert topic.references[0]["canonical_locator"] == (
        "https://github.com/example/research-agent"
    )


def test_bundle_rejects_source_hash_drift_and_path_escape(tmp_path: Path) -> None:
    path = _bundle(tmp_path)
    source = path.parent / "source.md"
    source.write_text(source.read_text() + "changed\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        KnowledgeBundleImporter(store=ImmutableStore(tmp_path / "data")).import_bundle(
            path,
            imported_by="operator:test",
        )

    value = yaml.safe_load(path.read_text())
    value["sources"][0]["path"] = "../outside.md"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match="confined relative"):
        KnowledgeBundleImporter(store=ImmutableStore(tmp_path / "other")).import_bundle(
            path,
            imported_by="operator:test",
        )
