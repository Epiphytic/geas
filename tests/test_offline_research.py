from datetime import UTC, datetime
from pathlib import Path

from research_agent.connectors import LocalFileConnector
from research_agent.discovery import CompilerIdentity, TermMatch
from research_agent.planning import ConceptVocabulary, QueryPlanValidator, deterministic_proposal
from research_agent.research import OfflineResearchRunner
from research_agent.store import ImmutableStore


def test_fixture_backed_question_produces_complete_audit_slice(tmp_path: Path) -> None:
    corpus = Path("tests/fixtures/local_corpus")
    connector = LocalFileConnector([corpus])
    proposal = deterministic_proposal(
        "How should an ontology preserve knowledge gaps?",
        connector_id=connector.manifest.id,
        concept_ids=("concept:ontology",),
    ).model_copy(update={"match": TermMatch.ALL})
    validator = QueryPlanValidator(
        vocabulary=ConceptVocabulary(
            concepts={"concept:ontology": ("ontology", "knowledge graph")}
        ),
        manifests={connector.manifest.id: connector.manifest},
    )
    plan = validator.validate(
        proposal,
        compiler=CompilerIdentity(id="compiler:fixture", version="1"),
    )
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    store = ImmutableStore(tmp_path / "store")
    store.initialize()

    result = OfflineResearchRunner(
        store=store,
        connector=connector,
        clock=lambda: instant,
    ).run(plan, topic_branch="topic:research-ontology")

    assert result.query_plan.id == plan.id
    assert result.discovery_run.result_count == 1
    assert len(result.hits) == 1
    assert len(result.source_versions) == 1
    assert result.acquisition_attempts[0].content_sha256
    assert result.coverage.accessible_count == 1
    assert not result.access_constraints
    assert set(result.record_hashes) == {
        "access-constraint",
        "acquisition-attempt",
        "connector-manifest",
        "coverage-run",
        "discovery-hit",
        "discovery-run",
        "query-plan",
    }
    blob_hash = result.source_versions[0].content_sha256
    assert (store.blob_root / blob_hash[:2] / blob_hash).is_file()


def test_fixture_run_with_fixed_clock_is_reproducible(tmp_path: Path) -> None:
    corpus = Path("tests/fixtures/local_corpus")
    connector = LocalFileConnector([corpus])
    proposal = deterministic_proposal(
        "prompt injection policy",
        connector_id=connector.manifest.id,
    )
    validator = QueryPlanValidator(
        vocabulary=ConceptVocabulary(concepts={}),
        manifests={connector.manifest.id: connector.manifest},
    )
    plan = validator.validate(
        proposal,
        compiler=CompilerIdentity(id="compiler:fixture", version="1"),
    )
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    store = ImmutableStore(tmp_path / "store")
    store.initialize()
    runner = OfflineResearchRunner(store=store, connector=connector, clock=lambda: instant)

    first = runner.run(plan)
    second = runner.run(plan)

    assert first == second
