from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.deposits import InformationStatus, UsagePermissions
from research_agent.knowledge import (
    ClaimProposal,
    Concept,
    ControversyProposal,
    EvidenceProposal,
    GapProposal,
    KnowledgeImporter,
    KnowledgeImportReceipt,
    KnowledgePack,
)
from research_agent.models import StrictModel, content_id
from research_agent.parsing import ParsedDocumentManager, ParsedIngestReceipt
from research_agent.store import ImmutableStore


class BundleSource(StrictModel):
    key: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquired_at: datetime
    original_locator: str
    title: str = Field(min_length=1)
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    published_at: datetime | None = None
    media_type: str | None = None
    license: str | None = None
    usage_conditions: tuple[str, ...] = ()
    rights_basis: str | None = None
    provenance_note: str = Field(min_length=1)

    @field_validator("authors", "usage_conditions")
    @classmethod
    def clean_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("source metadata values must not be blank")
        return tuple(dict.fromkeys(value.strip() for value in values))


class BundleEvidence(StrictModel):
    key: str
    source_key: str
    exact: str = Field(min_length=1)
    prefix: str | None = None
    suffix: str | None = None


class KnowledgeBundle(StrictModel):
    version: int = Field(ge=1, le=1)
    topic: str
    topic_concept_id: str
    recorded_at: datetime
    sources: tuple[BundleSource, ...] = Field(min_length=1)
    concepts: tuple[Concept, ...]
    evidence: tuple[BundleEvidence, ...]
    claims: tuple[ClaimProposal, ...]
    controversies: tuple[ControversyProposal, ...] = ()
    gaps: tuple[GapProposal, ...] = ()

    @classmethod
    def from_yaml(cls, path: Path) -> KnowledgeBundle:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    @model_validator(mode="after")
    def bundle_keys_are_valid(self) -> KnowledgeBundle:
        source_keys = [item.key for item in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("bundle source keys must be unique")
        unknown = sorted(
            {item.source_key for item in self.evidence} - set(source_keys)
        )
        if unknown:
            raise ValueError(f"bundle evidence has unknown source keys: {', '.join(unknown)}")
        return self


class SourceMetadata(StrictModel):
    id: str
    source_version_id: str
    original_locator: str
    title: str
    authors: tuple[str, ...]
    authorship_status: InformationStatus
    publisher: str | None = None
    published_at: datetime | None = None
    license: str | None = None
    license_status: InformationStatus
    usage_conditions: tuple[str, ...]
    usage_conditions_status: InformationStatus
    usage_permissions: UsagePermissions
    rights_basis: str | None = None
    rights_basis_status: InformationStatus
    provenance_note: str
    provenance_status: InformationStatus
    recorded_at: datetime
    recorded_by: str

    @model_validator(mode="after")
    def statuses_match_values(self) -> SourceMetadata:
        pairs = (
            (self.authorship_status, self.authors),
            (self.license_status, self.license),
            (self.usage_conditions_status, self.usage_conditions),
            (self.rights_basis_status, self.rights_basis),
            (self.provenance_status, self.provenance_note or self.original_locator),
        )
        for status, value in pairs:
            expected = InformationStatus.DECLARED if value else InformationStatus.UNKNOWN
            if status is not expected:
                raise ValueError("source metadata status does not match its value")
        return self


class BundleImportReceipt(StrictModel):
    bundle_path: str
    topic: str
    imported_by: str
    source_metadata_ids: tuple[str, ...]
    parse_receipts: tuple[ParsedIngestReceipt, ...]
    knowledge_receipt: KnowledgeImportReceipt
    record_hashes: dict[str, tuple[str, ...]]


class KnowledgeBundleImporter:
    version = "knowledge-bundle-importer/1"

    def __init__(self, *, store: ImmutableStore) -> None:
        self.store = store

    def import_bundle(
        self,
        bundle_path: Path,
        *,
        imported_by: str,
    ) -> BundleImportReceipt:
        resolved_bundle = bundle_path.resolve(strict=True)
        bundle_root = resolved_bundle.parent
        bundle = KnowledgeBundle.from_yaml(resolved_bundle)
        self.store.initialize()
        parse_receipts: list[ParsedIngestReceipt] = []
        metadata_records: list[SourceMetadata] = []
        digest_by_key: dict[str, str] = {}
        parse_hashes: dict[str, list[str]] = {}
        for source in bundle.sources:
            source_path = self._source_path(bundle_root, source.path)
            content = source_path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != source.expected_sha256:
                raise ValueError(f"bundle source hash mismatch: {source.key}")
            media_type = (
                source.media_type
                or mimetypes.guess_type(source_path.name)[0]
                or "application/octet-stream"
            )
            parsed = ParsedDocumentManager(
                store=self.store,
                clock=lambda: bundle.recorded_at,
            ).ingest(
                content,
                source_uri=f"bundle:{bundle_root.name}/{source.path}",
                media_type=media_type,
                connector_id="connector:maintained-bundle",
                license=source.license,
                acquired_at=source.acquired_at,
            )
            parse_receipts.append(parsed)
            digest_by_key[source.key] = digest
            for kind, hashes in parsed.record_hashes.items():
                parse_hashes.setdefault(kind, []).extend(hashes)
            fields = {
                "source_version_id": parsed.original_source_version_id,
                "original_locator": source.original_locator,
                "title": source.title,
                "authors": source.authors,
                "authorship_status": self._status(source.authors),
                "publisher": source.publisher,
                "published_at": source.published_at,
                "license": source.license,
                "license_status": self._status(source.license),
                "usage_conditions": source.usage_conditions,
                "usage_conditions_status": self._status(source.usage_conditions),
                "usage_permissions": UsagePermissions(),
                "rights_basis": source.rights_basis,
                "rights_basis_status": self._status(source.rights_basis),
                "provenance_note": source.provenance_note,
                "provenance_status": InformationStatus.DECLARED,
                "recorded_at": bundle.recorded_at,
                "recorded_by": imported_by,
            }
            metadata_records.append(
                SourceMetadata(
                    id=content_id("source-metadata", fields),
                    **fields,
                )
            )
        pack = KnowledgePack(
            version=1,
            topic=bundle.topic,
            topic_concept_id=bundle.topic_concept_id,
            concepts=bundle.concepts,
            evidence=tuple(
                EvidenceProposal(
                    key=item.key,
                    source_content_sha256=digest_by_key[item.source_key],
                    exact=item.exact,
                    prefix=item.prefix,
                    suffix=item.suffix,
                )
                for item in bundle.evidence
            ),
            claims=bundle.claims,
            controversies=bundle.controversies,
            gaps=bundle.gaps,
            inspect_source_sha256s=tuple(sorted(digest_by_key.values())),
        )
        knowledge_receipt = KnowledgeImporter(
            store=self.store,
            clock=lambda: bundle.recorded_at,
        ).import_pack(pack, imported_by=imported_by)
        metadata_hashes = tuple(
            self.store.put_record("source-metadata", item)
            for item in metadata_records
        )
        record_hashes = {
            kind: tuple(sorted(set(hashes)))
            for kind, hashes in sorted(parse_hashes.items())
        }
        record_hashes["source-metadata"] = metadata_hashes
        record_hashes["knowledge-import-receipt"] = (
            self.store.put_record("knowledge-import-receipt", knowledge_receipt),
        )
        return BundleImportReceipt(
            bundle_path=str(resolved_bundle),
            topic=bundle.topic,
            imported_by=imported_by,
            source_metadata_ids=tuple(item.id for item in metadata_records),
            parse_receipts=tuple(parse_receipts),
            knowledge_receipt=knowledge_receipt,
            record_hashes=record_hashes,
        )

    @staticmethod
    def _source_path(bundle_root: Path, value: str) -> Path:
        declared = PurePosixPath(value)
        if declared.is_absolute() or ".." in declared.parts:
            raise ValueError("bundle source paths must be confined relative paths")
        candidate = bundle_root / Path(*declared.parts)
        if candidate.is_symlink():
            raise ValueError("bundle source files must not be symbolic links")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(bundle_root):
            raise ValueError("bundle source path escapes its bundle")
        return resolved

    @staticmethod
    def _status(value: object) -> InformationStatus:
        return InformationStatus.DECLARED if value else InformationStatus.UNKNOWN
