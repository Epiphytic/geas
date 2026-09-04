"""Immutable, resumable source-work records and deterministic coordination."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityEvaluator,
    CapabilityRequest,
)
from research_agent.models import (
    ModelParameters,
    PolicyAction,
    PolicyStage,
    ProviderConfig,
    StrictModel,
    ThreatObservation,
    ThreatStatus,
    ThreatTarget,
    canonical_json,
    content_id,
    utc_now,
)
from research_agent.policy import PolicyEngine
from research_agent.store import ImmutableStore

if TYPE_CHECKING:
    from research_agent.extraction import AnchorGroundedExtractionManager, ExtractionProposalReceipt
    from research_agent.library import SourceLibraryManifest
    from research_agent.parsing import ParsedDocumentManager, ParsedIngestReceipt
    from research_agent.source_intent import SourceAdapter, SourceCandidate, SourceIntent


class SourceWorkPhase(StrEnum):
    CANDIDATE = "candidate"
    AUTHORIZED = "authorized"
    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    ACCESS_CONSTRAINED = "access_constrained"
    ARCHIVED = "archived"
    PARSED = "parsed"
    PARSER_CONSTRAINED = "parser_constrained"
    STRUCTURED = "structured"
    INDEXED = "indexed"
    ANCHORS_SELECTED = "anchors_selected"
    EXTRACTION_PROPOSED = "extraction_proposed"
    EXTRACTION_CONSTRAINED = "extraction_constrained"
    FINALIZED = "finalized"


class SourceWorkOutcome(StrEnum):
    SUCCESSFUL = "successful"
    CONSTRAINED_REQUIRED = "constrained_required"
    CONSTRAINED_OPTIONAL = "constrained_optional"
    INCOMPLETE = "incomplete"


_PREDECESSORS: dict[SourceWorkPhase, frozenset[SourceWorkPhase]] = {
    SourceWorkPhase.CANDIDATE: frozenset({SourceWorkPhase.FINALIZED}),
    SourceWorkPhase.AUTHORIZED: frozenset({SourceWorkPhase.CANDIDATE}),
    SourceWorkPhase.FETCHED: frozenset({SourceWorkPhase.AUTHORIZED}),
    SourceWorkPhase.NOT_MODIFIED: frozenset({SourceWorkPhase.AUTHORIZED}),
    SourceWorkPhase.ACCESS_CONSTRAINED: frozenset(
        {SourceWorkPhase.AUTHORIZED, SourceWorkPhase.FETCHED}
    ),
    SourceWorkPhase.ARCHIVED: frozenset({SourceWorkPhase.FETCHED}),
    SourceWorkPhase.PARSED: frozenset({SourceWorkPhase.ARCHIVED}),
    SourceWorkPhase.PARSER_CONSTRAINED: frozenset({SourceWorkPhase.ARCHIVED}),
    SourceWorkPhase.STRUCTURED: frozenset({SourceWorkPhase.PARSED}),
    SourceWorkPhase.INDEXED: frozenset({SourceWorkPhase.STRUCTURED}),
    SourceWorkPhase.ANCHORS_SELECTED: frozenset({SourceWorkPhase.INDEXED}),
    SourceWorkPhase.EXTRACTION_PROPOSED: frozenset({SourceWorkPhase.ANCHORS_SELECTED}),
    SourceWorkPhase.EXTRACTION_CONSTRAINED: frozenset(
        {
            SourceWorkPhase.PARSED,
            SourceWorkPhase.STRUCTURED,
            SourceWorkPhase.INDEXED,
            SourceWorkPhase.ANCHORS_SELECTED,
        }
    ),
    SourceWorkPhase.FINALIZED: frozenset(
        {
            SourceWorkPhase.NOT_MODIFIED,
            SourceWorkPhase.ACCESS_CONSTRAINED,
            SourceWorkPhase.PARSER_CONSTRAINED,
            SourceWorkPhase.EXTRACTION_PROPOSED,
            SourceWorkPhase.EXTRACTION_CONSTRAINED,
        }
    ),
}


def _digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


class SourceWorkItem(StrictModel):
    version: Literal[1] = 1
    ontology_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_intent_id: str
    source_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: str
    adapter_id: str
    adapter_version: str
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    extraction_validator_version: str = Field(min_length=1)
    capability_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: SourceWorkPhase
    predecessor_id: str | None = None
    predecessor_phase: SourceWorkPhase | None = None
    result_record_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def phase_has_an_exact_predecessor(self) -> SourceWorkItem:
        allowed = _PREDECESSORS[self.phase]
        if self.phase is SourceWorkPhase.CANDIDATE:
            if self.predecessor_id is None and self.predecessor_phase is None:
                return self
            if self.predecessor_id is None or self.predecessor_phase not in allowed:
                raise ValueError("candidate refresh must follow finalized source work")
            return self
        if self.predecessor_id is None or self.predecessor_phase not in allowed:
            raise ValueError("predecessor phase is invalid for source work phase")
        return self

    @property
    def id(self) -> str:
        return content_id("source-work", self.model_dump(mode="json"))

    @property
    def lineage_id(self) -> str:
        """Identity of compatible work, excluding transition and observation time."""
        fields = self.model_dump(
            mode="json",
            exclude={
                "phase",
                "predecessor_id",
                "predecessor_phase",
                "result_record_sha256",
                "created_at",
            },
        )
        return content_id("source-work-lineage", fields)


class SourceCheckpoint(StrictModel):
    version: Literal[1] = 1
    work_item_id: str
    phase: SourceWorkPhase
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=512)
    constraint: str | None = Field(default=None, min_length=1, max_length=128)
    retry_after: int | None = Field(default=None, ge=0)
    request_count: int = Field(default=1, ge=0)
    prior_source_version_id: str | None = None
    prior_source_record_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    semantic_outcome: SourceWorkOutcome | None = None
    recorded_at: datetime

    @field_validator("etag", "last_modified")
    @classmethod
    def validators_have_no_controls(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("conditional validator contains control characters")
        return value

    @field_validator("constraint")
    @classmethod
    def constraint_is_typed(cls, value: str | None) -> str | None:
        if value is not None and (
            value.casefold() != value or not value.replace("_", "").replace("-", "").isalnum()
        ):
            raise ValueError("source constraint must be a normalized type")
        return value

    @model_validator(mode="after")
    def metadata_matches_phase(self) -> SourceCheckpoint:
        if self.constraint is not None and self.phase is not SourceWorkPhase.ACCESS_CONSTRAINED:
            raise ValueError("typed source constraints require access_constrained phase")
        if self.retry_after is not None and self.constraint is None:
            raise ValueError("retry_after requires a typed source constraint")
        if (self.prior_source_version_id is None) != (self.prior_source_record_sha256 is None):
            raise ValueError("checkpoint source identity must be exact")
        if self.semantic_outcome is not None and self.phase is not SourceWorkPhase.FINALIZED:
            raise ValueError("semantic outcome requires a finalized checkpoint")
        return self

    @field_validator("recorded_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("source-checkpoint", self.model_dump(mode="json"))


class SourceUpdateReceipt(StrictModel):
    version: Literal[1] = 1
    source_intent_id: str
    work_item_ids: tuple[str, ...] = ()
    complete: bool
    finalized_at: datetime
    recovery_command: str | None = None
    source_intent_ids: tuple[str, ...] = ()
    completed_phases: tuple[SourceWorkPhase, ...] = ()
    source_version_ids: tuple[str, ...] = ()
    checkpoint_ids: tuple[str, ...] = ()
    constraint_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    semantic_outcomes: tuple[SourceWorkOutcome, ...] = ()

    @field_validator(
        "work_item_ids",
        "source_intent_ids",
        "source_version_ids",
        "checkpoint_ids",
        "constraint_ids",
        "proposal_ids",
    )
    @classmethod
    def normalize_work_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("semantic_outcomes")
    @classmethod
    def normalize_outcomes(
        cls, value: tuple[SourceWorkOutcome, ...]
    ) -> tuple[SourceWorkOutcome, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("finalized_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("source-update", self.model_dump(mode="json"))


class SourceWorkStore(Protocol):
    def get(self, work_item_id: str) -> SourceWorkItem | None: ...

    def put(self, item: SourceWorkItem) -> SourceWorkItem: ...

    def checkpoint(self, checkpoint: SourceCheckpoint) -> SourceCheckpoint: ...


@dataclass(frozen=True)
class FetchedSourcePayload:
    """Transient fetched bytes plus normalized, non-authoritative metadata."""

    content: bytes
    source_uri: str
    media_type: str
    connector_id: str
    license: str | None
    observed_at: datetime
    published_at: datetime | None = None
    valid_at: datetime | None = None


class SourceRetentionRequest(StrictModel):
    version: Literal[1] = 1
    source_intent_id: str
    source_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: str
    source_uri: str
    media_type: str
    connector_id: str
    license: str | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_length: int = Field(ge=0)

    @property
    def id(self) -> str:
        return content_id("source-retention-request", self.model_dump(mode="json"))


class SourceRetentionDecision(StrictModel):
    version: Literal[1] = 1
    request_id: str
    decision: Literal["allow", "deny"]
    storage_rights: str | None = None
    reason: str
    policy_version: str


class SourceRetentionPolicy(Protocol):
    def evaluate(self, request: SourceRetentionRequest) -> SourceRetentionDecision: ...


class LicensedSourceRetentionPolicy:
    """Conservative trusted mapping; response strings never create rights."""

    def __init__(self, allowed: dict[str, str] | None = None) -> None:
        self.allowed = dict(
            allowed
            or {
                "CC-BY-4.0": "trusted-license:cc-by-4.0",
                "CC0-1.0": "trusted-license:cc0-1.0",
                "MIT": "trusted-license:mit",
                "Apache-2.0": "trusted-license:apache-2.0",
            }
        )

    def evaluate(self, request: SourceRetentionRequest) -> SourceRetentionDecision:
        storage_rights = self.allowed.get(request.license or "")
        allowed = storage_rights is not None
        return SourceRetentionDecision(
            request_id=request.id,
            decision="allow" if allowed else "deny",
            storage_rights=storage_rights,
            reason="trusted retention mapping" if allowed else "missing retention rights",
            policy_version="licensed-source-retention/2",
        )


class SourceExtractionConfig(StrictModel):
    question: str = Field(min_length=1)
    provider: ProviderConfig
    max_output_tokens: int = Field(ge=1, le=524_288)
    model_parameters: ModelParameters
    allowed_concept_ids: tuple[str, ...] = ()
    debug_reasoning: bool = True
    allow_partial_items: bool = False

    @model_validator(mode="after")
    def output_fits_provider(self) -> SourceExtractionConfig:
        if self.max_output_tokens > self.provider.max_output_tokens:
            raise ValueError("extraction output limit exceeds trusted provider capacity")
        return self


class SourceExtractionAdapter(Protocol):
    validator_version: str
    external: bool

    def propose(
        self,
        *,
        source_version_id: str,
        structural_derivation_id: str,
        anchor_ids: tuple[str, ...],
    ) -> object: ...


class AnchorGroundedSourceExtractionAdapter:
    """Typed bridge to the real proposal-only extraction manager."""

    def __init__(
        self,
        manager: AnchorGroundedExtractionManager,
        config: SourceExtractionConfig,
        *,
        provider_registry: Mapping[str, ProviderConfig],
    ) -> None:
        trusted_provider = provider_registry.get(manager.provider)
        if trusted_provider is None:
            raise ValueError("extraction provider is absent from trusted registry")
        if config.provider != trusted_provider:
            raise ValueError("extraction config differs from trusted provider registry")
        self.manager = manager
        self.config = config
        self._provider_name = manager.provider
        self._provider = trusted_provider
        self._client = manager.client
        self._gate = getattr(manager.client, "gate", None)
        self.validator_version = manager.version
        self._validate_current_client()

    @property
    def external(self) -> bool:
        self._validate_current_client()
        return self._provider.external

    def _validate_current_client(self) -> None:
        from research_agent.providers import ModelClient

        if not isinstance(self.manager.client, ModelClient):
            raise ValueError("extraction requires the trusted ModelClient implementation")
        if self.manager.client is not self._client:
            raise ValueError("extraction manager client identity changed")
        if self.manager.provider != self._provider_name:
            raise ValueError("extraction manager differs from trusted provider name")
        if self.manager.model != self._provider.model:
            raise ValueError("extraction manager model differs from trusted provider config")
        if self.manager.client.name != self._provider_name:
            raise ValueError("extraction client differs from trusted provider name")
        if self.manager.client.config != self._provider:
            raise ValueError("extraction client differs from trusted provider configuration")
        if self.manager.client.parameters != self.config.model_parameters:
            raise ValueError("extraction client parameters differ from trusted configuration")
        if self.manager.client.gate is not self._gate:
            raise ValueError("extraction model-policy and budget gate identity changed")
        if self._provider.external and self._gate is None:
            raise ValueError("external extraction requires model-policy and budget gate")

    def propose(
        self,
        *,
        source_version_id: str,
        structural_derivation_id: str,
        anchor_ids: tuple[str, ...],
    ) -> ExtractionProposalReceipt:
        self._validate_current_client()
        receipt = self.manager.propose(
            question=self.config.question,
            structural_derivation_id=structural_derivation_id,
            anchor_ids=anchor_ids,
            allowed_concept_ids=self.config.allowed_concept_ids,
            max_output_tokens=self.config.max_output_tokens,
            model_parameters=self.config.model_parameters,
            debug_reasoning=self.config.debug_reasoning,
            allow_partial_items=self.config.allow_partial_items,
        )
        if (
            receipt.request.source_version_id != source_version_id
            or receipt.proposal.source_version_id != source_version_id
            or receipt.proposal.review_state != "proposed"
            or receipt.proposal.commit_authority != "none_proposal_only"
            or receipt.proposal.validator_version != self.validator_version
        ):
            raise ValueError("extraction manager violated the proposal-only source contract")
        return receipt


class FetchedSourceProvider(Protocol):
    def payload(
        self,
        candidate: SourceCandidate,
        checkpoint: SourceCheckpoint,
    ) -> FetchedSourcePayload: ...


class SourceWorkLimits(StrictModel):
    max_requests_per_run: int = Field(default=50, ge=1, le=10_000)
    max_bytes_per_run: int = Field(default=100_000_000, ge=1)
    max_depth: int = Field(default=1, ge=0, le=16)
    refresh_interval_seconds: int = Field(default=3600, ge=1, le=31_536_000)
    max_run_seconds: float = Field(default=1800.0, gt=0)
    finalization_reserve_seconds: float = Field(default=120.0, ge=0)

    @model_validator(mode="after")
    def reserve_fits_run(self) -> SourceWorkLimits:
        if self.finalization_reserve_seconds >= self.max_run_seconds:
            raise ValueError("source finalization reserve must be less than run limit")
        return self


class SourceAuthorityContext(StrictModel):
    """Trusted Git authority fields that retrieved data cannot replace."""

    authority_repository: str
    target_repository: str
    ref: str
    path: str


class SourceRefreshObservation(StrictModel):
    id: str
    source_intent_id: str
    locator: str
    status: Literal["not_modified", "access_constrained"]
    prior_source_version_id: str | None = None
    observed_at: datetime
    recorded_at: datetime


class SourceTemporalObservation(StrictModel):
    id: str
    source_intent_id: str
    locator: str
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime | None = None
    observed_at: datetime
    valid_at: datetime | None = None
    recorded_at: datetime
    predecessor_observation_id: str | None = None


class SourceSupersession(StrictModel):
    id: str
    source_intent_id: str
    predecessor_observation_id: str
    successor_observation_id: str
    recorded_at: datetime


class SourceWorkInterruption(RuntimeError):
    """A caller-injected interruption used to verify durable resumption."""


class SourceOperationError(RuntimeError):
    """A failed adapter operation with the number of actual requests attempted."""

    def __init__(self, message: str, *, request_count: int) -> None:
        super().__init__(message)
        if request_count < 0:
            raise ValueError("failed operation request count cannot be negative")
        self.request_count = request_count


def _digest_from_id(value: str, *, label: str) -> str:
    prefix = f"{label}:sha256:"
    if not value.startswith(prefix):
        raise ValueError(f"invalid {label} identity")
    return _digest(value.removeprefix(prefix), label=label)


class ImmutableSourceWorkStore:
    """Immutable work history with one atomic, rebuildable current-work index."""

    index_version = 1

    def __init__(self, store: ImmutableStore) -> None:
        self.store = store
        self.index_path = store.root / "source-work-current.json"
        self.lock_path = store.root / "source-work-current.lock"

    def get(self, work_item_id: str) -> SourceWorkItem | None:
        _digest_from_id(work_item_id, label="source-work")
        values = [
            item
            for value in self.store.iter_records("source-work")
            if (item := SourceWorkItem.model_validate(value)).id == work_item_id
        ]
        if len(values) > 1:
            raise ValueError("ambiguous immutable source-work identity")
        return values[0] if values else None

    def current(self, lineage_id: str) -> SourceWorkItem | None:
        tip = self._immutable_tip(lineage_id)
        indexed = self._read_index().get(lineage_id)
        if tip is None:
            if indexed is not None:
                raise ValueError("source-work current index does not match immutable history")
            return None
        if indexed != tip.id:
            index = self._read_index()
            index[lineage_id] = tip.id
            self._write_index(index)
        return tip

    def put(self, item: SourceWorkItem) -> SourceWorkItem:
        self.store.initialize()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self._read_index()
            current = self._immutable_tip(item.lineage_id)
            if item.phase is SourceWorkPhase.CANDIDATE:
                if current is None and item.predecessor_id is not None:
                    raise ValueError("candidate predecessor is missing from immutable lineage")
                if current is not None and current.phase is not SourceWorkPhase.FINALIZED:
                    if current.id == item.id:
                        return current
                    raise ValueError("candidate cannot replace unfinished source work")
                if current is not None and (
                    item.predecessor_id != current.id
                    or item.predecessor_phase is not SourceWorkPhase.FINALIZED
                ):
                    raise ValueError("candidate refresh must name immutable finalized tip")
            else:
                if current is None or item.predecessor_id != current.id:
                    raise ValueError("source work must name the current predecessor")
                predecessor = self.get(item.predecessor_id)
                if predecessor is None:
                    raise ValueError("source-work predecessor is missing")
                if item.predecessor_phase is not predecessor.phase:
                    raise ValueError("immutable predecessor phase does not match")
                if predecessor.phase not in _PREDECESSORS[item.phase]:
                    raise ValueError("immutable predecessor phase is illegal")
            self.store.put_record("source-work", item)
            index[item.lineage_id] = item.id
            self._write_index(index)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return item

    def _immutable_tip(self, lineage_id: str) -> SourceWorkItem | None:
        items = tuple(
            SourceWorkItem.model_validate(value)
            for value in self.store.iter_records("source-work")
            if SourceWorkItem.model_validate(value).lineage_id == lineage_id
        )
        if not items:
            return None
        by_id = {item.id: item for item in items}
        if len(by_id) != len(items):
            raise ValueError("ambiguous immutable source-work identity")
        referenced: set[str] = set()
        for item in items:
            if item.phase is SourceWorkPhase.FETCHED and (
                item.result_record_sha256 is None
                or not any(
                    hashlib.sha256(canonical_json(value)).hexdigest() == item.result_record_sha256
                    for value in self.store.iter_records("source-fetch-payload")
                )
            ):
                raise ValueError("fetched work has no durable authority record")
            if item.predecessor_id is None:
                continue
            predecessor = by_id.get(item.predecessor_id)
            if predecessor is None:
                raise ValueError("source-work predecessor is missing from lineage")
            if item.predecessor_phase is not predecessor.phase:
                raise ValueError("immutable predecessor phase does not match")
            if predecessor.phase not in _PREDECESSORS[item.phase]:
                raise ValueError("immutable predecessor phase is illegal")
            referenced.add(predecessor.id)
        tips = tuple(item for item in items if item.id not in referenced)
        if len(tips) != 1:
            raise ValueError("ambiguous immutable source-work tips")
        return tips[0]

    def checkpoint(self, checkpoint: SourceCheckpoint) -> SourceCheckpoint:
        item = self.get(checkpoint.work_item_id)
        if item is None or item.phase is not checkpoint.phase:
            raise ValueError("checkpoint must match an immutable source-work item")
        self.store.put_record("source-checkpoint", checkpoint)
        return checkpoint

    def latest_checkpoint(
        self,
        *,
        source_intent_id: str,
        locator: str,
    ) -> SourceCheckpoint | None:
        item_ids = {
            item.id
            for item in (
                SourceWorkItem.model_validate(value)
                for value in self.store.iter_records("source-work")
            )
            if item.source_intent_id == source_intent_id and item.locator == locator
        }
        values = [
            SourceCheckpoint.model_validate(value)
            for value in self.store.iter_records("source-checkpoint")
            if value.get("work_item_id") in item_ids
        ]
        return max(
            values,
            key=lambda item: (
                item.recorded_at,
                item.prior_source_version_id is not None,
                item.id,
            ),
            default=None,
        )

    def chain(self, item: SourceWorkItem) -> tuple[SourceWorkItem, ...]:
        chain = [item]
        while chain[-1].predecessor_id is not None:
            predecessor = self.get(chain[-1].predecessor_id)
            if predecessor is None:
                raise ValueError("source-work predecessor is missing")
            chain.append(predecessor)
        return tuple(reversed(chain))

    def current_items(self) -> tuple[SourceWorkItem, ...]:
        values = tuple(self.current(lineage_id) for lineage_id in sorted(self._read_index()))
        return tuple(item for item in values if item is not None)

    def _read_index(self) -> dict[str, str]:
        if not self.index_path.exists():
            return {}
        if self.index_path.is_symlink():
            raise ValueError("source-work current index cannot be a symlink")
        value = json.loads(self.index_path.read_bytes())
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError("source-work current index is invalid")
        return value

    def _write_index(self, index: dict[str, str]) -> None:
        temporary = self.index_path.with_name(f".source-work-current.{os.getpid()}.tmp")
        rendered = json.dumps(index, indent=2, sort_keys=True).encode() + b"\n"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, rendered)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.index_path)
        directory = os.open(self.index_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class CapabilityRequestFactory(Protocol):
    def __call__(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        capabilities: tuple[Capability, ...],
        now: datetime,
    ) -> CapabilityRequest: ...


class SourceWorkCoordinator:
    """Run bounded source work without granting declarations or model output authority."""

    def __init__(
        self,
        *,
        store: ImmutableStore,
        work_store: ImmutableSourceWorkStore,
        adapter: SourceAdapter,
        capability_evaluator: CapabilityEvaluator,
        capability_request: CapabilityRequestFactory,
        authority: SourceAuthorityContext,
        ontology_bundle_sha256: str,
        parser: ParsedDocumentManager | None = None,
        library_manifest: SourceLibraryManifest | None = None,
        library_database: Path | None = None,
        extraction: SourceExtractionAdapter | None = None,
        retention_policy: SourceRetentionPolicy | None = None,
        source_policy: PolicyEngine | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] | None = None,
        limits: SourceWorkLimits | None = None,
        after_phase: Callable[[SourceWorkPhase], None] | None = None,
    ) -> None:
        from research_agent.parsing import ParsedDocumentManager

        self.store = store
        self.work_store = work_store
        self.adapter = adapter
        self.capability_evaluator = capability_evaluator
        self.capability_request = capability_request
        self.authority = authority
        self.ontology_bundle_sha256 = _digest(
            ontology_bundle_sha256, label="ontology_bundle_sha256"
        )
        self.parser = parser or ParsedDocumentManager(store=store, clock=clock)
        self.library_manifest = library_manifest
        self.library_database = library_database
        self.extraction = extraction
        self.retention_policy = retention_policy or LicensedSourceRetentionPolicy()
        self.source_policy = source_policy or PolicyEngine()
        self.clock = clock
        self.monotonic = monotonic or time.monotonic
        self.limits = limits or SourceWorkLimits()
        self.after_phase = after_phase
        self._request_count = 0
        self._byte_count = 0
        self._started = 0.0

    def run_due(
        self,
        intents: tuple[SourceIntent, ...],
        *,
        now: datetime | None = None,
    ) -> SourceUpdateReceipt:
        instant = now or self.clock()
        self._started = self.monotonic()
        self._request_count = 0
        self._byte_count = 0
        ordered = tuple(sorted(intents, key=lambda item: (-item.priority, item.id.encode("utf-8"))))
        all_items: list[SourceWorkItem] = []
        source_ids: list[str] = []
        checkpoint_ids: list[str] = []
        constraint_ids: list[str] = []
        proposal_ids: list[str] = []
        complete = True
        processed_ids: list[str] = []
        for intent in ordered:
            if not self._is_due(intent, instant):
                continue
            if self._remaining() <= self.limits.finalization_reserve_seconds:
                complete = False
                break
            processed_ids.append(intent.id)
            try:
                result = self._run_intent(intent, instant)
            except Exception:
                if intent.required:
                    raise
                complete = False
                continue
            all_items.extend(result[0])
            source_ids.extend(result[1])
            checkpoint_ids.extend(result[2])
            constraint_ids.extend(result[3])
            proposal_ids.extend(result[4])
            complete = complete and (result[5] or not intent.required)
        phases = tuple(item.phase for item in all_items)
        semantic_outcomes = tuple(
            SourceWorkOutcome(str(outcome))
            for item in all_items
            if item.phase is SourceWorkPhase.FINALIZED
            and (outcome := self._result(item).get("semantic_outcome")) is not None
        )
        if not complete:
            semantic_outcomes = (*semantic_outcomes, SourceWorkOutcome.INCOMPLETE)
        receipt = SourceUpdateReceipt(
            source_intent_id=(
                processed_ids[0] if len(processed_ids) == 1 else "source-update-batch"
            ),
            source_intent_ids=tuple(processed_ids),
            work_item_ids=tuple(item.id for item in all_items),
            completed_phases=phases,
            source_version_ids=tuple(source_ids),
            checkpoint_ids=tuple(checkpoint_ids),
            constraint_ids=tuple(constraint_ids),
            proposal_ids=tuple(proposal_ids),
            semantic_outcomes=semantic_outcomes,
            complete=complete,
            finalized_at=instant,
            recovery_command=None if complete else "Run the same ontology-update command again",
        )
        self.store.put_record("source-update-receipt", receipt)
        return receipt

    def _run_intent(
        self,
        intent: SourceIntent,
        now: datetime,
    ) -> tuple[
        tuple[SourceWorkItem, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        bool,
    ]:
        effective_depth = min(intent.refresh.max_depth, self.limits.max_depth)
        effective_intent = intent.model_copy(
            update={"refresh": intent.refresh.model_copy(update={"max_depth": effective_depth})}
        )
        from research_agent.source_intent import DiscoveryKind, SourceCandidate, authorize_candidate

        select_adapter = getattr(self.adapter, "select", None)
        if select_adapter is not None:
            select_adapter(effective_intent)
        discovery_bound = int(
            getattr(
                self.adapter,
                "max_discovery_requests",
                0 if effective_intent.discovery.kind is DiscoveryKind.DIRECT_URL else 1,
            )
        )
        if self._request_count + discovery_bound > self.limits.max_requests_per_run:
            return (), (), (), (), (), False
        if effective_intent.discovery.kind is not DiscoveryKind.DIRECT_URL:
            discovery_target = SourceCandidate(
                intent_id=intent.id,
                locator=effective_intent.discovery.locator,
                discovered_at=now,
            )
            discovery = self._decision(intent, discovery_target, (Capability.SOURCE_DISCOVER,), now)
            if discovery.decision != "allow":
                raise PermissionError("source discovery capability denied")

        try:
            discovered = self.adapter.discover(effective_intent)
        except Exception as error:
            attempted = int(getattr(error, "request_count", 0))
            if attempted < 0 or attempted > discovery_bound:
                raise ValueError("invalid failed discovery request receipt") from error
            self._request_count += attempted
            raise
        discovery_count = int(getattr(self.adapter, "last_discovery_request_count", 0))
        if discovery_count > discovery_bound:
            raise ValueError("source adapter exceeded its discovery request bound")
        self._request_count += discovery_count
        authorized = tuple(authorize_candidate(item, intent) for item in discovered)
        by_locator: dict[str, SourceCandidate] = {}
        for candidate in authorized:
            current = by_locator.get(candidate.locator)
            if current is None or candidate.id < current.id:
                by_locator[candidate.locator] = candidate
        candidates = tuple(
            by_locator[locator]
            for locator in sorted(by_locator, key=lambda item: item.encode("utf-8"))
        )
        items: list[SourceWorkItem] = []
        source_ids: list[str] = []
        checkpoint_ids: list[str] = []
        constraint_ids: list[str] = []
        proposal_ids: list[str] = []
        complete = True
        for candidate in candidates[: intent.refresh.max_items]:
            if self._remaining() <= self.limits.finalization_reserve_seconds:
                complete = False
                break
            if self._request_count >= self.limits.max_requests_per_run:
                complete = False
                break
            chain, source_id, checks, constraints, proposals, item_complete = self._run_candidate(
                intent, candidate, now
            )
            items.extend(chain)
            if source_id is not None:
                source_ids.append(source_id)
            checkpoint_ids.extend(checks)
            constraint_ids.extend(constraints)
            proposal_ids.extend(proposals)
            complete = complete and item_complete
        return (
            tuple(items),
            tuple(source_ids),
            tuple(checkpoint_ids),
            tuple(constraint_ids),
            tuple(proposal_ids),
            complete,
        )

    def _run_candidate(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        now: datetime,
    ) -> tuple[
        tuple[SourceWorkItem, ...],
        str | None,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        bool,
    ]:
        fetch_decision = self._decision(intent, candidate, (Capability.SOURCE_FETCH,), now)
        base = SourceWorkItem(
            ontology_bundle_sha256=self.ontology_bundle_sha256,
            source_intent_id=intent.id,
            source_intent_sha256=intent.canonical_id.rsplit(":", 1)[-1],
            locator=candidate.locator,
            adapter_id=self.adapter.adapter_id,
            adapter_version=self.adapter.version,
            parser_id="document-parser-registry",
            parser_version=self.parser.registry.version,
            extraction_validator_version=getattr(
                self.extraction,
                "validator_version",
                "anchor-grounded-extraction-validator/3",
            ),
            capability_decision_sha256=self._authorization_fingerprint(fetch_decision),
            phase=SourceWorkPhase.CANDIDATE,
            predecessor_id=None,
            predecessor_phase=None,
            created_at=candidate.discovered_at,
        )
        current = self.work_store.current(base.lineage_id)
        if current is not None and current.phase is SourceWorkPhase.FINALIZED:
            if current.created_at >= now:
                outcome = self._result(current).get("semantic_outcome")
                return (
                    self.work_store.chain(current),
                    None,
                    (),
                    (),
                    (),
                    outcome == SourceWorkOutcome.SUCCESSFUL,
                )
            base = base.model_copy(
                update={
                    "predecessor_id": current.id,
                    "predecessor_phase": current.phase,
                    "created_at": now,
                }
            )
            base = SourceWorkItem.model_validate(base.model_dump())
        if current is None or current.phase is SourceWorkPhase.FINALIZED:
            current = self._record(base)
        checkpoint_ids: list[str] = []
        constraint_ids: list[str] = []
        proposal_ids: list[str] = []
        candidate_complete = True
        source_id = self._result(current).get("source_version_id")
        if current.phase is SourceWorkPhase.CANDIDATE:
            current = self._advance(current, SourceWorkPhase.AUTHORIZED, now)
        if fetch_decision.decision != "allow" and current.phase in {
            SourceWorkPhase.CANDIDATE,
            SourceWorkPhase.AUTHORIZED,
            SourceWorkPhase.FETCHED,
        }:
            constraint_ids.append(self._constraint(intent, candidate, "source.fetch denied", now))
            current = self._advance(current, SourceWorkPhase.ACCESS_CONSTRAINED, now)
        if current.phase is SourceWorkPhase.AUTHORIZED:
            archive = self._decision(intent, candidate, (Capability.SOURCE_ARCHIVE,), now)
            if archive.decision != "allow":
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.archive denied", now)
                )
                current = self._advance(current, SourceWorkPhase.ACCESS_CONSTRAINED, now)
        if current.phase is SourceWorkPhase.AUTHORIZED:
            prior = self.work_store.latest_checkpoint(
                source_intent_id=intent.id,
                locator=candidate.locator,
            )
            fetch_bound = int(getattr(self.adapter, "max_fetch_requests", 1))
            if self._request_count + fetch_bound > self.limits.max_requests_per_run:
                return self.work_store.chain(current), None, (), (), (), False
            try:
                fetched = self.adapter.fetch(candidate, prior=prior)
            except Exception as error:
                attempted = int(getattr(error, "request_count", 0))
                if attempted < 0 or attempted > fetch_bound:
                    raise ValueError("invalid failed fetch request receipt") from error
                self._request_count += attempted
                raise
            if fetched.request_count > fetch_bound:
                raise ValueError("source adapter exceeded its fetch request bound")
            self._request_count += fetched.request_count
            if fetched.phase not in {
                SourceWorkPhase.FETCHED,
                SourceWorkPhase.NOT_MODIFIED,
                SourceWorkPhase.ACCESS_CONSTRAINED,
            }:
                raise ValueError("source adapter returned an invalid fetch phase")
            if fetched.phase is SourceWorkPhase.NOT_MODIFIED and (
                prior is None
                or prior.prior_source_version_id is None
                or prior.prior_source_record_sha256 is None
                or not self._source_record_exists(
                    prior.prior_source_version_id,
                    prior.prior_source_record_sha256,
                )
            ):
                constraint_ids.append(
                    self._constraint(
                        intent,
                        candidate,
                        "304 prior source is unavailable",
                        now,
                        constraint_type="invalid_prior_source",
                    )
                )
                fetched = SourceCheckpoint(
                    work_item_id=candidate.id,
                    phase=SourceWorkPhase.ACCESS_CONSTRAINED,
                    constraint="invalid_prior_source",
                    request_count=fetched.request_count,
                    recorded_at=fetched.recorded_at,
                )
            fetched_values: dict[str, object] = {}
            fetched_record_sha256: str | None = None
            if fetched.phase is SourceWorkPhase.FETCHED:
                payload = self._adapter_payload(candidate, fetched)
                actual = hashlib.sha256(payload.content).hexdigest()
                if fetched.result_sha256 is not None and fetched.result_sha256 != actual:
                    raise ValueError("retained fetched bytes do not match the fetch checkpoint")
                if self._byte_count + len(payload.content) > self.limits.max_bytes_per_run:
                    constraint_ids.append(
                        self._constraint(intent, candidate, "source byte limit reached", now)
                    )
                    current = self._advance(
                        current, SourceWorkPhase.ACCESS_CONSTRAINED, fetched.recorded_at
                    )
                    durable_checkpoint = SourceCheckpoint(
                        work_item_id=current.id,
                        phase=SourceWorkPhase.ACCESS_CONSTRAINED,
                        constraint="byte_limit",
                        recorded_at=fetched.recorded_at,
                        request_count=fetched.request_count,
                    )
                    checkpoint_ids.append(self.work_store.checkpoint(durable_checkpoint).id)
                    fetched_values = {}
                else:
                    self._byte_count += len(payload.content)
                retention_request = SourceRetentionRequest(
                    source_intent_id=intent.id,
                    source_intent_sha256=intent.canonical_id.rsplit(":", 1)[-1],
                    locator=candidate.locator,
                    source_uri=payload.source_uri,
                    media_type=payload.media_type,
                    connector_id=payload.connector_id,
                    license=payload.license,
                    content_sha256=actual,
                    content_length=len(payload.content),
                )
                retention = self.retention_policy.evaluate(retention_request)
                if retention.request_id != retention_request.id:
                    raise ValueError("retention policy returned a decision for another request")
                self.store.put_record("source-retention-decision", retention)
                if current.phase is SourceWorkPhase.AUTHORIZED and retention.decision != "allow":
                    constraint_ids.append(
                        self._constraint(
                            intent,
                            candidate,
                            retention.reason,
                            now,
                            constraint_type="retention_denied",
                        )
                    )
                    current = self._advance(
                        current, SourceWorkPhase.ACCESS_CONSTRAINED, fetched.recorded_at
                    )
                    durable_checkpoint = SourceCheckpoint(
                        work_item_id=current.id,
                        phase=SourceWorkPhase.ACCESS_CONSTRAINED,
                        constraint="retention_denied",
                        recorded_at=fetched.recorded_at,
                        request_count=fetched.request_count,
                    )
                    checkpoint_ids.append(self.work_store.checkpoint(durable_checkpoint).id)
                elif current.phase is SourceWorkPhase.AUTHORIZED:
                    self.store.put_blob(payload.content)
                    fetched_values = {
                        "fetched_content_sha256": actual,
                        "fetched_source_uri": payload.source_uri,
                        "fetched_media_type": payload.media_type,
                        "fetched_connector_id": payload.connector_id,
                        "fetched_license": payload.license,
                        "fetched_observed_at": payload.observed_at.isoformat(),
                        "fetched_published_at": (
                            payload.published_at.isoformat() if payload.published_at else None
                        ),
                        "fetched_valid_at": (
                            payload.valid_at.isoformat() if payload.valid_at else None
                        ),
                    }
                    fetched_record_sha256 = self.store.put_record(
                        "source-fetch-payload", {"version": 1, **fetched_values}
                    )
            if current.phase is SourceWorkPhase.AUTHORIZED:
                not_modified_values = {}
                if fetched.phase is SourceWorkPhase.NOT_MODIFIED:
                    assert prior is not None
                    not_modified_values = {
                        "source_version_id": prior.prior_source_version_id,
                        "source_record_sha256": prior.prior_source_record_sha256,
                    }
                current = self._advance(
                    current,
                    fetched.phase,
                    fetched.recorded_at,
                    result_sha256=fetched.result_sha256,
                    result_values=fetched_values or not_modified_values,
                    result_record_sha256=fetched_record_sha256,
                )
            if (
                current.phase is not SourceWorkPhase.ACCESS_CONSTRAINED
                or fetched.phase is SourceWorkPhase.ACCESS_CONSTRAINED
            ):
                checkpoint_update: dict[str, object] = {"work_item_id": current.id}
                if (
                    fetched.phase is not SourceWorkPhase.FETCHED
                    and prior is not None
                    and prior.prior_source_version_id is not None
                ):
                    checkpoint_update.update(
                        prior_source_version_id=prior.prior_source_version_id,
                        prior_source_record_sha256=prior.prior_source_record_sha256,
                    )
                durable_checkpoint = fetched.model_copy(update=checkpoint_update)
                durable_checkpoint = SourceCheckpoint.model_validate(
                    durable_checkpoint.model_dump()
                )
                checkpoint_ids.append(self.work_store.checkpoint(durable_checkpoint).id)
            if fetched.phase is SourceWorkPhase.NOT_MODIFIED:
                self._refresh_observation(intent, candidate, "not_modified", now)
            elif fetched.phase is SourceWorkPhase.ACCESS_CONSTRAINED:
                constraint_ids.append(
                    self._constraint(
                        intent,
                        candidate,
                        "adapter constrained fetch",
                        now,
                        constraint_type=fetched.constraint,
                    )
                )
        if current.phase in {SourceWorkPhase.NOT_MODIFIED, SourceWorkPhase.ACCESS_CONSTRAINED}:
            constrained = current.phase is SourceWorkPhase.ACCESS_CONSTRAINED
            terminal_source_id = self._result(current).get("source_version_id")
            outcome = (
                SourceWorkOutcome.CONSTRAINED_REQUIRED
                if constrained and intent.required
                else SourceWorkOutcome.CONSTRAINED_OPTIONAL
                if constrained
                else SourceWorkOutcome.SUCCESSFUL
            )
            current, final_checkpoint = self._finalize(current, outcome, now)
            checkpoint_ids.append(final_checkpoint)
            return (
                self.work_store.chain(current),
                str(terminal_source_id) if terminal_source_id is not None else None,
                tuple(checkpoint_ids),
                tuple(constraint_ids),
                (),
                not constrained,
            )
        if current.phase is SourceWorkPhase.FETCHED:
            archive = self._decision(intent, candidate, (Capability.SOURCE_ARCHIVE,), now)
            if archive.decision != "allow":
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.archive revoked", now)
                )
                current = self._advance(current, SourceWorkPhase.ACCESS_CONSTRAINED, now)
                outcome = (
                    SourceWorkOutcome.CONSTRAINED_REQUIRED
                    if intent.required
                    else SourceWorkOutcome.CONSTRAINED_OPTIONAL
                )
                current, final_checkpoint = self._finalize(current, outcome, now)
                checkpoint_ids.append(final_checkpoint)
                return (
                    self.work_store.chain(current),
                    None,
                    tuple(checkpoint_ids),
                    tuple(constraint_ids),
                    (),
                    False,
                )
            payload = self._payload(candidate, current)
            expected = self._checkpoint_result(current)
            actual = hashlib.sha256(payload.content).hexdigest()
            if expected is not None and expected != actual:
                raise ValueError("retained fetched bytes do not match the fetch checkpoint")
            source = self.store.ingest_bytes(
                payload.content,
                source_uri=payload.source_uri,
                media_type=payload.media_type,
                connector_id=payload.connector_id,
                license=payload.license,
                acquired_at=payload.observed_at,
            )
            source_id = source.id
            source_record_sha256 = self.store.put_record("source-version", source)
            self._temporal_observation(intent, candidate, payload, source.id, now)
            current = self._advance(
                current,
                SourceWorkPhase.ARCHIVED,
                now,
                result_values={
                    "source_version_id": source.id,
                    "source_record_sha256": source_record_sha256,
                },
            )
            fetched_checkpoint = self.work_store.latest_checkpoint(
                source_intent_id=intent.id, locator=candidate.locator
            )
            archive_checkpoint = SourceCheckpoint(
                work_item_id=current.id,
                phase=SourceWorkPhase.ARCHIVED,
                result_sha256=hashlib.sha256(payload.content).hexdigest(),
                etag=fetched_checkpoint.etag if fetched_checkpoint else None,
                last_modified=(fetched_checkpoint.last_modified if fetched_checkpoint else None),
                request_count=0,
                prior_source_version_id=source.id,
                prior_source_record_sha256=source_record_sha256,
                recorded_at=now,
            )
            checkpoint_ids.append(self.work_store.checkpoint(archive_checkpoint).id)
        result = self._result(current)
        source_id = str(result.get("source_version_id") or source_id or "") or None
        if current.phase is SourceWorkPhase.ARCHIVED:
            if not self._extract_allowed(intent, candidate, now):
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract denied", now)
                )
                current = self._advance(current, SourceWorkPhase.PARSER_CONSTRAINED, now)
            else:
                if source_id is None:
                    raise ValueError("archived source work has no immutable source identity")
                source_record_sha256 = self._result(current).get("source_record_sha256")
                if not isinstance(source_record_sha256, str):
                    raise ValueError("archived source work has no acquisition identity")
                parsed = self.parser.parse_source(
                    source_id, source_record_sha256=source_record_sha256
                )
                current = self._advance(
                    current,
                    SourceWorkPhase.PARSED,
                    now,
                    result_values=self._parsed_result_values(parsed),
                )
        if current.phase is SourceWorkPhase.PARSER_CONSTRAINED:
            outcome = (
                SourceWorkOutcome.CONSTRAINED_REQUIRED
                if intent.required
                else SourceWorkOutcome.CONSTRAINED_OPTIONAL
            )
            current, final_checkpoint = self._finalize(current, outcome, now)
            checkpoint_ids.append(final_checkpoint)
            return (
                self.work_store.chain(current),
                source_id,
                tuple(checkpoint_ids),
                tuple(constraint_ids),
                (),
                False,
            )
        if current.phase is SourceWorkPhase.PARSED:
            if not self._extract_allowed(intent, candidate, now):
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract revoked", now)
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            else:
                current = self._advance(
                    current,
                    SourceWorkPhase.STRUCTURED,
                    now,
                    result_values=self._result_values(current),
                )
        if current.phase is SourceWorkPhase.STRUCTURED:
            if not self._extract_allowed(intent, candidate, now):
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract revoked", now)
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            else:
                self._rebuild_library(now)
                current = self._advance(
                    current,
                    SourceWorkPhase.INDEXED,
                    now,
                    result_values=self._result_values(current),
                )
        if current.phase is SourceWorkPhase.INDEXED:
            if not self._extract_allowed(intent, candidate, now):
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract revoked", now)
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            elif self._threat_blocks_extraction(current, source_id):
                constraint_ids.append(
                    self._constraint(
                        intent,
                        candidate,
                        "source policy blocks threatened evidence",
                        now,
                        constraint_type="threat_blocked",
                    )
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            else:
                anchor_values = self._result_values(current)
                anchor_values["anchor_ids"] = self._select_anchor_ids(current)
                current = self._advance(
                    current,
                    SourceWorkPhase.ANCHORS_SELECTED,
                    now,
                    result_values=anchor_values,
                )
        if current.phase is SourceWorkPhase.ANCHORS_SELECTED:
            if not self._extract_allowed(intent, candidate, now):
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract revoked", now)
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            elif self._threat_blocks_extraction(current, source_id):
                constraint_ids.append(
                    self._constraint(
                        intent,
                        candidate,
                        "source policy blocks threatened evidence",
                        now,
                        constraint_type="threat_blocked",
                    )
                )
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
                candidate_complete = False
            elif self.extraction is None:
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
            else:
                if self.extraction.external:
                    if self._remaining() <= self.limits.finalization_reserve_seconds:
                        constraint_ids.append(
                            self._constraint(
                                intent,
                                candidate,
                                "source finalization reserve reached",
                                now,
                            )
                        )
                        current = self._advance(
                            current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now
                        )
                        candidate_complete = False
                    else:
                        model = self._decision(intent, candidate, (Capability.MODEL_EXTERNAL,), now)
                        if model.decision != "allow":
                            constraint_ids.append(
                                self._constraint(intent, candidate, "model.external denied", now)
                            )
                            current = self._advance(
                                current,
                                SourceWorkPhase.EXTRACTION_CONSTRAINED,
                                now,
                            )
                            candidate_complete = False
                        elif self._threat_blocks_extraction(current, source_id):
                            constraint_ids.append(
                                self._constraint(
                                    intent,
                                    candidate,
                                    "source policy blocks threatened evidence",
                                    now,
                                    constraint_type="threat_blocked",
                                )
                            )
                            current = self._advance(
                                current,
                                SourceWorkPhase.EXTRACTION_CONSTRAINED,
                                now,
                            )
                            candidate_complete = False
                        else:
                            proposal_ids.extend(self._propose(current))
                            current = self._advance(
                                current, SourceWorkPhase.EXTRACTION_PROPOSED, now
                            )
                else:
                    proposal_ids.extend(self._propose(current))
                    current = self._advance(current, SourceWorkPhase.EXTRACTION_PROPOSED, now)
        if current.phase in {
            SourceWorkPhase.EXTRACTION_PROPOSED,
            SourceWorkPhase.EXTRACTION_CONSTRAINED,
        }:
            outcome = (
                SourceWorkOutcome.SUCCESSFUL
                if current.phase is SourceWorkPhase.EXTRACTION_PROPOSED or candidate_complete
                else SourceWorkOutcome.CONSTRAINED_REQUIRED
                if intent.required
                else SourceWorkOutcome.CONSTRAINED_OPTIONAL
            )
            current, final_checkpoint = self._finalize(current, outcome, now)
            checkpoint_ids.append(final_checkpoint)
        return (
            self.work_store.chain(current),
            source_id,
            tuple(checkpoint_ids),
            tuple(constraint_ids),
            tuple(proposal_ids),
            candidate_complete,
        )

    def _decision(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        capabilities: tuple[Capability, ...],
        now: datetime,
    ) -> CapabilityDecision:
        request = self.capability_request(intent, candidate, capabilities, now)
        expected_host = urlsplit(candidate.locator).hostname
        if (
            request.capabilities != capabilities
            or request.authority_repository != self.authority.authority_repository
            or request.target_repository != self.authority.target_repository
            or request.ref != self.authority.ref
            or request.path != self.authority.path
            or request.connector != self.adapter.adapter_id
            or request.host != expected_host
            or request.target != candidate.locator
            or request.bundle_sha256 != self.ontology_bundle_sha256
            or request.requested_at != now
        ):
            raise ValueError("capability factory did not return the exact capability request")
        decision = self.capability_evaluator.evaluate(request)
        self.store.initialize()
        self.store.put_record("capability-decision", decision)
        if decision.request.id != request.id:
            raise ValueError("capability evaluator returned a decision for another request")
        return decision

    def _extract_allowed(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        now: datetime,
    ) -> bool:
        decision = self._decision(intent, candidate, (Capability.SOURCE_EXTRACT,), now)
        return decision.decision == "allow"

    def _threat_blocks_extraction(
        self,
        item: SourceWorkItem,
        source_id: str | None,
    ) -> bool:
        result = self._result(item)
        source_versions = {
            value
            for value in (
                source_id,
                result.get("source_version_id"),
                result.get("derived_source_version_id"),
            )
            if isinstance(value, str) and value
        }
        if not source_versions:
            raise ValueError("threat policy requires an immutable source identity")
        records = tuple(self.store.iter_records("threat-observation"))
        blocked_status = False
        blocked_action = False
        for target_id in sorted(source_versions):
            persisted = tuple(
                ThreatObservation.model_validate(value)
                for value in records
                if value.get("target", {}).get("source_version") == target_id
            )
            current_targets = self._current_threat_tips(persisted) or (
                (ThreatTarget(source_version=target_id), ()),
            )
            for target, observations in current_targets:
                decision = self.source_policy.evaluate(
                    target=target,
                    workflow_id=item.lineage_id,
                    stage=PolicyStage.EXTRACTION,
                    observations=observations,
                )
                self.store.put_record("policy-decision", decision)
                blocked_status = blocked_status or any(
                    observation.status in {ThreatStatus.SUSPECTED, ThreatStatus.CONFIRMED}
                    for observation in observations
                )
                blocked_action = blocked_action or decision.action in {
                    PolicyAction.DENY,
                    PolicyAction.QUARANTINE,
                    PolicyAction.ALLOW_METADATA_ONLY,
                }
        return blocked_status or blocked_action

    @staticmethod
    def _threat_target_key(target: ThreatTarget) -> bytes:
        """Canonical identity over every current and future ThreatTarget field."""
        return json.dumps(
            target.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @classmethod
    def _current_threat_tips(
        cls,
        observations: tuple[ThreatObservation, ...],
    ) -> tuple[tuple[ThreatTarget, tuple[ThreatObservation, ...]], ...]:
        grouped: dict[bytes, list[ThreatObservation]] = {}
        for observation in observations:
            grouped.setdefault(cls._threat_target_key(observation.target), []).append(observation)

        current: list[tuple[ThreatTarget, tuple[ThreatObservation, ...]]] = []
        for target_key in sorted(grouped):
            target_observations = grouped[target_key]
            target_ids = {observation.id for observation in target_observations}
            superseded_ids = {
                observation.supersedes
                for observation in target_observations
                if observation.supersedes in target_ids
            }
            tips = tuple(
                sorted(
                    (
                        observation
                        for observation in target_observations
                        if observation.id not in superseded_ids
                    ),
                    key=lambda observation: observation.id,
                )
            )
            current.append((target_observations[0].target, tips))
        return tuple(current)

    @staticmethod
    def _authorization_fingerprint(decision: CapabilityDecision) -> str:
        """Stable authorization identity; observation timestamps are not authority."""
        request = decision.request.model_dump(mode="json", exclude={"requested_at"})
        value = {
            "request": request,
            "decision": decision.decision,
            "effective_capabilities": decision.effective_capabilities,
            "grant_ids": decision.grant_ids,
            "delegation_chain": decision.delegation_chain,
            "evaluator_version": decision.evaluator_version,
        }
        return hashlib.sha256(canonical_json(value)).hexdigest()

    def _record(
        self,
        item: SourceWorkItem,
        *,
        result_values: dict[str, object] | None = None,
    ) -> SourceWorkItem:
        recorded = self.work_store.put(item)
        if result_values:
            self._store_result(recorded, **result_values)
        self._after(recorded.phase)
        return recorded

    def _advance(
        self,
        predecessor: SourceWorkItem,
        phase: SourceWorkPhase,
        created_at: datetime,
        *,
        result_sha256: str | None = None,
        result_values: dict[str, object] | None = None,
        result_record_sha256: str | None = None,
    ) -> SourceWorkItem:
        item = predecessor.model_copy(
            update={
                "phase": phase,
                "predecessor_id": predecessor.id,
                "predecessor_phase": predecessor.phase,
                "result_record_sha256": result_record_sha256,
                "created_at": created_at,
            }
        )
        item = SourceWorkItem.model_validate(item.model_dump())
        values = dict(result_values or {})
        if result_sha256 is not None:
            values["checkpoint_result_sha256"] = result_sha256
        recorded = self._record(item, result_values=values)
        return recorded

    def _after(self, phase: SourceWorkPhase) -> None:
        if self.after_phase is not None:
            self.after_phase(phase)

    def _finalize(
        self,
        predecessor: SourceWorkItem,
        outcome: SourceWorkOutcome,
        now: datetime,
    ) -> tuple[SourceWorkItem, str]:
        finalized = self._advance(
            predecessor,
            SourceWorkPhase.FINALIZED,
            now,
            result_values={"semantic_outcome": outcome.value},
        )
        checkpoint = SourceCheckpoint(
            work_item_id=finalized.id,
            phase=SourceWorkPhase.FINALIZED,
            request_count=0,
            semantic_outcome=outcome,
            recorded_at=now,
        )
        return finalized, self.work_store.checkpoint(checkpoint).id

    def _adapter_payload(
        self,
        candidate: SourceCandidate,
        checkpoint: SourceCheckpoint,
    ) -> FetchedSourcePayload:
        provider = self.adapter
        payload_method = getattr(provider, "payload", None)
        if callable(payload_method):
            return payload_method(candidate, checkpoint)
        last_fetch = getattr(provider, "last_fetch", {})
        result = last_fetch.get(candidate.id) if isinstance(last_fetch, dict) else None
        if result is not None:
            if result.requested_url != candidate.locator:
                raise ValueError("source fetch requested URL differs from the authorized candidate")
            expected_final_url = (
                result.redirect_chain[-1] if result.redirect_chain else result.requested_url
            )
            if result.final_url != expected_final_url:
                raise ValueError("source fetch final URL differs from its exact redirect chain")
            if result.media_type is None:
                raise ValueError("fetched source result omitted its media type")
            return FetchedSourcePayload(
                content=result.content,
                source_uri=result.final_url,
                media_type=result.media_type,
                connector_id=self.adapter.adapter_id,
                license=None,
                observed_at=checkpoint.recorded_at,
            )
        last_acquired = getattr(provider, "last_acquired", {})
        acquired = last_acquired.get(candidate.id) if isinstance(last_acquired, dict) else None
        if acquired is not None:
            content = self.store.read_blob(acquired.snapshot.source_content_sha256)
            return FetchedSourcePayload(
                content=content,
                source_uri=acquired.snapshot.canonical_locator,
                media_type="text/plain",
                connector_id=acquired.snapshot.connector_id,
                license=acquired.snapshot.license,
                observed_at=acquired.snapshot.observed_at,
            )
        raise ValueError("source adapter did not retain fetched bytes for archival")

    def _payload(
        self,
        candidate: SourceCandidate,
        current: SourceWorkItem,
    ) -> FetchedSourcePayload:
        del candidate
        result = self._result(current)
        if not result and current.result_record_sha256 is not None:
            matches = [
                value
                for value in self.store.iter_records("source-fetch-payload")
                if hashlib.sha256(canonical_json(value)).hexdigest() == current.result_record_sha256
            ]
            if len(matches) != 1:
                raise ValueError("fetched payload authority record is missing or ambiguous")
            result = matches[0]
        digest = result.get("fetched_content_sha256")
        if not isinstance(digest, str):
            raise ValueError("fetched source work has no durable payload")
        return FetchedSourcePayload(
            content=self.store.read_blob(digest),
            source_uri=str(result["fetched_source_uri"]),
            media_type=str(result["fetched_media_type"]),
            connector_id=str(result["fetched_connector_id"]),
            license=(str(result["fetched_license"]) if result.get("fetched_license") else None),
            observed_at=datetime.fromisoformat(str(result["fetched_observed_at"])),
            published_at=(
                datetime.fromisoformat(str(result["fetched_published_at"]))
                if result.get("fetched_published_at")
                else None
            ),
            valid_at=(
                datetime.fromisoformat(str(result["fetched_valid_at"]))
                if result.get("fetched_valid_at")
                else None
            ),
        )

    def _store_result(self, item: SourceWorkItem, **values: object) -> None:
        self.store.put_record(
            "source-work-result",
            {"version": 1, "work_item_id": item.id, **values},
        )

    def _result(self, item: SourceWorkItem) -> dict[str, object]:
        values = [
            value
            for value in self.store.iter_records("source-work-result")
            if value.get("work_item_id") == item.id
        ]
        if len(values) > 1:
            raise ValueError("ambiguous source-work result")
        return values[0] if values else {}

    def _result_values(self, item: SourceWorkItem) -> dict[str, object]:
        return {
            key: value
            for key, value in self._result(item).items()
            if key not in {"version", "work_item_id"}
        }

    def _parsed_result_values(
        self,
        parsed: ParsedIngestReceipt,
    ) -> dict[str, object]:
        return {
            "source_version_id": parsed.original_source_version_id,
            "derived_source_version_id": parsed.derived_source_version_id,
            "structural_derivation_id": parsed.structural_derivation_id,
            "threat_observation_ids": parsed.threat_observation_ids,
        }

    def _select_anchor_ids(self, item: SourceWorkItem) -> tuple[str, ...]:
        derivation_id = self._result(item).get("structural_derivation_id")
        if not isinstance(derivation_id, str):
            raise ValueError("anchor selection requires an exact structural derivation")
        eligible = {"heading", "paragraph", "list_item", "footnote", "caption"}
        return tuple(
            sorted(
                value["id"]
                for value in self.store.iter_records("structural-anchor")
                if value.get("structural_derivation_id") == derivation_id
                and value.get("kind") in eligible
            )
        )

    def _checkpoint_result(self, item: SourceWorkItem) -> str | None:
        value = self._result(item).get("checkpoint_result_sha256")
        return str(value) if value is not None else None

    def _constraint(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        reason: str,
        now: datetime,
        *,
        constraint_type: str | None = None,
    ) -> str:
        fields = {
            "version": 1,
            "source_intent_id": intent.id,
            "locator": candidate.locator,
            "reason": reason,
            "constraint_type": constraint_type,
            "recorded_at": now,
        }
        identity = content_id("source-work-constraint", fields)
        digest = self.store.put_record("source-work-constraint", {"id": identity, **fields})
        self._refresh_observation(intent, candidate, "access_constrained", now)
        return f"source-work-constraint:sha256:{digest}"

    def _refresh_observation(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        status: Literal["not_modified", "access_constrained"],
        now: datetime,
    ) -> None:
        prior = self._latest_temporal(intent.id, candidate.locator)
        fields = {
            "source_intent_id": intent.id,
            "locator": candidate.locator,
            "status": status,
            "prior_source_version_id": (prior.source_version_id if prior is not None else None),
            "observed_at": now,
            "recorded_at": now,
        }
        observation = SourceRefreshObservation(
            id=content_id("source-refresh-observation", fields),
            **fields,
        )
        self.store.put_record("source-refresh-observation", observation)

    def _temporal_observation(
        self,
        intent: SourceIntent,
        candidate: SourceCandidate,
        payload: FetchedSourcePayload,
        source_version_id: str,
        now: datetime,
    ) -> None:
        prior = self._latest_temporal(intent.id, candidate.locator)
        fields = {
            "source_intent_id": intent.id,
            "locator": candidate.locator,
            "source_version_id": source_version_id,
            "source_content_sha256": hashlib.sha256(payload.content).hexdigest(),
            "published_at": payload.published_at,
            "observed_at": payload.observed_at,
            "valid_at": payload.valid_at,
            "recorded_at": now,
            "predecessor_observation_id": prior.id if prior is not None else None,
        }
        observation = SourceTemporalObservation(
            id=content_id("source-temporal-observation", fields),
            **fields,
        )
        self.store.put_record("source-temporal-observation", observation)
        if prior is not None and prior.id != observation.id:
            relation_fields = {
                "source_intent_id": intent.id,
                "predecessor_observation_id": prior.id,
                "successor_observation_id": observation.id,
                "recorded_at": now,
            }
            self.store.put_record(
                "source-supersession",
                SourceSupersession(
                    id=content_id("source-supersession", relation_fields),
                    **relation_fields,
                ),
            )

    def _latest_temporal(
        self,
        source_intent_id: str,
        locator: str,
    ) -> SourceTemporalObservation | None:
        values = [
            SourceTemporalObservation.model_validate(value)
            for value in self.store.iter_records("source-temporal-observation")
            if value.get("source_intent_id") == source_intent_id and value.get("locator") == locator
        ]
        return max(values, key=lambda item: (item.observed_at, item.id), default=None)

    def _source_record_exists(self, source_id: str, record_sha256: str) -> bool:
        records = tuple(
            value
            for value in self.store.iter_records("source-version")
            if value.get("id") == source_id
            and hashlib.sha256(canonical_json(value)).hexdigest() == record_sha256
        )
        if len(records) != 1:
            return False
        digest = records[0].get("content_sha256")
        if not isinstance(digest, str):
            return False
        try:
            content = self.store.read_blob(digest)
        except (FileNotFoundError, ValueError, RuntimeError):
            return False
        if hashlib.sha256(content).hexdigest() != digest:
            return False
        return any(
            value.get("original_source_version_id") == source_id
            and value.get("original_source_record_sha256") == record_sha256
            for value in self.store.iter_records("parsed-ingest-receipt")
        )

    def _rebuild_library(self, now: datetime) -> None:
        if self.library_manifest is None:
            return
        if self.library_database is None:
            raise ValueError("source-library manifest requires a projection path")
        from research_agent.library import SourceLibraryBuilder

        SourceLibraryBuilder(store=self.store, clock=lambda: now).build(
            self.library_manifest,
            self.library_database,
        )

    def _propose(self, item: SourceWorkItem) -> tuple[str, ...]:
        result = self._result(item)
        anchor_ids = tuple(str(value) for value in result.get("anchor_ids", ()))
        derived_source_id = result.get("derived_source_version_id")
        derivation_id = result.get("structural_derivation_id")
        if (
            not anchor_ids
            or not isinstance(derived_source_id, str)
            or not isinstance(derivation_id, str)
        ):
            raise ValueError("proposal extraction requires exact immutable anchors")
        derivations = tuple(
            value
            for value in self.store.iter_records("structural-derivation")
            if value.get("id") == derivation_id
            and value.get("source_version_id") == derived_source_id
        )
        if len(derivations) != 1 or not set(anchor_ids).issubset(
            set(derivations[0].get("anchor_ids", ()))
        ):
            raise ValueError("proposal anchors differ from the parsed source derivation")
        anchors = tuple(
            value
            for value in self.store.iter_records("structural-anchor")
            if value.get("id") in set(anchor_ids)
            and value.get("structural_derivation_id") == derivation_id
            and value.get("source_version_id") == derived_source_id
        )
        if len(anchors) != len(anchor_ids):
            raise ValueError("proposal anchors differ from the parsed source derivation")
        if self.extraction is None:
            raise TypeError("source extraction adapter is unavailable")
        proposal = self.extraction.propose(
            source_version_id=derived_source_id,
            structural_derivation_id=derivation_id,
            anchor_ids=anchor_ids,
        )
        proposal_id = getattr(proposal, "id", None)
        if proposal_id is None and hasattr(proposal, "proposal"):
            proposal_id = proposal.proposal.id
        if not isinstance(proposal_id, str):
            raise ValueError("source extractor returned no proposal identity")
        return (proposal_id,)

    def _is_due(self, intent: SourceIntent, now: datetime) -> bool:
        unfinished = [
            item
            for item in self.work_store.current_items()
            if item.source_intent_id == intent.id
            and item.locator == intent.discovery.locator
            and item.phase is not SourceWorkPhase.FINALIZED
        ]
        if unfinished:
            return True
        observations = [
            datetime.fromisoformat(str(value["finalized_at"]))
            for value in self.store.iter_records("source-update-receipt")
            if intent.id in value.get("source_intent_ids", ()) and value.get("complete") is True
        ]
        if not observations:
            return True
        interval = intent.refresh.interval_seconds
        return (now - max(observations)).total_seconds() >= interval

    def _remaining(self) -> float:
        return max(0.0, self.limits.max_run_seconds - (self.monotonic() - self._started))
