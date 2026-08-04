from datetime import UTC, datetime

from research_agent.extraction import (
    ProposedConcept,
    ProposedGap,
    ValidatedEvidenceSelector,
    ValidatedExtractionProposal,
    ValidatedProposedClaim,
)
from research_agent.knowledge import GapKind
from research_agent.model_evaluation import (
    compare_proposals,
    measure_proposal,
    slice_proposal,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _proposal(
    identifier: str,
    *,
    predicates: tuple[str, ...],
    concepts: tuple[ProposedConcept, ...] = (),
    gaps: int = 0,
) -> ValidatedExtractionProposal:
    evidence = ValidatedEvidenceSelector(
        anchor_id="structural-anchor:fixture",
        exact="grounded",
        start=0,
        end=8,
        exact_sha256="a" * 64,
    )
    claims = tuple(
        ValidatedProposedClaim(
            key=f"claim-{index}",
            subject="concept:topic",
            predicate=predicate,
            object=f"value-{index}",
            qualifiers={},
            stance="asserts",
            epistemic_status="observed",
            asserted_by="model:test",
            evidence=(evidence,),
        )
        for index, predicate in enumerate(predicates)
    )
    return ValidatedExtractionProposal(
        id=identifier,
        extraction_request_id=f"request:{identifier}",
        structural_derivation_id="structural-derivation:fixture",
        source_version_id="source:fixture",
        source_content_sha256="b" * 64,
        provider="local",
        model="fixture",
        proposed_at=NOW,
        concepts=concepts,
        claims=claims,
        controversies=(),
        gaps=tuple(
            ProposedGap(
                question=f"gap {index}",
                kind=GapKind.UNKNOWN,
                rationale="not established",
            )
            for index in range(gaps)
        ),
        raw_output_sha256="c" * 64,
        validator_version="fixture",
    )


def test_quality_measurement_rewards_atomic_structure_without_opaque_score() -> None:
    concept = ProposedConcept(
        key="retrieval",
        id="concept:retrieval",
        label="Retrieval",
        description="Retrieval behavior",
        broader=("concept:topic",),
    )
    proposal = _proposal(
        "proposal:one",
        predicates=("ep:retrieves", "ep:persists"),
        concepts=(concept,),
        gaps=1,
    )
    quality = measure_proposal(proposal)
    assert quality.claim_count == 2
    assert quality.distinct_predicate_count == 2
    assert quality.hierarchy_edge_count == 1
    assert quality.unique_claim_ratio == 1.0


def test_comparison_requires_multiple_nonredundant_improvements() -> None:
    baseline = _proposal("proposal:high", predicates=("ep:retrieves",))
    candidate = _proposal(
        "proposal:max",
        predicates=("ep:retrieves", "ep:persists"),
        gaps=1,
    )
    comparison = compare_proposals(baseline, candidate)
    assert comparison.recommendation == "candidate"
    assert "candidate improves grounded claims" in comparison.reasons
    assert "candidate improves knowledge gaps" in comparison.reasons


def test_proposal_slice_returns_only_requested_concept_subtree() -> None:
    retrieval = ProposedConcept(
        key="retrieval",
        id="concept:retrieval",
        label="Retrieval",
        description="Retrieval behavior",
        broader=("concept:topic",),
    )
    search = ProposedConcept(
        key="search",
        id="concept:search",
        label="Search",
        description="Search behavior",
        broader=("concept:retrieval",),
    )
    unrelated = ProposedConcept(
        key="interface",
        id="concept:interface",
        label="Interface",
        description="Interface behavior",
        broader=("concept:topic",),
    )
    proposal = _proposal(
        "proposal:slice",
        predicates=("ep:retrieves", "ep:searches", "ep:interfaces"),
        concepts=(retrieval, search, unrelated),
    )
    claims = list(proposal.claims)
    claims[0] = claims[0].model_copy(update={"subject": "concept:retrieval"})
    claims[1] = claims[1].model_copy(update={"subject": "concept:search"})
    claims[2] = claims[2].model_copy(update={"subject": "concept:interface"})
    proposal = proposal.model_copy(update={"claims": tuple(claims)})

    result = slice_proposal(proposal, "concept:retrieval")

    assert result.concept_ids == ("concept:retrieval", "concept:search")
    assert {item.subject for item in result.claims} == {
        "concept:retrieval",
        "concept:search",
    }
    topic = slice_proposal(proposal, "concept:topic")
    assert set(topic.concept_ids) == {
        "concept:topic",
        "concept:retrieval",
        "concept:search",
        "concept:interface",
    }
