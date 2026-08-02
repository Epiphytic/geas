from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from research_agent.models import StrictModel, content_id, utc_now


class WorkflowState(StrEnum):
    QUEUED = "queued"
    FETCHED = "fetched"
    QUARANTINED = "quarantined"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    STAGED = "staged"
    APPROVED = "approved"
    COMMITTED = "committed"
    REJECTED = "rejected"


class ActorKind(StrEnum):
    ORCHESTRATOR = "orchestrator"
    VALIDATOR = "validator"
    POLICY_ENGINE = "policy_engine"
    HUMAN = "human"
    COMMITTER = "committer"
    MODEL = "model"


class WorkflowTransition(StrictModel):
    id: str
    workflow_id: str
    source_version: str
    from_state: WorkflowState
    to_state: WorkflowState
    actor_kind: ActorKind
    actor_id: str
    artifact_hashes: tuple[str, ...] = Field(default_factory=tuple)
    occurred_at: datetime


_ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.QUEUED: {WorkflowState.FETCHED, WorkflowState.REJECTED},
    WorkflowState.FETCHED: {WorkflowState.QUARANTINED, WorkflowState.REJECTED},
    WorkflowState.QUARANTINED: {WorkflowState.EXTRACTED, WorkflowState.REJECTED},
    WorkflowState.EXTRACTED: {WorkflowState.VALIDATED, WorkflowState.REJECTED},
    WorkflowState.VALIDATED: {WorkflowState.STAGED, WorkflowState.REJECTED},
    WorkflowState.STAGED: {WorkflowState.APPROVED, WorkflowState.REJECTED},
    WorkflowState.APPROVED: {WorkflowState.COMMITTED, WorkflowState.REJECTED},
    WorkflowState.COMMITTED: set(),
    WorkflowState.REJECTED: set(),
}

_REQUIRED_ACTOR: dict[WorkflowState, set[ActorKind]] = {
    WorkflowState.FETCHED: {ActorKind.ORCHESTRATOR},
    WorkflowState.QUARANTINED: {ActorKind.ORCHESTRATOR, ActorKind.POLICY_ENGINE},
    WorkflowState.EXTRACTED: {ActorKind.ORCHESTRATOR},
    WorkflowState.VALIDATED: {ActorKind.VALIDATOR},
    WorkflowState.STAGED: {ActorKind.ORCHESTRATOR},
    WorkflowState.APPROVED: {ActorKind.POLICY_ENGINE, ActorKind.HUMAN},
    WorkflowState.COMMITTED: {ActorKind.COMMITTER},
    WorkflowState.REJECTED: {
        ActorKind.ORCHESTRATOR,
        ActorKind.VALIDATOR,
        ActorKind.POLICY_ENGINE,
        ActorKind.HUMAN,
        ActorKind.COMMITTER,
    },
}


class WorkflowEngine:
    version = "workflow/1"

    def transition(
        self,
        *,
        workflow_id: str,
        source_version: str,
        from_state: WorkflowState,
        to_state: WorkflowState,
        actor_kind: ActorKind,
        actor_id: str,
        artifact_hashes: tuple[str, ...] = (),
    ) -> WorkflowTransition:
        if to_state not in _ALLOWED_TRANSITIONS[from_state]:
            raise ValueError(f"transition {from_state} -> {to_state} is not allowed")
        if actor_kind is ActorKind.MODEL:
            raise ValueError("models cannot transition workflow state")
        if actor_kind not in _REQUIRED_ACTOR[to_state]:
            raise ValueError(f"{actor_kind} cannot transition a workflow to {to_state}")
        immutable_states = {
            WorkflowState.VALIDATED,
            WorkflowState.STAGED,
            WorkflowState.APPROVED,
            WorkflowState.COMMITTED,
        }
        if to_state in immutable_states and not artifact_hashes:
            raise ValueError(f"{to_state} requires immutable artifact hashes")

        payload = {
            "workflow_id": workflow_id,
            "source_version": source_version,
            "from_state": from_state,
            "to_state": to_state,
            "actor_kind": actor_kind,
            "actor_id": actor_id,
            "artifact_hashes": artifact_hashes,
            "engine_version": self.version,
        }
        return WorkflowTransition(
            id=content_id("workflow-transition", payload),
            workflow_id=workflow_id,
            source_version=source_version,
            from_state=from_state,
            to_state=to_state,
            actor_kind=actor_kind,
            actor_id=actor_id,
            artifact_hashes=artifact_hashes,
            occurred_at=utc_now(),
        )
