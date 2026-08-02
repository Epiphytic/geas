from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from pydantic_core import to_jsonable_python


def canonical_json(value: Any) -> bytes:
    """Return the stable JSON representation used for record hashes."""
    value = to_jsonable_python(value, exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def content_id(prefix: str, value: Any) -> str:
    return f"{prefix}:sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ThreatStatus(StrEnum):
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    REMEDIATED = "remediated"
    FALSE_POSITIVE = "false_positive"


class ThreatSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DetectorKind(StrEnum):
    DETERMINISTIC_RULE = "deterministic_rule"
    SANDBOX_OBSERVATION = "sandbox_observation"
    HUMAN = "human"
    MODEL = "model"
    EXTERNAL_FEED = "external_feed"


class EvidenceSelector(StrictModel):
    type: Literal["text_quote", "byte_range", "json_pointer", "external_reference"]
    exact: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    pointer: str | None = None


class SourceVersion(StrictModel):
    id: str
    source_uri: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquired_at: datetime
    media_type: str
    byte_length: int = Field(ge=0)
    predecessor: str | None = None
    connector_id: str
    trust_zone: Literal["trusted", "untrusted", "quarantined"] = "untrusted"
    license: str | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        source_uri: str,
        content: bytes,
        media_type: str,
        connector_id: str,
        acquired_at: datetime | None = None,
        predecessor: str | None = None,
        license: str | None = None,
    ) -> SourceVersion:
        digest = hashlib.sha256(content).hexdigest()
        return cls(
            id=f"source:sha256:{digest}",
            source_uri=source_uri,
            content_sha256=digest,
            acquired_at=acquired_at or utc_now(),
            media_type=media_type,
            byte_length=len(content),
            predecessor=predecessor,
            connector_id=connector_id,
            license=license,
        )


class EvidenceFragment(StrictModel):
    id: str
    source_version: str
    selector: EvidenceSelector
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class Claim(StrictModel):
    id: str
    subject: str
    predicate: str
    object: str | int | float | bool
    qualifiers: dict[str, str | int | float | bool] = Field(default_factory=dict)
    stance: Literal["asserts", "denies", "questions", "reports"]
    epistemic_status: Literal["observed", "inferred", "hypothesized", "consensus"]
    asserted_by: str
    evidence: tuple[str, ...] = Field(min_length=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    recorded_at: datetime
    review_state: ReviewState = ReviewState.PROPOSED

    @field_validator("valid_until")
    @classmethod
    def valid_interval(cls, value: datetime | None, info: Any) -> datetime | None:
        start = info.data.get("valid_from")
        if value is not None and start is not None and value < start:
            raise ValueError("valid_until must not precede valid_from")
        return value


class ThreatTarget(StrictModel):
    source_version: str
    evidence_fragment: str | None = None
    connector_id: str | None = None


class Detector(StrictModel):
    kind: DetectorKind
    id: str
    version: str | None = None


class ThreatObservation(StrictModel):
    id: str
    target: ThreatTarget
    threat_type: str
    status: ThreatStatus
    detected_at: datetime
    detector: Detector
    evidence: tuple[str, ...] = Field(min_length=1)
    severity: ThreatSeverity
    attempted_action: str | None = None
    policy_rule: str | None = None
    supersedes: str | None = None

    @model_validator(mode="after")
    def model_cannot_confirm(self) -> ThreatObservation:
        if self.detector.kind is DetectorKind.MODEL and self.status is ThreatStatus.CONFIRMED:
            raise ValueError("model detectors may create suspected observations only")
        return self


class ThreatAssessment(StrictModel):
    id: str
    target: ThreatTarget
    observation_ids: tuple[str, ...] = Field(min_length=1)
    status: ThreatStatus
    severity: ThreatSeverity
    assessed_at: datetime
    assessed_by: Detector
    rationale: str
    supersedes: str | None = None

    @model_validator(mode="after")
    def model_cannot_confirm(self) -> ThreatAssessment:
        if self.assessed_by.kind is DetectorKind.MODEL and self.status is ThreatStatus.CONFIRMED:
            raise ValueError("a model exposed to content cannot confirm an assessment")
        return self


class PolicyAction(StrEnum):
    ALLOW = "allow"
    ALLOW_METADATA_ONLY = "allow_metadata_only"
    SANDBOX = "sandbox"
    QUARANTINE = "quarantine"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyStage(StrEnum):
    RETRIEVAL = "retrieval"
    EXTRACTION = "extraction"
    COMMIT = "commit"


class PolicyDecision(StrictModel):
    id: str
    target: ThreatTarget
    workflow_id: str
    stage: PolicyStage
    action: PolicyAction
    rule_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    decided_at: datetime
    engine_version: str


class ProviderConfig(StrictModel):
    kind: Literal["openai_compatible"]
    base_url: HttpUrl
    model: str
    api_key_env: str = ""
    external: bool
    max_output_tokens: int = Field(gt=0, le=65536)

    @model_validator(mode="after")
    def endpoint_matches_trust_boundary(self) -> ProviderConfig:
        host = self.base_url.host
        if self.external:
            if self.base_url.scheme != "https":
                raise ValueError("external model providers require HTTPS")
            return self
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("local model providers require a literal loopback address") from exc
        if not address.is_loopback:
            raise ValueError("local model providers require a loopback address")
        return self
