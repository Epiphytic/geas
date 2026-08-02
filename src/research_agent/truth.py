from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator

from research_agent.models import StrictModel, canonical_json, content_id, utc_now


class ArtifactRole(StrEnum):
    ONTOLOGY = "ontology"
    OPERATIONAL_POLICY = "operational_policy"
    RECORD_SCHEMA = "record_schema"
    IMMUTABLE_RECORD = "immutable_record"
    SOURCE_BLOB = "source_blob"


class TruthArtifact(StrictModel):
    locator: str
    role: ArtifactRole
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)


class TruthPolicy(StrictModel):
    version: int = Field(ge=1)
    ontology_globs: tuple[str, ...] = Field(min_length=1)
    operational_policy_paths: tuple[str, ...] = ()
    record_schema_paths: tuple[str, ...] = Field(min_length=1)
    record_directory: str
    blob_directory: str
    database_to_canonical: Literal["forbidden"]
    canonical_drift_action: Literal["create_snapshot_then_rebuild"]
    projection_drift_action: Literal["discard_and_rebuild"]

    @field_validator(
        "ontology_globs",
        "operational_policy_paths",
        "record_schema_paths",
        "record_directory",
        "blob_directory",
    )
    @classmethod
    def paths_are_relative(cls, value: Any) -> Any:
        values = value if isinstance(value, tuple) else (value,)
        for item in values:
            if Path(item).is_absolute() or ".." in Path(item).parts:
                raise ValueError("truth policy paths must remain within their configured roots")
        return value

    @classmethod
    def from_yaml(cls, path: Path) -> TruthPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class TruthSnapshot(StrictModel):
    id: str
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[TruthArtifact, ...]
    created_at: datetime
    created_by: str
    predecessor: str | None = None
    builder_version: str


class DriftKind(StrEnum):
    ADDED = "added"
    MISSING = "missing"
    CHANGED = "changed"
    PROJECTION_MISSING = "projection_missing"
    PROJECTION_UNSTAMPED = "projection_unstamped"
    PROJECTION_STALE = "projection_stale"
    PROJECTION_MUTATED = "projection_mutated"


class DriftItem(StrictModel):
    kind: DriftKind
    locator: str
    expected: str | None = None
    actual: str | None = None


class DriftReport(StrictModel):
    snapshot_id: str
    clean: bool
    items: tuple[DriftItem, ...]
    recommended_action: Literal[
        "none",
        "create_snapshot_then_rebuild",
        "discard_and_rebuild",
    ]
    checked_at: datetime


class TruthManager:
    version = "truth-manager/1"

    def __init__(
        self,
        *,
        workspace_root: Path,
        store_root: Path,
        policy: TruthPolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store_root = store_root.resolve()
        self.policy = policy
        self.clock = clock

    def capture(self, *, created_by: str, predecessor: str | None = None) -> TruthSnapshot:
        artifacts = self.inventory()
        policy_sha256 = hashlib.sha256(canonical_json(self.policy)).hexdigest()
        state_payload = {
            "policy_sha256": policy_sha256,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
        state_digest = hashlib.sha256(canonical_json(state_payload)).hexdigest()
        created_at = self.clock()
        identity = {
            "state_digest": state_digest,
            "created_at": created_at,
            "created_by": created_by,
            "predecessor": predecessor,
            "builder_version": self.version,
        }
        return TruthSnapshot(
            id=content_id("truth-snapshot", identity),
            state_digest=state_digest,
            policy_sha256=policy_sha256,
            artifacts=artifacts,
            created_at=created_at,
            created_by=created_by,
            predecessor=predecessor,
            builder_version=self.version,
        )

    def verify(self, snapshot: TruthSnapshot) -> DriftReport:
        actual = {item.locator: item for item in self.inventory()}
        expected = {item.locator: item for item in snapshot.artifacts}
        items: list[DriftItem] = []
        actual_policy_sha256 = hashlib.sha256(canonical_json(self.policy)).hexdigest()
        if actual_policy_sha256 != snapshot.policy_sha256:
            items.append(
                DriftItem(
                    kind=DriftKind.CHANGED,
                    locator="policy:truth-policy",
                    expected=snapshot.policy_sha256,
                    actual=actual_policy_sha256,
                )
            )
        for locator in sorted(expected.keys() - actual.keys()):
            items.append(
                DriftItem(
                    kind=DriftKind.MISSING,
                    locator=locator,
                    expected=expected[locator].canonical_sha256,
                )
            )
        for locator in sorted(actual.keys() - expected.keys()):
            items.append(
                DriftItem(
                    kind=DriftKind.ADDED,
                    locator=locator,
                    actual=actual[locator].canonical_sha256,
                )
            )
        for locator in sorted(expected.keys() & actual.keys()):
            expected_item = expected[locator]
            actual_item = actual[locator]
            if expected_item != actual_item:
                items.append(
                    DriftItem(
                        kind=DriftKind.CHANGED,
                        locator=locator,
                        expected=expected_item.canonical_sha256,
                        actual=actual_item.canonical_sha256,
                    )
                )
        return DriftReport(
            snapshot_id=snapshot.id,
            clean=not items,
            items=tuple(items),
            recommended_action="none" if not items else "create_snapshot_then_rebuild",
            checked_at=self.clock(),
        )

    def inventory(self) -> tuple[TruthArtifact, ...]:
        artifacts: list[TruthArtifact] = []
        ontology_paths: set[Path] = set()
        for pattern in self.policy.ontology_globs:
            ontology_paths.update(
                path for path in self.workspace_root.glob(pattern) if path.is_file()
            )
        if not ontology_paths:
            raise ValueError("truth policy did not resolve any canonical ontology files")
        for path in sorted(ontology_paths):
            artifacts.append(self._file_artifact(path, ArtifactRole.ONTOLOGY, "workspace"))
        for relative in self.policy.operational_policy_paths:
            path = self.workspace_root / relative
            if not path.is_file():
                raise ValueError(f"missing canonical operational policy: {relative}")
            artifacts.append(
                self._file_artifact(path, ArtifactRole.OPERATIONAL_POLICY, "workspace")
            )
        for relative in self.policy.record_schema_paths:
            path = self.workspace_root / relative
            if not path.is_file():
                raise ValueError(f"missing canonical record schema: {relative}")
            artifacts.append(self._file_artifact(path, ArtifactRole.RECORD_SCHEMA, "workspace"))

        record_root = self.store_root / self.policy.record_directory
        if record_root.exists():
            for path in sorted(record_root.rglob("*.json")):
                if path.relative_to(record_root).parts[0] == "truth-snapshot":
                    continue
                artifacts.append(self._record_artifact(path, record_root))
        blob_root = self.store_root / self.policy.blob_directory
        if blob_root.exists():
            for path in sorted(item for item in blob_root.rglob("*") if item.is_file()):
                artifacts.append(self._blob_artifact(path, blob_root))
        return tuple(sorted(artifacts, key=lambda item: item.locator))

    def _file_artifact(
        self,
        path: Path,
        role: ArtifactRole,
        namespace: str,
    ) -> TruthArtifact:
        self._assert_confined(path, self.workspace_root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        relative = path.relative_to(self.workspace_root).as_posix()
        return TruthArtifact(
            locator=f"{namespace}:{relative}",
            role=role,
            canonical_sha256=digest,
            storage_sha256=digest,
            byte_length=len(content),
        )

    @staticmethod
    def _record_artifact(path: Path, root: Path) -> TruthArtifact:
        TruthManager._assert_confined(path, root)
        content = path.read_bytes()
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"invalid immutable JSON record: {path}") from None
        canonical_digest = hashlib.sha256(canonical_json(value)).hexdigest()
        if path.stem != canonical_digest:
            raise ValueError(f"immutable record filename does not match content: {path}")
        return TruthArtifact(
            locator=f"record:{path.relative_to(root).as_posix()}",
            role=ArtifactRole.IMMUTABLE_RECORD,
            canonical_sha256=canonical_digest,
            storage_sha256=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
        )

    @staticmethod
    def _blob_artifact(path: Path, root: Path) -> TruthArtifact:
        TruthManager._assert_confined(path, root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if path.name != digest:
            raise ValueError(f"blob filename does not match content: {path}")
        return TruthArtifact(
            locator=f"blob:{path.relative_to(root).as_posix()}",
            role=ArtifactRole.SOURCE_BLOB,
            canonical_sha256=digest,
            storage_sha256=digest,
            byte_length=len(content),
        )

    @staticmethod
    def _assert_confined(path: Path, root: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"canonical artifact escapes its configured root: {path}")


class ProjectionStamp(StrictModel):
    snapshot_id: str
    truth_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int = Field(ge=1)
    builder_version: str
    stamped_at: datetime


class SQLiteProjectionGuard:
    metadata_table = "_research_projection_metadata"

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock

    def stamp(
        self,
        database: Path,
        snapshot: TruthSnapshot,
        *,
        schema_version: int,
        builder_version: str,
    ) -> ProjectionStamp:
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.metadata_table} (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                )
                """
            )
            projection_digest = self.logical_digest(connection)
            stamp = ProjectionStamp(
                snapshot_id=snapshot.id,
                truth_state_digest=snapshot.state_digest,
                projection_digest=projection_digest,
                schema_version=schema_version,
                builder_version=builder_version,
                stamped_at=self.clock(),
            )
            connection.execute(
                f"""
                INSERT INTO {self.metadata_table}(singleton, payload)
                VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET payload = excluded.payload
                """,
                (canonical_json(stamp).decode(),),
            )
            connection.commit()
        return stamp

    def verify(
        self,
        database: Path,
        snapshot: TruthSnapshot,
        *,
        truth_report: DriftReport | None = None,
    ) -> DriftReport:
        if truth_report is not None and truth_report.snapshot_id != snapshot.id:
            raise ValueError("truth report does not apply to the selected snapshot")
        items = list(truth_report.items if truth_report else ())
        if not database.exists():
            items.append(DriftItem(kind=DriftKind.PROJECTION_MISSING, locator=str(database)))
        else:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                stamp = self._read_stamp(connection)
                if stamp is None:
                    items.append(
                        DriftItem(
                            kind=DriftKind.PROJECTION_UNSTAMPED,
                            locator=str(database),
                        )
                    )
                else:
                    if (
                        stamp.snapshot_id != snapshot.id
                        or stamp.truth_state_digest != snapshot.state_digest
                    ):
                        items.append(
                            DriftItem(
                                kind=DriftKind.PROJECTION_STALE,
                                locator=str(database),
                                expected=snapshot.state_digest,
                                actual=stamp.truth_state_digest,
                            )
                        )
                    actual_digest = self.logical_digest(connection)
                    if actual_digest != stamp.projection_digest:
                        items.append(
                            DriftItem(
                                kind=DriftKind.PROJECTION_MUTATED,
                                locator=str(database),
                                expected=stamp.projection_digest,
                                actual=actual_digest,
                            )
                        )
        canonical_drift = any(
            item.kind in {DriftKind.ADDED, DriftKind.MISSING, DriftKind.CHANGED} for item in items
        )
        return DriftReport(
            snapshot_id=snapshot.id,
            clean=not items,
            items=tuple(items),
            recommended_action=(
                "create_snapshot_then_rebuild"
                if canonical_drift
                else "discard_and_rebuild"
                if items
                else "none"
            ),
            checked_at=self.clock(),
        )

    def logical_digest(self, connection: sqlite3.Connection) -> str:
        objects = connection.execute(
            """
            SELECT type, name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND name != ?
            ORDER BY type, name
            """,
            (self.metadata_table,),
        ).fetchall()
        logical: list[dict[str, Any]] = []
        for object_type, name, sql in objects:
            entry: dict[str, Any] = {"type": object_type, "name": name, "sql": sql}
            if object_type == "table":
                quoted = name.replace('"', '""')
                rows = connection.execute(f'SELECT * FROM "{quoted}"').fetchall()
                normalized = [[_sqlite_value(value) for value in row] for row in rows]
                entry["rows"] = sorted(
                    normalized,
                    key=lambda row: canonical_json(row),
                )
            logical.append(entry)
        return hashlib.sha256(canonical_json(logical)).hexdigest()

    def _read_stamp(self, connection: sqlite3.Connection) -> ProjectionStamp | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self.metadata_table,),
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            f"SELECT payload FROM {self.metadata_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        return ProjectionStamp.model_validate_json(row[0])


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")
