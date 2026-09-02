from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

import pytest

from research_agent.projection import TopicView


def _topic(*, reverse: bool = False) -> TopicView:
    """A complete projection fixture, including intentionally hostile source data."""
    values = {
        "concepts": (
            {
                "id": "concept:root",
                "label": "Root",
                "description": "Root definition",
                "broader": "",
                "synonyms": "root term",
            },
            {
                "id": "concept:child",
                "label": "Child",
                "description": "Child definition",
                "broader": "concept:root",
                "synonyms": "",
            },
        ),
        "sources": (
            {
                "id": "source:one",
                "title": 'Source \\\"title\\\"',
                "authorship_status": "verified",
                "source_uri": "https://archive.example.test/one",
                "original_locator": "https://origin.example.test/one",
                "content_sha256": "1" * 64,
                "authors_json": '["Ada"]',
                "publisher": "Example Publisher",
                "published_at": "2026-08-01T12:00:00+00:00",
                "acquired_at": "2026-08-02T12:00:00+00:00",
                "license": "CC-BY-4.0",
                "license_status": "confirmed",
                "usage_conditions_json": '{"attribution":true}',
                "usage_conditions_status": "confirmed",
                "usage_permissions_json": '{"store":true}',
                "rights_basis": "license",
                "rights_basis_status": "confirmed",
                "provenance_note": "operator supplied",
                "provenance_status": "verified",
                "connector_id": "local_file",
                "roles_json": '["primary"]',
                "associated_at": "2026-08-03T12:00:00+00:00",
                "associated_by": "operator",
            },
        ),
        "claims": (
            {
                "id": "claim:one",
                "subject": "concept:root",
                "predicate": "supports",
                "object_json": '{"name":"child","rank":1}',
                "stance": "asserts",
                "epistemic_status": "observed",
                "asserted_by": "source:one",
                "qualifiers_json": "{}",
                "evidence_id": "evidence:one",
                "source_id": "source:one",
                "source_uri": "https://origin.example.test/one",
                "exact_text": 'quote """ ' + "\\" + "\n" + "> injected <urn:evil> a <urn:Bad> .",
                "selector_start": 7,
                "selector_end": 12,
            },
        ),
        "controversies": (
            {
                "id": "controversy:one",
                "question": "Which claim?",
                "description": "Competing evidence",
                "status": "open",
                "claim_ids": "claim:one",
            },
        ),
        "gaps": (
            {
                "id": "gap:one",
                "question": "What remains?",
                "rationale": "More evidence is needed.",
                "topic_concept_id": "concept:root",
                "kind": "evidence",
                "status": "open",
                "priority": 1,
                "related_claim_ids": "claim:one",
            },
        ),
        "threats": (
            {
                "id": "threat:one",
                "source_version": "source:one",
                "source_uri": "https://archive.example.test/one",
                "threat_type": "prompt_injection",
                "status": "suspected",
                "severity": "high",
                "detector_kind": "deterministic_rule",
                "detector_id": "rule-1",
                "policy_rule": "source-policy",
                "detected_at": "2026-08-04T12:00:00+00:00",
            },
        ),
        "references": (
            {
                "id": "reference:doi/10.1000",
                "identifier_kind": "doi",
                "identifier_value": "10.1000/example",
                "relation": "cites",
                "canonical_locator": "https://doi.org/10.1000/example",
                "source_id": "source:one",
                "source_uri": "https://origin.example.test/one",
                "structural_anchor_id": "anchor:one",
                "start": 10,
                "end": 20,
                "signal": "exact",
                "page_number": 4,
                "resolved_discovery_hit_ids": "discovery:one",
                "resolved_open_access_resolution_ids": "resolution:one",
            },
        ),
    }
    if reverse:
        values = {name: tuple(reversed(records)) for name, records in values.items()}
    return TopicView(
        topic_concept_id="concept:root",
        as_of=datetime(2026, 8, 31, 12, tzinfo=UTC),
        descendant_concept_ids=("concept:root", "concept:child"),
        projection_snapshot_id="truth:snapshot/one",
        **values,
    )


def test_render_topic_turtle_maps_the_complete_topic_projection_deterministically() -> None:
    """Catches a renderer omitting a TopicView record type or leaking input row order."""
    from research_agent.rdf_render import render_topic_turtle

    rendered = render_topic_turtle(_topic())

    assert rendered == render_topic_turtle(_topic())
    assert rendered == render_topic_turtle(_topic(reverse=True))
    assert rendered.startswith("@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n")
    assert "geas:Concept" in rendered
    assert "geas:Claim" in rendered
    assert "geas:Evidence" in rendered
    assert "geas:Source" in rendered
    assert "geas:Controversy" in rendered
    assert "geas:KnowledgeGap" in rendered
    assert "geas:ThreatObservation" in rendered
    assert "geas:BibliographicReference" in rendered
    assert "urn:geas:snapshot:truth%3Asnapshot%2Fone" in rendered
    assert "urn:geas:reference:reference%3Adoi%2F10.1000" in rendered
    assert 'dcterms:creator "[\\\"Ada\\\"]"' in rendered
    statement = (
        '"{\\\"object\\\":{\\\"name\\\":\\\"child\\\",\\\"rank\\\":1},'
        '\\\"predicate\\\":\\\"supports\\\",\\\"subject\\\":\\\"concept:root\\\"}"'
    )
    assert statement in rendered


def test_render_topic_turtle_maps_current_source_threat_and_reference_fields_as_literals() -> None:
    """Catches projection fields silently disappearing from the RDF ABox."""
    from research_agent.rdf_render import render_topic_turtle

    rendered = render_topic_turtle(_topic())

    source = "<urn:geas:source:source%3Aone>"
    threat = "<urn:geas:threat:threat%3Aone>"
    reference = "<urn:geas:reference:reference%3Adoi%2F10.1000>"
    assert f'{source} dcterms:creator "[\\\"Ada\\\"]" .' in rendered
    assert f'{source} geas:authorshipStatus "verified" .' in rendered
    assert f'{source} dcterms:issued "2026-08-01T12:00:00+00:00" .' in rendered
    assert f'{source} geas:acquiredAt "2026-08-02T12:00:00+00:00" .' in rendered
    assert f'{source} geas:licenseStatus "confirmed" .' in rendered
    assert f'{source} geas:connectorId "local_file" .' in rendered
    assert f'{source} geas:topicRoles "[\\\"primary\\\"]" .' in rendered
    assert f'{source} geas:usageConditions "{{\\\"attribution\\\":true}}" .' in rendered
    assert f'{source} geas:usageConditionsStatus "confirmed" .' in rendered
    assert f'{source} geas:usagePermissions "{{\\\"store\\\":true}}" .' in rendered
    assert f'{source} geas:rightsBasis "license" .' in rendered
    assert f'{source} geas:rightsBasisStatus "confirmed" .' in rendered
    assert f'{source} geas:provenanceNote "operator supplied" .' in rendered
    assert f'{source} geas:provenanceStatus "verified" .' in rendered
    assert f'{source} geas:associatedAt "2026-08-03T12:00:00+00:00" .' in rendered
    assert f'{source} geas:associatedBy "operator" .' in rendered
    assert f'{threat} geas:detectedAt "2026-08-04T12:00:00+00:00" .' in rendered
    assert f'{threat} geas:detectorKind "deterministic_rule" .' in rendered
    assert f'{reference} geas:pageNumber "4" .' in rendered
    assert f'{reference} geas:resolvedDiscoveryHitIds "discovery:one" .' in rendered
    assert f'{reference} geas:resolvedOpenAccessResolutionIds "resolution:one" .' in rendered


def test_render_topic_turtle_declares_every_geas_term_used_by_an_abox_row() -> None:
    """Catches an ABox mapping with a missing TBox property or class declaration."""
    from research_agent.rdf_render import render_topic_turtle

    rendered = render_topic_turtle(_topic())
    instance_lines = [line for line in rendered.splitlines() if line.startswith("<urn:geas:")]
    used_terms = {
        term
        for line in instance_lines
        for term in re.findall(
            r"(?<!urn:)\bgeas:[A-Za-z][A-Za-z0-9]*", line.partition("> ")[2]
        )
    }

    assert used_terms
    assert "geas:Source" in used_terms
    assert "geas:source" not in used_terms
    for term in used_terms:
        assert (
            f"{term} rdf:type rdf:Property ." in rendered
            or f"{term} rdf:type rdfs:Class ." in rendered
        )
    assert "geas:authors rdf:type rdf:Property ." not in rendered


def test_render_topic_turtle_uses_concrete_abox_subjects_for_every_record_type() -> None:
    """Catches type names being present only in the TBox without instance mappings."""
    from research_agent.rdf_render import render_topic_turtle

    rendered = render_topic_turtle(_topic())

    assert "<urn:geas:concept:concept%3Aroot> rdf:type geas:Concept ." in rendered
    assert "<urn:geas:claim:claim%3Aone> rdf:type geas:Claim ." in rendered
    assert "<urn:geas:evidence:evidence%3Aone> rdf:type geas:Evidence ." in rendered
    assert "<urn:geas:source:source%3Aone> rdf:type geas:Source ." in rendered
    assert "<urn:geas:controversy:controversy%3Aone> rdf:type geas:Controversy ." in rendered
    assert "<urn:geas:gap:gap%3Aone> rdf:type geas:KnowledgeGap ." in rendered
    assert "<urn:geas:threat:threat%3Aone> rdf:type geas:ThreatObservation ." in rendered
    assert (
        "<urn:geas:reference:reference%3Adoi%2F10.1000> "
        "rdf:type geas:BibliographicReference ."
    ) in rendered
    assert "<urn:geas:snapshot:truth%3Asnapshot%2Fone> rdf:type geas:Snapshot ." in rendered


def test_render_topic_turtle_matches_the_fixed_golden_byte_snapshot() -> None:
    """Catches any byte-level change to the complete deterministic projection fixture."""
    from research_agent.rdf_render import render_topic_turtle

    digest = hashlib.sha256(render_topic_turtle(_topic()).encode()).hexdigest()

    assert digest == "3d323697e12195209f99c5f7edd9d46ca8c7eb77054423492193b05093b67162"


def test_render_topic_turtle_keeps_hostile_text_and_locators_inert_literals() -> None:
    """Catches source text closing a Turtle literal or turning a locator into an IRI."""
    from research_agent.rdf_render import render_topic_turtle

    rendered = render_topic_turtle(_topic())

    quote_lines = [line for line in rendered.splitlines() if "geas:exactQuote" in line]
    assert len(quote_lines) == 4  # The one data triple plus the static TBox declaration.
    quote_line = next(line for line in quote_lines if line.startswith("<urn:geas:evidence:"))
    assert quote_line.count(chr(92) + '"') >= 3
    assert chr(92) + "n> injected <urn:evil> a <urn:Bad> ." in quote_line
    assert '<https://archive.example.test/one>' not in rendered
    assert '"https://archive.example.test/one"' in rendered
    assert '"https://doi.org/10.1000/example"' in rendered


def test_render_topic_turtle_omits_absent_optional_fields() -> None:
    """Catches invented provenance values for optional source metadata."""
    from research_agent.rdf_render import render_topic_turtle

    topic = _topic().model_copy(update={"sources": ({"id": "source:bare"},)})

    rendered = render_topic_turtle(topic)

    source_lines = [line for line in rendered.splitlines() if "source%3Abare" in line]
    assert source_lines
    assert all("dcterms:title" not in line for line in source_lines)
    assert "unknown" not in "\n".join(source_lines)


def test_render_topic_turtle_rejects_conflicting_duplicate_evidence() -> None:
    """Catches arbitrary selection when a repeated evidence ID has incompatible rows."""
    from research_agent.rdf_render import render_topic_turtle

    first = _topic().claims[0]
    conflict = {**first, "exact_text": "different exact quote"}
    topic = _topic().model_copy(update={"claims": (first, conflict)})

    with pytest.raises(ValueError, match="conflicting duplicate evidence"):
        render_topic_turtle(topic)


def test_render_topic_turtle_allows_one_claim_with_two_distinct_evidence_sources() -> None:
    """Catches source join metadata being mistakenly treated as claim identity."""
    from research_agent.rdf_render import render_topic_turtle

    first = _topic().claims[0]
    second = {
        **first,
        "evidence_id": "evidence:two",
        "source_id": "source:two",
        "source_uri": "https://origin.example.test/two",
        "exact_text": "second exact quote",
        "acquired_at": "2026-09-01T00:00:00+00:00",
        "license": "CC0-1.0",
    }
    topic = _topic().model_copy(update={"claims": (second, first)})

    rendered = render_topic_turtle(topic)

    claim = "<urn:geas:claim:claim%3Aone>"
    assert rendered.count(f"{claim} geas:supportedBy") == 2
    assert f"{claim} geas:supportedBy <urn:geas:evidence:evidence%3Aone> ." in rendered
    assert f"{claim} geas:supportedBy <urn:geas:evidence:evidence%3Atwo> ." in rendered
    assert rendered == render_topic_turtle(topic.model_copy(update={"claims": (first, second)}))


def test_render_topic_turtle_rejects_conflicting_claim_rows() -> None:
    """Catches arbitrary selection when a repeated claim ID changes its statement."""
    from research_agent.rdf_render import render_topic_turtle

    first = _topic().claims[0]
    conflict = {**first, "predicate": "contradicts"}
    topic = _topic().model_copy(update={"claims": (first, conflict)})

    with pytest.raises(ValueError, match="conflicting duplicate claim"):
        render_topic_turtle(topic)


def test_render_topic_turtle_percent_encodes_hostile_identifier_components() -> None:
    """Catches a record ID escaping its IRI and injecting an arbitrary RDF term."""
    from research_agent.rdf_render import render_topic_turtle

    hostile_id = "source:unsafe> a <urn:evil>"
    topic = _topic().model_copy(update={"sources": ({"id": hostile_id},)})

    rendered = render_topic_turtle(topic)

    assert "<urn:geas:source:source%3Aunsafe%3E%20a%20%3Curn%3Aevil%3E>" in rendered
    assert "<urn:geas:source:source:unsafe> a <urn:evil>>" not in rendered


def test_render_topic_turtle_escapes_control_characters_in_untrusted_literals() -> None:
    """Catches control characters terminating or invalidating a Turtle short literal."""
    from research_agent.rdf_render import render_topic_turtle

    topic = _topic().model_copy(
        update={"sources": ({"id": "source:control", "title": "A\x00B\x1fC"},)}
    )

    rendered = render_topic_turtle(topic)

    assert 'dcterms:title "A\\u0000B\\u001fC" .' in rendered


def test_render_topic_turtle_deduplicates_identical_claim_evidence_join_rows() -> None:
    """Catches a join fan-out emitting repeated claim or evidence resources."""
    from research_agent.rdf_render import render_topic_turtle

    claim = _topic().claims[0]
    topic = _topic().model_copy(update={"claims": (claim, dict(claim))})

    rendered = render_topic_turtle(topic)

    assert rendered.count("<urn:geas:claim:claim%3Aone> geas:supportedBy") == 1
    assert rendered.count("<urn:geas:evidence:evidence%3Aone> geas:exactQuote") == 1


def test_render_topic_turtle_parses_with_rdflib_when_available() -> None:
    """Catches invalid Turtle escaping without making rdflib a runtime dependency."""
    rdflib = pytest.importorskip("rdflib")
    from research_agent.rdf_render import render_topic_turtle

    graph = rdflib.Graph()
    graph.parse(data=render_topic_turtle(_topic()), format="turtle")

    assert len(graph) > 0
    assert not any(isinstance(term, rdflib.BNode) for triple in graph for term in triple)
