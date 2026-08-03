from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from research_agent.citations import BibliographicReference, ReferenceRelation
from research_agent.knowledge import Controversy, GapStatus, KnowledgeGap
from research_agent.models import (
    Claim,
    ReviewState,
    StrictModel,
    ThreatObservation,
    ThreatStatus,
    content_id,
)
from research_agent.store import ImmutableStore


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class KnowledgeAuditFinding(StrictModel):
    id: str
    rule_id: str
    severity: AuditSeverity
    record_id: str
    related_record_ids: tuple[str, ...] = ()
    message: str
    recommended_action: str


class KnowledgeAuditReport(StrictModel):
    id: str
    as_of: datetime
    findings: tuple[KnowledgeAuditFinding, ...]
    counts: dict[str, int]
    clean: bool
    auditor_version: str


class DeterministicKnowledgeAuditor:
    """Check canonical graph invariants without exposing records to a model."""

    version = "deterministic-knowledge-auditor/1"

    def audit(self, store: ImmutableStore, *, as_of: datetime) -> KnowledgeAuditReport:
        claims = {
            item.id: item
            for item in (
                Claim.model_validate(value) for value in store.iter_records("claim")
            )
        }
        gaps = tuple(
            KnowledgeGap.model_validate(value)
            for value in store.iter_records("knowledge-gap")
        )
        controversies = tuple(
            Controversy.model_validate(value)
            for value in store.iter_records("controversy")
        )
        observations = tuple(
            ThreatObservation.model_validate(value)
            for value in store.iter_records("threat-observation")
        )
        fragments = {
            value["id"]: value for value in store.iter_records("evidence-fragment")
        }
        references = tuple(
            BibliographicReference.model_validate(value)
            for value in store.iter_records("bibliographic-reference")
        )
        active_threat_sources = {
            item.target.source_version
            for item in observations
            if item.status in {ThreatStatus.SUSPECTED, ThreatStatus.CONFIRMED}
        }
        findings: list[KnowledgeAuditFinding] = []

        def add(
            rule_id: str,
            severity: AuditSeverity,
            record_id: str,
            message: str,
            recommended_action: str,
            related: tuple[str, ...] = (),
        ) -> None:
            fields = {
                "rule_id": rule_id,
                "severity": severity,
                "record_id": record_id,
                "related_record_ids": tuple(sorted(related)),
                "message": message,
                "recommended_action": recommended_action,
            }
            findings.append(
                KnowledgeAuditFinding(
                    id=content_id("knowledge-audit-finding", fields),
                    **fields,
                )
            )

        for claim in claims.values():
            missing = tuple(sorted(set(claim.evidence) - set(fragments)))
            if missing:
                add(
                    "claim-evidence-exists",
                    AuditSeverity.ERROR,
                    claim.id,
                    "Claim references evidence fragments absent from canonical storage.",
                    "Restore the immutable evidence records or supersede the claim.",
                    missing,
                )
            tainted = tuple(
                sorted(
                    evidence_id
                    for evidence_id in claim.evidence
                    if evidence_id in fragments
                    and fragments[evidence_id]["source_version"] in active_threat_sources
                )
            )
            if claim.review_state is ReviewState.ACCEPTED and tainted:
                add(
                    "accepted-claim-active-source-threat",
                    AuditSeverity.ERROR,
                    claim.id,
                    "Accepted claim depends on a source with an active threat observation.",
                    "Review the source and supersede or re-evidence the claim.",
                    tainted,
                )

        for controversy in controversies:
            selected = [claims[item] for item in controversy.claim_ids if item in claims]
            missing = tuple(sorted(set(controversy.claim_ids) - set(claims)))
            if missing:
                add(
                    "controversy-claims-exist",
                    AuditSeverity.ERROR,
                    controversy.id,
                    "Controversy references claims absent from canonical storage.",
                    "Restore the claims or supersede the controversy.",
                    missing,
                )
            stances = {item.stance for item in selected}
            objects = {str(item.object).casefold() for item in selected}
            if len(stances) < 2 and len(objects) < 2:
                add(
                    "controversy-has-distinct-positions",
                    AuditSeverity.WARNING,
                    controversy.id,
                    "Controversy does not contain deterministically distinct positions.",
                    "Add an opposing, questioning, or substantively different claim.",
                    controversy.claim_ids,
                )

        for gap in gaps:
            if (
                gap.status is not GapStatus.RESOLVED
                and gap.freshness_deadline is not None
                and gap.freshness_deadline <= as_of
            ):
                add(
                    "gap-freshness-deadline",
                    AuditSeverity.WARNING,
                    gap.id,
                    "Open knowledge gap is past its freshness deadline.",
                    "Run a new query plan and record its coverage before review.",
                    gap.searched_query_plan_ids,
                )
            if gap.status is GapStatus.RESOLVED and not gap.related_claim_ids:
                add(
                    "resolved-gap-has-resolution-evidence",
                    AuditSeverity.WARNING,
                    gap.id,
                    "Resolved knowledge gap has no related claim.",
                    "Link the resolving claim or record a superseding gap rationale.",
                )

        for reference in references:
            if reference.relation is ReferenceRelation.RETRACTS:
                add(
                    "explicit-retraction-reference",
                    AuditSeverity.WARNING,
                    reference.id,
                    "A deterministic textual signal says this source retracts an identifier.",
                    "Resolve the identifier against authoritative retraction metadata and review.",
                    (reference.identifier_id, reference.structural_anchor_id),
                )

        ordered = tuple(sorted(findings, key=lambda item: (item.severity, item.rule_id, item.id)))
        counts = {
            severity.value: sum(item.severity is severity for item in ordered)
            for severity in AuditSeverity
        }
        fields = {
            "as_of": as_of,
            "finding_ids": tuple(item.id for item in ordered),
            "auditor_version": self.version,
        }
        return KnowledgeAuditReport(
            id=content_id("knowledge-audit-report", fields),
            as_of=as_of,
            findings=ordered,
            counts=counts,
            clean=counts[AuditSeverity.ERROR.value] == 0,
            auditor_version=self.version,
        )
