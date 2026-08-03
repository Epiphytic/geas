from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_agent.approvals import (
    ApprovalRegistry,
    ApprovalRequest,
    AuthenticatedPrincipal,
)


def _request(label: str = "one") -> ApprovalRequest:
    return ApprovalRequest.create(
        provider="openai",
        model="gpt-5.2",
        operation="conflict_analysis",
        data_class="authorized_workspace",
        input_kind="metadata_only",
        model_route="local_preferred",
        run_id="run:test",
        input_sha256=("a" if label == "one" else "b") * 64,
        max_output_tokens=100,
        reserved_cost_microusd=1_000,
        model_policy_version=2,
        budget_policy_version=1,
    )


def _principal(now: datetime) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="user:operator",
        deployment_id="deployment:research",
        session_id="session:authenticated",
        authenticated_at=now,
        authentication_method="deployment_session",
    )


def test_authenticated_receipt_is_single_use(tmp_path: Path) -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    registry = ApprovalRegistry(tmp_path / "usage.sqlite")
    request = _request()
    receipt = registry.issue(
        request,
        _principal(now),
        expires_at=now + timedelta(minutes=10),
        now=now,
    )

    consumed = registry.consume(
        receipt.id,
        expected_request_id=request.id,
        now=now + timedelta(minutes=1),
    )
    assert consumed.actor_id == "user:operator"
    with pytest.raises(ValueError, match="already been consumed"):
        registry.consume(
            receipt.id,
            expected_request_id=request.id,
            now=now + timedelta(minutes=2),
        )


def test_receipt_cannot_approve_a_different_request(tmp_path: Path) -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    registry = ApprovalRegistry(tmp_path / "usage.sqlite")
    receipt = registry.issue(
        _request("one"),
        _principal(now),
        expires_at=now + timedelta(minutes=10),
        now=now,
    )

    with pytest.raises(ValueError, match="does not match"):
        registry.consume(
            receipt.id,
            expected_request_id=_request("two").id,
            now=now + timedelta(minutes=1),
        )


def test_expired_receipt_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    registry = ApprovalRegistry(tmp_path / "usage.sqlite")
    request = _request()
    receipt = registry.issue(
        request,
        _principal(now),
        expires_at=now + timedelta(minutes=1),
        now=now,
    )

    with pytest.raises(ValueError, match="expired"):
        registry.consume(
            receipt.id,
            expected_request_id=request.id,
            now=now + timedelta(minutes=2),
        )
