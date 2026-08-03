from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from research_agent.models import StrictModel


class StorageRights(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    PROHIBITED = "prohibited"


class GeneralSearchProviderPolicy(StrictModel):
    connector_id: str
    enabled: bool
    priority: int = Field(ge=1)
    credential_env: str
    storage_rights: StorageRights
    persist_normalized_results: bool
    raw_response_retention_days: int = Field(ge=0)
    max_requests_per_run: int = Field(ge=1)
    max_requests_per_month: int = Field(ge=1)
    monthly_cost_ceiling_usd: float = Field(gt=0)

    @model_validator(mode="after")
    def storage_requires_rights(self) -> GeneralSearchProviderPolicy:
        if self.persist_normalized_results and self.storage_rights is not StorageRights.CONFIRMED:
            raise ValueError("persistent search results require confirmed storage rights")
        if self.raw_response_retention_days and self.storage_rights is not StorageRights.CONFIRMED:
            raise ValueError("raw response retention requires confirmed storage rights")
        return self


class DomainIndexProviderPolicy(StrictModel):
    connector_id: str
    enabled: bool
    priority: int = Field(ge=1)
    credential_env: str = ""
    metadata_license: str
    persist_normalized_metadata: bool
    raw_response_retention_days: Literal[0]
    max_requests_per_run: int = Field(ge=1, le=100)
    cost_accounting: Literal["none", "provider_reported_only"]
    daily_free_allowance_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def reported_cost_declares_allowance(self) -> DomainIndexProviderPolicy:
        if (
            self.cost_accounting == "provider_reported_only"
            and self.daily_free_allowance_usd is None
        ):
            raise ValueError("provider-reported cost accounting requires a declared allowance")
        if self.cost_accounting == "none" and self.daily_free_allowance_usd is not None:
            raise ValueError("a free allowance requires provider-reported cost accounting")
        return self


class ResearchPolicy(StrictModel):
    version: int = Field(ge=1)
    open_source_acquisition_order: tuple[str, ...] = Field(min_length=1)
    general_search_results_are_evidence: bool
    general_search_providers: tuple[GeneralSearchProviderPolicy, ...] = Field(min_length=1)
    domain_index_providers: tuple[DomainIndexProviderPolicy, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> ResearchPolicy:
        if self.general_search_results_are_evidence:
            raise ValueError("general search results may not be evidence")
        if len(self.open_source_acquisition_order) != len(set(self.open_source_acquisition_order)):
            raise ValueError("acquisition priorities must be unique")
        connector_ids = [item.connector_id for item in self.general_search_providers]
        if len(connector_ids) != len(set(connector_ids)):
            raise ValueError("general search connector ids must be unique")
        priorities = [item.priority for item in self.general_search_providers if item.enabled]
        if len(priorities) != len(set(priorities)):
            raise ValueError("enabled provider priorities must be unique")
        domain_ids = [item.connector_id for item in self.domain_index_providers]
        if len(domain_ids) != len(set(domain_ids)):
            raise ValueError("domain-index connector ids must be unique")
        domain_priorities = [
            item.priority for item in self.domain_index_providers if item.enabled
        ]
        if len(domain_priorities) != len(set(domain_priorities)):
            raise ValueError("enabled domain-index priorities must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ResearchPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def provider(self, connector_id: str) -> GeneralSearchProviderPolicy:
        for provider in self.general_search_providers:
            if provider.connector_id == connector_id:
                return provider
        raise KeyError(connector_id)

    def domain_index(self, connector_id: str) -> DomainIndexProviderPolicy:
        for provider in self.domain_index_providers:
            if provider.connector_id == connector_id:
                return provider
        raise KeyError(connector_id)
