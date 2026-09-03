"""Immutable, resumable source-work contract records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel, content_id


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
    SourceWorkPhase.ACCESS_CONSTRAINED: frozenset({SourceWorkPhase.AUTHORIZED}),
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
    predecessor_id: str | None
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


class SourceCheckpoint(StrictModel):
    version: Literal[1] = 1
    work_item_id: str
    phase: SourceWorkPhase
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime

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

    @field_validator("work_item_ids")
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
