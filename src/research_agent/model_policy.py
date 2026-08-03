from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, HttpUrl, model_validator

from research_agent.budget import (
    BudgetPolicy,
    BudgetTreatment,
    UsageLedger,
    UsageReservation,
    UsageSettlement,
    reserved_model_input_tokens,
)
from research_agent.deposits import ModelRoute
from research_agent.models import ProviderConfig, StrictModel, content_id


class ModelOperation(StrEnum):
    MODEL_SMOKE = "model_smoke"
    QUERY_COMPILATION = "query_compilation"
    ONTOLOGY_EXTRACTION = "ontology_extraction"
    ONTOLOGY_DESIGN = "ontology_design"
    CONFLICT_ANALYSIS = "conflict_analysis"
    GAP_ANALYSIS = "gap_analysis"


class DataClass(StrEnum):
    PUBLIC = "public"
    AUTHORIZED_WORKSPACE = "authorized_workspace"
    LICENSED = "licensed"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class InputKind(StrEnum):
    METADATA_ONLY = "metadata_only"
    SOURCE_CONTENT = "source_content"


class ExternalProviderRule(StrictModel):
    provider: str = Field(min_length=1)
    base_urls: tuple[HttpUrl, ...] = Field(min_length=1)
    models: tuple[str, ...] = Field(min_length=1)
    operations: frozenset[ModelOperation] = Field(min_length=1)
    data_classes: frozenset[DataClass] = Field(min_length=1)


class ModelUsePolicy(StrictModel):
    version: int = Field(ge=1)
    automatic_external_calls: bool
    unknown_data_class_external: Literal["forbidden"]
    external_providers: tuple[ExternalProviderRule, ...]

    @model_validator(mode="after")
    def provider_rules_are_unambiguous(self) -> ModelUsePolicy:
        names = [rule.provider for rule in self.external_providers]
        if len(names) != len(set(names)):
            raise ValueError("external provider rules must have unique names")
        if any(DataClass.UNKNOWN in rule.data_classes for rule in self.external_providers):
            raise ValueError("unknown data cannot be preauthorized for external use")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ModelUsePolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def rule(self, provider: str) -> ExternalProviderRule:
        for rule in self.external_providers:
            if rule.provider == provider:
                return rule
        raise ValueError(f"external provider {provider!r} is not allowlisted")


class ModelUseContext(StrictModel):
    operation: ModelOperation
    data_class: DataClass
    input_kind: InputKind
    model_route: ModelRoute = ModelRoute.LOCAL_PREFERRED
    human_approved: bool = False
    run_id: str = Field(min_length=1)


class ModelUseAuthorization(StrictModel):
    id: str
    provider: str
    model: str
    external: bool
    operation: ModelOperation
    data_class: DataClass
    input_kind: InputKind
    model_route: ModelRoute
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(gt=0)
    human_approved: bool
    usage_reservation_id: str | None = None
    budget_treatment: BudgetTreatment | None = None
    policy_version: int
    decision: Literal["allow"]


class ModelUseGate:
    """Deterministic, non-model authorization for one declared call context."""

    def __init__(
        self,
        policy: ModelUsePolicy,
        context: ModelUseContext,
        *,
        budget_policy: BudgetPolicy | None = None,
        usage_ledger: UsageLedger | None = None,
    ) -> None:
        self.policy = policy
        self.context = context
        self.budget_policy = budget_policy
        self.usage_ledger = usage_ledger
        self.last_authorization: ModelUseAuthorization | None = None
        self.last_reservation: UsageReservation | None = None
        self.last_settlement: UsageSettlement | None = None

    def authorize(
        self,
        *,
        provider: str,
        config: ProviderConfig,
        system: str,
        user: str,
        max_output_tokens: int,
    ) -> ModelUseAuthorization:
        if config.external:
            self._authorize_external(provider, config)
        input_sha256 = hashlib.sha256(system.encode() + b"\x00" + user.encode()).hexdigest()
        reservation = None
        if config.external:
            if self.budget_policy is None or self.usage_ledger is None:
                raise ValueError("external model use requires budget policy and usage ledger")
            reservation = self.usage_ledger.reserve_model(
                policy=self.budget_policy,
                provider=provider,
                model=config.model,
                run_id=self.context.run_id,
                request_key=input_sha256,
                input_tokens=reserved_model_input_tokens(system, user),
                output_tokens=max_output_tokens,
                human_approved=self.context.human_approved,
            )
            self.last_reservation = reservation
        fields = {
            "provider": provider,
            "model": config.model,
            "external": config.external,
            **self.context.model_dump(mode="json"),
            "input_sha256": input_sha256,
            "max_output_tokens": max_output_tokens,
            "usage_reservation_id": reservation.id if reservation else None,
            "budget_treatment": reservation.budget_treatment if reservation else None,
            "policy_version": self.policy.version,
            "decision": "allow",
        }
        authorization = ModelUseAuthorization(
            id=content_id("model-authorization", fields),
            provider=provider,
            model=config.model,
            external=config.external,
            operation=self.context.operation,
            data_class=self.context.data_class,
            input_kind=self.context.input_kind,
            model_route=self.context.model_route,
            input_sha256=input_sha256,
            max_output_tokens=max_output_tokens,
            human_approved=self.context.human_approved,
            usage_reservation_id=reservation.id if reservation else None,
            budget_treatment=reservation.budget_treatment if reservation else None,
            policy_version=self.policy.version,
            decision="allow",
        )
        self.last_authorization = authorization
        return authorization

    def settle(self, *, input_tokens: int | None, output_tokens: int | None) -> None:
        if self.last_reservation is None:
            return
        assert self.budget_policy is not None
        assert self.usage_ledger is not None
        settlement = self.usage_ledger.settle_model(
            self.last_reservation,
            policy=self.budget_policy,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self.last_settlement = settlement
        if settlement.status == "overrun":
            raise ValueError("provider token usage exceeded the deterministic reservation")

    def _authorize_external(self, provider: str, config: ProviderConfig) -> None:
        rule = self.policy.rule(provider)
        configured_url = str(config.base_url).rstrip("/")
        allowed_urls = {str(url).rstrip("/") for url in rule.base_urls}
        if configured_url not in allowed_urls:
            raise ValueError(f"base URL is not allowlisted for {provider!r}")
        if config.model not in rule.models:
            raise ValueError(f"model {config.model!r} is not allowlisted for {provider!r}")
        if self.context.operation not in rule.operations:
            raise ValueError(
                f"operation {self.context.operation!r} is not allowlisted for {provider!r}"
            )
        if self.context.data_class is DataClass.UNKNOWN:
            raise ValueError("unknown data classification is forbidden for external providers")
        if self.context.data_class not in rule.data_classes:
            raise ValueError(
                f"data class {self.context.data_class!r} is not allowlisted for {provider!r}"
            )
        if (
            self.context.input_kind is InputKind.SOURCE_CONTENT
            and self.context.model_route is not ModelRoute.EXTERNAL_ALLOWED
        ):
            raise ValueError("source content is not marked external_allowed")
        if not self.policy.automatic_external_calls and not self.context.human_approved:
            raise ValueError("external provider use requires human approval")
