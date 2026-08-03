import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.citations import (
    CitationDocumentManager,
    CitationError,
    DeterministicCitationExtractor,
    IdentifierKind,
    ReferenceRelation,
)
from research_agent.discovery import DiscoveryHit, OpenAccessResolution
from research_agent.parsing import ParsedDocumentManager
from research_agent.projection import (
    KnowledgeQueryEngine,
    QueryRecordType,
    SQLiteKnowledgeProjection,
)
from research_agent.store import ImmutableStore
from research_agent.structure import DeterministicStructuralExtractor
from research_agent.truth import TruthManager, TruthPolicy

INSTANT = datetime(2026, 8, 3, tzinfo=UTC)
TEXT = """# Review

The update corrects https://example.org/old-result and mentions PMID: 12345678.

## References

- DOI: 10.1000/example.1
- arXiv: 2401.01234v2
- PMCID: PMC123456
- Unsafe http://127.0.0.1/private is not a retrievable identifier.
"""


def _extract():
    structural, anchors = DeterministicStructuralExtractor().extract(
        TEXT,
        text_derivation_id="text-derivation:test",
        source_version_id="source:test",
        source_content_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
        input_media_type="text/markdown",
        extracted_at=INSTANT,
    )
    return DeterministicCitationExtractor().extract(
        TEXT,
        structural_derivation=structural,
        anchors=anchors,
    )


def test_citations_have_stable_identifiers_relations_and_exact_anchors() -> None:
    derivation, identifiers, references = _extract()
    by_value = {item.value: item for item in identifiers}
    relation_by_value = {
        by_value_id.value: reference.relation
        for reference in references
        for by_value_id in identifiers
        if by_value_id.id == reference.identifier_id
    }

    assert set(relation_by_value) == {
        "https://example.org/old-result",
        "12345678",
        "10.1000/example.1",
        "2401.01234v2",
        "PMC123456",
    }
    assert relation_by_value["https://example.org/old-result"] is ReferenceRelation.CORRECTS
    assert relation_by_value["12345678"] is ReferenceRelation.MENTIONS
    assert relation_by_value["10.1000/example.1"] is ReferenceRelation.CITES
    assert relation_by_value["2401.01234v2"] is ReferenceRelation.CITES
    assert relation_by_value["PMC123456"] is ReferenceRelation.CITES
    assert by_value["10.1000/example.1"].kind is IdentifierKind.DOI
    assert derivation.relation_counts == {"cites": 3, "corrects": 1, "mentions": 1}
    for reference in references:
        assert hashlib.sha256(TEXT[reference.start : reference.end].encode()).hexdigest() == (
            reference.exact_sha256
        )


def test_same_structural_text_produces_identical_citation_graph() -> None:
    assert _extract() == _extract()


def test_balanced_identifier_parentheses_survive_markdown_delimiters() -> None:
    text = (
        "# References\n\n"
        "- [Source](https://example.org/path_(v1))\n"
        "- DOI: 10.1000/example(2024).\n"
    )
    structural, anchors = DeterministicStructuralExtractor().extract(
        text,
        text_derivation_id="text-derivation:balanced",
        source_version_id="source:balanced",
        source_content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        input_media_type="text/markdown",
        extracted_at=INSTANT,
    )
    _, identifiers, _ = DeterministicCitationExtractor().extract(
        text,
        structural_derivation=structural,
        anchors=anchors,
    )

    assert {item.value for item in identifiers} == {
        "https://example.org/path_(v1)",
        "10.1000/example(2024)",
    }


def test_citation_extraction_rejects_mismatched_structural_hash() -> None:
    structural, anchors = DeterministicStructuralExtractor().extract(
        "valid",
        text_derivation_id="text-derivation:test",
        source_version_id="source:test",
        source_content_sha256=hashlib.sha256(b"valid").hexdigest(),
        input_media_type="text/plain",
        extracted_at=INSTANT,
    )
    with pytest.raises(CitationError, match="content hash"):
        DeterministicCitationExtractor().extract(
            "tampered",
            structural_derivation=structural,
            anchors=anchors,
        )


def test_parse_auto_derives_and_projection_queries_reference_provenance(
    tmp_path: Path,
) -> None:
    store = ImmutableStore(tmp_path / "data")
    receipt = ParsedDocumentManager(store=store, clock=lambda: INSTANT).ingest(
        TEXT.encode(),
        source_uri="https://example.org/review.md",
        media_type="text/markdown",
        connector_id="connector:test",
        license="CC-BY-4.0",
    )
    repeated = CitationDocumentManager(store=store).derive_stored(
        receipt.structural_derivation_id
    )
    assert repeated.citation_derivation_id == receipt.citation_derivation_id
    assert repeated.bibliographic_reference_ids == receipt.bibliographic_reference_ids
    discovery = DiscoveryHit(
        id="discovery-hit:doi-example",
        upstream_id="10.1000/example.1",
        canonical_locator="https://doi.org/10.1000/example.1",
        title="Resolved example",
        upstream_rank=1,
        discovery_run_id="discovery-run:test",
        known_entity_ids=("doi:10.1000/example.1",),
        acquisition_eligible=False,
    )
    resolution = OpenAccessResolution(
        id="open-access-resolution:doi-example",
        doi="10.1000/example.1",
        canonical_locator="https://doi.org/10.1000/example.1",
        connector_id="connector:test",
        resolved_at=INSTANT,
        response_sha256="0" * 64,
        is_open_access=False,
        oa_status="closed",
        title="Resolved example",
    )
    store.put_record("discovery-hit", discovery)
    store.put_record("open-access-resolution", resolution)

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
    result = KnowledgeQueryEngine(database).query(
        "example old result corrects",
        record_types=(QueryRecordType.REFERENCE,),
    )

    assert build.schema_version == 8
    assert build.counts["citation_derivations"] == 1
    assert build.counts["research_identifiers"] == 5
    assert build.counts["bibliographic_references"] == 5
    assert build.counts["identifier_discovery_links"] == 1
    assert build.counts["identifier_open_access_links"] == 1
    hit = next(
        item
        for item in result.hits
        if item.identifier_value == "https://example.org/old-result"
    )
    assert hit.reference_relation == "corrects"
    assert hit.identifier_kind == "url"
    assert hit.identifier_value == "https://example.org/old-result"
    assert hit.source_uri == "https://example.org/review.md"
    assert hit.anchor_kind == "paragraph"
    doi_hit = next(
        item
        for item in KnowledgeQueryEngine(database).query(
            "10.1000 example",
            record_types=(QueryRecordType.REFERENCE,),
        ).hits
        if item.identifier_kind == "doi"
    )
    assert doi_hit.resolved_discovery_hit_ids == (discovery.id,)
    assert doi_hit.resolved_open_access_resolution_ids == (resolution.id,)
    identifier = KnowledgeQueryEngine(database).identifier(
        IdentifierKind.DOI,
        "https://doi.org/10.1000/EXAMPLE.1",
    )
    assert identifier.value == "10.1000/example.1"
    assert len(identifier.references) == 1
    assert identifier.discovery_hits[0]["id"] == discovery.id
    assert identifier.discovery_hits[0]["match_rule"] == "exact_known_entity_id"
    assert identifier.open_access_resolutions[0]["id"] == resolution.id
    assert identifier.open_access_resolutions[0]["match_rule"] == "exact_normalized_doi"
