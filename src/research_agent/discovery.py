from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel, content_id


class ConnectorCapability(StrEnum):
    DISCOVERY = "discovery"
    METADATA = "metadata"
    FULL_TEXT = "full_text"
    ARCHIVE = "archive"
    LOCAL_FILE = "local_file"


class SourceClass(StrEnum):
    LOCAL_FILE = "local_file"
    SCHOLARLY = "scholarly"
    GOVERNMENT = "government"
    NEWS = "news"
    WEB = "web"
    ARCHIVE = "archive"
    REPOSITORY = "repository"


class AcquisitionState(StrEnum):
    DISCOVERED = "discovered"
    METADATA_ACQUIRED = "metadata_acquired"
    CONTENT_ACQUIRED = "content_acquired"
    QUARANTINED = "quarantined"
    PARSED = "parsed"
    EVIDENCE_ADDRESSABLE = "evidence_addressable"
    EXTRACTION_PROPOSED = "extraction_proposed"


class AccessConstraintReason(StrEnum):
    PAYWALL = "paywall"
    AUTHENTICATION = "authentication"
    ROBOTS_POLICY = "robots_policy"
    CAPTCHA = "captcha"
    DENIED = "denied"
    UNAVAILABLE_API = "unavailable_api"
    LICENSING_UNCERTAIN = "licensing_uncertain"
    MISSING_ARCHIVE = "missing_archive"
    NOT_FOUND = "not_found"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    SIZE_LIMIT = "size_limit"
    POLICY = "policy"


class TermMatch(StrEnum):
    ANY = "any"
    ALL = "all"


class CompilerIdentity(StrictModel):
    id: str
    version: str


class QueryPlan(StrictModel):
    id: str
    question: str = Field(min_length=1, max_length=10_000)
    concept_ids: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = Field(min_length=1)
    source_classes: frozenset[SourceClass] = Field(min_length=1)
    languages: tuple[str, ...] = ("en",)
    jurisdictions: tuple[str, ...] = ()
    time_start: datetime | None = None
    time_end: datetime | None = None
    minimum_primary_sources: int = Field(default=0, ge=0)
    minimum_independent_sources: int = Field(default=1, ge=0)
    require_controversy_search: bool = True
    capabilities: frozenset[ConnectorCapability] = Field(min_length=1)
    connector_ids: tuple[str, ...] = Field(min_length=1)
    match: TermMatch = TermMatch.ANY
    result_limit: int = Field(ge=1)
    page_limit: int = Field(ge=1)
    max_content_bytes: int = Field(ge=1)
    stop_after_empty_pages: int = Field(ge=1)
    compiler: CompilerIdentity
    human_approved: bool = False
    lossy_clauses: tuple[str, ...] = ()

    @field_validator("exact_terms")
    @classmethod
    def normalize_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            sorted({re.sub(r"\s+", " ", item.strip()).casefold() for item in value if item.strip()})
        )
        if not normalized:
            raise ValueError("at least one non-empty exact term is required")
        return normalized

    @field_validator("languages")
    @classmethod
    def normalize_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({item.strip().lower() for item in value if item.strip()}))
        if not normalized:
            raise ValueError("at least one language is required")
        for language in normalized:
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
                raise ValueError(f"invalid language tag: {language!r}")
        return normalized

    @model_validator(mode="after")
    def validate_interval(self) -> QueryPlan:
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("time_end must not precede time_start")
        return self


class DiscoveryRun(StrictModel):
    id: str
    query_plan_id: str
    connector_id: str
    connector_version: str
    normalized_query: str
    started_at: datetime
    ended_at: datetime
    index_snapshot: str | None = None
    pagination_cursors: tuple[str, ...] = ()
    response_sha256s: tuple[str, ...] = ()
    termination_reason: str
    result_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    rejection_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    rate_limited: bool = False
    truncated: bool = False

    @model_validator(mode="after")
    def validate_times(self) -> DiscoveryRun:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        return self


class DiscoveryHit(StrictModel):
    id: str
    upstream_id: str | None = None
    canonical_locator: str
    title: str
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    published_at: datetime | None = None
    media_type: str | None = None
    language: str | None = None
    upstream_rank: int = Field(ge=1)
    snippet: str | None = None
    discovery_run_id: str
    known_entity_ids: tuple[str, ...] = ()
    acquisition_eligible: bool
    threat_observation_ids: tuple[str, ...] = ()


class AcquisitionAttempt(StrictModel):
    id: str
    discovery_hit_id: str | None = None
    explicit_locator: str | None = None
    connector_id: str
    resolved_locator: str
    redirect_chain: tuple[str, ...] = ()
    outcome: str
    state: AcquisitionState
    attempted_at: datetime
    content_length: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    robots_outcome: str = "not_applicable"
    terms_outcome: str = "operator_configured"
    authentication_outcome: str = "not_required"
    licensing_outcome: str = "unknown"
    policy_outcome: str
    retry_classification: str = "none"
    next_eligible_attempt_at: datetime | None = None

    @model_validator(mode="after")
    def validate_target_and_content(self) -> AcquisitionAttempt:
        if bool(self.discovery_hit_id) == bool(self.explicit_locator):
            raise ValueError("exactly one acquisition target is required")
        missing_content_metadata = (
            self.content_sha256 is None or self.content_length is None or self.media_type is None
        )
        if self.state is AcquisitionState.CONTENT_ACQUIRED and missing_content_metadata:
            raise ValueError("content acquisition requires hash, length, and media type")
        return self


class AccessConstraint(StrictModel):
    id: str
    target_id: str
    locator: str
    reason: AccessConstraintReason
    observed_at: datetime
    connector_id: str
    lawful_alternatives: tuple[str, ...] = ()
    human_resolvable: bool
    detail: str | None = None


class CoverageRun(StrictModel):
    id: str
    query_plan_id: str
    topic_branch: str
    competency_questions: tuple[str, ...] = Field(min_length=1)
    discovery_run_ids: tuple[str, ...]
    searched_source_classes: frozenset[SourceClass]
    excluded_source_classes: frozenset[SourceClass]
    languages: tuple[str, ...]
    accessible_count: int = Field(ge=0)
    inaccessible_count: int = Field(ge=0)
    metadata_only_count: int = Field(ge=0)
    known_index_limitations: tuple[str, ...]
    unresolved_gap_ids: tuple[str, ...]
    measured_at: datetime
    freshness_deadline: datetime

    @model_validator(mode="after")
    def validate_freshness(self) -> CoverageRun:
        if self.freshness_deadline <= self.measured_at:
            raise ValueError("freshness deadline must follow measurement time")
        return self


class ConnectorManifest(StrictModel):
    id: str
    version: str
    capabilities: frozenset[ConnectorCapability] = Field(min_length=1)
    source_classes: frozenset[SourceClass] = Field(min_length=1)
    allowed_schemes: frozenset[str] = Field(min_length=1)
    allowed_hosts: frozenset[str] = frozenset()
    credential_env_vars: tuple[str, ...] = ()
    query_fields: frozenset[str] = frozenset()
    filter_fields: frozenset[str] = frozenset()
    max_results: int = Field(ge=1)
    max_pages: int = Field(ge=1)
    max_response_bytes: int = Field(ge=1)
    supported_media_types: frozenset[str] = Field(min_length=1)
    redistribution: str
    parser_version: str
    normalization_version: str
    network_trust_zone: str
    terms_note: str


class DiscoveryRequest(StrictModel):
    query_plan_id: str
    exact_terms: tuple[str, ...] = Field(min_length=1)
    match: TermMatch
    result_limit: int = Field(ge=1)
    page_limit: int = Field(ge=1)
    languages: tuple[str, ...]


class DiscoveryCandidate(StrictModel):
    upstream_id: str
    canonical_locator: str
    title: str
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    published_at: datetime | None = None
    media_type: str
    language: str | None = None
    snippet: str | None = None
    score: float = Field(ge=0)


class DiscoveryPage(StrictModel):
    candidates: tuple[DiscoveryCandidate, ...]
    cursor: str | None = None
    next_cursor: str | None = None
    rejected_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AcquisitionRequest(StrictModel):
    discovery_hit_id: str
    locator: str
    max_content_bytes: int = Field(ge=1)


class AcquisitionResult(StrictModel):
    locator: str
    content: bytes
    media_type: str


class DiscoveryConnector(Protocol):
    manifest: ConnectorManifest

    def normalize_query(self, request: DiscoveryRequest) -> str: ...

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]: ...


class AcquisitionConnector(Protocol):
    manifest: ConnectorManifest

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult: ...


class ResearchConnector(DiscoveryConnector, AcquisitionConnector, Protocol):
    """A connector that can discover and acquire through the narrow contracts."""


def identified(prefix: str, fields: dict[str, object]) -> str:
    """Create a content-derived ID from supplied canonical fields."""
    return content_id(prefix, fields)


def locator_path(locator: str) -> Path:
    """Convert a file locator to a path without accepting other schemes."""
    from urllib.parse import unquote, urlsplit

    parsed = urlsplit(locator)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("local connector accepts only local file URIs")
    return Path(unquote(parsed.path))
