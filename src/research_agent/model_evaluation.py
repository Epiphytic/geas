from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import Field

from research_agent.extraction import (
    ProposedConcept,
    ProposedControversy,
    ProposedGap,
    ValidatedExtractionProposal,
    ValidatedProposedClaim,
)
from research_agent.models import StrictModel, content_id


class ProposalQuality(StrictModel):
    proposal_id: str
    claim_count: int = Field(ge=0)
    distinct_predicate_count: int = Field(ge=0)
    concept_count: int = Field(ge=0)
    hierarchy_edge_count: int = Field(ge=0)
    controversy_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    evidence_selector_count: int = Field(ge=0)
    unique_claim_ratio: float = Field(ge=0.0, le=1.0)
    claims_per_concept: float = Field(ge=0.0)


class ProposalComparison(StrictModel):
    id: str
    baseline: ProposalQuality
    candidate: ProposalQuality
    recommendation: Literal["baseline", "candidate", "inconclusive"]
    reasons: tuple[str, ...]


class ProposalSlice(StrictModel):
    proposal_id: str
    root_concept_id: str
    concept_ids: tuple[str, ...]
    concepts: tuple[ProposedConcept, ...]
    claims: tuple[ValidatedProposedClaim, ...]
    controversies: tuple[ProposedControversy, ...]
    gaps: tuple[ProposedGap, ...]


def measure_proposal(proposal: ValidatedExtractionProposal) -> ProposalQuality:
    signatures = {
        (
            claim.subject,
            claim.predicate,
            str(claim.object),
            tuple(sorted(claim.qualifiers.items())),
            claim.stance,
        )
        for claim in proposal.claims
    }
    claim_count = len(proposal.claims)
    concept_count = len(proposal.concepts)
    return ProposalQuality(
        proposal_id=proposal.id,
        claim_count=claim_count,
        distinct_predicate_count=len({claim.predicate for claim in proposal.claims}),
        concept_count=concept_count,
        hierarchy_edge_count=sum(len(concept.broader) for concept in proposal.concepts),
        controversy_count=len(proposal.controversies),
        gap_count=len(proposal.gaps),
        evidence_selector_count=sum(
            len(claim.evidence) for claim in proposal.claims
        ),
        unique_claim_ratio=(len(signatures) / claim_count if claim_count else 1.0),
        claims_per_concept=(claim_count / max(1, concept_count)),
    )


def compare_proposals(
    baseline: ValidatedExtractionProposal,
    candidate: ValidatedExtractionProposal,
) -> ProposalComparison:
    """Prefer a candidate only for measurable structure/coverage gains.

    This intentionally avoids one opaque scalar. More verbose output alone is
    not a win: the candidate may not regress unique-claim ratio, and must
    improve at least two independent coverage or structure dimensions.
    """
    first = measure_proposal(baseline)
    second = measure_proposal(candidate)
    dimensions = (
        ("grounded claims", first.claim_count, second.claim_count),
        (
            "distinct predicates",
            first.distinct_predicate_count,
            second.distinct_predicate_count,
        ),
        ("concepts", first.concept_count, second.concept_count),
        ("hierarchy edges", first.hierarchy_edge_count, second.hierarchy_edge_count),
        ("controversies", first.controversy_count, second.controversy_count),
        ("knowledge gaps", first.gap_count, second.gap_count),
    )
    improvements = tuple(
        label for label, old, new in dimensions if new > old
    )
    regressions = tuple(
        label for label, old, new in dimensions if new < old
    )
    uniqueness_regressed = second.unique_claim_ratio + 0.02 < first.unique_claim_ratio
    reasons: list[str] = [
        f"candidate improves {label}" for label in improvements
    ]
    reasons.extend(f"candidate reduces {label}" for label in regressions)
    if uniqueness_regressed:
        reasons.append("candidate materially reduces unique-claim ratio")

    if len(improvements) >= 2 and not uniqueness_regressed and len(regressions) <= 1:
        recommendation: Literal["baseline", "candidate", "inconclusive"] = "candidate"
    elif len(regressions) >= 2 or uniqueness_regressed:
        recommendation = "baseline"
    else:
        recommendation = "inconclusive"
    if not reasons:
        reasons.append("deterministic quality dimensions are equal")
    fields = {
        "baseline": first,
        "candidate": second,
        "recommendation": recommendation,
        "reasons": tuple(reasons),
    }
    return ProposalComparison(
        id=content_id("model-parameter-comparison", fields),
        **fields,
    )


def find_proposal(
    records: Iterable[dict[str, object]],
    proposal_id: str,
) -> ValidatedExtractionProposal:
    matches = [
        ValidatedExtractionProposal.model_validate(value)
        for value in records
        if value.get("id") == proposal_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one extraction proposal {proposal_id!r}")
    return matches[0]


def slice_proposal(
    proposal: ValidatedExtractionProposal,
    concept_id: str,
    *,
    include_descendants: bool = True,
) -> ProposalSlice:
    proposed_ids = {item.id for item in proposal.concepts}
    referenced_ids = {item.subject for item in proposal.claims}
    parent_ids = {
        parent for item in proposal.concepts for parent in item.broader
    }
    if concept_id not in proposed_ids | referenced_ids | parent_ids:
        raise ValueError(f"proposal does not reference concept {concept_id!r}")
    selected = {concept_id}
    if include_descendants:
        changed = True
        while changed:
            children = {
                item.id
                for item in proposal.concepts
                if set(item.broader) & selected
            }
            changed = not children.issubset(selected)
            selected.update(children)
    concepts = tuple(
        item for item in proposal.concepts if item.id in selected
    )
    claims = tuple(
        item for item in proposal.claims if item.subject in selected
    )
    claim_keys = {item.key for item in claims}
    controversies = tuple(
        item
        for item in proposal.controversies
        if set(item.claim_keys) & claim_keys
    )
    gaps = tuple(
        item
        for item in proposal.gaps
        if set(item.related_claim_keys) & claim_keys
    )
    return ProposalSlice(
        proposal_id=proposal.id,
        root_concept_id=concept_id,
        concept_ids=tuple(sorted(selected)),
        concepts=concepts,
        claims=claims,
        controversies=controversies,
        gaps=gaps,
    )
