from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from research_agent.bundles import SourceMetadata
from research_agent.citations import (
    BibliographicReference,
    CitationDerivation,
    IdentifierKind,
    ResearchIdentifier,
    normalize_research_identifier,
)
from research_agent.discovery import CoverageRun, DiscoveryHit, OpenAccessResolution
from research_agent.extraction import ValidatedExtractionProposal
from research_agent.knowledge import (
    Concept,
    Controversy,
    KnowledgeGap,
    TopicSourceAssociation,
)
from research_agent.models import (
    Claim,
    EvidenceFragment,
    SourceVersion,
    StrictModel,
    ThreatAssessment,
    ThreatObservation,
)
from research_agent.parsing import TextDerivation
from research_agent.store import ImmutableStore
from research_agent.structure import AnchorKind, StructuralAnchor, StructuralDerivation
from research_agent.truth import SQLiteProjectionGuard, TruthManager, TruthSnapshot


class QueryRecordType(StrEnum):
    CONCEPT = "concept"
    CLAIM = "claim"
    EVIDENCE = "evidence"
    CONTROVERSY = "controversy"
    GAP = "gap"
    SOURCE = "source"
    THREAT = "threat"
    DISCOVERY = "discovery"
    RESOLUTION = "resolution"
    DOCUMENT = "document"
    ANCHOR = "anchor"
    IDENTIFIER = "identifier"
    REFERENCE = "reference"
    SOURCE_METADATA = "source_metadata"
    PROPOSAL = "proposal"


class KnowledgeQueryPlan(StrictModel):
    question: str
    tokens: tuple[str, ...]
    fts_expression: str
    record_types: tuple[QueryRecordType, ...]
    limit: int = Field(ge=1, le=1000)
    sql: str
    parameters: tuple[str | int, ...]
    compiler_version: str


class KnowledgeThreatContext(StrictModel):
    id: str
    threat_type: str
    status: str
    severity: str


class KnowledgeHit(StrictModel):
    record_type: QueryRecordType
    record_id: str
    title: str
    snippet: str
    rank: float
    source_version_id: str | None = None
    derived_source_version_id: str | None = None
    source_uri: str | None = None
    trust_zone: str | None = None
    threat_observation_ids: tuple[str, ...] = ()
    threats: tuple[KnowledgeThreatContext, ...] = ()
    anchor_kind: str | None = None
    anchor_start: int | None = None
    anchor_end: int | None = None
    anchor_page_number: int | None = None
    anchor_parent_id: str | None = None
    anchor_synthetic: bool | None = None
    identifier_kind: str | None = None
    identifier_value: str | None = None
    canonical_locator: str | None = None
    reference_relation: str | None = None
    reference_signal: str | None = None
    resolved_discovery_hit_ids: tuple[str, ...] = ()
    resolved_open_access_resolution_ids: tuple[str, ...] = ()
    proposal_provider: str | None = None
    proposal_model: str | None = None
    proposal_review_state: str | None = None
    proposal_commit_authority: str | None = None


class KnowledgeQueryResult(StrictModel):
    plan: KnowledgeQueryPlan
    hits: tuple[KnowledgeHit, ...]
    truncated: bool
    projection_snapshot_id: str


class IdentifierView(StrictModel):
    identifier_id: str
    kind: IdentifierKind
    value: str
    canonical_locator: str
    references: tuple[dict[str, Any], ...]
    discovery_hits: tuple[dict[str, Any], ...]
    open_access_resolutions: tuple[dict[str, Any], ...]
    projection_snapshot_id: str


class TopicView(StrictModel):
    topic_concept_id: str
    as_of: datetime | None = None
    descendant_concept_ids: tuple[str, ...]
    concepts: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    controversies: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    threats: tuple[dict[str, Any], ...]
    references: tuple[dict[str, Any], ...] = ()
    query_mode: str = "exact_recursive_provenance"
    projection_snapshot_id: str


class ProjectionBuildResult(StrictModel):
    database: str
    snapshot_id: str
    schema_version: int
    builder_version: str
    counts: dict[str, int]
    projection_digest: str


_TOKEN = re.compile(r"[\w][\w.-]*", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "which",
        "with",
    }
)


class DeterministicQueryCompiler:
    version = "deterministic-local-query/1"
    sql = """
        SELECT record_type, record_id, title,
               snippet(knowledge_fts, 3, '[', ']', ' … ', 24) AS excerpt,
               bm25(knowledge_fts, 8.0, 1.0, 2.0, 1.0) AS score
        FROM knowledge_fts
        WHERE knowledge_fts MATCH ?
          AND record_type IN ({type_placeholders})
        ORDER BY score, record_type, record_id
        LIMIT ?
    """

    def compile(
        self,
        question: str,
        *,
        record_types: tuple[QueryRecordType, ...] = tuple(QueryRecordType),
        limit: int = 25,
    ) -> KnowledgeQueryPlan:
        if not question.strip():
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 1000:
            raise ValueError("query limit must be between 1 and 1000")
        normalized_types = tuple(sorted(set(record_types), key=lambda item: item.value))
        if not normalized_types:
            raise ValueError("at least one query record type is required")
        tokens = tuple(
            sorted(
                {
                    match.group(0).casefold()
                    for match in _TOKEN.finditer(question)
                    if match.group(0).casefold() not in _STOPWORDS
                }
            )
        )
        if not tokens:
            raise ValueError("query contains no searchable terms")
        escaped = tuple(token.replace('"', '""') for token in tokens)
        expression = " OR ".join(f'"{token}"*' for token in escaped)
        placeholders = ",".join("?" for _ in normalized_types)
        sql = self.sql.format(type_placeholders=placeholders).strip()
        parameters: tuple[str | int, ...] = (
            expression,
            *(item.value for item in normalized_types),
            limit + 1,
        )
        return KnowledgeQueryPlan(
            question=question,
            tokens=tokens,
            fts_expression=expression,
            record_types=normalized_types,
            limit=limit,
            sql=sql,
            parameters=parameters,
            compiler_version=self.version,
        )


class SQLiteKnowledgeProjection:
    schema_version = 8
    builder_version = "sqlite-knowledge-projection/9"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        workspace_root: Path,
        vocabulary_path: Path | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.vocabulary_path = vocabulary_path.resolve() if vocabulary_path else None

    def build(
        self,
        database: Path,
        *,
        snapshot: TruthSnapshot,
        truth_manager: TruthManager,
    ) -> ProjectionBuildResult:
        before = truth_manager.verify(snapshot)
        if not before.clean:
            raise ValueError("canonical truth does not match the selected snapshot")
        database = database.resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        temporary = database.with_name(f".{database.name}.{os.getpid()}.tmp")
        if temporary.exists():
            temporary.unlink()
        try:
            counts = self._build_database(temporary)
            after = truth_manager.verify(snapshot)
            if not after.clean:
                raise ValueError("canonical truth changed while the projection was building")
            stamp = SQLiteProjectionGuard().stamp(
                temporary,
                snapshot,
                schema_version=self.schema_version,
                builder_version=self.builder_version,
            )
            os.replace(temporary, database)
        finally:
            if temporary.exists():
                temporary.unlink()
        return ProjectionBuildResult(
            database=str(database),
            snapshot_id=snapshot.id,
            schema_version=self.schema_version,
            builder_version=self.builder_version,
            counts=counts,
            projection_digest=stamp.projection_digest,
        )

    def _build_database(self, database: Path) -> dict[str, int]:
        concepts = self._concepts()

        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            self._create_schema(connection)
            counts = self._insert(
                connection,
                concepts=concepts,
                sources=self._sources(),
                topic_sources=(
                    TopicSourceAssociation.model_validate(value)
                    for value in self.store.iter_records("topic-source")
                ),
                source_metadata=(
                    SourceMetadata.model_validate(value)
                    for value in self.store.iter_records("source-metadata")
                ),
                fragments=self._evidence_fragments(),
                claims=(Claim.model_validate(value) for value in self.store.iter_records("claim")),
                controversies=(
                    Controversy.model_validate(value)
                    for value in self.store.iter_records("controversy")
                ),
                gaps=(
                    KnowledgeGap.model_validate(value)
                    for value in self.store.iter_records("knowledge-gap")
                ),
                observations=self._threat_observations(),
                assessments=(
                    ThreatAssessment.model_validate(value)
                    for value in self.store.iter_records("threat-assessment")
                ),
                coverage=(
                    CoverageRun.model_validate(value)
                    for value in self.store.iter_records("coverage-run")
                ),
                discovery_hits=(
                    DiscoveryHit.model_validate(value)
                    for value in self.store.iter_records("discovery-hit")
                ),
                resolutions=(
                    OpenAccessResolution.model_validate(value)
                    for value in self.store.iter_records("open-access-resolution")
                ),
                derivations=(
                    TextDerivation.model_validate(value)
                    for value in self.store.iter_records("text-derivation")
                ),
                structural_derivations=(
                    StructuralDerivation.model_validate(value)
                    for value in self.store.iter_records("structural-derivation")
                ),
                structural_anchors=(
                    StructuralAnchor.model_validate(value)
                    for value in self.store.iter_records("structural-anchor")
                ),
                citation_derivations=(
                    CitationDerivation.model_validate(value)
                    for value in self.store.iter_records("citation-derivation")
                ),
                research_identifiers=(
                    ResearchIdentifier.model_validate(value)
                    for value in self.store.iter_records("research-identifier")
                ),
                bibliographic_references=(
                    BibliographicReference.model_validate(value)
                    for value in self.store.iter_records("bibliographic-reference")
                ),
                extraction_proposals=(
                    ValidatedExtractionProposal.model_validate(value)
                    for value in self.store.iter_records("extraction-proposal")
                ),
            )
            connection.execute("PRAGMA optimize")
            connection.commit()
            invalid = connection.execute("PRAGMA foreign_key_check").fetchall()
            if invalid:
                raise ValueError(f"projection has foreign-key violations: {invalid!r}")
        return counts

    def _concepts(self) -> tuple[Concept, ...]:
        records: dict[str, Concept] = {}
        for value in self.store.iter_records("concept"):
            item = Concept.model_validate(value)
            existing = records.get(item.id)
            if existing is not None and existing != item:
                raise ValueError(
                    f"conflicting canonical concept records share ID {item.id}"
                )
            records[item.id] = item
        vocabulary_path = self.vocabulary_path or (
            self.workspace_root / "config/query-vocabulary.yaml"
        )
        if vocabulary_path.exists():
            vocabulary = yaml.safe_load(vocabulary_path.read_text()).get("concepts", {})
            for concept_id, synonyms in sorted(vocabulary.items()):
                if concept_id not in records:
                    label = concept_id.rsplit(":", 1)[-1].replace("-", " ").title()
                    records[concept_id] = Concept(
                        id=concept_id,
                        label=label,
                        description=f"Controlled vocabulary concept for {label}.",
                        synonyms=tuple(synonyms),
                        recorded_at=datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
                        recorded_by="system:controlled-vocabulary",
                    )
        return tuple(records[key] for key in sorted(records))

    def _sources(self) -> tuple[SourceVersion, ...]:
        grouped: dict[str, list[SourceVersion]] = {}
        for value in self.store.iter_records("source-version"):
            item = SourceVersion.model_validate(value)
            grouped.setdefault(item.id, []).append(item)
        selected = []
        for source_id, items in sorted(grouped.items()):
            digests = {item.content_sha256 for item in items}
            if len(digests) != 1:
                raise ValueError(
                    f"conflicting canonical source records share ID {source_id}"
                )
            selected.append(
                min(
                    items,
                    key=lambda item: (
                        item.connector_id == "connector:maintained-bundle",
                        item.license is None,
                        json.dumps(
                            item.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            )
        return tuple(selected)

    def _evidence_fragments(self) -> tuple[EvidenceFragment, ...]:
        """Select one projection row for each content-derived evidence identity."""
        grouped: dict[str, list[EvidenceFragment]] = {}
        for value in self.store.iter_records("evidence-fragment"):
            item = EvidenceFragment.model_validate(value)
            grouped.setdefault(item.id, []).append(item)
        selected = []
        for fragment_id, items in sorted(grouped.items()):
            identities = {
                json.dumps(
                    item.model_dump(mode="json", exclude={"created_at"}),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for item in items
            }
            if len(identities) != 1:
                raise ValueError(
                    f"conflicting canonical evidence records share ID {fragment_id}"
                )
            # Evidence identity intentionally excludes observation time. Preserve
            # the earliest immutable observation in the disposable projection.
            selected.append(
                min(
                    items,
                    key=lambda item: (
                        item.created_at,
                        json.dumps(
                            item.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            )
        return tuple(selected)

    def _threat_observations(self) -> tuple[ThreatObservation, ...]:
        """Select one projection row for each content-derived threat identity."""
        grouped: dict[str, list[ThreatObservation]] = {}
        for value in self.store.iter_records("threat-observation"):
            item = ThreatObservation.model_validate(value)
            grouped.setdefault(item.id, []).append(item)
        selected = []
        for observation_id, items in sorted(grouped.items()):
            identities = {
                json.dumps(
                    item.model_dump(mode="json", exclude={"detected_at"}),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for item in items
            }
            if len(identities) != 1:
                raise ValueError(
                    "conflicting canonical threat observations share ID "
                    f"{observation_id}"
                )
            selected.append(min(items, key=lambda item: item.detected_at))
        return tuple(selected)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE concept (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                description TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                recorded_by TEXT NOT NULL,
                review_state TEXT NOT NULL
            );
            CREATE TABLE concept_parent (
                concept_id TEXT NOT NULL REFERENCES concept(id),
                parent_id TEXT NOT NULL REFERENCES concept(id),
                PRIMARY KEY (concept_id, parent_id)
            );
            CREATE TABLE concept_synonym (
                concept_id TEXT NOT NULL REFERENCES concept(id),
                synonym TEXT NOT NULL,
                PRIMARY KEY (concept_id, synonym)
            );
            CREATE TABLE source (
                id TEXT PRIMARY KEY,
                source_uri TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                connector_id TEXT NOT NULL,
                trust_zone TEXT NOT NULL,
                license TEXT
            );
            CREATE TABLE topic_source_association (
                id TEXT PRIMARY KEY,
                topic_concept_id TEXT NOT NULL REFERENCES concept(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                roles_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                recorded_by TEXT NOT NULL
            );
            CREATE TABLE source_metadata (
                id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL REFERENCES source(id),
                original_locator TEXT NOT NULL,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                authorship_status TEXT NOT NULL,
                publisher TEXT,
                published_at TEXT,
                license TEXT,
                license_status TEXT NOT NULL,
                usage_conditions_json TEXT NOT NULL,
                usage_conditions_status TEXT NOT NULL,
                usage_permissions_json TEXT NOT NULL,
                rights_basis TEXT,
                rights_basis_status TEXT NOT NULL,
                provenance_note TEXT NOT NULL,
                provenance_status TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                recorded_by TEXT NOT NULL
            );
            CREATE TABLE evidence_fragment (
                id TEXT PRIMARY KEY,
                source_version TEXT NOT NULL REFERENCES source(id),
                selector_type TEXT NOT NULL,
                exact_text TEXT,
                prefix_text TEXT,
                suffix_text TEXT,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE claim (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_json TEXT NOT NULL,
                qualifiers_json TEXT NOT NULL,
                stance TEXT NOT NULL,
                epistemic_status TEXT NOT NULL,
                asserted_by TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                recorded_at TEXT NOT NULL,
                review_state TEXT NOT NULL
            );
            CREATE TABLE claim_evidence (
                claim_id TEXT NOT NULL REFERENCES claim(id),
                evidence_id TEXT NOT NULL REFERENCES evidence_fragment(id),
                PRIMARY KEY (claim_id, evidence_id)
            );
            CREATE TABLE controversy (
                id TEXT PRIMARY KEY,
                topic_concept_id TEXT NOT NULL REFERENCES concept(id),
                question TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                recorded_by TEXT NOT NULL,
                review_state TEXT NOT NULL
            );
            CREATE TABLE controversy_claim (
                controversy_id TEXT NOT NULL REFERENCES controversy(id),
                claim_id TEXT NOT NULL REFERENCES claim(id),
                PRIMARY KEY (controversy_id, claim_id)
            );
            CREATE TABLE knowledge_gap (
                id TEXT PRIMARY KEY,
                topic_concept_id TEXT NOT NULL REFERENCES concept(id),
                question TEXT NOT NULL,
                kind TEXT NOT NULL,
                rationale TEXT NOT NULL,
                priority INTEGER NOT NULL,
                status TEXT NOT NULL,
                freshness_deadline TEXT,
                recorded_at TEXT NOT NULL,
                recorded_by TEXT NOT NULL,
                review_state TEXT NOT NULL
            );
            CREATE TABLE gap_claim (
                gap_id TEXT NOT NULL REFERENCES knowledge_gap(id),
                claim_id TEXT NOT NULL REFERENCES claim(id),
                PRIMARY KEY (gap_id, claim_id)
            );
            CREATE TABLE gap_query_plan (
                gap_id TEXT NOT NULL REFERENCES knowledge_gap(id),
                query_plan_id TEXT NOT NULL,
                PRIMARY KEY (gap_id, query_plan_id)
            );
            CREATE TABLE threat_observation (
                id TEXT PRIMARY KEY,
                source_version TEXT NOT NULL REFERENCES source(id),
                evidence_fragment TEXT REFERENCES evidence_fragment(id),
                threat_type TEXT NOT NULL,
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                detector_kind TEXT NOT NULL,
                detector_id TEXT NOT NULL,
                attempted_action TEXT,
                policy_rule TEXT
            );
            CREATE TABLE threat_assessment (
                id TEXT PRIMARY KEY,
                source_version TEXT NOT NULL REFERENCES source(id),
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                assessed_at TEXT NOT NULL,
                assessor_kind TEXT NOT NULL,
                assessor_id TEXT NOT NULL,
                rationale TEXT NOT NULL
            );
            CREATE TABLE coverage_run (
                id TEXT PRIMARY KEY,
                query_plan_id TEXT NOT NULL,
                topic_branch TEXT NOT NULL,
                measured_at TEXT NOT NULL,
                freshness_deadline TEXT NOT NULL,
                accessible_count INTEGER NOT NULL,
                inaccessible_count INTEGER NOT NULL,
                metadata_only_count INTEGER NOT NULL
            );
            CREATE TABLE discovery_hit (
                id TEXT PRIMARY KEY,
                upstream_id TEXT,
                canonical_locator TEXT NOT NULL,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                publisher TEXT,
                published_at TEXT,
                media_type TEXT,
                language TEXT,
                upstream_rank INTEGER NOT NULL,
                snippet TEXT,
                discovery_run_id TEXT NOT NULL,
                acquisition_eligible INTEGER NOT NULL,
                known_entity_ids_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE open_access_resolution (
                id TEXT PRIMARY KEY,
                doi TEXT NOT NULL,
                canonical_locator TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                resolved_at TEXT NOT NULL,
                response_sha256 TEXT NOT NULL,
                is_open_access INTEGER NOT NULL,
                oa_status TEXT NOT NULL,
                title TEXT NOT NULL,
                genre TEXT,
                is_paratext INTEGER NOT NULL
            );
            CREATE TABLE open_access_location (
                resolution_id TEXT NOT NULL REFERENCES open_access_resolution(id),
                ordinal INTEGER NOT NULL,
                url TEXT NOT NULL,
                landing_page_url TEXT,
                pdf_url TEXT,
                host_type TEXT NOT NULL,
                version TEXT NOT NULL,
                license TEXT,
                license_status TEXT NOT NULL,
                evidence TEXT,
                repository_institution TEXT,
                is_best INTEGER NOT NULL,
                automatic_acquisition_eligible INTEGER NOT NULL,
                PRIMARY KEY (resolution_id, ordinal)
            );
            CREATE TABLE text_derivation (
                id TEXT PRIMARY KEY,
                original_source_version_id TEXT NOT NULL REFERENCES source(id),
                derived_source_version_id TEXT NOT NULL REFERENCES source(id),
                original_content_sha256 TEXT NOT NULL,
                derived_content_sha256 TEXT NOT NULL,
                input_media_type TEXT NOT NULL,
                output_media_type TEXT NOT NULL,
                parser_id TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                parser_runtime TEXT NOT NULL,
                extraction_scope TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                character_count INTEGER NOT NULL,
                warnings_json TEXT NOT NULL
            );
            CREATE TABLE structural_derivation (
                id TEXT PRIMARY KEY,
                text_derivation_id TEXT NOT NULL REFERENCES text_derivation(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                source_content_sha256 TEXT NOT NULL,
                input_media_type TEXT NOT NULL,
                extractor_id TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                offset_unit TEXT NOT NULL,
                anchor_counts_json TEXT NOT NULL
            );
            CREATE TABLE structural_anchor (
                id TEXT PRIMARY KEY,
                structural_derivation_id TEXT NOT NULL
                    REFERENCES structural_derivation(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                source_content_sha256 TEXT NOT NULL,
                kind TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                exact_sha256 TEXT NOT NULL,
                label TEXT,
                level INTEGER,
                parent_id TEXT REFERENCES structural_anchor(id)
                    DEFERRABLE INITIALLY DEFERRED,
                page_number INTEGER,
                synthetic INTEGER NOT NULL,
                UNIQUE (structural_derivation_id, ordinal)
            );
            CREATE TABLE citation_derivation (
                id TEXT PRIMARY KEY,
                structural_derivation_id TEXT NOT NULL
                    REFERENCES structural_derivation(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                source_content_sha256 TEXT NOT NULL,
                extractor_id TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                relation_counts_json TEXT NOT NULL
            );
            CREATE TABLE research_identifier (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                canonical_locator TEXT NOT NULL,
                UNIQUE (kind, value)
            );
            CREATE TABLE bibliographic_reference (
                id TEXT PRIMARY KEY,
                citation_derivation_id TEXT NOT NULL
                    REFERENCES citation_derivation(id),
                structural_anchor_id TEXT NOT NULL REFERENCES structural_anchor(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                source_content_sha256 TEXT NOT NULL,
                identifier_id TEXT NOT NULL REFERENCES research_identifier(id),
                relation TEXT NOT NULL,
                signal TEXT NOT NULL,
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                exact_sha256 TEXT NOT NULL
            );
            CREATE TABLE identifier_discovery_hit (
                identifier_id TEXT NOT NULL REFERENCES research_identifier(id),
                discovery_hit_id TEXT NOT NULL REFERENCES discovery_hit(id),
                match_rule TEXT NOT NULL,
                PRIMARY KEY (identifier_id, discovery_hit_id)
            );
            CREATE TABLE identifier_open_access_resolution (
                identifier_id TEXT NOT NULL REFERENCES research_identifier(id),
                resolution_id TEXT NOT NULL REFERENCES open_access_resolution(id),
                match_rule TEXT NOT NULL,
                PRIMARY KEY (identifier_id, resolution_id)
            );
            CREATE TABLE extraction_proposal (
                id TEXT PRIMARY KEY,
                extraction_request_id TEXT NOT NULL,
                structural_derivation_id TEXT NOT NULL
                    REFERENCES structural_derivation(id),
                source_version_id TEXT NOT NULL REFERENCES source(id),
                source_content_sha256 TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                proposed_at TEXT NOT NULL,
                concepts_json TEXT NOT NULL,
                claims_json TEXT NOT NULL,
                controversies_json TEXT NOT NULL,
                gaps_json TEXT NOT NULL,
                raw_output_sha256 TEXT NOT NULL,
                review_state TEXT NOT NULL,
                validator_version TEXT NOT NULL,
                commit_authority TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                record_type UNINDEXED,
                record_id UNINDEXED,
                title,
                body,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE INDEX claim_subject_idx ON claim(subject);
            CREATE INDEX topic_source_topic_idx
                ON topic_source_association(topic_concept_id, source_version_id);
            CREATE INDEX source_metadata_source_idx
                ON source_metadata(source_version_id, id);
            CREATE INDEX claim_validity_idx ON claim(valid_from, valid_until);
            CREATE INDEX controversy_topic_idx ON controversy(topic_concept_id);
            CREATE INDEX gap_topic_status_idx ON knowledge_gap(topic_concept_id, status, priority);
            CREATE INDEX threat_source_status_idx
                ON threat_observation(source_version, status, severity);
            CREATE INDEX structural_anchor_source_range_idx
                ON structural_anchor(source_version_id, start, end);
            CREATE INDEX structural_anchor_parent_idx
                ON structural_anchor(parent_id, ordinal);
            CREATE INDEX structural_anchor_kind_idx
                ON structural_anchor(structural_derivation_id, kind, ordinal);
            CREATE INDEX bibliographic_reference_identifier_idx
                ON bibliographic_reference(identifier_id, relation);
            CREATE INDEX bibliographic_reference_anchor_idx
                ON bibliographic_reference(structural_anchor_id, start);
            CREATE INDEX citation_derivation_structure_idx
                ON citation_derivation(structural_derivation_id);
            CREATE INDEX extraction_proposal_review_idx
                ON extraction_proposal(review_state, proposed_at, id);
            """
        )

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        concepts: tuple[Concept, ...],
        sources: Iterable[SourceVersion],
        topic_sources: Iterable[TopicSourceAssociation],
        source_metadata: Iterable[SourceMetadata],
        fragments: Iterable[EvidenceFragment],
        claims: Iterable[Claim],
        controversies: Iterable[Controversy],
        gaps: Iterable[KnowledgeGap],
        observations: Iterable[ThreatObservation],
        assessments: Iterable[ThreatAssessment],
        coverage: Iterable[CoverageRun],
        discovery_hits: Iterable[DiscoveryHit],
        resolutions: Iterable[OpenAccessResolution],
        derivations: Iterable[TextDerivation],
        structural_derivations: Iterable[StructuralDerivation],
        structural_anchors: Iterable[StructuralAnchor],
        citation_derivations: Iterable[CitationDerivation],
        research_identifiers: Iterable[ResearchIdentifier],
        bibliographic_references: Iterable[BibliographicReference],
        extraction_proposals: Iterable[ValidatedExtractionProposal],
    ) -> dict[str, int]:
        counts = {
            "concepts": 0,
            "sources": 0,
            "topic_source_associations": 0,
            "source_metadata": 0,
            "evidence_fragments": 0,
            "claims": 0,
            "controversies": 0,
            "knowledge_gaps": 0,
            "threat_observations": 0,
            "threat_assessments": 0,
            "coverage_runs": 0,
            "discovery_hits": 0,
            "open_access_resolutions": 0,
            "open_access_locations": 0,
            "text_derivations": 0,
            "structural_derivations": 0,
            "structural_anchors": 0,
            "citation_derivations": 0,
            "research_identifiers": 0,
            "bibliographic_references": 0,
            "identifier_discovery_links": 0,
            "identifier_open_access_links": 0,
            "extraction_proposals": 0,
        }

        def add_fts(record_type: QueryRecordType, record_id: str, title: str, body: str) -> None:
            connection.execute(
                """
                INSERT INTO knowledge_fts(record_type, record_id, title, body)
                VALUES (?, ?, ?, ?)
                """,
                (record_type, record_id, title, body),
            )

        for item in concepts:
            counts["concepts"] += 1
            connection.execute(
                "INSERT INTO concept VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.label,
                    item.description,
                    item.recorded_at.isoformat(),
                    item.recorded_by,
                    item.review_state.value,
                ),
            )
            if item.review_state.value == "accepted":
                add_fts(
                    QueryRecordType.CONCEPT,
                    item.id,
                    item.label,
                    " ".join((item.description, *item.synonyms)),
                )
        for item in concepts:
            connection.executemany(
                "INSERT INTO concept_parent VALUES (?, ?)",
                ((item.id, parent) for parent in sorted(set(item.broader))),
            )
            connection.executemany(
                "INSERT INTO concept_synonym VALUES (?, ?)",
                ((item.id, synonym) for synonym in sorted(set(item.synonyms))),
            )
        for item in sources:
            counts["sources"] += 1
            connection.execute(
                "INSERT INTO source VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.source_uri,
                    item.content_sha256,
                    item.acquired_at.isoformat(),
                    item.media_type,
                    item.byte_length,
                    item.connector_id,
                    item.trust_zone,
                    item.license,
                ),
            )
            add_fts(
                QueryRecordType.SOURCE,
                item.id,
                item.source_uri,
                f"{item.connector_id} {item.media_type} {item.license or ''}",
            )
        for item in topic_sources:
            counts["topic_source_associations"] += 1
            connection.execute(
                "INSERT INTO topic_source_association VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.topic_concept_id,
                    item.source_version_id,
                    json.dumps([role.value for role in item.roles]),
                    item.recorded_at.isoformat(),
                    item.recorded_by,
                ),
            )
        for item in source_metadata:
            counts["source_metadata"] += 1
            connection.execute(
                """
                INSERT INTO source_metadata
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.source_version_id,
                    item.original_locator,
                    item.title,
                    json.dumps(item.authors, ensure_ascii=False),
                    item.authorship_status,
                    item.publisher,
                    item.published_at.isoformat() if item.published_at else None,
                    item.license,
                    item.license_status,
                    json.dumps(item.usage_conditions, ensure_ascii=False),
                    item.usage_conditions_status,
                    json.dumps(item.usage_permissions.model_dump(mode="json"), sort_keys=True),
                    item.rights_basis,
                    item.rights_basis_status,
                    item.provenance_note,
                    item.provenance_status,
                    item.recorded_at.isoformat(),
                    item.recorded_by,
                ),
            )
            add_fts(
                QueryRecordType.SOURCE_METADATA,
                item.id,
                item.title,
                " ".join(
                    (
                        *item.authors,
                        item.publisher or "",
                        item.original_locator,
                        item.license or "unknown license",
                        *item.usage_conditions,
                        item.provenance_note,
                    )
                ),
            )
        for item in fragments:
            counts["evidence_fragments"] += 1
            connection.execute(
                "INSERT INTO evidence_fragment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.source_version,
                    item.selector.type,
                    item.selector.exact,
                    item.selector.prefix,
                    item.selector.suffix,
                    item.content_sha256,
                    item.created_at.isoformat(),
                ),
            )
            add_fts(
                QueryRecordType.EVIDENCE,
                item.id,
                f"Evidence from {item.source_version}",
                item.selector.exact or "",
            )
        for item in claims:
            counts["claims"] += 1
            connection.execute(
                "INSERT INTO claim VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.subject,
                    item.predicate,
                    json.dumps(item.object, ensure_ascii=False, sort_keys=True),
                    json.dumps(item.qualifiers, ensure_ascii=False, sort_keys=True),
                    item.stance,
                    item.epistemic_status,
                    item.asserted_by,
                    item.valid_from.isoformat() if item.valid_from else None,
                    item.valid_until.isoformat() if item.valid_until else None,
                    item.recorded_at.isoformat(),
                    item.review_state.value,
                ),
            )
            connection.executemany(
                "INSERT INTO claim_evidence VALUES (?, ?)",
                ((item.id, evidence_id) for evidence_id in sorted(set(item.evidence))),
            )
            if item.review_state.value == "accepted":
                add_fts(
                    QueryRecordType.CLAIM,
                    item.id,
                    f"{item.subject} {item.predicate}",
                    (
                        f"{item.object} {item.asserted_by} {item.stance} "
                        f"{item.epistemic_status} {json.dumps(item.qualifiers)}"
                    ),
                )
        for item in controversies:
            counts["controversies"] += 1
            connection.execute(
                "INSERT INTO controversy VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.topic_concept_id,
                    item.question,
                    item.description,
                    item.status,
                    item.recorded_at.isoformat(),
                    item.recorded_by,
                    item.review_state.value,
                ),
            )
            connection.executemany(
                "INSERT INTO controversy_claim VALUES (?, ?)",
                ((item.id, claim_id) for claim_id in sorted(set(item.claim_ids))),
            )
            if item.review_state.value == "accepted":
                add_fts(
                    QueryRecordType.CONTROVERSY,
                    item.id,
                    item.question,
                    item.description,
                )
        for item in gaps:
            counts["knowledge_gaps"] += 1
            connection.execute(
                "INSERT INTO knowledge_gap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.topic_concept_id,
                    item.question,
                    item.kind,
                    item.rationale,
                    item.priority,
                    item.status,
                    item.freshness_deadline.isoformat() if item.freshness_deadline else None,
                    item.recorded_at.isoformat(),
                    item.recorded_by,
                    item.review_state.value,
                ),
            )
            connection.executemany(
                "INSERT INTO gap_claim VALUES (?, ?)",
                (
                    (item.id, claim_id)
                    for claim_id in sorted(set(item.related_claim_ids))
                ),
            )
            connection.executemany(
                "INSERT INTO gap_query_plan VALUES (?, ?)",
                (
                    (item.id, plan_id)
                    for plan_id in sorted(set(item.searched_query_plan_ids))
                ),
            )
            if item.review_state.value == "accepted":
                add_fts(
                    QueryRecordType.GAP,
                    item.id,
                    item.question,
                    f"{item.kind} {item.rationale}",
                )
        for item in observations:
            counts["threat_observations"] += 1
            connection.execute(
                "INSERT INTO threat_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.target.source_version,
                    item.target.evidence_fragment,
                    item.threat_type,
                    item.status,
                    item.severity,
                    item.detected_at.isoformat(),
                    item.detector.kind,
                    item.detector.id,
                    item.attempted_action,
                    item.policy_rule,
                ),
            )
            add_fts(
                QueryRecordType.THREAT,
                item.id,
                item.threat_type,
                f"{item.status} {item.severity} {item.detector.id}",
            )
        for item in assessments:
            counts["threat_assessments"] += 1
            connection.execute(
                "INSERT INTO threat_assessment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.target.source_version,
                    item.status,
                    item.severity,
                    item.assessed_at.isoformat(),
                    item.assessed_by.kind,
                    item.assessed_by.id,
                    item.rationale,
                ),
            )
        for item in coverage:
            counts["coverage_runs"] += 1
            connection.execute(
                "INSERT INTO coverage_run VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.query_plan_id,
                    item.topic_branch,
                    item.measured_at.isoformat(),
                    item.freshness_deadline.isoformat(),
                    item.accessible_count,
                    item.inaccessible_count,
                    item.metadata_only_count,
                ),
            )
        for item in discovery_hits:
            counts["discovery_hits"] += 1
            connection.execute(
                """
                INSERT INTO discovery_hit
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.upstream_id,
                    item.canonical_locator,
                    item.title,
                    json.dumps(item.authors, ensure_ascii=False),
                    item.publisher,
                    item.published_at.isoformat() if item.published_at else None,
                    item.media_type,
                    item.language,
                    item.upstream_rank,
                    item.snippet,
                    item.discovery_run_id,
                    int(item.acquisition_eligible),
                    json.dumps(item.known_entity_ids, ensure_ascii=False),
                    json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            add_fts(
                QueryRecordType.DISCOVERY,
                item.id,
                item.title,
                " ".join(
                    (
                        *item.authors,
                        item.publisher or "",
                        item.canonical_locator,
                        item.snippet or "",
                        json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                    )
                ),
            )
        for item in resolutions:
            counts["open_access_resolutions"] += 1
            connection.execute(
                "INSERT INTO open_access_resolution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.doi,
                    item.canonical_locator,
                    item.connector_id,
                    item.resolved_at.isoformat(),
                    item.response_sha256,
                    int(item.is_open_access),
                    item.oa_status,
                    item.title,
                    item.genre,
                    int(item.is_paratext),
                ),
            )
            for ordinal, location in enumerate(item.locations):
                counts["open_access_locations"] += 1
                connection.execute(
                    """
                    INSERT INTO open_access_location
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        ordinal,
                        location.url,
                        location.landing_page_url,
                        location.pdf_url,
                        location.host_type,
                        location.version,
                        location.license,
                        location.license_status,
                        location.evidence,
                        location.repository_institution,
                        int(location.is_best),
                        int(location.automatic_acquisition_eligible),
                    ),
                )
            add_fts(
                QueryRecordType.RESOLUTION,
                item.id,
                item.title,
                " ".join(
                    (
                        item.doi,
                        item.oa_status,
                        item.genre or "",
                        *(
                            f"{location.url} {location.host_type} "
                            f"{location.version} {location.license or 'unknown'} "
                            f"{location.repository_institution or ''} "
                            f"{location.evidence or ''}"
                            for location in item.locations
                        ),
                    )
                ),
            )
        for item in derivations:
            counts["text_derivations"] += 1
            connection.execute(
                "INSERT INTO text_derivation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.original_source_version_id,
                    item.derived_source_version_id,
                    item.original_content_sha256,
                    item.derived_content_sha256,
                    item.input_media_type,
                    item.output_media_type,
                    item.parser_id,
                    item.parser_version,
                    item.parser_runtime,
                    item.extraction_scope,
                    item.extracted_at.isoformat(),
                    item.character_count,
                    json.dumps(item.warnings, ensure_ascii=False),
                ),
            )
            text = self.store.read_blob(item.derived_content_sha256).decode(
                "utf-8",
                errors="strict",
            )
            add_fts(
                QueryRecordType.DOCUMENT,
                item.id,
                f"Untrusted derived text from {item.original_source_version_id}",
                text,
            )
        anchors = tuple(structural_anchors)
        anchors_by_derivation: dict[str, list[StructuralAnchor]] = {}
        for anchor in anchors:
            anchors_by_derivation.setdefault(
                anchor.structural_derivation_id,
                [],
            ).append(anchor)
        for item in structural_derivations:
            selected = sorted(
                anchors_by_derivation.get(item.id, []),
                key=lambda anchor: anchor.ordinal,
            )
            if tuple(anchor.id for anchor in selected) != item.anchor_ids:
                raise ValueError("structural derivation anchor index mismatch")
            counts["structural_derivations"] += 1
            connection.execute(
                "INSERT INTO structural_derivation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.text_derivation_id,
                    item.source_version_id,
                    item.source_content_sha256,
                    item.input_media_type,
                    item.extractor_id,
                    item.extractor_version,
                    item.extracted_at.isoformat(),
                    item.offset_unit,
                    json.dumps(item.anchor_counts, sort_keys=True),
                ),
            )
        for item in anchors:
            text = self.store.read_blob(item.source_content_sha256).decode(
                "utf-8",
                errors="strict",
            )
            if item.end > len(text):
                raise ValueError("structural anchor exceeds source text")
            exact = text[item.start : item.end]
            if hashlib.sha256(exact.encode()).hexdigest() != item.exact_sha256:
                raise ValueError("structural anchor selector hash mismatch")
            counts["structural_anchors"] += 1
            connection.execute(
                "INSERT INTO structural_anchor VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.structural_derivation_id,
                    item.source_version_id,
                    item.source_content_sha256,
                    item.kind,
                    item.ordinal,
                    item.start,
                    item.end,
                    item.exact_sha256,
                    item.label,
                    item.level,
                    item.parent_id,
                    item.page_number,
                    int(item.synthetic),
                ),
            )
            if item.kind is AnchorKind.SECTION:
                add_fts(
                    QueryRecordType.ANCHOR,
                    item.id,
                    item.label or f"section {item.ordinal}",
                    item.label or "",
                )
            elif item.kind not in {AnchorKind.DOCUMENT, AnchorKind.PAGE}:
                add_fts(
                    QueryRecordType.ANCHOR,
                    item.id,
                    item.label or f"{item.kind.value} {item.ordinal}",
                    exact,
                )
        identifiers = tuple(research_identifiers)
        identifiers_by_id = {item.id: item for item in identifiers}
        for item in identifiers:
            counts["research_identifiers"] += 1
            connection.execute(
                "INSERT INTO research_identifier VALUES (?, ?, ?, ?)",
                (item.id, item.kind, item.value, item.canonical_locator),
            )
            add_fts(
                QueryRecordType.IDENTIFIER,
                item.id,
                f"{item.kind.value} {item.value}",
                item.canonical_locator,
            )
        discovery_rows = connection.execute(
            """
            SELECT id, canonical_locator, known_entity_ids_json
            FROM discovery_hit
            ORDER BY id
            """
        ).fetchall()
        resolution_rows = connection.execute(
            "SELECT id, doi FROM open_access_resolution ORDER BY id"
        ).fetchall()
        for item in identifiers:
            entity_id = f"{item.kind.value}:{item.value}"
            for discovery_id, locator, known_ids_json in discovery_rows:
                known_ids = set(json.loads(known_ids_json))
                rule = None
                if entity_id in known_ids:
                    rule = "exact_known_entity_id"
                elif item.canonical_locator == locator:
                    rule = "exact_canonical_locator"
                if rule is not None:
                    connection.execute(
                        "INSERT INTO identifier_discovery_hit VALUES (?, ?, ?)",
                        (item.id, discovery_id, rule),
                    )
                    counts["identifier_discovery_links"] += 1
            if item.kind.value == "doi":
                for resolution_id, doi in resolution_rows:
                    if item.value == doi:
                        connection.execute(
                            "INSERT INTO identifier_open_access_resolution VALUES (?, ?, ?)",
                            (item.id, resolution_id, "exact_normalized_doi"),
                        )
                        counts["identifier_open_access_links"] += 1
        references = tuple(bibliographic_references)
        references_by_derivation: dict[str, list[BibliographicReference]] = {}
        for reference in references:
            references_by_derivation.setdefault(
                reference.citation_derivation_id,
                [],
            ).append(reference)
        for item in citation_derivations:
            selected = sorted(
                references_by_derivation.get(item.id, []),
                key=lambda reference: (reference.start, reference.end, reference.identifier_id),
            )
            if tuple(reference.id for reference in selected) != item.reference_ids:
                raise ValueError("citation derivation reference index mismatch")
            if tuple(
                sorted({reference.identifier_id for reference in selected})
            ) != item.identifier_ids:
                raise ValueError("citation derivation identifier index mismatch")
            counts["citation_derivations"] += 1
            connection.execute(
                "INSERT INTO citation_derivation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.structural_derivation_id,
                    item.source_version_id,
                    item.source_content_sha256,
                    item.extractor_id,
                    item.extractor_version,
                    item.extracted_at.isoformat(),
                    json.dumps(item.relation_counts, sort_keys=True),
                ),
            )
        anchors_by_id = {item.id: item for item in anchors}
        for item in references:
            identifier = identifiers_by_id.get(item.identifier_id)
            anchor = anchors_by_id.get(item.structural_anchor_id)
            if identifier is None or anchor is None:
                raise ValueError("bibliographic reference has an unknown identifier or anchor")
            text = self.store.read_blob(item.source_content_sha256).decode(
                "utf-8",
                errors="strict",
            )
            if (
                item.end > len(text)
                or not (anchor.start <= item.start < item.end <= anchor.end)
                or hashlib.sha256(text[item.start : item.end].encode()).hexdigest()
                != item.exact_sha256
            ):
                raise ValueError("bibliographic reference selector mismatch")
            counts["bibliographic_references"] += 1
            connection.execute(
                "INSERT INTO bibliographic_reference VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.citation_derivation_id,
                    item.structural_anchor_id,
                    item.source_version_id,
                    item.source_content_sha256,
                    item.identifier_id,
                    item.relation,
                    item.signal,
                    item.start,
                    item.end,
                    item.exact_sha256,
                ),
            )
            add_fts(
                QueryRecordType.REFERENCE,
                item.id,
                f"{item.relation.value} {identifier.kind.value} {identifier.value}",
                (
                    f"{identifier.canonical_locator} {item.signal} "
                    f"{text[anchor.start:anchor.end]}"
                ),
            )
        for item in extraction_proposals:
            counts["extraction_proposals"] += 1
            concepts_json = json.dumps(
                [value.model_dump(mode="json") for value in item.concepts],
                ensure_ascii=False,
                sort_keys=True,
            )
            claims_json = json.dumps(
                [value.model_dump(mode="json") for value in item.claims],
                ensure_ascii=False,
                sort_keys=True,
            )
            controversies_json = json.dumps(
                [value.model_dump(mode="json") for value in item.controversies],
                ensure_ascii=False,
                sort_keys=True,
            )
            gaps_json = json.dumps(
                [value.model_dump(mode="json") for value in item.gaps],
                ensure_ascii=False,
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO extraction_proposal
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.extraction_request_id,
                    item.structural_derivation_id,
                    item.source_version_id,
                    item.source_content_sha256,
                    item.provider,
                    item.model,
                    item.proposed_at.isoformat(),
                    concepts_json,
                    claims_json,
                    controversies_json,
                    gaps_json,
                    item.raw_output_sha256,
                    item.review_state,
                    item.validator_version,
                    item.commit_authority,
                ),
            )
            add_fts(
                QueryRecordType.PROPOSAL,
                item.id,
                f"Proposed ontology extraction by {item.provider}:{item.model}",
                " ".join(
                    (
                        concepts_json,
                        claims_json,
                        controversies_json,
                        gaps_json,
                        item.review_state.value,
                        item.validator_version,
                    )
                ),
            )
        return counts


class KnowledgeQueryEngine:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def query(
        self,
        question: str,
        *,
        record_types: tuple[QueryRecordType, ...] = tuple(QueryRecordType),
        limit: int = 25,
    ) -> KnowledgeQueryResult:
        plan = DeterministicQueryCompiler().compile(
            question,
            record_types=record_types,
            limit=limit,
        )
        with self._connect() as connection:
            rows = connection.execute(plan.sql, plan.parameters).fetchall()
            snapshot_id = self._snapshot_id(connection)
            hits = tuple(
                self._knowledge_hit(connection, row)
                for row in rows[:limit]
            )
        truncated = len(rows) > limit
        return KnowledgeQueryResult(
            plan=plan,
            hits=hits,
            truncated=truncated,
            projection_snapshot_id=snapshot_id,
        )

    def identifier(self, kind: IdentifierKind, value: str) -> IdentifierView:
        normalized, locator = normalize_research_identifier(kind, value)
        with self._connect() as connection:
            identifier = connection.execute(
                """
                SELECT id, kind, value, canonical_locator
                FROM research_identifier
                WHERE kind = ? AND value = ?
                """,
                (kind.value, normalized),
            ).fetchone()
            if identifier is None:
                raise ValueError(f"unknown research identifier: {kind.value}:{normalized}")
            references = self._rows(
                connection,
                """
                SELECT br.id, br.relation, br.signal, br.start, br.end,
                       br.structural_anchor_id, a.kind AS anchor_kind,
                       a.page_number, original.id AS source_id,
                       original.source_uri, original.trust_zone,
                       COALESCE((
                           SELECT group_concat(t.id)
                           FROM threat_observation t
                           WHERE t.source_version = a.source_version_id
                       ), '') AS threat_observation_ids
                FROM bibliographic_reference br
                JOIN structural_anchor a ON a.id = br.structural_anchor_id
                JOIN structural_derivation sd
                  ON sd.id = a.structural_derivation_id
                JOIN text_derivation td ON td.id = sd.text_derivation_id
                JOIN source original ON original.id = td.original_source_version_id
                WHERE br.identifier_id = ?
                ORDER BY original.source_uri, br.start, br.id
                """,
                (identifier["id"],),
            )
            discovery_hits = self._rows(
                connection,
                """
                SELECT d.*, idh.match_rule
                FROM identifier_discovery_hit idh
                JOIN discovery_hit d ON d.id = idh.discovery_hit_id
                WHERE idh.identifier_id = ?
                ORDER BY d.id
                """,
                (identifier["id"],),
            )
            resolutions = self._rows(
                connection,
                """
                SELECT r.*, ioar.match_rule
                FROM identifier_open_access_resolution ioar
                JOIN open_access_resolution r ON r.id = ioar.resolution_id
                WHERE ioar.identifier_id = ?
                ORDER BY r.id
                """,
                (identifier["id"],),
            )
            snapshot_id = self._snapshot_id(connection)
        if identifier["canonical_locator"] != locator:
            raise ValueError("stored identifier locator does not match current normalization")
        return IdentifierView(
            identifier_id=identifier["id"],
            kind=IdentifierKind(identifier["kind"]),
            value=identifier["value"],
            canonical_locator=identifier["canonical_locator"],
            references=references,
            discovery_hits=discovery_hits,
            open_access_resolutions=resolutions,
            projection_snapshot_id=snapshot_id,
        )

    @staticmethod
    def _knowledge_hit(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> KnowledgeHit:
        record_type = QueryRecordType(row["record_type"])
        metadata: dict[str, Any] = {}
        if record_type is QueryRecordType.ANCHOR:
            anchor = connection.execute(
                """
                SELECT a.source_version_id AS derived_source_version_id,
                       original.id AS source_version_id,
                       original.source_uri,
                       original.trust_zone,
                       a.kind,
                       a.start,
                       a.end,
                       a.page_number,
                       a.parent_id,
                       a.synthetic
                FROM structural_anchor AS a
                JOIN structural_derivation AS sd
                  ON sd.id = a.structural_derivation_id
                JOIN text_derivation AS td
                  ON td.id = sd.text_derivation_id
                JOIN source AS original
                  ON original.id = td.original_source_version_id
                WHERE a.id = ?
                """,
                (row["record_id"],),
            ).fetchone()
            if anchor is None:
                raise ValueError("anchor FTS row has no structural record")
            threats = connection.execute(
                """
                SELECT id, threat_type, status, severity
                FROM threat_observation
                WHERE source_version = ?
                ORDER BY id
                """,
                (anchor["derived_source_version_id"],),
            ).fetchall()
            metadata = {
                "source_version_id": anchor["source_version_id"],
                "derived_source_version_id": anchor["derived_source_version_id"],
                "source_uri": anchor["source_uri"],
                "trust_zone": anchor["trust_zone"],
                "threat_observation_ids": tuple(item["id"] for item in threats),
                "threats": tuple(
                    KnowledgeThreatContext(
                        id=item["id"],
                        threat_type=item["threat_type"],
                        status=item["status"],
                        severity=item["severity"],
                    )
                    for item in threats
                ),
                "anchor_kind": anchor["kind"],
                "anchor_start": anchor["start"],
                "anchor_end": anchor["end"],
                "anchor_page_number": anchor["page_number"],
                "anchor_parent_id": anchor["parent_id"],
                "anchor_synthetic": bool(anchor["synthetic"]),
            }
        elif record_type is QueryRecordType.IDENTIFIER:
            identifier = connection.execute(
                """
                SELECT kind, value, canonical_locator
                FROM research_identifier
                WHERE id = ?
                """,
                (row["record_id"],),
            ).fetchone()
            if identifier is None:
                raise ValueError("identifier FTS row has no identifier record")
            metadata = {
                "identifier_kind": identifier["kind"],
                "identifier_value": identifier["value"],
                "canonical_locator": identifier["canonical_locator"],
            }
        elif record_type is QueryRecordType.REFERENCE:
            reference = connection.execute(
                """
                SELECT br.relation, br.signal, br.start, br.end,
                       ri.kind AS identifier_kind, ri.value AS identifier_value,
                       ri.canonical_locator,
                       a.id AS anchor_id, a.kind AS anchor_kind,
                       a.page_number, a.parent_id, a.synthetic,
                       a.source_version_id AS derived_source_version_id,
                       original.id AS source_version_id,
                       original.source_uri, original.trust_zone
                FROM bibliographic_reference AS br
                JOIN research_identifier AS ri ON ri.id = br.identifier_id
                JOIN structural_anchor AS a ON a.id = br.structural_anchor_id
                JOIN structural_derivation AS sd
                  ON sd.id = a.structural_derivation_id
                JOIN text_derivation AS td ON td.id = sd.text_derivation_id
                JOIN source AS original ON original.id = td.original_source_version_id
                WHERE br.id = ?
                """,
                (row["record_id"],),
            ).fetchone()
            if reference is None:
                raise ValueError("reference FTS row has no bibliographic record")
            threats = connection.execute(
                """
                SELECT id, threat_type, status, severity
                FROM threat_observation
                WHERE source_version = ?
                ORDER BY id
                """,
                (reference["derived_source_version_id"],),
            ).fetchall()
            discovery_links = connection.execute(
                """
                SELECT idh.discovery_hit_id
                FROM identifier_discovery_hit idh
                JOIN bibliographic_reference br
                  ON br.identifier_id = idh.identifier_id
                WHERE br.id = ?
                ORDER BY idh.discovery_hit_id
                """,
                (row["record_id"],),
            ).fetchall()
            resolution_links = connection.execute(
                """
                SELECT ioar.resolution_id
                FROM identifier_open_access_resolution ioar
                JOIN bibliographic_reference br
                  ON br.identifier_id = ioar.identifier_id
                WHERE br.id = ?
                ORDER BY ioar.resolution_id
                """,
                (row["record_id"],),
            ).fetchall()
            metadata = {
                "source_version_id": reference["source_version_id"],
                "derived_source_version_id": reference["derived_source_version_id"],
                "source_uri": reference["source_uri"],
                "trust_zone": reference["trust_zone"],
                "threat_observation_ids": tuple(item["id"] for item in threats),
                "threats": tuple(
                    KnowledgeThreatContext(
                        id=item["id"],
                        threat_type=item["threat_type"],
                        status=item["status"],
                        severity=item["severity"],
                    )
                    for item in threats
                ),
                "anchor_kind": reference["anchor_kind"],
                "anchor_start": reference["start"],
                "anchor_end": reference["end"],
                "anchor_page_number": reference["page_number"],
                "anchor_parent_id": reference["parent_id"],
                "anchor_synthetic": bool(reference["synthetic"]),
                "identifier_kind": reference["identifier_kind"],
                "identifier_value": reference["identifier_value"],
                "canonical_locator": reference["canonical_locator"],
                "reference_relation": reference["relation"],
                "reference_signal": reference["signal"],
                "resolved_discovery_hit_ids": tuple(item[0] for item in discovery_links),
                "resolved_open_access_resolution_ids": tuple(
                    item[0] for item in resolution_links
                ),
            }
        elif record_type is QueryRecordType.PROPOSAL:
            proposal = connection.execute(
                """
                SELECT ep.provider, ep.model, ep.review_state, ep.commit_authority,
                       ep.source_version_id AS derived_source_version_id,
                       original.id AS source_version_id,
                       original.source_uri, original.trust_zone
                FROM extraction_proposal ep
                JOIN structural_derivation sd
                  ON sd.id = ep.structural_derivation_id
                JOIN text_derivation td ON td.id = sd.text_derivation_id
                JOIN source original ON original.id = td.original_source_version_id
                WHERE ep.id = ?
                """,
                (row["record_id"],),
            ).fetchone()
            if proposal is None:
                raise ValueError("proposal FTS row has no extraction proposal")
            threats = connection.execute(
                """
                SELECT id, threat_type, status, severity
                FROM threat_observation
                WHERE source_version = ?
                ORDER BY id
                """,
                (proposal["derived_source_version_id"],),
            ).fetchall()
            metadata = {
                "source_version_id": proposal["source_version_id"],
                "derived_source_version_id": proposal["derived_source_version_id"],
                "source_uri": proposal["source_uri"],
                "trust_zone": proposal["trust_zone"],
                "threat_observation_ids": tuple(item["id"] for item in threats),
                "threats": tuple(
                    KnowledgeThreatContext(
                        id=item["id"],
                        threat_type=item["threat_type"],
                        status=item["status"],
                        severity=item["severity"],
                    )
                    for item in threats
                ),
                "proposal_provider": proposal["provider"],
                "proposal_model": proposal["model"],
                "proposal_review_state": proposal["review_state"],
                "proposal_commit_authority": proposal["commit_authority"],
            }
        return KnowledgeHit(
            record_type=record_type,
            record_id=row["record_id"],
            title=row["title"],
            snippet=row["excerpt"],
            rank=row["score"],
            **metadata,
        )

    def topic(self, concept_id: str, *, as_of: datetime | None = None) -> TopicView:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM concept WHERE id = ?",
                (concept_id,),
            ).fetchone()
            if exists is None:
                raise ValueError(f"unknown concept: {concept_id}")
            descendants = tuple(
                row[0]
                for row in connection.execute(
                    """
                    WITH RECURSIVE descendants(id) AS (
                        VALUES (?)
                        UNION
                        SELECT cp.concept_id
                        FROM concept_parent cp
                        JOIN descendants d ON cp.parent_id = d.id
                    )
                    SELECT id FROM descendants ORDER BY id
                    """,
                    (concept_id,),
                )
            )
            placeholders = ",".join("?" for _ in descendants)
            concepts = self._rows(
                connection,
                f"""
                SELECT c.*,
                       COALESCE(group_concat(DISTINCT cs.synonym), '') AS synonyms,
                       COALESCE(group_concat(DISTINCT cp.parent_id), '') AS broader
                FROM concept c
                LEFT JOIN concept_synonym cs ON cs.concept_id = c.id
                LEFT JOIN concept_parent cp ON cp.concept_id = c.id
                WHERE c.id IN ({placeholders})
                GROUP BY c.id
                ORDER BY c.id
                """,
                descendants,
            )
            temporal_clause = ""
            claim_parameters: tuple[str, ...] = descendants
            if as_of is not None:
                temporal_clause = (
                    "AND (c.valid_from IS NULL OR c.valid_from <= ?) "
                    "AND (c.valid_until IS NULL OR c.valid_until >= ?)"
                )
                instant = as_of.isoformat()
                claim_parameters = (*descendants, instant, instant)
            claims = self._rows(
                connection,
                f"""
                SELECT c.*, ef.id AS evidence_id, ef.exact_text,
                       s.id AS source_id, s.source_uri, s.acquired_at, s.license
                FROM claim c
                JOIN claim_evidence ce ON ce.claim_id = c.id
                JOIN evidence_fragment ef ON ef.id = ce.evidence_id
                JOIN source s ON s.id = ef.source_version
                WHERE c.subject IN ({placeholders}) AND c.review_state = 'accepted'
                  {temporal_clause}
                ORDER BY c.subject, c.predicate, c.id, ef.id
                """,
                claim_parameters,
            )
            sources = self._rows(
                connection,
                f"""
                SELECT DISTINCT s.*, tsa.roles_json, tsa.recorded_at AS associated_at,
                       tsa.recorded_by AS associated_by,
                       sm.id AS metadata_id, sm.original_locator, sm.title,
                       sm.authors_json, sm.authorship_status, sm.publisher,
                       sm.published_at, sm.license_status, sm.usage_conditions_json,
                       sm.usage_conditions_status, sm.usage_permissions_json,
                       sm.rights_basis, sm.rights_basis_status, sm.provenance_note,
                       sm.provenance_status
                FROM topic_source_association tsa
                JOIN source s ON s.id = tsa.source_version_id
                LEFT JOIN source_metadata sm ON sm.source_version_id = s.id
                WHERE tsa.topic_concept_id IN ({placeholders})
                ORDER BY s.source_uri, s.id
                """,
                descendants,
            )
            controversies = self._rows(
                connection,
                f"""
                SELECT c.*, group_concat(cc.claim_id) AS claim_ids
                FROM controversy c
                JOIN controversy_claim cc ON cc.controversy_id = c.id
                WHERE c.topic_concept_id IN ({placeholders})
                  AND c.review_state = 'accepted'
                GROUP BY c.id
                ORDER BY c.status, c.question
                """,
                descendants,
            )
            gaps = self._rows(
                connection,
                f"""
                SELECT g.*, COALESCE(group_concat(gc.claim_id), '') AS related_claim_ids
                FROM knowledge_gap g
                LEFT JOIN gap_claim gc ON gc.gap_id = g.id
                WHERE g.topic_concept_id IN ({placeholders})
                  AND g.review_state = 'accepted'
                GROUP BY g.id
                ORDER BY CASE g.status WHEN 'open' THEN 0 ELSE 1 END,
                         g.priority DESC, g.question
                """,
                descendants,
            )
            threats = self._rows(
                connection,
                f"""
                SELECT DISTINCT t.id, t.source_version, s.source_uri, t.threat_type,
                       t.status, t.severity, t.detected_at, t.detector_kind,
                       t.detector_id, t.policy_rule
                FROM threat_observation t
                JOIN source s ON s.id = t.source_version
                JOIN topic_source_association tsa ON tsa.source_version_id = s.id
                WHERE tsa.topic_concept_id IN ({placeholders})
                ORDER BY t.severity DESC, t.id
                """,
                descendants,
            )
            references = self._rows(
                connection,
                f"""
                SELECT br.id, br.relation, br.signal, br.start, br.end,
                       br.structural_anchor_id, ri.kind AS identifier_kind,
                       ri.value AS identifier_value, ri.canonical_locator,
                       original.id AS source_id, original.source_uri,
                       a.page_number,
                       COALESCE((
                           SELECT group_concat(idh.discovery_hit_id)
                           FROM identifier_discovery_hit idh
                           WHERE idh.identifier_id = ri.id
                       ), '') AS resolved_discovery_hit_ids,
                       COALESCE((
                           SELECT group_concat(ioar.resolution_id)
                           FROM identifier_open_access_resolution ioar
                           WHERE ioar.identifier_id = ri.id
                       ), '') AS resolved_open_access_resolution_ids
                FROM bibliographic_reference br
                JOIN research_identifier ri ON ri.id = br.identifier_id
                JOIN structural_anchor a ON a.id = br.structural_anchor_id
                JOIN structural_derivation sd
                  ON sd.id = a.structural_derivation_id
                JOIN text_derivation td ON td.id = sd.text_derivation_id
                JOIN source original ON original.id = td.original_source_version_id
                JOIN topic_source_association tsa
                  ON tsa.source_version_id = original.id
                WHERE tsa.topic_concept_id IN ({placeholders})
                ORDER BY ri.kind, ri.value, br.id
                """,
                descendants,
            )
            snapshot_id = self._snapshot_id(connection)
        return TopicView(
            topic_concept_id=concept_id,
            as_of=as_of,
            descendant_concept_ids=descendants,
            concepts=concepts,
            sources=sources,
            claims=claims,
            controversies=controversies,
            gaps=gaps,
            threats=threats,
            references=references,
            projection_snapshot_id=snapshot_id,
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise ValueError(f"projection does not exist: {self.database}")
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _rows(
        connection: sqlite3.Connection,
        sql: str,
        parameters: tuple[str, ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in connection.execute(sql, parameters))

    @staticmethod
    def _snapshot_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT payload FROM _research_projection_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ValueError("projection is not stamped")
        return json.loads(row[0])["snapshot_id"]
