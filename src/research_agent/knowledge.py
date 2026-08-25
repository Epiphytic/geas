from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.models import (
    Claim,
    Detector,
    DetectorKind,
    EvidenceFragment,
    EvidenceSelector,
    ReviewState,
    StrictModel,
    ThreatObservation,
    ThreatSeverity,
    ThreatStatus,
    ThreatTarget,
    content_id,
    utc_now,
)
from research_agent.store import ImmutableStore


class Concept(StrictModel):
    id: str
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    broader: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    recorded_at: datetime
    recorded_by: str
    review_state: ReviewState = ReviewState.ACCEPTED

    @field_validator("broader", "synonyms")
    @classmethod
    def normalized_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip() for value in values if value.strip()}))


class ControversyStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_RESOLVED = "partially_resolved"
    RESOLVED = "resolved"


class Controversy(StrictModel):
    id: str
    topic_concept_id: str
    question: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = Field(min_length=2)
    status: ControversyStatus = ControversyStatus.OPEN
    recorded_at: datetime
    recorded_by: str
    review_state: ReviewState = ReviewState.ACCEPTED

    @field_validator("claim_ids")
    @classmethod
    def unique_claims(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(values)))
        if len(normalized) < 2:
            raise ValueError("a controversy requires at least two distinct claims")
        return normalized


class GapKind(StrEnum):
    UNKNOWN = "unknown"
    INACCESSIBLE = "inaccessible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    STALE = "stale"
    MISSING_SOURCE_CLASS = "missing_source_class"


class GapStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class TopicSourceRole(StrEnum):
    EVIDENCE = "evidence"
    INSPECTED = "inspected"


class TopicSourceAssociation(StrictModel):
    id: str
    topic_concept_id: str
    source_version_id: str
    roles: tuple[TopicSourceRole, ...] = Field(min_length=1)
    recorded_at: datetime
    recorded_by: str

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, values: tuple[TopicSourceRole, ...]) -> tuple[TopicSourceRole, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))


class KnowledgeGap(StrictModel):
    id: str
    topic_concept_id: str
    question: str = Field(min_length=1)
    kind: GapKind = GapKind.UNKNOWN
    rationale: str = Field(min_length=1)
    related_claim_ids: tuple[str, ...] = ()
    searched_query_plan_ids: tuple[str, ...] = ()
    priority: int = Field(default=50, ge=0, le=100)
    status: GapStatus = GapStatus.OPEN
    freshness_deadline: datetime | None = None
    recorded_at: datetime
    recorded_by: str
    review_state: ReviewState = ReviewState.ACCEPTED


class EvidenceProposal(StrictModel):
    key: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact: str = Field(min_length=1)
    prefix: str | None = None
    suffix: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def range_is_complete(self) -> EvidenceProposal:
        if (self.start is None) != (self.end is None):
            raise ValueError("evidence start and end must be supplied together")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("evidence end must be greater than start")
        return self


class ClaimProposal(StrictModel):
    key: str
    subject: str
    predicate: str
    object: str | int | float | bool
    qualifiers: dict[str, str | int | float | bool] = Field(default_factory=dict)
    stance: Literal["asserts", "denies", "questions", "reports"]
    epistemic_status: Literal["observed", "inferred", "hypothesized", "consensus"]
    asserted_by: str
    evidence_keys: tuple[str, ...] = Field(min_length=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class ControversyProposal(StrictModel):
    topic_concept_id: str
    question: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_keys: tuple[str, ...] = Field(min_length=2)
    status: ControversyStatus = ControversyStatus.OPEN


class GapProposal(StrictModel):
    topic_concept_id: str
    question: str = Field(min_length=1)
    kind: GapKind = GapKind.UNKNOWN
    rationale: str = Field(min_length=1)
    related_claim_keys: tuple[str, ...] = ()
    searched_query_plan_ids: tuple[str, ...] = ()
    priority: int = Field(default=50, ge=0, le=100)
    status: GapStatus = GapStatus.OPEN
    freshness_deadline: datetime | None = None


class KnowledgePack(StrictModel):
    version: Literal[1]
    topic: str
    topic_concept_id: str
    concepts: tuple[Concept, ...]
    evidence: tuple[EvidenceProposal, ...]
    claims: tuple[ClaimProposal, ...]
    controversies: tuple[ControversyProposal, ...] = ()
    gaps: tuple[GapProposal, ...] = ()
    inspect_source_sha256s: tuple[str, ...] = ()

    @classmethod
    def from_yaml(cls, path: Path) -> KnowledgePack:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    @model_validator(mode="after")
    def keys_are_unique_and_references_exist(self) -> KnowledgePack:
        evidence_keys = [item.key for item in self.evidence]
        claim_keys = [item.key for item in self.claims]
        concept_ids = [item.id for item in self.concepts]
        for label, values in (
            ("evidence", evidence_keys),
            ("claim", claim_keys),
            ("concept", concept_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} identifiers must be unique")
        unknown_evidence = sorted(
            {
                key
                for claim in self.claims
                for key in claim.evidence_keys
                if key not in set(evidence_keys)
            }
        )
        if unknown_evidence:
            raise ValueError(f"unknown evidence keys: {', '.join(unknown_evidence)}")
        unknown_claims = sorted(
            {
                key
                for item in (*self.controversies, *self.gaps)
                for key in (
                    item.claim_keys
                    if isinstance(item, ControversyProposal)
                    else item.related_claim_keys
                )
                if key not in set(claim_keys)
            }
        )
        if unknown_claims:
            raise ValueError(f"unknown claim keys: {', '.join(unknown_claims)}")
        if self.topic_concept_id not in set(concept_ids):
            raise ValueError("topic_concept_id must identify a concept in the pack")
        return self


class KnowledgeImportReceipt(StrictModel):
    id: str
    topic: str
    imported_at: datetime
    imported_by: str
    concept_ids: tuple[str, ...]
    evidence_fragment_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    controversy_ids: tuple[str, ...]
    gap_ids: tuple[str, ...]
    threat_observation_ids: tuple[str, ...]
    topic_source_association_ids: tuple[str, ...]
    record_hashes: dict[str, tuple[str, ...]]
    importer_version: str


class DeterministicThreatScanner:
    """Find inert source-text instructions without interpreting or executing them."""

    version = "deterministic-threat-scanner/2"
    patterns = (
        (
            "threat:indirect-prompt-injection:instruction-override",
            ThreatSeverity.HIGH,
            re.compile(
                r"\b(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|system)"
                r"\s+(?:instructions?|prompts?)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "threat:indirect-prompt-injection:tool-command",
            ThreatSeverity.HIGH,
            re.compile(
                r"\b(?:call|invoke|execute|run)\s+(?:the\s+)?(?:tool|shell|command)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "threat:credential-exfiltration-request",
            ThreatSeverity.CRITICAL,
            re.compile(
                r"\b(?:reveal|send|upload|print)\s+(?:the\s+)?(?:api\s+key|credentials?|secrets?)\b",
                re.IGNORECASE,
            ),
        ),
    )

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock

    def scan(
        self,
        source_id: str,
        content: bytes,
    ) -> tuple[tuple[EvidenceFragment, ThreatObservation], ...]:
        text = content.decode("utf-8", errors="replace")
        findings: list[tuple[EvidenceFragment, ThreatObservation]] = []
        for threat_type, severity, pattern in self.patterns:
            for match in pattern.finditer(text):
                exact = match.group(0)
                # Exact text alone is not a unique selector when the same hostile
                # phrase occurs more than once in one immutable source version.
                # Preserve the character range so every observation remains
                # independently attributable and receives a distinct content ID.
                selector = EvidenceSelector(
                    type="text_quote",
                    exact=exact,
                    start=match.start(),
                    end=match.end(),
                )
                fragment_fields = {
                    "source_version": source_id,
                    "selector": selector,
                    "content_sha256": hashlib.sha256(exact.encode()).hexdigest(),
                }
                fragment = EvidenceFragment(
                    id=content_id("evidence-fragment", fragment_fields),
                    source_version=source_id,
                    selector=selector,
                    content_sha256=fragment_fields["content_sha256"],
                    created_at=self.clock(),
                )
                observation_fields = {
                    "target": {
                        "source_version": source_id,
                        "evidence_fragment": fragment.id,
                    },
                    "threat_type": threat_type,
                    "detector": self.version,
                    "exact": exact.casefold(),
                }
                observation = ThreatObservation(
                    id=content_id("threat-observation", observation_fields),
                    target=ThreatTarget(
                        source_version=source_id,
                        evidence_fragment=fragment.id,
                    ),
                    threat_type=threat_type,
                    status=ThreatStatus.SUSPECTED,
                    detected_at=self.clock(),
                    detector=Detector(
                        kind=DetectorKind.DETERMINISTIC_RULE,
                        id="rule:inert-source-instruction",
                        version=self.version,
                    ),
                    evidence=(fragment.id,),
                    severity=severity,
                    attempted_action=exact,
                    policy_rule="source-text-is-data",
                )
                findings.append((fragment, observation))
        return tuple(findings)


class KnowledgeImporter:
    version = "knowledge-importer/1"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        clock: Callable[[], datetime] = utc_now,
        scanner: DeterministicThreatScanner | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.scanner = scanner or DeterministicThreatScanner(clock=clock)

    def import_pack(self, pack: KnowledgePack, *, imported_by: str) -> KnowledgeImportReceipt:
        self.store.initialize()
        imported_at = self.clock()
        sources = {
            value["content_sha256"]: value for value in self.store.iter_records("source-version")
        }
        required_digests = {
            *(item.source_content_sha256 for item in pack.evidence),
            *pack.inspect_source_sha256s,
        }
        missing = sorted(required_digests - set(sources))
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"knowledge pack references missing source blobs: {names}")

        self._validate_hierarchy(pack.concepts)
        evidence_by_key: dict[str, EvidenceFragment] = {}
        for proposal in pack.evidence:
            source = sources[proposal.source_content_sha256]
            content = self.store.read_blob(proposal.source_content_sha256)
            text = content.decode("utf-8", errors="strict")
            if proposal.start is not None and proposal.end is not None:
                if text[proposal.start : proposal.end] != proposal.exact:
                    raise ValueError(
                        f"evidence {proposal.key!r} does not match its exact range"
                    )
            elif text.count(proposal.exact) != 1:
                raise ValueError(
                    f"evidence {proposal.key!r} exact text must occur exactly once in its source"
                )
            if proposal.prefix and proposal.prefix not in text:
                raise ValueError(f"evidence {proposal.key!r} prefix is absent")
            if proposal.suffix and proposal.suffix not in text:
                raise ValueError(f"evidence {proposal.key!r} suffix is absent")
            selector = EvidenceSelector(
                type="text_quote",
                exact=proposal.exact,
                prefix=proposal.prefix,
                suffix=proposal.suffix,
                start=proposal.start,
                end=proposal.end,
            )
            fragment_fields = {
                "source_version": source["id"],
                "selector": selector,
                "content_sha256": hashlib.sha256(proposal.exact.encode()).hexdigest(),
            }
            evidence_by_key[proposal.key] = EvidenceFragment(
                id=content_id("evidence-fragment", fragment_fields),
                source_version=source["id"],
                selector=selector,
                content_sha256=fragment_fields["content_sha256"],
                created_at=imported_at,
            )

        scan_fragments: dict[str, EvidenceFragment] = {}
        observations: dict[str, ThreatObservation] = {}
        for digest in sorted(required_digests):
            source = sources[digest]
            for fragment, observation in self.scanner.scan(
                source["id"],
                self.store.read_blob(digest),
            ):
                scan_fragments[fragment.id] = fragment
                observations[observation.id] = observation
        poisoned_source_ids = {
            observation.target.source_version for observation in observations.values()
        }
        for key, fragment in evidence_by_key.items():
            if fragment.source_version in poisoned_source_ids:
                raise ValueError(
                    f"evidence {key!r} is from a source with a suspected deterministic threat"
                )

        claims_by_key: dict[str, Claim] = {}
        for proposal in pack.claims:
            fields: dict[str, Any] = {
                "subject": proposal.subject,
                "predicate": proposal.predicate,
                "object": proposal.object,
                "qualifiers": proposal.qualifiers,
                "stance": proposal.stance,
                "epistemic_status": proposal.epistemic_status,
                "asserted_by": proposal.asserted_by,
                "evidence": tuple(
                    sorted(evidence_by_key[key].id for key in proposal.evidence_keys)
                ),
                "valid_from": proposal.valid_from,
                "valid_until": proposal.valid_until,
                "recorded_at": imported_at,
                "review_state": ReviewState.ACCEPTED,
            }
            claims_by_key[proposal.key] = Claim(
                id=content_id("claim", fields),
                **fields,
            )

        controversies: list[Controversy] = []
        for proposal in pack.controversies:
            fields = {
                "topic_concept_id": proposal.topic_concept_id,
                "question": proposal.question,
                "description": proposal.description,
                "claim_ids": tuple(sorted(claims_by_key[key].id for key in proposal.claim_keys)),
                "status": proposal.status,
                "recorded_at": imported_at,
                "recorded_by": imported_by,
                "review_state": ReviewState.ACCEPTED,
            }
            controversies.append(Controversy(id=content_id("controversy", fields), **fields))

        gaps: list[KnowledgeGap] = []
        for proposal in pack.gaps:
            fields = {
                "topic_concept_id": proposal.topic_concept_id,
                "question": proposal.question,
                "kind": proposal.kind,
                "rationale": proposal.rationale,
                "related_claim_ids": tuple(
                    sorted(claims_by_key[key].id for key in proposal.related_claim_keys)
                ),
                "searched_query_plan_ids": proposal.searched_query_plan_ids,
                "priority": proposal.priority,
                "status": proposal.status,
                "freshness_deadline": proposal.freshness_deadline,
                "recorded_at": imported_at,
                "recorded_by": imported_by,
                "review_state": ReviewState.ACCEPTED,
            }
            gaps.append(KnowledgeGap(id=content_id("knowledge-gap", fields), **fields))

        roles_by_digest: dict[str, set[TopicSourceRole]] = {
            digest: {TopicSourceRole.INSPECTED} for digest in pack.inspect_source_sha256s
        }
        for proposal in pack.evidence:
            roles_by_digest.setdefault(proposal.source_content_sha256, set()).add(
                TopicSourceRole.EVIDENCE
            )
        associations: list[TopicSourceAssociation] = []
        for digest, roles in sorted(roles_by_digest.items()):
            fields = {
                "topic_concept_id": pack.topic_concept_id,
                "source_version_id": sources[digest]["id"],
                "roles": tuple(sorted(roles, key=lambda item: item.value)),
                "recorded_at": imported_at,
                "recorded_by": imported_by,
            }
            associations.append(
                TopicSourceAssociation(
                    id=content_id("topic-source-association", fields),
                    **fields,
                )
            )

        values: dict[str, tuple[StrictModel, ...]] = {
            "concept": pack.concepts,
            "evidence-fragment": (*evidence_by_key.values(), *scan_fragments.values()),
            "claim": tuple(claims_by_key.values()),
            "controversy": tuple(controversies),
            "knowledge-gap": tuple(gaps),
            "threat-observation": tuple(observations.values()),
            "topic-source": tuple(associations),
        }
        hashes = {
            kind: tuple(self.store.put_record(kind, item) for item in records)
            for kind, records in values.items()
        }
        receipt_fields = {
            "topic": pack.topic,
            "imported_at": imported_at,
            "imported_by": imported_by,
            "concept_ids": tuple(sorted(item.id for item in pack.concepts)),
            "evidence_fragment_ids": tuple(
                sorted(item.id for item in (*evidence_by_key.values(), *scan_fragments.values()))
            ),
            "claim_ids": tuple(sorted(item.id for item in claims_by_key.values())),
            "controversy_ids": tuple(sorted(item.id for item in controversies)),
            "gap_ids": tuple(sorted(item.id for item in gaps)),
            "threat_observation_ids": tuple(sorted(observations)),
            "topic_source_association_ids": tuple(sorted(item.id for item in associations)),
            "record_hashes": hashes,
            "importer_version": self.version,
        }
        receipt = KnowledgeImportReceipt(
            id=content_id("knowledge-import-receipt", receipt_fields),
            **receipt_fields,
        )
        self.store.put_record("knowledge-import-receipt", receipt)
        return receipt

    @staticmethod
    def _validate_hierarchy(concepts: tuple[Concept, ...]) -> None:
        concept_ids = {item.id for item in concepts}
        graph = {item.id: item.broader for item in concepts}
        unknown = sorted(
            parent for item in concepts for parent in item.broader if parent not in concept_ids
        )
        if unknown:
            raise ValueError(f"unknown broader concepts: {', '.join(unknown)}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("concept hierarchy contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph[node]:
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for node in sorted(graph):
            visit(node)
