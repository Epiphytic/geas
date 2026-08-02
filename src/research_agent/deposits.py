from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from coincurve import PublicKeyXOnly
from pydantic import Field, model_validator

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


class InformationStatus(StrEnum):
    UNKNOWN = "unknown"
    DECLARED = "declared"


class PermissionStatus(StrEnum):
    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    NOT_ALLOWED = "not_allowed"


class UsagePermissions(StrictModel):
    archive: PermissionStatus = PermissionStatus.UNKNOWN
    quote: PermissionStatus = PermissionStatus.UNKNOWN
    transform: PermissionStatus = PermissionStatus.UNKNOWN
    redistribute_original: PermissionStatus = PermissionStatus.UNKNOWN


class UsagePermissionOverrides(StrictModel):
    archive: PermissionStatus | None = None
    quote: PermissionStatus | None = None
    transform: PermissionStatus | None = None
    redistribute_original: PermissionStatus | None = None


class NostrClaim(StrEnum):
    OWNERSHIP = "ownership"
    AUTHORSHIP = "authorship"
    PUBLICATION = "publication"


class NostrEvent(StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    pubkey: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: int = Field(ge=0)
    kind: int = Field(ge=0, le=65535)
    tags: tuple[tuple[str, ...], ...]
    content: str
    sig: str = Field(pattern=r"^[0-9a-f]{128}$")

    @model_validator(mode="after")
    def tags_are_nonempty(self) -> NostrEvent:
        if any(not tag for tag in self.tags):
            raise ValueError("Nostr event tags must contain at least a tag name")
        return self


class NostrSignatureEvidence(StrictModel):
    event: NostrEvent
    claimed_relation: NostrClaim
    bound_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_tag: Literal["x", "ox"]
    event_id_verified: Literal[True]
    signature_verified: Literal[True]
    content_binding_verified: Literal[True]
    verifier: Literal["nip01+nip94/coincurve-1"]

    @model_validator(mode="after")
    def cryptographic_claim_is_valid(self) -> NostrSignatureEvidence:
        binding_tag = validated_nostr_binding_tag(self.event, self.bound_content_sha256)
        if binding_tag != self.binding_tag:
            raise ValueError("recorded Nostr binding tag does not match verified event")
        return self


class DepositDefaults(StrictModel):
    scope_label: str = Field(min_length=1)
    index_content: bool
    include_in_ontology: bool
    model_route: ModelRoute
    redistribution_status: RedistributionStatus
    usage_permissions: UsagePermissions
    retention_policy: str = Field(min_length=1)

    @model_validator(mode="after")
    def redistribution_fields_agree(self) -> DepositDefaults:
        expected = {
            RedistributionStatus.UNKNOWN: PermissionStatus.UNKNOWN,
            RedistributionStatus.NOT_GRANTED: PermissionStatus.NOT_ALLOWED,
            RedistributionStatus.GRANTED: PermissionStatus.ALLOWED,
        }[self.redistribution_status]
        if self.usage_permissions.redistribute_original is not expected:
            raise ValueError(
                "redistribution_status and redistribute_original permission must agree"
            )
        return self


class DepositOverrides(StrictModel):
    scope_label: str | None = Field(default=None, min_length=1)
    index_content: bool | None = None
    include_in_ontology: bool | None = None
    model_route: ModelRoute | None = None
    redistribution_status: RedistributionStatus | None = None
    usage_permissions: UsagePermissionOverrides | None = None
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
    authors: tuple[str, ...] = ()
    authorship_status: InformationStatus
    scope_label: str
    index_content: bool
    include_in_ontology: bool
    model_route: ModelRoute
    redistribution_status: RedistributionStatus
    usage_permissions: UsagePermissions
    license: str | None = None
    license_status: InformationStatus
    usage_conditions: tuple[str, ...] = ()
    usage_conditions_status: InformationStatus
    retention_policy: str
    rights_basis: str | None = None
    rights_basis_status: InformationStatus
    provenance_note: str | None = None
    provenance_status: InformationStatus
    nostr_signature_evidence: tuple[NostrSignatureEvidence, ...] = ()
    policy_version: int
    access_enforcement: Literal["deployment_boundary_only"]

    @model_validator(mode="after")
    def metadata_and_evidence_are_consistent(self) -> DepositRecord:
        pairs = (
            ("authorship", self.authorship_status, self.authors),
            ("license", self.license_status, self.license),
            ("usage conditions", self.usage_conditions_status, self.usage_conditions),
            ("rights basis", self.rights_basis_status, self.rights_basis),
            ("provenance", self.provenance_status, self.provenance_note or self.original_locator),
        )
        for label, status, value in pairs:
            expected = InformationStatus.DECLARED if value else InformationStatus.UNKNOWN
            if status is not expected:
                raise ValueError(f"{label} status does not match its recorded value")
        source_digest = self.source_version.removeprefix("source:sha256:")
        if any(
            evidence.bound_content_sha256 != source_digest
            for evidence in self.nostr_signature_evidence
        ):
            raise ValueError("Nostr evidence must bind the deposit source version")
        DepositDefaults(
            scope_label=self.scope_label,
            index_content=self.index_content,
            include_in_ontology=self.include_in_ontology,
            model_route=self.model_route,
            redistribution_status=self.redistribution_status,
            usage_permissions=self.usage_permissions,
            retention_policy=self.retention_policy,
        )
        return self


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
        authors: tuple[str, ...] = (),
        usage_conditions: tuple[str, ...] = (),
        rights_basis: str | None = None,
        provenance_note: str | None = None,
        nostr_evidence: tuple[tuple[NostrEvent, NostrClaim], ...] = (),
        overrides: DepositOverrides | None = None,
    ) -> DepositResult:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {resolved}")
        if not deposited_by.strip():
            raise ValueError("deposited_by is required")
        authors = self._clean_values(authors, "author")
        usage_conditions = self._clean_values(usage_conditions, "usage condition")
        license = self._clean_optional(license)
        rights_basis = self._clean_optional(rights_basis)
        provenance_note = self._clean_optional(provenance_note)
        content = resolved.read_bytes()
        source_digest = hashlib.sha256(content).hexdigest()
        verified_nostr = tuple(
            verify_nostr_file_evidence(event, claim, source_digest)
            for event, claim in nostr_evidence
        )
        effective = self._effective(overrides or DepositOverrides())
        source = self.store.ingest_bytes(
            content,
            source_uri=source_uri or resolved.as_uri(),
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
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
            "authors": authors,
            "authorship_status": self._status(authors),
            **effective.model_dump(mode="json"),
            "license": license,
            "license_status": self._status(license),
            "usage_conditions": usage_conditions,
            "usage_conditions_status": self._status(usage_conditions),
            "rights_basis": rights_basis,
            "rights_basis_status": self._status(rights_basis),
            "provenance_note": provenance_note,
            "provenance_status": self._status(provenance_note or original_locator),
            "nostr_signature_evidence": verified_nostr,
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
            authors=authors,
            authorship_status=self._status(authors),
            **effective.model_dump(),
            license=license,
            license_status=self._status(license),
            usage_conditions=usage_conditions,
            usage_conditions_status=self._status(usage_conditions),
            rights_basis=rights_basis,
            rights_basis_status=self._status(rights_basis),
            provenance_note=provenance_note,
            provenance_status=self._status(provenance_note or original_locator),
            nostr_signature_evidence=verified_nostr,
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
        override_values = overrides.model_dump()
        permission_overrides = override_values.pop("usage_permissions")
        redistribution_override = override_values.get("redistribution_status")
        explicit_redistribution_permission = (permission_overrides or {}).get(
            "redistribute_original"
        )
        status_to_permission = {
            RedistributionStatus.UNKNOWN: PermissionStatus.UNKNOWN,
            RedistributionStatus.NOT_GRANTED: PermissionStatus.NOT_ALLOWED,
            RedistributionStatus.GRANTED: PermissionStatus.ALLOWED,
        }
        permission_to_status = {value: key for key, value in status_to_permission.items()}
        if redistribution_override is not None and explicit_redistribution_permission is None:
            permission_overrides = permission_overrides or {}
            permission_overrides["redistribute_original"] = status_to_permission[
                redistribution_override
            ]
        elif explicit_redistribution_permission is not None and redistribution_override is None:
            override_values["redistribution_status"] = permission_to_status[
                explicit_redistribution_permission
            ]
        values.update({name: value for name, value in override_values.items() if value is not None})
        if permission_overrides is not None:
            values["usage_permissions"].update(
                {name: value for name, value in permission_overrides.items() if value is not None}
            )
        return DepositDefaults.model_validate(values)

    @staticmethod
    def _status(value: object) -> InformationStatus:
        return InformationStatus.DECLARED if value else InformationStatus.UNKNOWN

    @staticmethod
    def _clean_optional(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @classmethod
    def _clean_values(cls, values: tuple[str, ...], label: str) -> tuple[str, ...]:
        cleaned = tuple(filter(None, (cls._clean_optional(value) for value in values)))
        if len(cleaned) != len(values):
            raise ValueError(f"{label} values must not be blank")
        return cleaned


def nostr_event_id(event: NostrEvent) -> str:
    serialized = json.dumps(
        [0, event.pubkey, event.created_at, event.kind, event.tags, event.content],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def verify_nostr_file_evidence(
    event: NostrEvent,
    claim: NostrClaim,
    content_sha256: str,
) -> NostrSignatureEvidence:
    binding_tag = validated_nostr_binding_tag(event, content_sha256)
    return NostrSignatureEvidence(
        event=event,
        claimed_relation=claim,
        bound_content_sha256=content_sha256,
        binding_tag=binding_tag,
        event_id_verified=True,
        signature_verified=True,
        content_binding_verified=True,
        verifier="nip01+nip94/coincurve-1",
    )


def validated_nostr_binding_tag(
    event: NostrEvent,
    content_sha256: str,
) -> Literal["x", "ox"]:
    calculated_id = nostr_event_id(event)
    if calculated_id != event.id:
        raise ValueError("Nostr event id does not match its NIP-01 serialization")
    try:
        signature_valid = PublicKeyXOnly(bytes.fromhex(event.pubkey)).verify(
            bytes.fromhex(event.sig),
            bytes.fromhex(event.id),
        )
    except ValueError as exc:
        raise ValueError("Nostr event contains an invalid public key or signature") from exc
    if not signature_valid:
        raise ValueError("Nostr event signature is invalid")
    if event.kind != 1063:
        raise ValueError("file evidence must be a NIP-94 kind 1063 event")
    matching_tags = [
        tag[0]
        for tag in event.tags
        if len(tag) >= 2 and tag[0] in {"x", "ox"} and tag[1] == content_sha256
    ]
    if not matching_tags:
        raise ValueError("Nostr event does not bind the deposited file hash with x or ox")
    return "x" if "x" in matching_tags else "ox"
