from datetime import UTC, datetime
from pathlib import Path

from research_agent.audit import DeterministicKnowledgeAuditor
from research_agent.bundles import KnowledgeBundleImporter
from research_agent.projection import (
    KnowledgeQueryEngine,
    QueryRecordType,
    SQLiteKnowledgeProjection,
)
from research_agent.render import render_topic_markdown
from research_agent.store import ImmutableStore
from research_agent.truth import SQLiteProjectionGuard, TruthManager, TruthPolicy

BUNDLE = Path("ontology/open-source-research-agents/bundle.yaml")
INSTANT = datetime(2026, 8, 3, 16, tzinfo=UTC)


def test_maintained_open_source_research_agent_ontology_end_to_end(
    tmp_path: Path,
) -> None:
    store = ImmutableStore(tmp_path / "data")
    receipt = KnowledgeBundleImporter(store=store).import_bundle(
        BUNDLE,
        imported_by="operator:test",
    )
    audit = DeterministicKnowledgeAuditor().audit(store, as_of=INSTANT)

    assert len(receipt.parse_receipts) == 9
    assert len(receipt.knowledge_receipt.claim_ids) == 46
    assert len(receipt.knowledge_receipt.controversy_ids) == 5
    assert len(receipt.knowledge_receipt.gap_ids) == 7
    assert len(receipt.knowledge_receipt.threat_observation_ids) == 4
    assert sum(
        len(item.bibliographic_reference_ids)
        for item in receipt.parse_receipts
    ) == 27
    assert audit.clean
    assert audit.findings == ()

    manager = TruthManager(
        workspace_root=Path("."),
        store_root=store.root,
        policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
        clock=lambda: INSTANT,
    )
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    build = SQLiteKnowledgeProjection(store=store, workspace_root=Path(".")).build(
        database,
        snapshot=snapshot,
        truth_manager=manager,
    )
    engine = KnowledgeQueryEngine(database)
    topic = engine.topic("concept:open-source-research-agents")
    persistent = engine.query(
        "persistent ontology exact evidence deterministic retrieval",
        record_types=(
            QueryRecordType.CLAIM,
            QueryRecordType.CONCEPT,
            QueryRecordType.GAP,
        ),
    )
    poisoned = engine.query(
        "prompt injection poisoned source threat",
        record_types=(QueryRecordType.THREAT,),
    )
    references = engine.query(
        "STORM hierarchical mind map",
        record_types=(QueryRecordType.CLAIM, QueryRecordType.REFERENCE),
    )

    assert build.counts["source_metadata"] == 9
    assert build.counts["claims"] == 46
    assert build.counts["bibliographic_references"] == 27
    assert len(topic.descendant_concept_ids) == 14
    assert len(topic.sources) == 9
    assert len({item["id"] for item in topic.claims}) == 46
    assert len(topic.controversies) == 5
    assert len(topic.gaps) == 7
    assert len(topic.threats) == 4
    assert len(topic.references) == 27
    assert persistent.hits
    assert len(poisoned.hits) == 4
    assert references.hits
    assert all(item["source_uri"].startswith("bundle:") for item in topic.sources)
    assert all(item["original_locator"] for item in topic.sources)
    markdown = render_topic_markdown(topic)
    assert "## Citation and reference graph" in markdown
    assert "## Poisoned or tainted source observations" in markdown
    assert "Ignore all previous instructions" not in markdown
    assert "GPT Researcher source card" in markdown
    assert SQLiteProjectionGuard(clock=lambda: INSTANT).verify(
        database,
        snapshot,
        truth_report=manager.verify(snapshot),
    ).clean
