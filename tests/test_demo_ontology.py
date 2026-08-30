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

BUNDLES = (
    Path("ontology/open-source-research-agents/bundle.yaml"),
    Path(
        "ontology/open-source-research-agents/generated/"
        "alibaba-nlp-deepresearch/bundle.yaml"
    ),
    Path(
        "ontology/open-source-research-agents/generated/"
        "dzhng-deep-research/bundle.yaml"
    ),
)
INSTANT = datetime(2026, 8, 29, 17, tzinfo=UTC)


def test_maintained_open_source_research_agent_ontology_end_to_end(
    tmp_path: Path,
) -> None:
    store = ImmutableStore(tmp_path / "data")
    receipts = tuple(
        KnowledgeBundleImporter(store=store).import_bundle(
            bundle,
            imported_by="operator:test",
        )
        for bundle in BUNDLES
    )
    audit = DeterministicKnowledgeAuditor().audit(store, as_of=INSTANT)

    assert sum(len(receipt.parse_receipts) for receipt in receipts) == 11
    assert sum(len(receipt.knowledge_receipt.claim_ids) for receipt in receipts) == 69
    assert sum(len(receipt.knowledge_receipt.controversy_ids) for receipt in receipts) == 5
    assert sum(len(receipt.knowledge_receipt.gap_ids) for receipt in receipts) == 13
    assert sum(
        len(receipt.knowledge_receipt.threat_observation_ids) for receipt in receipts
    ) == 4
    assert sum(
        len(item.bibliographic_reference_ids)
        for receipt in receipts
        for item in receipt.parse_receipts
    ) == 82
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

    assert build.counts["source_metadata"] == 11
    assert build.counts["claims"] == 69
    assert build.counts["bibliographic_references"] == 82
    assert len(topic.descendant_concept_ids) == 26
    assert len(topic.sources) == 11
    assert len({item["id"] for item in topic.claims}) == 69
    assert len(topic.controversies) == 5
    assert len(topic.gaps) == 13
    assert len(topic.threats) == 4
    assert len(topic.references) == 82
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
