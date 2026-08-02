from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from research_agent.models import SourceVersion, StrictModel, content_id
from research_agent.store import ImmutableStore


class AcquisitionMethod(StrEnum):
    LOCAL_FILE = "local_file"
    BROWSER_SAVE = "browser_save"
    EMAIL_EXPORT = "email_export"
    ZOTERO_EXPORT = "zotero_export"
    API_EXPORT = "api_export"
    OTHER = "other"


class ModelRoute(StrEnum):
    LOCAL_PREFERRED = "local_preferred"
    EXTERNAL_ALLOWED = "external_allowed"


class RedistributionStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_GRANTED = "not_granted"
    GRANTED = "granted"


class DepositDefaults(StrictModel):
    scope_label: str = Field(min_length=1)
    index_content: bool
    include_in_ontology: bool
    model_route: ModelRoute
    redistribution_status: RedistributionStatus
    retention_policy: str = Field(min_length=1)


class DepositOverrides(StrictModel):
    scope_label: str | None = Field(default=None, min_length=1)
    index_content: bool | None = None
    include_in_ontology: bool | None = None
    model_route: ModelRoute | None = None
    redistribution_status: RedistributionStatus | None = None
    retention_policy: str | None = Field(default=None, min_length=1)


class DepositPolicy(StrictModel):
    version: int = Field(ge=1)
    authorization_boundary: Literal["deployment"]
    per_record_access_control: Literal[False]
    labels_are_advisory: Literal[True]
    defaults: DepositDefaults

    @classmethod
    def from_yaml(cls, path: Path) -> DepositPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class DepositRecord(StrictModel):
    id: str
    source_version: str
    deposited_by: str = Field(min_length=1)
    deposited_at: datetime
    acquisition_method: AcquisitionMethod
    original_filename: str
    original_locator: str | None = None
    scope_label: str
    index_content: bool
    include_in_ontology: bool
    model_route: ModelRoute
    redistribution_status: RedistributionStatus
    retention_policy: str
    rights_basis: str | None = None
    provenance_note: str | None = None
    policy_version: int
    access_enforcement: Literal["deployment_boundary_only"]


class DepositResult(StrictModel):
    source: SourceVersion
    deposit: DepositRecord
    record_hash: str


class DepositManager:
    version = "deposit-manager/1"

    def __init__(self, *, store: ImmutableStore, policy: DepositPolicy) -> None:
        self.store = store
        self.policy = policy

    def deposit_file(
        self,
        path: Path,
        *,
        deposited_by: str,
        acquisition_method: AcquisitionMethod = AcquisitionMethod.LOCAL_FILE,
        original_locator: str | None = None,
        source_uri: str | None = None,
        license: str | None = None,
        rights_basis: str | None = None,
        provenance_note: str | None = None,
        overrides: DepositOverrides | None = None,
    ) -> DepositResult:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {resolved}")
        if not deposited_by.strip():
            raise ValueError("deposited_by is required")
        effective = self._effective(overrides or DepositOverrides())
        source = self.store.ingest_file(
            resolved,
            source_uri=source_uri,
            connector_id="connector:user-deposit",
            license=license,
        )
        fields = {
            "source_version": source.id,
            "deposited_by": deposited_by,
            "deposited_at": source.acquired_at,
            "acquisition_method": acquisition_method,
            "original_filename": resolved.name,
            "original_locator": original_locator,
            **effective.model_dump(mode="json"),
            "rights_basis": rights_basis,
            "provenance_note": provenance_note,
            "policy_version": self.policy.version,
            "manager_version": self.version,
        }
        deposit = DepositRecord(
            id=content_id("deposit", fields),
            source_version=source.id,
            deposited_by=deposited_by,
            deposited_at=source.acquired_at,
            acquisition_method=acquisition_method,
            original_filename=resolved.name,
            original_locator=original_locator,
            **effective.model_dump(),
            rights_basis=rights_basis,
            provenance_note=provenance_note,
            policy_version=self.policy.version,
            access_enforcement="deployment_boundary_only",
        )
        return DepositResult(
            source=source,
            deposit=deposit,
            record_hash=self.store.put_record("deposit", deposit),
        )

    def _effective(self, overrides: DepositOverrides) -> DepositDefaults:
        values = self.policy.defaults.model_dump()
        values.update(
            {name: value for name, value in overrides.model_dump().items() if value is not None}
        )
        return DepositDefaults.model_validate(values)
