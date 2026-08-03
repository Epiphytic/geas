from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from research_agent.models import StrictModel, content_id


class ServiceKind(StrEnum):
    MODEL = "model"
    SEARCH = "search"
    OTHER = "other"


class BillingBasis(StrEnum):
    METERED = "metered"
    SUBSCRIPTION_INCLUDED = "subscription_included"
    ENTERPRISE_COMMIT = "enterprise_commit"
    NO_MARGINAL_COST = "no_marginal_cost"
    OTHER = "other"


class BudgetTreatment(StrEnum):
    COUNTED = "counted"
    EXCLUDED_FROM_COST = "excluded_from_cost"


class AutomaticEnvelope(StrictModel):
    max_cost_microusd_per_call: int = Field(ge=0)
    max_cost_microusd_per_run: int = Field(ge=0)
    max_cost_microusd_per_day: int = Field(ge=0)
    max_cost_microusd_per_month: int = Field(ge=0)
    max_calls_per_run: int = Field(gt=0)
    max_input_tokens_per_call: int = Field(gt=0)
    max_output_tokens_per_call: int = Field(gt=0)


class AccountRule(StrictModel):
    service: ServiceKind
    provider: str = Field(min_length=1)
    model: str | None = None
    billing_basis: BillingBasis
    budget_treatment: BudgetTreatment
    accounting_note: str = Field(min_length=1)
    input_cost_microusd_per_million_tokens: int | None = Field(default=None, ge=0)
    output_cost_microusd_per_million_tokens: int | None = Field(default=None, ge=0)
    unit_cost_microusd: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def counted_accounts_have_pricing(self) -> AccountRule:
        if self.budget_treatment is BudgetTreatment.COUNTED:
            if self.service is ServiceKind.MODEL and (
                self.input_cost_microusd_per_million_tokens is None
                or self.output_cost_microusd_per_million_tokens is None
            ):
                raise ValueError("counted model accounts require input and output pricing")
            if self.service is not ServiceKind.MODEL and self.unit_cost_microusd is None:
                raise ValueError("counted non-model accounts require unit pricing")
        if (
            self.budget_treatment is BudgetTreatment.EXCLUDED_FROM_COST
            and self.billing_basis is BillingBasis.METERED
        ):
            raise ValueError("metered accounts cannot be excluded from cost")
        return self


class BudgetPolicy(StrictModel):
    version: int = Field(ge=1)
    currency: Literal["USD"]
    automatic_envelope: AutomaticEnvelope
    accounts: tuple[AccountRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def accounts_are_unique(self) -> BudgetPolicy:
        keys = [(item.service, item.provider, item.model) for item in self.accounts]
        if len(keys) != len(set(keys)):
            raise ValueError("budget account rules must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> BudgetPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def account(self, service: ServiceKind, provider: str, model: str | None) -> AccountRule:
        for account in self.accounts:
            if (
                account.service is service
                and account.provider == provider
                and account.model == model
            ):
                return account
        raise ValueError(f"accounting is unknown for {service}:{provider}:{model}")

    def estimate_model_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        return UsageLedger._cost(
            self.account(ServiceKind.MODEL, provider, model),
            input_tokens,
            output_tokens,
        )


class UsageReservation(StrictModel):
    id: str
    service: ServiceKind
    provider: str
    model: str | None
    run_id: str
    request_key: str
    budget_treatment: BudgetTreatment
    billing_basis: BillingBasis
    accounting_note: str
    input_tokens_reserved: int
    output_tokens_reserved: int
    cost_microusd_reserved: int
    human_approved: bool
    created_at: datetime
    policy_version: int
    status: Literal["reserved"]


class UsageSettlement(StrictModel):
    reservation_id: str
    input_tokens_actual: int | None = Field(default=None, ge=0)
    output_tokens_actual: int | None = Field(default=None, ge=0)
    cost_microusd_actual: int | None = Field(default=None, ge=0)
    status: Literal["settled", "settled_estimate", "overrun"]


def reserved_model_input_tokens(system: str, user: str) -> int:
    # A byte-level upper bound plus fixed chat-envelope allowance. If a provider
    # ever reports more, settlement marks an overrun and the output is rejected.
    return len(system.encode()) + len(user.encode()) + 2_048


class UsageLedger:
    schema_version = 1

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    run_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    budget_treatment TEXT NOT NULL,
                    billing_basis TEXT NOT NULL,
                    accounting_note TEXT NOT NULL,
                    input_tokens_reserved INTEGER NOT NULL,
                    output_tokens_reserved INTEGER NOT NULL,
                    cost_microusd_reserved INTEGER NOT NULL,
                    input_tokens_actual INTEGER,
                    output_tokens_actual INTEGER,
                    cost_microusd_actual INTEGER,
                    human_approved INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def reserve_model(
        self,
        *,
        policy: BudgetPolicy,
        provider: str,
        model: str,
        run_id: str,
        request_key: str,
        input_tokens: int,
        output_tokens: int,
        human_approved: bool,
        now: datetime | None = None,
    ) -> UsageReservation:
        if not run_id.strip():
            raise ValueError("run_id is required for budget accounting")
        account = policy.account(ServiceKind.MODEL, provider, model)
        envelope = policy.automatic_envelope
        if input_tokens > envelope.max_input_tokens_per_call:
            raise ValueError("input token reservation exceeds per-call limit")
        if output_tokens > envelope.max_output_tokens_per_call:
            raise ValueError("output token reservation exceeds per-call limit")
        reserved_cost = self._cost(account, input_tokens, output_tokens)
        if (
            account.budget_treatment is BudgetTreatment.COUNTED
            and reserved_cost > envelope.max_cost_microusd_per_call
            and not human_approved
        ):
            raise ValueError("reserved cost exceeds automatic per-call limit")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        created_at = timestamp.isoformat()
        day = timestamp.date().isoformat()
        month = day[:7]
        self.initialize()
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_calls = self._scalar(
                connection,
                "SELECT COUNT(*) FROM usage WHERE run_id = ?",
                (run_id,),
            )
            if run_calls >= envelope.max_calls_per_run and not human_approved:
                raise ValueError("automatic external call count exceeds per-run limit")
            if account.budget_treatment is BudgetTreatment.COUNTED and not human_approved:
                checks = (
                    (
                        "run",
                        envelope.max_cost_microusd_per_run,
                        "run_id = ?",
                        (run_id,),
                    ),
                    (
                        "daily",
                        envelope.max_cost_microusd_per_day,
                        "day_utc = ?",
                        (day,),
                    ),
                    (
                        "monthly",
                        envelope.max_cost_microusd_per_month,
                        "month_utc = ?",
                        (month,),
                    ),
                )
                for label, limit, clause, parameters in checks:
                    consumed = self._consumed_cost(connection, clause, parameters)
                    if consumed + reserved_cost > limit:
                        raise ValueError(f"reserved cost exceeds automatic {label} limit")
            sequence = self._scalar(connection, "SELECT COUNT(*) FROM usage", ())
            fields = {
                "service": ServiceKind.MODEL,
                "provider": provider,
                "model": model,
                "run_id": run_id,
                "request_key": request_key,
                "sequence": sequence,
                "created_at": created_at,
                "policy_version": policy.version,
            }
            reservation_id = content_id("usage-reservation", fields)
            connection.execute(
                """
                INSERT INTO usage VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    reservation_id,
                    ServiceKind.MODEL,
                    provider,
                    model,
                    run_id,
                    request_key,
                    account.budget_treatment,
                    account.billing_basis,
                    account.accounting_note,
                    input_tokens,
                    output_tokens,
                    reserved_cost,
                    int(human_approved),
                    created_at,
                    day,
                    month,
                    policy.version,
                    "reserved",
                ),
            )
        return UsageReservation(
            id=reservation_id,
            service=ServiceKind.MODEL,
            provider=provider,
            model=model,
            run_id=run_id,
            request_key=request_key,
            budget_treatment=account.budget_treatment,
            billing_basis=account.billing_basis,
            accounting_note=account.accounting_note,
            input_tokens_reserved=input_tokens,
            output_tokens_reserved=output_tokens,
            cost_microusd_reserved=reserved_cost,
            human_approved=human_approved,
            created_at=timestamp,
            policy_version=policy.version,
            status="reserved",
        )

    def settle_model(
        self,
        reservation: UsageReservation,
        *,
        policy: BudgetPolicy,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> UsageSettlement:
        if input_tokens is None or output_tokens is None:
            status = "settled_estimate"
            actual_cost = None
        else:
            account = policy.account(ServiceKind.MODEL, reservation.provider, reservation.model)
            actual_cost = self._cost(account, input_tokens, output_tokens)
            status = (
                "overrun"
                if input_tokens > reservation.input_tokens_reserved
                or output_tokens > reservation.output_tokens_reserved
                else "settled"
            )
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE usage
                SET input_tokens_actual = ?, output_tokens_actual = ?,
                    cost_microusd_actual = ?, status = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (input_tokens, output_tokens, actual_cost, status, reservation.id),
            )
            if cursor.rowcount != 1:
                raise ValueError("usage reservation is missing or already settled")
        return UsageSettlement(
            reservation_id=reservation.id,
            input_tokens_actual=input_tokens,
            output_tokens_actual=output_tokens,
            cost_microusd_actual=actual_cost,
            status=status,
        )

    @staticmethod
    def _cost(account: AccountRule, input_tokens: int, output_tokens: int) -> int:
        if account.budget_treatment is BudgetTreatment.EXCLUDED_FROM_COST:
            return 0
        assert account.input_cost_microusd_per_million_tokens is not None
        assert account.output_cost_microusd_per_million_tokens is not None
        numerator = (
            input_tokens * account.input_cost_microusd_per_million_tokens
            + output_tokens * account.output_cost_microusd_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000

    @staticmethod
    def _scalar(
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[object, ...],
    ) -> int:
        row = connection.execute(query, parameters).fetchone()
        return int(row[0])

    @classmethod
    def _consumed_cost(
        cls,
        connection: sqlite3.Connection,
        clause: str,
        parameters: tuple[object, ...],
    ) -> int:
        return cls._scalar(
            connection,
            f"""
            SELECT COALESCE(SUM(
                CASE
                    WHEN status IN ('settled', 'overrun')
                    THEN COALESCE(cost_microusd_actual, cost_microusd_reserved)
                    ELSE cost_microusd_reserved
                END
            ), 0)
            FROM usage
            WHERE budget_treatment = 'counted' AND {clause}
            """,
            parameters,
        )
