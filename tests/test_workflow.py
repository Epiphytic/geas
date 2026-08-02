import pytest

from research_agent.workflow import ActorKind, WorkflowEngine, WorkflowState


def test_model_cannot_transition_workflow() -> None:
    with pytest.raises(ValueError, match="models cannot transition"):
        WorkflowEngine().transition(
            workflow_id="workflow:1",
            source_version="source:1",
            from_state=WorkflowState.QUARANTINED,
            to_state=WorkflowState.EXTRACTED,
            actor_kind=ActorKind.MODEL,
            actor_id="model:local",
        )


def test_only_committer_can_commit() -> None:
    with pytest.raises(ValueError, match="cannot transition"):
        WorkflowEngine().transition(
            workflow_id="workflow:1",
            source_version="source:1",
            from_state=WorkflowState.APPROVED,
            to_state=WorkflowState.COMMITTED,
            actor_kind=ActorKind.HUMAN,
            actor_id="person:reviewer",
        )


def test_validated_state_requires_artifact_hash() -> None:
    with pytest.raises(ValueError, match="requires immutable artifact hashes"):
        WorkflowEngine().transition(
            workflow_id="workflow:1",
            source_version="source:1",
            from_state=WorkflowState.EXTRACTED,
            to_state=WorkflowState.VALIDATED,
            actor_kind=ActorKind.VALIDATOR,
            actor_id="validator:schema",
        )


def test_committer_can_commit_approved_artifact() -> None:
    transition = WorkflowEngine().transition(
        workflow_id="workflow:1",
        source_version="source:1",
        from_state=WorkflowState.APPROVED,
        to_state=WorkflowState.COMMITTED,
        actor_kind=ActorKind.COMMITTER,
        actor_id="process:committer",
        artifact_hashes=("sha256:abc",),
    )
    assert transition.to_state is WorkflowState.COMMITTED


def test_commit_requires_artifact_hash() -> None:
    with pytest.raises(ValueError, match="requires immutable artifact hashes"):
        WorkflowEngine().transition(
            workflow_id="workflow:1",
            source_version="source:1",
            from_state=WorkflowState.APPROVED,
            to_state=WorkflowState.COMMITTED,
            actor_kind=ActorKind.COMMITTER,
            actor_id="process:committer",
        )
