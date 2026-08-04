from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.bundles import KnowledgeBundle, KnowledgeBundleImporter
from research_agent.candidate_bundles import CandidateBundleError, CandidateBundleWriter
from research_agent.discovery_acquisition import RepositorySnapshot
from research_agent.extraction import AnchorGroundedExtractionManager
from research_agent.parsing import ParsedDocumentManager
from research_agent.store import ImmutableStore

NOW = datetime(2026, 8, 3, tzinfo=UTC)
TEXT = "# Project\n\nThe project searches sources and maintains cited knowledge.\n"


class _Client:
    def __init__(self, anchor_id: str) -> None:
        self.anchor_id = anchor_id

    def complete_json(self, **_kwargs):
        return {
            "version": 1,
            "concepts": [
                {
                    "key": "search-capability",
                    "id": "concept:search-capability",
                    "label": "Search capability",
                    "description": "The project's source-search capability.",
                    "broader": ["concept:research-agents"],
                }
            ],
            "claims": [
                {
                    "key": "searches",
                    "subject": "concept:search-capability",
                    "predicate": "ep:capability",
                    "object": "searches sources",
                    "stance": "asserts",
                    "epistemic_status": "observed",
                    "evidence": [
                        {
                            "anchor_id": self.anchor_id,
                            "exact": (
                                "The project searches sources and maintains cited knowledge."
                            ),
                        }
                    ],
                }
            ],
        }


def _fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ImmutableStore(tmp_path / "data")
    parsed = ParsedDocumentManager(store=store, clock=lambda: NOW).ingest(
        TEXT.encode(),
        source_uri="https://example.invalid/README.md",
        media_type="text/markdown",
        connector_id="connector:github-repository",
        license="Apache-2.0",
        acquired_at=NOW,
    )
    anchor = next(
        value
        for value in store.iter_records("structural-anchor")
        if value["kind"] == "paragraph"
    )
    from research_agent.knowledge import Concept, KnowledgeImporter, KnowledgePack

    KnowledgeImporter(store=store, clock=lambda: NOW).import_pack(
        KnowledgePack(
            version=1,
            topic="Research agents",
            topic_concept_id="concept:research-agents",
            concepts=(
                Concept(
                    id="concept:research-agents",
                    label="Research agents",
                    description="Agents that perform research.",
                    recorded_at=NOW,
                    recorded_by="operator:test",
                ),
            ),
            evidence=(),
            claims=(),
        ),
        imported_by="operator:test",
    )
    proposal = AnchorGroundedExtractionManager(
        store=store,
        client=_Client(anchor["id"]),
        provider="deepseek_local",
        model="fixture",
        clock=lambda: NOW,
    ).propose(
        question="What does the project do?",
        structural_derivation_id=parsed.structural_derivation_id,
        anchor_ids=(anchor["id"],),
        allowed_concept_ids=("concept:research-agents",),
    ).proposal
    snapshot = RepositorySnapshot(
        id="repository-snapshot:test",
        discovery_hit_id="discovery-hit:test",
        repository="Example/Research",
        canonical_locator="https://github.com/Example/Research",
        api_locator="https://api.github.com/repos/Example/Research",
        default_branch="main",
        commit_sha="a" * 40,
        readme_path="README.md",
        readme_blob_sha="b" * 40,
        source_version_id=parsed.derived_source_version_id,
        source_content_sha256=proposal.source_content_sha256,
        license="Apache-2.0",
        archived=False,
        fork=False,
        observed_at=NOW,
    )
    return workspace, store, proposal, snapshot


def test_candidate_bundle_is_deterministic_ranged_and_reimportable(tmp_path) -> None:
    workspace, store, proposal, snapshot = _fixture(tmp_path)
    writer = CandidateBundleWriter(store=store, workspace=workspace)
    first = writer.write(
        proposal,
        snapshot,
        topic="Research agents",
        topic_concept_id="concept:research-agents",
        output_root=Path("ontology/generated"),
    )
    first_bytes = (workspace / first).read_bytes()
    second = writer.write(
        proposal,
        snapshot,
        topic="Research agents",
        topic_concept_id="concept:research-agents",
        output_root=Path("ontology/generated"),
    )
    bundle = KnowledgeBundle.from_yaml(workspace / second)

    assert (workspace / second).read_bytes() == first_bytes
    assert bundle.evidence[0].start is not None
    assert bundle.evidence[0].end is not None
    assert {item.id for item in bundle.concepts} == {
        "concept:example:research:search-capability",
        "concept:research-agents",
    }
    assert bundle.claims[0].subject == "concept:example:research:search-capability"
    imported = KnowledgeBundleImporter(
        store=ImmutableStore(tmp_path / "reimported")
    ).import_bundle(workspace / second, imported_by="vcs:test")
    assert imported.knowledge_receipt.claim_ids


def test_candidate_bundle_namespaces_same_model_id_per_repository(tmp_path) -> None:
    workspace, store, proposal, snapshot = _fixture(tmp_path)
    writer = CandidateBundleWriter(store=store, workspace=workspace)
    first = KnowledgeBundle.from_yaml(
        workspace
        / writer.write(
            proposal,
            snapshot,
            topic="Research agents",
            topic_concept_id="concept:research-agents",
            output_root=Path("ontology/generated"),
        )
    )
    second = KnowledgeBundle.from_yaml(
        workspace
        / writer.write(
            proposal,
            snapshot.model_copy(
                update={
                    "repository": "Other/Research",
                    "canonical_locator": "https://github.com/Other/Research",
                    "api_locator": "https://api.github.com/repos/Other/Research",
                }
            ),
            topic="Research agents",
            topic_concept_id="concept:research-agents",
            output_root=Path("ontology/generated"),
        )
    )

    first_proposed = {item.id for item in first.concepts} - {"concept:research-agents"}
    second_proposed = {item.id for item in second.concepts} - {"concept:research-agents"}
    assert first_proposed == {"concept:example:research:search-capability"}
    assert second_proposed == {"concept:other:research:search-capability"}
    assert first_proposed.isdisjoint(second_proposed)

    merged_store = ImmutableStore(tmp_path / "merged")
    importer = KnowledgeBundleImporter(store=merged_store)
    importer.import_bundle(
        workspace / "ontology/generated/example-research/bundle.yaml",
        imported_by="vcs:test",
    )
    importer.import_bundle(
        workspace / "ontology/generated/other-research/bundle.yaml",
        imported_by="vcs:test",
    )
    concept_ids = [item["id"] for item in merged_store.iter_records("concept")]
    assert len(concept_ids) == len(set(concept_ids))


def test_candidate_bundle_rejects_unknown_license(tmp_path) -> None:
    workspace, store, proposal, snapshot = _fixture(tmp_path)
    with pytest.raises(CandidateBundleError, match="license"):
        CandidateBundleWriter(store=store, workspace=workspace).write(
            proposal,
            snapshot.model_copy(update={"license": None}),
            topic="Research agents",
            topic_concept_id="concept:research-agents",
            output_root=Path("ontology/generated"),
        )
