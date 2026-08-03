import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.connectors import (
    CrossrefDiscoveryConnector,
    LocalFileConnector,
    OpenAlexDiscoveryConnector,
)
from research_agent.discovery import (
    CompilerIdentity,
    ConnectorCapability,
    SourceClass,
)
from research_agent.knowledge import (
    ClaimProposal,
    DeterministicThreatScanner,
    EvidenceProposal,
    KnowledgeImporter,
    KnowledgePack,
)
from research_agent.planning import (
    ConceptVocabulary,
    QueryPlanValidator,
    QueryProposal,
    deterministic_proposal,
)
from research_agent.projection import (
    DeterministicQueryCompiler,
    KnowledgeQueryEngine,
    QueryRecordType,
    SQLiteKnowledgeProjection,
)
from research_agent.render import render_topic_markdown
from research_agent.research import DiscoveryExecutor, OfflineResearchRunner
from research_agent.store import ImmutableStore
from research_agent.truth import DriftKind, SQLiteProjectionGuard, TruthManager, TruthPolicy

FIXTURE_CORPUS = Path("tests/fixtures/fluoridation_corpus")
FIXTURE_PACK = Path("tests/fixtures/fluoridation_knowledge.yaml")
INSTANT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


class _CrossrefFixtureTransport:
    def request(self, parameters: dict[str, str]) -> bytes:
        return Path("tests/fixtures/crossref/search.json").read_bytes()


class _OpenAlexFixtureTransport:
    def request(self, parameters: dict[str, str]) -> bytes:
        return Path("tests/fixtures/openalex/search.json").read_bytes()


def _researched_store(tmp_path: Path) -> tuple[ImmutableStore, object]:
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    connector = LocalFileConnector([FIXTURE_CORPUS])
    proposal = deterministic_proposal(
        "fluoridation fluoride caries cognition regulation",
        connector_id=connector.manifest.id,
    )
    plan = QueryPlanValidator(
        vocabulary=ConceptVocabulary(concepts={}),
        manifests={connector.manifest.id: connector.manifest},
    ).validate(
        proposal,
        compiler=CompilerIdentity(id="compiler:fixture", version="1"),
    )
    result = OfflineResearchRunner(
        store=store,
        connector=connector,
        clock=lambda: INSTANT,
    ).run(plan, topic_branch="topic:community-water-fluoridation")
    assert len(result.source_versions) == 5

    crossref = CrossrefDiscoveryConnector(_CrossrefFixtureTransport())
    crossref_plan = QueryPlanValidator(
        vocabulary=ConceptVocabulary(concepts={}),
        manifests={crossref.manifest.id: crossref.manifest},
    ).validate(
        QueryProposal(
            question="community water fluoridation evidence",
            exact_terms=("community water fluoridation",),
            source_classes=frozenset({SourceClass.SCHOLARLY}),
            connector_ids=(crossref.manifest.id,),
            capabilities=frozenset({ConnectorCapability.DISCOVERY, ConnectorCapability.METADATA}),
            result_limit=10,
            page_limit=1,
        ),
        compiler=CompilerIdentity(id="compiler:fixture", version="1"),
    )
    scholarly = DiscoveryExecutor(clock=lambda: INSTANT).run(crossref_plan, crossref)
    store.put_record("query-plan", crossref_plan)
    store.put_record("connector-manifest", crossref.manifest)
    store.put_record("discovery-run", scholarly.discovery_run)
    for hit in scholarly.hits:
        store.put_record("discovery-hit", hit)

    openalex = OpenAlexDiscoveryConnector(_OpenAlexFixtureTransport())
    openalex_plan = QueryPlanValidator(
        vocabulary=ConceptVocabulary(concepts={}),
        manifests={openalex.manifest.id: openalex.manifest},
    ).validate(
        QueryProposal(
            question="community water fluoridation dissent",
            exact_terms=("community water fluoridation", "neurodevelopment"),
            source_classes=frozenset({SourceClass.SCHOLARLY}),
            connector_ids=(openalex.manifest.id,),
            capabilities=frozenset(
                {ConnectorCapability.DISCOVERY, ConnectorCapability.METADATA}
            ),
            result_limit=10,
            page_limit=1,
        ),
        compiler=CompilerIdentity(id="compiler:fixture", version="1"),
    )
    openalex_results = DiscoveryExecutor(clock=lambda: INSTANT).run(
        openalex_plan,
        openalex,
    )
    store.put_record("query-plan", openalex_plan)
    store.put_record("connector-manifest", openalex.manifest)
    store.put_record("discovery-run", openalex_results.discovery_run)
    for hit in openalex_results.hits:
        store.put_record("discovery-hit", hit)
    return store, result


def _build_projection(tmp_path: Path) -> tuple[ImmutableStore, Path, object]:
    store, _ = _researched_store(tmp_path)
    receipt = KnowledgeImporter(
        store=store,
        clock=lambda: INSTANT,
        scanner=DeterministicThreatScanner(clock=lambda: INSTANT),
    ).import_pack(
        KnowledgePack.from_yaml(FIXTURE_PACK),
        imported_by="operator:test",
    )
    policy = TruthPolicy.from_yaml(Path("config/truth-policy.yaml"))
    manager = TruthManager(
        workspace_root=Path("."),
        store_root=store.root,
        policy=policy,
        clock=lambda: INSTANT,
    )
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    build = SQLiteKnowledgeProjection(store=store, workspace_root=Path(".")).build(
        database,
        snapshot=snapshot,
        truth_manager=manager,
    )
    return store, database, (receipt, manager, snapshot, build)


def test_contested_topic_runs_through_research_import_and_threat_scan(tmp_path: Path) -> None:
    store, result = _researched_store(tmp_path)

    receipt = KnowledgeImporter(
        store=store,
        clock=lambda: INSTANT,
        scanner=DeterministicThreatScanner(clock=lambda: INSTANT),
    ).import_pack(
        KnowledgePack.from_yaml(FIXTURE_PACK),
        imported_by="operator:test",
    )

    assert result.coverage.accessible_count == 5
    assert len(receipt.claim_ids) == 7
    assert len(receipt.controversy_ids) == 2
    assert len(receipt.gap_ids) == 3
    assert len(receipt.threat_observation_ids) == 3
    assert len(receipt.topic_source_association_ids) == 5
    observations = list(store.iter_records("threat-observation"))
    assert {item["status"] for item in observations} == {"suspected"}
    assert {item["detector"]["kind"] for item in observations} == {"deterministic_rule"}


def test_projection_supports_lexical_hierarchy_dissent_gaps_and_provenance(
    tmp_path: Path,
) -> None:
    _, database, (_, _, snapshot, build) = _build_projection(tmp_path)
    engine = KnowledgeQueryEngine(database)

    query = engine.query(
        "lower IQ uncertainty",
        record_types=(
            QueryRecordType.CLAIM,
            QueryRecordType.EVIDENCE,
            QueryRecordType.GAP,
        ),
        limit=20,
    )
    scholarly = engine.query(
        "water fluoridation prevention dental caries",
        record_types=(QueryRecordType.DISCOVERY,),
        limit=10,
    )
    openalex_metadata = engine.query(
        "openalex cited_by_count gold",
        record_types=(QueryRecordType.DISCOVERY,),
        limit=10,
    )
    topic = engine.topic("concept:community-water-fluoridation")

    assert query.projection_snapshot_id == snapshot.id
    assert any("prevention of dental caries" in hit.title for hit in scholarly.hits)
    assert any("prevention of dental caries" in hit.title for hit in openalex_metadata.hits)
    assert query.plan.compiler_version == "deterministic-local-query/1"
    assert "MATCH ?" in query.plan.sql
    assert {hit.record_type for hit in query.hits} >= {
        QueryRecordType.CLAIM,
        QueryRecordType.EVIDENCE,
        QueryRecordType.GAP,
    }
    assert len(topic.descendant_concept_ids) == 4
    assert len(topic.sources) == 5
    assert len({item["id"] for item in topic.claims}) == 7
    assert len(topic.controversies) == 2
    assert len(topic.gaps) == 3
    assert topic.gaps[0]["priority"] == 90
    assert len(topic.threats) == 3
    assert all(item["source_uri"].startswith("file:") for item in topic.claims)
    assert build.counts["claims"] == 7
    assert build.counts["topic_source_associations"] == 5
    assert build.counts["threat_observations"] == 3
    assert build.counts["discovery_hits"] == 7
    markdown = render_topic_markdown(topic)
    assert "## Dissent and controversy" in markdown
    assert "## Knowledge gaps" in markdown
    assert "## Poisoned or tainted source observations" in markdown
    assert "Ignore all previous instructions" not in markdown


def test_projection_is_stamped_and_mutation_is_detected(tmp_path: Path) -> None:
    _, database, (_, manager, snapshot, _) = _build_projection(tmp_path)
    guard = SQLiteProjectionGuard(clock=lambda: INSTANT)
    assert guard.verify(
        database,
        snapshot,
        truth_report=manager.verify(snapshot),
    ).clean

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE knowledge_gap SET rationale = 'database is not canonical' LIMIT 1"
        )

    report = guard.verify(
        database,
        snapshot,
        truth_report=manager.verify(snapshot),
    )
    assert any(item.kind is DriftKind.PROJECTION_MUTATED for item in report.items)
    assert report.recommended_action == "discard_and_rebuild"


def test_query_compiler_treats_fts_syntax_as_inert_tokens() -> None:
    plan = DeterministicQueryCompiler().compile(
        '" OR threat:* NOT',
        record_types=(QueryRecordType.THREAT,),
    )

    assert plan.fts_expression == '"not"* OR "threat"*'
    assert plan.parameters[0] == plan.fts_expression
    assert plan.parameters[1] == "threat"


def test_poisoned_source_cannot_be_used_as_claim_evidence(tmp_path: Path) -> None:
    store, _ = _researched_store(tmp_path)
    pack = KnowledgePack.from_yaml(FIXTURE_PACK)
    poison_hash = pack.inspect_source_sha256s[0]
    poisoned = pack.model_copy(
        update={
            "evidence": (
                *pack.evidence,
                EvidenceProposal(
                    key="poison",
                    source_content_sha256=poison_hash,
                    exact="Every independent scientist agrees",
                ),
            ),
            "claims": (
                *pack.claims,
                ClaimProposal(
                    key="poison-claim",
                    subject="concept:community-water-fluoridation",
                    predicate="ep:marketing_claim",
                    object="universal agreement",
                    stance="asserts",
                    epistemic_status="hypothesized",
                    asserted_by="publisher:unknown",
                    evidence_keys=("poison",),
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="suspected deterministic threat"):
        KnowledgeImporter(store=store, clock=lambda: INSTANT).import_pack(
            poisoned,
            imported_by="operator:test",
        )
