"""Immutable, resumable source-work records and deterministic coordination."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityEvaluator,
    CapabilityRequest,
)
from research_agent.models import StrictModel, content_id, utc_now
from research_agent.store import ImmutableStore

if TYPE_CHECKING:
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


_PREDECESSORS: dict[SourceWorkPhase, frozenset[SourceWorkPhase]] = {
    SourceWorkPhase.CANDIDATE: frozenset(),
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
    SourceWorkPhase.EXTRACTION_CONSTRAINED: frozenset({SourceWorkPhase.ANCHORS_SELECTED}),
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
            if self.predecessor_id is not None or self.predecessor_phase is not None:
                raise ValueError("candidate source work has no predecessor")
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
            exclude={"phase", "predecessor_id", "predecessor_phase", "created_at"},
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
            value.casefold() != value
            or not value.replace("_", "").replace("-", "").isalnum()
        ):
            raise ValueError("source constraint must be a normalized type")
        return value

    @model_validator(mode="after")
    def metadata_matches_phase(self) -> SourceCheckpoint:
        if self.constraint is not None and self.phase is not SourceWorkPhase.ACCESS_CONSTRAINED:
            raise ValueError("typed source constraints require access_constrained phase")
        if self.retry_after is not None and self.constraint is None:
            raise ValueError("retry_after requires a typed source constraint")
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
        work_item_id = self._read_index().get(lineage_id)
        if work_item_id is None:
            return None
        item = self.get(work_item_id)
        if item is None or item.lineage_id != lineage_id:
            raise ValueError("source-work current index does not match immutable history")
        return item

    def put(self, item: SourceWorkItem) -> SourceWorkItem:
        self.store.initialize()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self._read_index()
            current_id = index.get(item.lineage_id)
            current = self.get(current_id) if current_id is not None else None
            if item.phase is SourceWorkPhase.CANDIDATE:
                if current is not None and current.phase is not SourceWorkPhase.FINALIZED:
                    if current.id == item.id:
                        return current
                    raise ValueError("candidate cannot replace unfinished source work")
            elif current is None or item.predecessor_id != current.id:
                raise ValueError("source work must name the current predecessor")
            self.store.put_record("source-work", item)
            index[item.lineage_id] = item.id
            self._write_index(index)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return item

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
        return max(values, key=lambda item: (item.recorded_at, item.id), default=None)

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
        ontology_bundle_sha256: str,
        parser: ParsedDocumentManager | None = None,
        library_manifest: SourceLibraryManifest | None = None,
        library_database: Path | None = None,
        extractor: object | None = None,
        external_model: bool = False,
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
        self.ontology_bundle_sha256 = _digest(
            ontology_bundle_sha256, label="ontology_bundle_sha256"
        )
        self.parser = parser or ParsedDocumentManager(store=store, clock=clock)
        self.library_manifest = library_manifest
        self.library_database = library_database
        self.extractor = extractor
        self.external_model = external_model
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
        from research_agent.source_intent import authorize_candidate

        discovered = self.adapter.discover(effective_intent)
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
            self._request_count += 1
            if self._request_count > self.limits.max_requests_per_run:
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
            extraction_validator_version="anchor-grounded-extraction-validator/1",
            capability_decision_sha256=fetch_decision.sha256,
            phase=SourceWorkPhase.CANDIDATE,
            predecessor_id=None,
            predecessor_phase=None,
            created_at=candidate.discovered_at,
        )
        current = self.work_store.current(base.lineage_id)
        if current is None or current.phase is SourceWorkPhase.FINALIZED:
            current = self._record(base)
        checkpoint_ids: list[str] = []
        constraint_ids: list[str] = []
        proposal_ids: list[str] = []
        candidate_complete = True
        source_id = self._result(current).get("source_version_id")
        if current.phase is SourceWorkPhase.CANDIDATE:
            current = self._advance(current, SourceWorkPhase.AUTHORIZED, now)
        if fetch_decision.decision != "allow":
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
            fetched = self.adapter.fetch(candidate, prior=prior)
            if fetched.phase not in {
                SourceWorkPhase.FETCHED,
                SourceWorkPhase.NOT_MODIFIED,
                SourceWorkPhase.ACCESS_CONSTRAINED,
            }:
                raise ValueError("source adapter returned an invalid fetch phase")
            current = self._advance(
                current,
                fetched.phase,
                fetched.recorded_at,
                result_sha256=fetched.result_sha256,
            )
            durable_checkpoint = fetched.model_copy(update={"work_item_id": current.id})
            durable_checkpoint = SourceCheckpoint.model_validate(
                durable_checkpoint.model_dump()
            )
            checkpoint_ids.append(
                self.work_store.checkpoint(durable_checkpoint).id
            )
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
            current = self._advance(current, SourceWorkPhase.FINALIZED, now)
            return (
                self.work_store.chain(current),
                None,
                tuple(checkpoint_ids),
                tuple(constraint_ids),
                (),
                not constrained,
            )
        if current.phase is SourceWorkPhase.FETCHED:
            payload = self._payload(candidate, current)
            self._byte_count += len(payload.content)
            if self._byte_count > self.limits.max_bytes_per_run:
                constraint_ids.append(
                    self._constraint(intent, candidate, "source byte limit reached", now)
                )
                current = self._advance(current, SourceWorkPhase.ACCESS_CONSTRAINED, now)
                current = self._advance(current, SourceWorkPhase.FINALIZED, now)
                return (
                    self.work_store.chain(current),
                    None,
                    tuple(checkpoint_ids),
                    tuple(constraint_ids),
                    (),
                    False,
                )
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
            self._temporal_observation(intent, candidate, payload, source.id, now)
            current = self._advance(
                current,
                SourceWorkPhase.ARCHIVED,
                now,
                result_values={"source_version_id": source.id},
            )
        result = self._result(current)
        source_id = str(result.get("source_version_id") or source_id or "") or None
        if current.phase is SourceWorkPhase.ARCHIVED:
            extract = self._decision(intent, candidate, (Capability.SOURCE_EXTRACT,), now)
            if extract.decision != "allow":
                constraint_ids.append(
                    self._constraint(intent, candidate, "source.extract denied", now)
                )
                current = self._advance(current, SourceWorkPhase.PARSER_CONSTRAINED, now)
            else:
                if source_id is None:
                    raise ValueError("archived source work has no immutable source identity")
                parsed = self.parser.parse_source(source_id)
                current = self._advance(
                    current,
                    SourceWorkPhase.PARSED,
                    now,
                    result_values=self._parsed_result_values(parsed),
                )
        if current.phase is SourceWorkPhase.PARSER_CONSTRAINED:
            current = self._advance(current, SourceWorkPhase.FINALIZED, now)
            return (
                self.work_store.chain(current),
                source_id,
                tuple(checkpoint_ids),
                tuple(constraint_ids),
                (),
                False,
            )
        if current.phase is SourceWorkPhase.PARSED:
            current = self._advance(
                current,
                SourceWorkPhase.STRUCTURED,
                now,
                result_values=self._result_values(current),
            )
        if current.phase is SourceWorkPhase.STRUCTURED:
            self._rebuild_library(now)
            current = self._advance(
                current,
                SourceWorkPhase.INDEXED,
                now,
                result_values=self._result_values(current),
            )
        if current.phase is SourceWorkPhase.INDEXED:
            current = self._advance(
                current,
                SourceWorkPhase.ANCHORS_SELECTED,
                now,
                result_values=self._result_values(current),
            )
        if current.phase is SourceWorkPhase.ANCHORS_SELECTED:
            if self.extractor is None:
                current = self._advance(current, SourceWorkPhase.EXTRACTION_CONSTRAINED, now)
            else:
                if self.external_model:
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
                        model = self._decision(
                            intent, candidate, (Capability.MODEL_EXTERNAL,), now
                        )
                        if model.decision != "allow":
                            constraint_ids.append(
                                self._constraint(
                                    intent, candidate, "model.external denied", now
                                )
                            )
                            current = self._advance(
                                current,
                                SourceWorkPhase.EXTRACTION_CONSTRAINED,
                                now,
                            )
                            candidate_complete = False
                        else:
                            proposal_ids.extend(self._propose(current, source_id))
                            current = self._advance(
                                current, SourceWorkPhase.EXTRACTION_PROPOSED, now
                            )
                else:
                    proposal_ids.extend(self._propose(current, source_id))
                    current = self._advance(current, SourceWorkPhase.EXTRACTION_PROPOSED, now)
        if current.phase in {
            SourceWorkPhase.EXTRACTION_PROPOSED,
            SourceWorkPhase.EXTRACTION_CONSTRAINED,
        }:
            current = self._advance(current, SourceWorkPhase.FINALIZED, now)
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
        decision = self.capability_evaluator.evaluate(request)
        self.store.initialize()
        self.store.put_record("capability-decision", decision)
        if decision.request.id != request.id:
            raise ValueError("capability evaluator returned a decision for another request")
        return decision

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
    ) -> SourceWorkItem:
        item = predecessor.model_copy(
            update={
                "phase": phase,
                "predecessor_id": predecessor.id,
                "predecessor_phase": predecessor.phase,
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

    def _payload(
        self,
        candidate: SourceCandidate,
        current: SourceWorkItem,
    ) -> FetchedSourcePayload:
        provider = self.adapter
        payload_method = getattr(provider, "payload", None)
        if callable(payload_method):
            checkpoint = SourceCheckpoint(
                work_item_id=candidate.id,
                phase=SourceWorkPhase.FETCHED,
                result_sha256=self._checkpoint_result(current),
                recorded_at=current.created_at,
            )
            return payload_method(candidate, checkpoint)
        last_fetch = getattr(provider, "last_fetch", {})
        result = last_fetch.get(candidate.id) if isinstance(last_fetch, dict) else None
        if result is not None:
            return FetchedSourcePayload(
                content=result.content,
                source_uri=result.locator,
                media_type=result.media_type,
                connector_id=self.adapter.adapter_id,
                license=None,
                observed_at=current.created_at,
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
        eligible = {
            "heading",
            "paragraph",
            "list_item",
            "footnote",
            "caption",
        }
        exact_anchor_ids = tuple(
            sorted(
                value["id"]
                for value in self.store.iter_records("structural-anchor")
                if value.get("structural_derivation_id") == parsed.structural_derivation_id
                and value.get("kind") in eligible
            )
        )
        return {
            "source_version_id": parsed.original_source_version_id,
            "derived_source_version_id": parsed.derived_source_version_id,
            "structural_derivation_id": parsed.structural_derivation_id,
            "anchor_ids": exact_anchor_ids,
            "threat_observation_ids": parsed.threat_observation_ids,
        }

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

    def _propose(self, item: SourceWorkItem, source_id: str | None) -> tuple[str, ...]:
        result = self._result(item)
        anchor_ids = tuple(str(value) for value in result.get("anchor_ids", ()))
        if not anchor_ids or source_id is None:
            raise ValueError("proposal extraction requires exact immutable anchors")
        propose = getattr(self.extractor, "propose", None)
        if not callable(propose):
            raise TypeError("source extractor must provide propose()")
        proposal = propose(
            source_version_id=source_id,
            structural_derivation_id=str(result["structural_derivation_id"]),
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
            if intent.id in value.get("source_intent_ids", ())
        ]
        if not observations:
            return True
        interval = intent.refresh.interval_seconds
        return (now - max(observations)).total_seconds() >= interval

    def _remaining(self) -> float:
        return max(0.0, self.limits.max_run_seconds - (self.monotonic() - self._started))
