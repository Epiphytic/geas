import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.store import ImmutableStore
from research_agent.truth import (
    DriftKind,
    SQLiteProjectionGuard,
    TruthManager,
    TruthPolicy,
)


def _policy() -> TruthPolicy:
    return TruthPolicy(
        version=1,
        ontology_globs=("ontology/**/*.yaml",),
        record_schema_paths=("schema.py",),
        record_directory="records",
        blob_directory="blobs/sha256",
        database_to_canonical="forbidden",
        canonical_drift_action="create_snapshot_then_rebuild",
        projection_drift_action="discard_and_rebuild",
    )


def _manager(
    workspace: Path,
    store: ImmutableStore,
    *,
    instant: datetime,
) -> TruthManager:
    return TruthManager(
        workspace_root=workspace,
        store_root=store.root,
        policy=_policy(),
        clock=lambda: instant,
    )


def _workspace(tmp_path: Path) -> tuple[Path, ImmutableStore]:
    workspace = tmp_path / "workspace"
    ontology = workspace / "ontology"
    ontology.mkdir(parents=True)
    (ontology / "knowledge.yaml").write_text("concepts:\n  - ontology\n")
    (workspace / "schema.py").write_text("SCHEMA_VERSION = 1\n")
    source = workspace / "source.txt"
    source.write_text("canonical evidence")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    store.ingest_file(source)
    return workspace, store


def test_truth_snapshot_is_clean_and_excludes_its_own_record(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    digest = store.put_record("truth-snapshot", snapshot)

    report = manager.verify(snapshot)

    assert report.clean
    assert report.recommended_action == "none"
    assert store.record_path("truth-snapshot", digest).is_file()
    assert all(not item.locator.startswith("record:truth-snapshot/") for item in snapshot.artifacts)


def test_ontology_change_is_canonical_drift(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    (workspace / "ontology" / "knowledge.yaml").write_text("concepts:\n  - changed\n")

    report = manager.verify(snapshot)

    assert not report.clean
    assert report.recommended_action == "create_snapshot_then_rebuild"
    assert report.items[0].kind is DriftKind.CHANGED
    assert report.items[0].locator == "workspace:ontology/knowledge.yaml"


def test_required_git_tracking_excludes_unreviewed_ontology_candidates(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(
        ("git", "-C", str(workspace), "add", "ontology/knowledge.yaml", "schema.py"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "canonical fixture",
        ),
        check=True,
    )
    candidate = workspace / "ontology" / "generated" / "candidate.yaml"
    candidate.parent.mkdir()
    candidate.write_text("claims:\n  - unreviewed\n")
    policy = _policy().model_copy(update={"ontology_git_tracking": "required"})
    manager = TruthManager(
        workspace_root=workspace,
        store_root=store.root,
        policy=policy,
        clock=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )

    snapshot = manager.capture(created_by="operator:test")

    locators = {item.locator for item in snapshot.artifacts}
    assert "workspace:ontology/knowledge.yaml" in locators
    assert "workspace:ontology/generated/candidate.yaml" not in locators

    (workspace / "ontology" / "knowledge.yaml").write_text(
        "concepts:\n  - dirty-unreviewed-change\n"
    )
    with pytest.raises(ValueError, match="differs from HEAD"):
        manager.capture(created_by="operator:test")


def test_new_immutable_record_is_detected_as_added_truth(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    store.put_record("claim", {"id": "claim:new", "value": "new"})

    report = manager.verify(snapshot)

    assert any(item.kind is DriftKind.ADDED for item in report.items)


def test_corrupt_immutable_record_fails_closed(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    record = next(store.record_root.rglob("*.json"))
    record.write_text("{}")

    with pytest.raises(ValueError, match="filename does not match content"):
        manager.capture(created_by="operator:test")


def test_canonical_symlink_escape_fails_closed(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("concepts: [outside]\n")
    (workspace / "ontology" / "escape.yaml").symlink_to(outside)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="escapes its configured root"):
        _manager(workspace, store, instant=instant).capture(created_by="operator:test")


def test_projection_mutation_is_detected_and_rebuild_is_required(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE concept(id TEXT PRIMARY KEY, label TEXT)")
        connection.execute("INSERT INTO concept VALUES ('concept:1', 'Ontology')")
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )
    assert guard.verify(database, snapshot, truth_report=manager.verify(snapshot)).clean

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE concept SET label = 'Mutated'")

    report = guard.verify(database, snapshot, truth_report=manager.verify(snapshot))
    assert not report.clean
    assert report.recommended_action == "discard_and_rebuild"
    assert any(item.kind is DriftKind.PROJECTION_MUTATED for item in report.items)


def test_projection_stale_against_new_truth_snapshot(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    old_snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        old_snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )
    (workspace / "ontology" / "knowledge.yaml").write_text("concepts:\n  - expanded\n")
    new_snapshot = manager.capture(created_by="operator:test", predecessor=old_snapshot.id)

    report = guard.verify(
        database,
        new_snapshot,
        truth_report=manager.verify(new_snapshot),
    )

    assert not report.clean
    assert any(item.kind is DriftKind.PROJECTION_STALE for item in report.items)


def test_policy_forbids_database_as_canonical_source() -> None:
    with pytest.raises(ValidationError):
        TruthPolicy.model_validate(
            {
                **_policy().model_dump(mode="json"),
                "database_to_canonical": "allowed",
            }
        )
