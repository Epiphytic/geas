from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.discovery_acquisition import RepositorySnapshot
from research_agent.models import SourceVersion, StrictModel, canonical_json, content_id, utc_now
from research_agent.parsing import TextDerivation
from research_agent.store import ImmutableStore
from research_agent.structure import AnchorKind, StructuralAnchor


class SourceLibraryManifest(StrictModel):
    """A reusable, ontology-independent selection over immutable source history."""

    version: Literal[1]
    id: str = Field(pattern=r"^library:[A-Za-z0-9][A-Za-z0-9._:-]*$")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4000)
    repositories: tuple[str, ...] = ()
    source_version_ids: tuple[str, ...] = ()
    source_uri_prefixes: tuple[str, ...] = ()
    connector_ids: tuple[str, ...] = ()
    include_all_parsed_sources: bool = False

    @field_validator(
        "repositories",
        "source_version_ids",
        "source_uri_prefixes",
        "connector_ids",
    )
    @classmethod
    def selectors_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(item.strip() for item in value)
        if any(not item for item in stripped):
            raise ValueError("source-library selectors must not be blank")
        if len(set(stripped)) != len(stripped):
            raise ValueError("source-library selectors must be unique")
        return stripped

    @model_validator(mode="after")
    def has_selection(self) -> SourceLibraryManifest:
        if not (
            self.include_all_parsed_sources
            or self.repositories
            or self.source_version_ids
            or self.source_uri_prefixes
            or self.connector_ids
        ):
            raise ValueError("source library requires at least one source selector")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> SourceLibraryManifest:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class SourceLibrarySnapshot(StrictModel):
    version: Literal[1] = 1
    id: str
    library_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    source_version_ids: tuple[str, ...]
    text_derivation_ids: tuple[str, ...]
    repository_snapshot_ids: tuple[str, ...]


class SourceLibraryBuildResult(StrictModel):
    database: str
    snapshot: SourceLibrarySnapshot
    source_count: int
    searchable_anchor_count: int
    repository_count: int
    projection_digest: str


class SourceLibraryDescription(StrictModel):
    manifest: SourceLibraryManifest
    snapshot: SourceLibrarySnapshot
    sources: tuple[dict[str, Any], ...]
    searchable_anchor_count: int


class SourceLibraryQueryPlan(StrictModel):
    question: str
    tokens: tuple[str, ...]
    fts_expression: str
    limit: int = Field(ge=1, le=1000)
    sql: str
    parameters: tuple[str | int, ...]
    compiler_version: str


class SourceLibraryHit(StrictModel):
    anchor_id: str
    source_version_id: str
    derived_source_version_id: str
    source_uri: str
    repository: str | None = None
    title: str
    snippet: str
    rank: float
    anchor_kind: str
    start: int
    end: int
    page_number: int | None = None
    threat_observation_ids: tuple[str, ...] = ()


class SourceLibraryQueryResult(StrictModel):
    library_id: str
    snapshot_id: str
    plan: SourceLibraryQueryPlan
    hits: tuple[SourceLibraryHit, ...]
    truncated: bool


class SourceContextFragment(StrictModel):
    anchor_id: str
    source_version_id: str
    source_uri: str
    repository: str | None = None
    anchor_kind: str
    start: int
    end: int
    text: str
    complete_anchor: bool
    threat_observation_ids: tuple[str, ...] = ()


class SourceLibraryContext(StrictModel):
    library_id: str
    snapshot_id: str
    question: str
    query_plan: SourceLibraryQueryPlan
    fragments: tuple[SourceContextFragment, ...]
    character_count: int
    truncated: bool


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


class DeterministicLibraryQueryCompiler:
    version = "deterministic-source-library-query/1"
    sql = """
        SELECT a.anchor_id, a.source_version_id, a.derived_source_version_id,
               a.source_uri, a.repository, a.title, a.anchor_kind, a.start, a.end,
               a.page_number, a.exact_text, a.threat_observation_ids,
               snippet(library_fts, 2, '[', ']', ' … ', 32) AS excerpt,
               bm25(library_fts, 4.0, 1.0, 2.0) AS score
        FROM library_fts
        JOIN library_anchor a ON a.anchor_id = library_fts.anchor_id
        WHERE library_fts MATCH ?
        ORDER BY score, a.source_uri, a.start, a.anchor_id
        LIMIT ?
    """

    def compile(self, question: str, *, limit: int = 25) -> SourceLibraryQueryPlan:
        if not question.strip():
            raise ValueError("library query must not be empty")
        if not 1 <= limit <= 1000:
            raise ValueError("library query limit must be between 1 and 1000")
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
            raise ValueError("library query contains no searchable terms")
        escaped = tuple(token.replace('"', '""') for token in tokens)
        expression = " OR ".join(f'"{token}"*' for token in escaped)
        return SourceLibraryQueryPlan(
            question=question,
            tokens=tokens,
            fts_expression=expression,
            limit=limit,
            sql=self.sql.strip(),
            parameters=(expression, limit + 1),
            compiler_version=self.version,
        )


class SourceLibraryBuilder:
    schema_version = 1
    builder_version = "source-library-projection/1"
    _searchable_kinds = frozenset(
        {
            AnchorKind.HEADING,
            AnchorKind.PARAGRAPH,
            AnchorKind.LIST_ITEM,
            AnchorKind.FOOTNOTE,
            AnchorKind.CAPTION,
        }
    )

    def __init__(self, *, store: ImmutableStore) -> None:
        self.store = store

    def build(
        self,
        manifest: SourceLibraryManifest,
        database: Path,
    ) -> SourceLibraryBuildResult:
        self.store.initialize()
        sources = self._sources()
        derivations = tuple(
            TextDerivation.model_validate(value)
            for value in self.store.iter_records("text-derivation")
        )
        repositories = tuple(
            RepositorySnapshot.model_validate(value)
            for value in self.store.iter_records("repository-snapshot")
        )
        selected_source_ids, selected_repository_ids = self._select(
            manifest,
            sources=sources,
            derivations=derivations,
            repositories=repositories,
        )
        selected_derivations = tuple(
            sorted(
                (
                    item
                    for item in derivations
                    if item.original_source_version_id in selected_source_ids
                    or item.derived_source_version_id in selected_source_ids
                ),
                key=lambda item: item.id,
            )
        )
        if not selected_derivations:
            raise ValueError("source-library selection contains no parsed text")
        selected_derived_ids = {
            item.derived_source_version_id for item in selected_derivations
        }
        selected_repository_ids.update(
            item.id
            for item in repositories
            if item.source_version_id in selected_derived_ids
        )
        selected_original_ids = tuple(
            sorted({item.original_source_version_id for item in selected_derivations})
        )
        manifest_sha256 = hashlib.sha256(canonical_json(manifest)).hexdigest()
        snapshot_fields = {
            "version": 1,
            "library_id": manifest.id,
            "manifest_sha256": manifest_sha256,
            "created_at": utc_now(),
            "source_version_ids": selected_original_ids,
            "text_derivation_ids": tuple(item.id for item in selected_derivations),
            "repository_snapshot_ids": tuple(sorted(selected_repository_ids)),
        }
        snapshot = SourceLibrarySnapshot(
            id=content_id("source-library-snapshot", snapshot_fields),
            **snapshot_fields,
        )
        self.store.put_record("source-library-manifest", manifest)
        self.store.put_record("source-library-snapshot", snapshot)
        database = database.resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        temporary = database.with_name(f".{database.name}.{os.getpid()}.tmp")
        if temporary.exists():
            temporary.unlink()
        try:
            connection = sqlite3.connect(temporary)
            connection.row_factory = sqlite3.Row
            self._create_schema(connection)
            anchor_count = self._populate(
                connection,
                manifest=manifest,
                snapshot=snapshot,
                sources=sources,
                derivations=selected_derivations,
                repositories=repositories,
            )
            connection.commit()
            connection.close()
            os.replace(temporary, database)
        finally:
            if temporary.exists():
                temporary.unlink()
        projection_digest = hashlib.sha256(database.read_bytes()).hexdigest()
        return SourceLibraryBuildResult(
            database=str(database),
            snapshot=snapshot,
            source_count=len(selected_original_ids),
            searchable_anchor_count=anchor_count,
            repository_count=len(selected_repository_ids),
            projection_digest=projection_digest,
        )

    def _sources(self) -> dict[str, SourceVersion]:
        grouped: dict[str, list[SourceVersion]] = {}
        for value in self.store.iter_records("source-version"):
            source = SourceVersion.model_validate(value)
            grouped.setdefault(source.id, []).append(source)
        return {
            source_id: sorted(
                values,
                key=lambda item: (
                    item.source_uri,
                    item.connector_id,
                    item.acquired_at,
                ),
            )[0]
            for source_id, values in grouped.items()
        }

    @staticmethod
    def _select(
        manifest: SourceLibraryManifest,
        *,
        sources: dict[str, SourceVersion],
        derivations: tuple[TextDerivation, ...],
        repositories: tuple[RepositorySnapshot, ...],
    ) -> tuple[set[str], set[str]]:
        parsed_ids = {
            source_id
            for item in derivations
            for source_id in (
                item.original_source_version_id,
                item.derived_source_version_id,
            )
        }
        selected: set[str] = set()
        if manifest.include_all_parsed_sources:
            selected.update(parsed_ids)
        selected.update(item for item in manifest.source_version_ids if item in parsed_ids)
        prefixes = tuple(item.casefold() for item in manifest.source_uri_prefixes)
        connectors = set(manifest.connector_ids)
        for source in sources.values():
            if prefixes and source.source_uri.casefold().startswith(prefixes):
                selected.add(source.id)
            if source.connector_id in connectors:
                selected.add(source.id)
        requested_repositories = {item.casefold() for item in manifest.repositories}
        selected_repository_ids: set[str] = set()
        for snapshot in repositories:
            if snapshot.repository.casefold() in requested_repositories:
                selected.add(snapshot.source_version_id)
                selected_repository_ids.add(snapshot.id)
        missing_sources = sorted(set(manifest.source_version_ids) - parsed_ids)
        if missing_sources:
            raise ValueError(
                "source library references unknown or unparsed source versions: "
                + ", ".join(missing_sources)
            )
        known_repositories = {item.repository.casefold() for item in repositories}
        missing_repositories = sorted(requested_repositories - known_repositories)
        if missing_repositories:
            raise ValueError(
                "source library references repositories absent from the source store: "
                + ", ".join(missing_repositories)
            )
        return selected, selected_repository_ids

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE library_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL
            );
            CREATE TABLE library_source (
                source_version_id TEXT PRIMARY KEY,
                source_uri TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                license TEXT,
                repository TEXT
            );
            CREATE TABLE library_anchor (
                anchor_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL,
                derived_source_version_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                repository TEXT,
                title TEXT NOT NULL,
                anchor_kind TEXT NOT NULL,
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                page_number INTEGER,
                exact_text TEXT NOT NULL,
                threat_observation_ids TEXT NOT NULL,
                FOREIGN KEY (source_version_id)
                    REFERENCES library_source(source_version_id)
            );
            CREATE VIRTUAL TABLE library_fts USING fts5(
                anchor_id UNINDEXED,
                title,
                body,
                tokenize = 'unicode61'
            );
            """
        )

    def _populate(
        self,
        connection: sqlite3.Connection,
        *,
        manifest: SourceLibraryManifest,
        snapshot: SourceLibrarySnapshot,
        sources: dict[str, SourceVersion],
        derivations: tuple[TextDerivation, ...],
        repositories: tuple[RepositorySnapshot, ...],
    ) -> int:
        metadata = {
            "schema_version": self.schema_version,
            "builder_version": self.builder_version,
            "manifest": manifest.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
        }
        connection.execute(
            "INSERT INTO library_metadata(singleton, payload) VALUES (1, ?)",
            (json.dumps(metadata, sort_keys=True),),
        )
        repository_by_derived = {
            item.source_version_id: item.repository for item in repositories
        }
        repository_by_original: dict[str, str] = {}
        for derivation in derivations:
            repository = repository_by_derived.get(derivation.derived_source_version_id)
            if repository is not None:
                repository_by_original[derivation.original_source_version_id] = repository
        selected_original_ids = set(snapshot.source_version_ids)
        for source_id in sorted(selected_original_ids):
            source = sources[source_id]
            connection.execute(
                """
                INSERT INTO library_source(
                    source_version_id, source_uri, connector_id, media_type,
                    acquired_at, license, repository
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.source_uri,
                    source.connector_id,
                    source.media_type,
                    source.acquired_at.isoformat(),
                    source.license,
                    repository_by_original.get(source.id),
                ),
            )
        anchors = tuple(
            StructuralAnchor.model_validate(value)
            for value in self.store.iter_records("structural-anchor")
        )
        threats_by_source: dict[str, list[str]] = {}
        for value in self.store.iter_records("threat-observation"):
            target = value.get("target", {})
            source_id = target.get("source_version")
            observation_id = value.get("id")
            if isinstance(source_id, str) and isinstance(observation_id, str):
                threats_by_source.setdefault(source_id, []).append(observation_id)
        derivation_by_structure = {
            value["id"]: value["text_derivation_id"]
            for value in self.store.iter_records("structural-derivation")
            if isinstance(value.get("id"), str)
            and isinstance(value.get("text_derivation_id"), str)
        }
        text_derivation_by_id = {item.id: item for item in derivations}
        count = 0
        for anchor in sorted(
            anchors,
            key=lambda item: (item.source_version_id, item.ordinal, item.id),
        ):
            if anchor.kind not in self._searchable_kinds:
                continue
            text_derivation_id = derivation_by_structure.get(anchor.structural_derivation_id)
            derivation = text_derivation_by_id.get(text_derivation_id or "")
            if derivation is None:
                continue
            source = sources[derivation.original_source_version_id]
            text = self.store.read_blob(derivation.derived_content_sha256).decode("utf-8")
            exact = text[anchor.start : anchor.end]
            if hashlib.sha256(exact.encode()).hexdigest() != anchor.exact_sha256:
                raise ValueError(
                    f"source-library anchor does not match immutable text: {anchor.id}"
                )
            title = repository_by_original.get(source.id) or source.source_uri
            threat_ids = tuple(
                sorted(
                    set(
                        threats_by_source.get(source.id, [])
                        + threats_by_source.get(derivation.derived_source_version_id, [])
                    )
                )
            )
            connection.execute(
                """
                INSERT INTO library_anchor(
                    anchor_id, source_version_id, derived_source_version_id,
                    source_uri, repository, title, anchor_kind, start, end,
                    page_number, exact_text, threat_observation_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor.id,
                    source.id,
                    derivation.derived_source_version_id,
                    source.source_uri,
                    repository_by_original.get(source.id),
                    title,
                    anchor.kind.value,
                    anchor.start,
                    anchor.end,
                    anchor.page_number,
                    exact,
                    json.dumps(threat_ids),
                ),
            )
            connection.execute(
                "INSERT INTO library_fts(anchor_id, title, body) VALUES (?, ?, ?)",
                (anchor.id, title, exact),
            )
            count += 1
        return count


class SourceLibraryQueryEngine:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def query(self, question: str, *, limit: int = 25) -> SourceLibraryQueryResult:
        plan = DeterministicLibraryQueryCompiler().compile(question, limit=limit)
        with self._connect() as connection:
            rows = connection.execute(plan.sql, plan.parameters).fetchall()
            metadata = self._metadata(connection)
        hits = tuple(self._hit(row) for row in rows[:limit])
        return SourceLibraryQueryResult(
            library_id=metadata["snapshot"]["library_id"],
            snapshot_id=metadata["snapshot"]["id"],
            plan=plan,
            hits=hits,
            truncated=len(rows) > limit,
        )

    def describe(self) -> SourceLibraryDescription:
        with self._connect() as connection:
            metadata = self._metadata(connection)
            sources = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT source_version_id, source_uri, connector_id, media_type,
                           acquired_at, license, repository
                    FROM library_source
                    ORDER BY source_uri, source_version_id
                    """
                )
            )
            anchor_count = connection.execute(
                "SELECT count(*) FROM library_anchor"
            ).fetchone()[0]
        return SourceLibraryDescription(
            manifest=SourceLibraryManifest.model_validate(metadata["manifest"]),
            snapshot=SourceLibrarySnapshot.model_validate(metadata["snapshot"]),
            sources=sources,
            searchable_anchor_count=anchor_count,
        )

    def context(
        self,
        question: str,
        *,
        limit: int = 25,
        max_characters: int = 16_000,
    ) -> SourceLibraryContext:
        if not 256 <= max_characters <= 1_000_000:
            raise ValueError("context character budget must be between 256 and 1000000")
        result = self.query(question, limit=limit)
        fragments: list[SourceContextFragment] = []
        used = 0
        truncated = result.truncated
        with self._connect() as connection:
            for hit in result.hits:
                row = connection.execute(
                    "SELECT exact_text FROM library_anchor WHERE anchor_id = ?",
                    (hit.anchor_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("library query hit has no exact anchor")
                text = row["exact_text"]
                remaining = max_characters - used
                if remaining <= 0:
                    truncated = True
                    break
                selected = text[:remaining]
                complete = len(selected) == len(text)
                fragments.append(
                    SourceContextFragment(
                        anchor_id=hit.anchor_id,
                        source_version_id=hit.source_version_id,
                        source_uri=hit.source_uri,
                        repository=hit.repository,
                        anchor_kind=hit.anchor_kind,
                        start=hit.start,
                        end=hit.start + len(selected),
                        text=selected,
                        complete_anchor=complete,
                        threat_observation_ids=hit.threat_observation_ids,
                    )
                )
                used += len(selected)
                if not complete:
                    truncated = True
                    break
        return SourceLibraryContext(
            library_id=result.library_id,
            snapshot_id=result.snapshot_id,
            question=question,
            query_plan=result.plan,
            fragments=tuple(fragments),
            character_count=used,
            truncated=truncated,
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise ValueError(f"source-library projection does not exist: {self.database}")
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload FROM library_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ValueError("source-library projection is not stamped")
        return json.loads(row["payload"])

    @staticmethod
    def _hit(row: sqlite3.Row) -> SourceLibraryHit:
        return SourceLibraryHit(
            anchor_id=row["anchor_id"],
            source_version_id=row["source_version_id"],
            derived_source_version_id=row["derived_source_version_id"],
            source_uri=row["source_uri"],
            repository=row["repository"],
            title=row["title"],
            snippet=row["excerpt"],
            rank=row["score"],
            anchor_kind=row["anchor_kind"],
            start=row["start"],
            end=row["end"],
            page_number=row["page_number"],
            threat_observation_ids=tuple(json.loads(row["threat_observation_ids"])),
        )
