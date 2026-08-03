from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from research_agent.models import StrictModel, content_id


class AuthenticatedPrincipal(StrictModel):
    actor_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    authenticated_at: datetime
    authentication_method: Literal["deployment_session", "local_os_session"]


class ApprovalRequest(StrictModel):
    id: str
    provider: str
    model: str
    operation: str
    data_class: str
    input_kind: str
    model_route: str
    run_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(gt=0)
    reserved_cost_microusd: int = Field(ge=0)
    model_policy_version: int
    budget_policy_version: int

    @classmethod
    def create(cls, **fields: object) -> ApprovalRequest:
        return cls(id=content_id("approval-request", fields), **fields)


class ApprovalReceipt(StrictModel):
    id: str
    request_id: str
    actor_id: str
    deployment_id: str
    session_id: str
    authentication_method: Literal["deployment_session", "local_os_session"]
    approved_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=1)
    status: Literal["approved"]

    @model_validator(mode="after")
    def expiry_follows_approval(self) -> ApprovalReceipt:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")
        return self


class ApprovalRegistry:
    """Single-use approval receipts issued only by a trusted deployment adapter."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    authentication_method TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    nonce TEXT NOT NULL UNIQUE,
                    consumed_at TEXT
                )
                """
            )

    def issue(
        self,
        request: ApprovalRequest,
        principal: AuthenticatedPrincipal,
        *,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> ApprovalReceipt:
        approved_at = (now or datetime.now(UTC)).astimezone(UTC)
        if principal.authenticated_at > approved_at:
            raise ValueError("principal authentication time is in the future")
        nonce = str(uuid4())
        fields = {
            "request_id": request.id,
            "actor_id": principal.actor_id,
            "deployment_id": principal.deployment_id,
            "session_id": principal.session_id,
            "authentication_method": principal.authentication_method,
            "approved_at": approved_at,
            "expires_at": expires_at.astimezone(UTC),
            "nonce": nonce,
        }
        receipt = ApprovalReceipt(
            id=content_id("approval-receipt", fields),
            **fields,
            status="approved",
        )
        self.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    receipt.id,
                    receipt.request_id,
                    receipt.actor_id,
                    receipt.deployment_id,
                    receipt.session_id,
                    receipt.authentication_method,
                    receipt.approved_at.isoformat(),
                    receipt.expires_at.isoformat(),
                    receipt.nonce,
                ),
            )
        return receipt

    def consume(
        self,
        receipt_id: str,
        *,
        expected_request_id: str,
        now: datetime | None = None,
    ) -> ApprovalReceipt:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        self.initialize()
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, request_id, actor_id, deployment_id, session_id,
                       authentication_method, approved_at, expires_at, nonce, consumed_at
                FROM approvals WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("approval receipt does not exist")
            if row[1] != expected_request_id:
                raise ValueError("approval receipt does not match this request")
            if row[9] is not None:
                raise ValueError("approval receipt has already been consumed")
            expires_at = datetime.fromisoformat(row[7])
            if expires_at <= timestamp:
                raise ValueError("approval receipt has expired")
            connection.execute(
                "UPDATE approvals SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (timestamp.isoformat(), receipt_id),
            )
        return ApprovalReceipt(
            id=row[0],
            request_id=row[1],
            actor_id=row[2],
            deployment_id=row[3],
            session_id=row[4],
            authentication_method=row[5],
            approved_at=datetime.fromisoformat(row[6]),
            expires_at=expires_at,
            nonce=row[8],
            status="approved",
        )
