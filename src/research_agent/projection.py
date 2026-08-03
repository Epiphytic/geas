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

from research_agent.discovery import CoverageRun, DiscoveryHit, OpenAccessResolution
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


class KnowledgeQueryResult(StrictModel):
    plan: KnowledgeQueryPlan
    hits: tuple[KnowledgeHit, ...]
    truncated: bool
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
    schema_version = 6
    builder_version = "sqlite-knowledge-projection/6"

    def __init__(self, *, store: ImmutableStore, workspace_root: Path) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()

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
                sources=(
                    SourceVersion.model_validate(value)
                    for value in self.store.iter_records("source-version")
                ),
                topic_sources=(
                    TopicSourceAssociation.model_validate(value)
                    for value in self.store.iter_records("topic-source")
                ),
                fragments=(
                    EvidenceFragment.model_validate(value)
                    for value in self.store.iter_records("evidence-fragment")
                ),
                claims=(Claim.model_validate(value) for value in self.store.iter_records("claim")),
                controversies=(
                    Controversy.model_validate(value)
                    for value in self.store.iter_records("controversy")
                ),
                gaps=(
                    KnowledgeGap.model_validate(value)
                    for value in self.store.iter_records("knowledge-gap")
                ),
                observations=(
                    ThreatObservation.model_validate(value)
                    for value in self.store.iter_records("threat-observation")
                ),
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
            )
            connection.execute("PRAGMA optimize")
            connection.commit()
            invalid = connection.execute("PRAGMA foreign_key_check").fetchall()
            if invalid:
                raise ValueError(f"projection has foreign-key violations: {invalid!r}")
        return counts

    def _concepts(self) -> tuple[Concept, ...]:
        records = {
            item.id: item
            for item in (
                Concept.model_validate(value) for value in self.store.iter_records("concept")
            )
        }
        vocabulary_path = self.workspace_root / "config/query-vocabulary.yaml"
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
            """
        )

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        concepts: tuple[Concept, ...],
        sources: Iterable[SourceVersion],
        topic_sources: Iterable[TopicSourceAssociation],
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
    ) -> dict[str, int]:
        counts = {
            "concepts": 0,
            "sources": 0,
            "topic_source_associations": 0,
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
                ((item.id, parent) for parent in item.broader),
            )
            connection.executemany(
                "INSERT INTO concept_synonym VALUES (?, ?)",
                ((item.id, synonym) for synonym in item.synonyms),
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
                ((item.id, evidence_id) for evidence_id in item.evidence),
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
                ((item.id, claim_id) for claim_id in item.claim_ids),
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
                ((item.id, claim_id) for claim_id in item.related_claim_ids),
            )
            connection.executemany(
                "INSERT INTO gap_query_plan VALUES (?, ?)",
                ((item.id, plan_id) for plan_id in item.searched_query_plan_ids),
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
                       tsa.recorded_by AS associated_by
                FROM topic_source_association tsa
                JOIN source s ON s.id = tsa.source_version_id
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
