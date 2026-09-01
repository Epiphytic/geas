import ctypes
import sqlite3
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import research_agent.truth as truth_module
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
    artifact_manifest = workspace / "ontology" / "sample" / "artifacts.yaml"
    artifact_manifest.parent.mkdir()
    artifact_manifest.write_text("version: 1\nartifacts: []\n")
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(workspace),
            "add",
            "ontology/knowledge.yaml",
            "ontology/sample/artifacts.yaml",
            "schema.py",
        ),
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
    policy = _policy().model_copy(
        update={
            "ontology_exclude_globs": ("ontology/**/artifacts.yaml",),
            "ontology_git_tracking": "required",
        }
    )
    manager = TruthManager(
        workspace_root=workspace,
        store_root=store.root,
        policy=policy,
        clock=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )

    snapshot = manager.capture(created_by="operator:test")

    locators = {item.locator for item in snapshot.artifacts}
    assert "workspace:ontology/knowledge.yaml" in locators
    assert "workspace:ontology/sample/artifacts.yaml" not in locators
    assert "workspace:ontology/generated/candidate.yaml" not in locators

    (workspace / "ontology" / "knowledge.yaml").write_text(
        "concepts:\n  - dirty-unreviewed-change\n"
    )
    dirty_snapshot = manager.capture(created_by="operator:test")
    assert dirty_snapshot.state_digest == snapshot.state_digest
    assert manager.verify(snapshot).clean

    (workspace / "ontology" / "knowledge.yaml").unlink()
    missing_snapshot = manager.capture(created_by="operator:test")
    assert missing_snapshot.state_digest == snapshot.state_digest


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


def _projection_candidate_paths(database: Path) -> tuple[Path, ...]:
    return tuple(database.parent.glob(f".{database.name}.*"))


def _write_projection_fixture(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE concept(id TEXT PRIMARY KEY, label TEXT)")
        connection.execute("INSERT INTO concept VALUES ('concept:1', 'Ontology')")


def test_projection_stamp_foreign_key_failure_preserves_unstamped_source(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
            INSERT INTO child(parent_id) VALUES (99);
            """
        )
    original = database.read_bytes()
    guard = SQLiteProjectionGuard(clock=lambda: instant)

    with pytest.raises(ValueError, match="foreign-key"):
        guard.stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()
    assert not guard.verify(database, snapshot).clean
    with pytest.raises(ValueError, match="unstamped|invalid|foreign-key"):
        guard.require_compatible(
            database,
            expected_schema_version=1,
            expected_builder_version="projection-builder/test",
        )


@pytest.mark.parametrize(
    ("boundary", "message"),
    (
        ("_normalize_sqlite_header", "header write"),
        ("_validate_stamped_projection", "integrity"),
        ("_fsync_file", "file fsync"),
        ("_fsync_directory", "directory fsync"),
    ),
)
def test_projection_stamp_candidate_failure_preserves_original_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    message: str,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(message)

    monkeypatch.setattr(truth_module, boundary, fail, raising=False)

    with pytest.raises(OSError, match=message):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_post_canonical_integrity_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    canonicalize = truth_module._canonicalize_sqlite_projection

    def corrupt_after_canonicalization(
        candidate: Path,
        authority: object,
    ) -> None:
        canonicalize(candidate, authority)
        with candidate.open("r+b") as stream:
            stream.seek(4096)
            stream.write(b"\xff")

    monkeypatch.setattr(
        truth_module,
        "_canonicalize_sqlite_projection",
        corrupt_after_canonicalization,
    )

    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_preserves_source_mode(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    database.chmod(0o640)

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert stat.S_IMODE(database.stat().st_mode) == 0o640
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_can_replace_read_only_source_and_preserves_mode(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    database.chmod(0o444)

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert stat.S_IMODE(database.stat().st_mode) == 0o444
    assert SQLiteProjectionGuard().require_compatible(
        database,
        expected_schema_version=1,
        expected_builder_version="projection-builder/test",
    ).snapshot_id == snapshot.id


def test_projection_stamp_rejects_active_sidecar_without_mutation(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    sidecar = Path(f"{database}-wal")
    sidecar.write_bytes(b"active")

    with pytest.raises(ValueError, match="active SQLite sidecar"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert sidecar.read_bytes() == b"active"
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_rejects_source_change_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    replace = truth_module._replace_candidate_if_source_unchanged

    def mutate_then_replace(*args: object, **kwargs: object) -> None:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO concept VALUES ('concept:concurrent', 'Concurrent')"
            )
        replace(*args, **kwargs)

    monkeypatch.setattr(
        truth_module,
        "_replace_candidate_if_source_unchanged",
        mutate_then_replace,
    )

    with pytest.raises(ValueError, match="changed while projection stamp was prepared"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT label FROM concept WHERE id = 'concept:concurrent'"
        ).fetchone() == ("Concurrent",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = '_research_projection_metadata'"
        ).fetchone() is None
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_closes_candidate_descriptor_before_namespace_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    apply_mode = truth_module._apply_candidate_mode
    install = truth_module._replace_candidate_if_source_unchanged
    candidate_descriptor = -1

    def record_descriptor(*args: object, **kwargs: object) -> None:
        nonlocal candidate_descriptor
        candidate_descriptor = int(args[0])
        apply_mode(*args, **kwargs)

    def assert_closed_before_install(*args: object, **kwargs: object) -> None:
        with pytest.raises(OSError):
            truth_module.os.fstat(candidate_descriptor)
        install(*args, **kwargs)

    monkeypatch.setattr(truth_module, "_apply_candidate_mode", record_descriptor)
    monkeypatch.setattr(
        truth_module,
        "_replace_candidate_if_source_unchanged",
        assert_closed_before_install,
    )

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert candidate_descriptor >= 0


def test_projection_stamp_rejects_copy_not_bound_to_authenticated_source_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    alternate = tmp_path / "alternate.sqlite"
    _write_projection_fixture(alternate)
    with sqlite3.connect(alternate) as connection:
        connection.execute("UPDATE concept SET label = 'Mutated!'")
    alternate_bytes = alternate.read_bytes()
    assert len(alternate_bytes) == len(original)

    read = truth_module.os.read
    source_descriptor: int | None = None
    source_reads = 0
    restored = False

    def mutate_during_second_source_pass(file_descriptor: int, size: int) -> bytes:
        nonlocal source_descriptor, source_reads, restored
        if source_descriptor is None:
            source_descriptor = file_descriptor
        if file_descriptor == source_descriptor and not restored:
            source_reads += 1
            if source_reads == 3:
                with database.open("r+b") as stream:
                    stream.write(alternate_bytes)
                    stream.flush()
                    truth_module.os.fsync(stream.fileno())
            elif source_reads == 4:
                with database.open("r+b") as stream:
                    stream.write(original)
                    stream.flush()
                    truth_module.os.fsync(stream.fileno())
                restored = True
        return read(file_descriptor, size)

    monkeypatch.setattr(truth_module.os, "read", mutate_during_second_source_pass)

    with pytest.raises(ValueError, match="copied knowledge projection.*source"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_atomic_exchange_preserves_concurrent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    concurrent = tmp_path / "concurrent.sqlite"
    with sqlite3.connect(concurrent) as connection:
        connection.execute("CREATE TABLE concurrent(value TEXT)")
        connection.execute("INSERT INTO concurrent VALUES ('preserve me')")
    concurrent_bytes = concurrent.read_bytes()
    exchange = truth_module._atomic_exchange_paths
    calls = 0

    def replace_at_exchange_boundary(first: Path, second: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            concurrent.replace(database)
        exchange(first, second)

    monkeypatch.setattr(
        truth_module,
        "_atomic_exchange_paths",
        replace_at_exchange_boundary,
    )

    with pytest.raises(ValueError, match="changed while projection stamp was prepared"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == concurrent_bytes
    assert _projection_candidate_paths(database) == ()


def test_projection_stamp_rejects_unsupported_atomic_exchange_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    monkeypatch.setattr(truth_module.sys, "platform", "unsupported")

    with pytest.raises(OSError, match="atomic projection exchange is unsupported"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


@pytest.mark.skipif(
    truth_module.sys.platform == "win32",
    reason="native Windows uses ReplaceFileW/MoveFileExW adapters",
)
def test_atomic_exchange_swaps_two_regular_files(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    truth_module._atomic_exchange_paths(first, second)

    assert first.read_bytes() == b"second"
    assert second.read_bytes() == b"first"


def test_atomic_rollback_never_overwrites_newer_destination_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    candidate = transaction / "candidate.sqlite"
    candidate.write_bytes(b"stamped candidate")
    candidate_information = candidate.stat()
    parent_information = tmp_path.stat()
    directory_information = transaction.stat()
    authority = truth_module._CandidateAuthority(
        parent_directory=tmp_path,
        parent_device=parent_information.st_dev,
        parent_inode=parent_information.st_ino,
        transaction_directory=transaction,
        directory_device=directory_information.st_dev,
        directory_inode=directory_information.st_ino,
        candidate=candidate,
        candidate_device=candidate_information.st_dev,
        candidate_inode=candidate_information.st_ino,
    )
    database = tmp_path / "query.sqlite"
    database.write_bytes(b"selected source")
    source_identity = truth_module._capture_projection_identity(database)
    assert source_identity is not None
    truth_module._atomic_exchange_paths(candidate, database)
    newer = tmp_path / "newer.sqlite"
    newer.write_bytes(b"newer destination")
    newer_bytes = newer.read_bytes()
    moves = 0

    def move_no_replace(source: Path, destination: Path) -> None:
        nonlocal moves
        moves += 1
        if moves == 1:
            newer.replace(database)
        if destination.exists():
            raise FileExistsError(destination)
        source.rename(destination)

    monkeypatch.setattr(
        truth_module,
        "_move_path_no_replace",
        move_no_replace,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="rollback.*(quarantined|without clobbering)"):
        truth_module._rollback_atomic_exchange(
            candidate,
            database,
            source_identity,
            authority,
        )

    assert database.read_bytes() == newer_bytes


def test_projection_stamp_rejects_sidecar_created_at_exchange_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    sidecar = Path(f"{database}-wal")
    exchange = truth_module._atomic_exchange_paths
    exchanges = 0

    def add_sidecar_after_exchange(first: Path, second: Path) -> None:
        nonlocal exchanges
        exchange(first, second)
        exchanges += 1
        if exchanges == 1:
            sidecar.write_bytes(b"concurrent sqlite owner")

    monkeypatch.setattr(
        truth_module,
        "_atomic_exchange_paths",
        add_sidecar_after_exchange,
    )

    with pytest.raises(
        (ValueError, RuntimeError),
        match="sidecar|rollback.*quarantined",
    ):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert sidecar.read_bytes() == b"concurrent sqlite owner"


def test_windows_projection_replace_adapter_preserves_atomic_cas_and_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    concurrent = tmp_path / "concurrent.sqlite"
    concurrent.write_bytes(b"concurrent owner")
    concurrent_bytes = concurrent.read_bytes()
    replace_calls = 0
    flushes: list[Path] = []

    def replace_file(database_path: Path, candidate: Path, backup: Path | None) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            concurrent.replace(database_path)
        if backup is None:
            database_path.unlink()
            candidate.replace(database_path)
            return
        database_path.replace(backup)
        candidate.replace(database_path)

    monkeypatch.setattr(truth_module.sys, "platform", "win32")
    monkeypatch.setattr(truth_module, "_windows_replace_file", replace_file)
    monkeypatch.setattr(
        truth_module,
        "_windows_flush_directory",
        lambda path: flushes.append(path),
    )
    monkeypatch.setattr(
        truth_module,
        "_windows_apply_candidate_mode",
        lambda fd, mode: truth_module.os.fchmod(fd, mode),
    )

    with pytest.raises(ValueError, match="changed while projection stamp was prepared"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == concurrent_bytes
    assert replace_calls == 1
    assert flushes
    assert _projection_candidate_paths(database) == ()


def test_windows_projection_rollback_never_overwrites_newer_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    displaced = tmp_path / "displaced.sqlite"
    displaced.write_bytes(b"displaced concurrent owner")
    newer = tmp_path / "newer.sqlite"
    newer.write_bytes(b"newer concurrent owner")
    newer_bytes = newer.read_bytes()
    move_calls = 0

    def replace_file(database_path: Path, candidate: Path, backup: Path | None) -> None:
        displaced.replace(database_path)
        assert backup is not None
        database_path.replace(backup)
        candidate.replace(database_path)

    def move_no_replace(source: Path, target: Path) -> None:
        nonlocal move_calls
        move_calls += 1
        if move_calls == 1:
            newer.replace(database)
        if target.exists():
            raise FileExistsError(target)
        source.rename(target)

    monkeypatch.setattr(truth_module.sys, "platform", "win32")
    monkeypatch.setattr(truth_module, "_windows_replace_file", replace_file)
    monkeypatch.setattr(truth_module, "_move_path_no_replace", move_no_replace)
    monkeypatch.setattr(truth_module, "_windows_move_no_replace", move_no_replace)
    monkeypatch.setattr(truth_module, "_windows_flush_directory", lambda path: None)
    monkeypatch.setattr(
        truth_module,
        "_windows_apply_candidate_mode",
        lambda fd, mode: truth_module.os.fchmod(fd, mode),
    )

    with pytest.raises(
        (RuntimeError, ValueError),
        match=(
            "rollback.*(quarantined|without clobbering)"
            "|changed while projection stamp was prepared"
        ),
    ):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == newer_bytes


def test_windows_replace_file_uses_only_supported_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeFunction:
        def __init__(self, result: int = 1) -> None:
            self.result = result

        def __call__(self, *args: object) -> int:
            calls.append(args)
            return self.result

    class Kernel32:
        ReplaceFileW = FakeFunction()

    monkeypatch.setattr(truth_module, "_windows_kernel32", lambda: Kernel32())

    truth_module._windows_replace_file(
        tmp_path / "database.sqlite",
        tmp_path / "candidate.sqlite",
        tmp_path / "backup.sqlite",
    )

    assert calls == [
        (
            str(tmp_path / "database.sqlite"),
            str(tmp_path / "candidate.sqlite"),
            str(tmp_path / "backup.sqlite"),
            0,
            None,
            None,
        )
    ]


@pytest.mark.parametrize("winerror", (1175, 1176, 1177))
def test_windows_documented_replace_failures_preserve_selected_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    candidate = transaction / "candidate.sqlite"
    candidate.write_bytes(b"candidate bytes")
    candidate_information = candidate.stat()
    authority = truth_module._CandidateAuthority(
        parent_directory=tmp_path,
        parent_device=tmp_path.stat().st_dev,
        parent_inode=tmp_path.stat().st_ino,
        transaction_directory=transaction,
        directory_device=transaction.stat().st_dev,
        directory_inode=transaction.stat().st_ino,
        candidate=candidate,
        candidate_device=candidate_information.st_dev,
        candidate_inode=candidate_information.st_ino,
    )
    database = tmp_path / "query.sqlite"
    database.write_bytes(b"selected destination")
    selected = database.read_bytes()
    identity = truth_module._capture_projection_identity(database)
    assert identity is not None

    def fail_replace(
        database_path: Path,
        _candidate_path: Path,
        backup_path: Path | None,
    ) -> None:
        assert backup_path is not None
        if winerror == 1177:
            database_path.replace(backup_path)
        error = OSError(f"ReplaceFileW failed with {winerror}")
        error.winerror = winerror  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(truth_module, "_windows_flush_directory", lambda path: None)
    monkeypatch.setattr(truth_module, "_windows_replace_file", fail_replace)

    with pytest.raises(OSError, match=str(winerror)):
        truth_module._replace_candidate_windows(
            candidate,
            database,
            identity,
            authority,
        )

    assert database.read_bytes() == selected
    assert candidate.read_bytes() == b"candidate bytes"
    assert not (transaction / "displaced.sqlite").exists()


def test_windows_kernel32_prototypes_preserve_64_bit_handles() -> None:
    class FakeFunction:
        argtypes: object = None
        restype: object = None

    class Kernel32:
        CreateFileW = FakeFunction()
        FlushFileBuffers = FakeFunction()
        CloseHandle = FakeFunction()
        ReplaceFileW = FakeFunction()
        MoveFileExW = FakeFunction()
        GetFileInformationByHandleEx = FakeFunction()
        SetFileInformationByHandle = FakeFunction()
        CreateDirectoryW = FakeFunction()
        LocalFree = FakeFunction()

    kernel32 = truth_module._configure_windows_kernel32(Kernel32())

    assert kernel32.CreateFileW.restype is truth_module.wintypes.HANDLE
    assert kernel32.FlushFileBuffers.argtypes == [truth_module.wintypes.HANDLE]
    assert kernel32.CloseHandle.argtypes == [truth_module.wintypes.HANDLE]
    assert kernel32.GetFileInformationByHandleEx.argtypes[0] is (
        truth_module.wintypes.HANDLE
    )
    assert kernel32.SetFileInformationByHandle.argtypes[0] is (
        truth_module.wintypes.HANDLE
    )


def test_windows_directory_durability_uses_write_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[tuple[object, ...]] = []
    flushed: list[int] = []
    closed: list[int] = []

    class FakeFunction:
        restype: object = None

        def __init__(self, function: object) -> None:
            self.function = function

        def __call__(self, *args: object) -> object:
            return self.function(*args)  # type: ignore[operator]

    class Kernel32:
        CreateFileW = FakeFunction(
            lambda *args: create_calls.append(args) or 41
        )
        FlushFileBuffers = FakeFunction(
            lambda handle: flushed.append(handle) or 1
        )
        CloseHandle = FakeFunction(lambda handle: closed.append(handle) or 1)

    monkeypatch.setattr(truth_module, "_windows_kernel32", lambda: Kernel32())

    truth_module._windows_flush_directory(tmp_path)

    assert create_calls[0][1] == 0x40000000  # GENERIC_WRITE
    assert create_calls[0][5] & 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    assert flushed == [41]
    assert closed == [41]


def test_windows_private_directory_uses_protected_owner_only_dacl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sddl: list[str] = []
    created: list[str] = []

    class FakeFunction:
        def __init__(self, function: object) -> None:
            self.function = function

        def __call__(self, *args: object) -> object:
            return self.function(*args)  # type: ignore[operator]

    def convert(value: str, revision: int, descriptor: object, size: object) -> int:
        sddl.append(value)
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(7)
        return 1

    class Advapi32:
        ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(convert)

    class Kernel32:
        CreateDirectoryW = FakeFunction(
            lambda path, attributes: created.append(path) or 1
        )
        LocalFree = FakeFunction(lambda descriptor: 0)

    monkeypatch.setattr(
        truth_module.ctypes,
        "WinDLL",
        lambda *args, **kwargs: Advapi32(),
        raising=False,
    )
    monkeypatch.setattr(truth_module, "_windows_kernel32", lambda: Kernel32())

    destination = tmp_path / "private"
    truth_module._windows_create_private_directory(destination)

    assert sddl == ["D:P(A;;FA;;;OW)"]
    assert created == [str(destination)]


def test_windows_candidate_mode_updates_readonly_attribute_by_open_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, int]] = []

    class FakeFunction:
        def __init__(self, function: object) -> None:
            self.function = function

        def __call__(self, *args: object) -> object:
            return self.function(*args)  # type: ignore[operator]

    def get_info(handle: int, kind: int, pointer: object, size: int) -> int:
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(truth_module._WindowsFileBasicInfo),
        )[0]
        information.FileAttributes = 0x20
        return 1

    def set_info(handle: int, kind: int, pointer: object, size: int) -> int:
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(truth_module._WindowsFileBasicInfo),
        )[0]
        observed.append((handle, information.FileAttributes))
        return 1

    class Kernel32:
        GetFileInformationByHandleEx = FakeFunction(get_info)
        SetFileInformationByHandle = FakeFunction(set_info)

    monkeypatch.setattr(truth_module, "_windows_kernel32", lambda: Kernel32())
    monkeypatch.setattr(truth_module, "_windows_os_handle", lambda descriptor: 71)

    truth_module._windows_apply_candidate_mode(9, 0o444)

    assert observed == [(71, 0x21)]


def test_windows_projection_move_adapter_installs_absent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    flushes: list[Path] = []

    def move_no_replace(candidate: Path, target: Path) -> None:
        if target.exists():
            raise FileExistsError(target)
        candidate.replace(target)

    monkeypatch.setattr(truth_module.sys, "platform", "win32")
    monkeypatch.setattr(truth_module, "_move_path_no_replace", move_no_replace)
    monkeypatch.setattr(
        truth_module,
        "_windows_flush_directory",
        lambda path: flushes.append(path),
    )
    monkeypatch.setattr(
        truth_module,
        "_windows_apply_candidate_mode",
        lambda fd, mode: truth_module.os.fchmod(fd, mode),
    )

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert SQLiteProjectionGuard().require_compatible(
        database,
        expected_schema_version=1,
        expected_builder_version="projection-builder/test",
    ).snapshot_id == snapshot.id
    assert flushes


def test_absent_projection_install_never_unlinks_replacement_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"replacement candidate owner")
    replacement_bytes = replacement.read_bytes()
    unlink = Path.unlink

    def replace_at_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "candidate.sqlite" and ".stamp-" in path.parent.name:
            replacement.replace(path)
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", replace_at_unlink)

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert replacement.read_bytes() == replacement_bytes


def test_windows_absent_projection_rollback_preserves_newer_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    newer = tmp_path / "newer.sqlite"
    newer.write_bytes(b"newer absent-destination owner")
    newer_bytes = newer.read_bytes()
    moves = 0

    def move_no_replace(source: Path, target: Path) -> None:
        nonlocal moves
        moves += 1
        if moves == 2:
            newer.replace(database)
        if target.exists():
            raise FileExistsError(target)
        source.rename(target)

    flushes = 0

    def fail_after_install(path: Path) -> None:
        nonlocal flushes
        flushes += 1
        if flushes == 2:
            raise OSError("post-install durability failure")

    monkeypatch.setattr(truth_module.sys, "platform", "win32")
    monkeypatch.setattr(truth_module, "_move_path_no_replace", move_no_replace)
    monkeypatch.setattr(truth_module, "_windows_flush_directory", fail_after_install)
    monkeypatch.setattr(
        truth_module,
        "_windows_apply_candidate_mode",
        lambda fd, mode: truth_module.os.fchmod(fd, mode),
    )

    with pytest.raises((OSError, RuntimeError)):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == newer_bytes


def test_candidate_mode_fallback_does_not_follow_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    database.chmod(0o444)
    monkeypatch.delattr(truth_module.os, "fchmod")

    SQLiteProjectionGuard(clock=lambda: instant).stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert stat.S_IMODE(database.stat().st_mode) == 0o444


def test_windows_candidate_mode_uses_open_handle_on_python_312(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate.sqlite"
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    candidate = transaction / "candidate.sqlite"
    candidate.write_bytes(b"candidate")
    descriptor = truth_module.os.open(candidate, truth_module.os.O_RDWR)
    calls: list[tuple[int, int]] = []
    fchmod = truth_module.os.fchmod
    try:
        monkeypatch.setattr(truth_module.sys, "platform", "win32")
        monkeypatch.delattr(truth_module.os, "fchmod", raising=False)
        monkeypatch.setattr(
            truth_module.os,
            "chmod",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("path chmod must not be used on Windows 3.12")
            ),
        )
        monkeypatch.setattr(
            truth_module,
            "_windows_apply_candidate_mode",
            lambda fd, mode: (calls.append((fd, mode)), fchmod(fd, mode)),
            raising=False,
        )
        information = candidate.stat()
        authority = truth_module._CandidateAuthority(
            parent_directory=tmp_path,
            parent_device=tmp_path.stat().st_dev,
            parent_inode=tmp_path.stat().st_ino,
            transaction_directory=transaction,
            directory_device=transaction.stat().st_dev,
            directory_inode=transaction.stat().st_ino,
            candidate=candidate,
            candidate_device=information.st_dev,
            candidate_inode=information.st_ino,
        )

        truth_module._apply_candidate_mode(descriptor, authority, 0o600)
    finally:
        truth_module.os.close(descriptor)

    assert calls == [(descriptor, 0o600)]


def test_raw_sqlite_open_includes_platform_binary_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    binary_flag = 0x40000000
    opened_flags: list[int] = []
    open_file = truth_module.os.open

    def record_open(path: object, flags: int, *args: object) -> int:
        opened_flags.append(flags)
        return open_file(path, flags & ~binary_flag, *args)

    monkeypatch.setattr(truth_module, "_O_BINARY", binary_flag)
    monkeypatch.setattr(truth_module.os, "open", record_open)

    file_descriptor = truth_module._open_projection_read_only(database)
    truth_module.os.close(file_descriptor)

    assert opened_flags
    assert all(flags & binary_flag for flags in opened_flags)


def test_projection_stamp_rejects_candidate_symlink_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(original)
    outside_bytes = outside.read_bytes()
    validate = truth_module._candidate_ready_for_sqlite

    def substitute_then_validate(authority: object) -> None:
        candidate = authority.candidate
        candidate.unlink()
        candidate.symlink_to(outside)
        validate(authority)

    monkeypatch.setattr(
        truth_module,
        "_candidate_ready_for_sqlite",
        substitute_then_validate,
    )

    with pytest.raises(ValueError, match="candidate.*unsafe|identity"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert outside.read_bytes() == outside_bytes
    assert database.read_bytes() == original
    leftovers = _projection_candidate_paths(database)
    assert len(leftovers) == 1
    replacement = leftovers[0] / "candidate.sqlite"
    assert replacement.is_symlink()
    assert replacement.resolve() == outside


def test_projection_stamp_never_unlinks_substituted_candidate_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    original = database.read_bytes()
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(original)
    validate = truth_module._candidate_ready_for_sqlite

    def substitute_then_validate(authority: object) -> None:
        candidate = authority.candidate
        candidate.unlink()
        candidate.hardlink_to(outside)
        validate(authority)

    monkeypatch.setattr(
        truth_module,
        "_candidate_ready_for_sqlite",
        substitute_then_validate,
    )

    with pytest.raises(ValueError, match="candidate.*unsafe|identity"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    leftovers = _projection_candidate_paths(database)
    assert len(leftovers) == 1
    replacement = leftovers[0] / "candidate.sqlite"
    assert replacement.stat().st_ino == outside.stat().st_ino
    assert outside.stat().st_nlink == 2
    assert outside.read_bytes() == original
    assert database.read_bytes() == original


@pytest.mark.parametrize("operation", ("candidate", "builder"))
def test_projection_cleanup_never_unlinks_replacement_at_remove_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    transaction = tmp_path / "transaction"
    transaction.mkdir(mode=0o700)
    candidate = transaction / "candidate.sqlite"
    candidate.write_bytes(b"owned candidate")
    expected_identity = truth_module._capture_projection_identity(candidate)
    assert expected_identity is not None
    candidate_information = candidate.stat()
    authority = truth_module._CandidateAuthority(
        parent_directory=tmp_path,
        parent_device=tmp_path.stat().st_dev,
        parent_inode=tmp_path.stat().st_ino,
        transaction_directory=transaction,
        directory_device=transaction.stat().st_dev,
        directory_inode=transaction.stat().st_ino,
        candidate=candidate,
        candidate_device=candidate_information.st_dev,
        candidate_inode=candidate_information.st_ino,
    )
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"replacement owner")
    replacement_bytes = replacement.read_bytes()
    move = truth_module._move_path_no_replace
    injected = False

    def replace_at_remove_boundary(source: Path, destination: Path) -> None:
        nonlocal injected
        if source == candidate and not injected:
            replacement.replace(candidate)
            injected = True
        move(source, destination)

    monkeypatch.setattr(
        truth_module,
        "_move_path_no_replace",
        replace_at_remove_boundary,
    )

    with pytest.raises(ValueError, match="identity.*unsafe"):
        if operation == "candidate":
            truth_module._unlink_candidate_identity(authority)
        else:
            SQLiteProjectionGuard.cleanup_install_transaction(
                candidate,
                expected_identity,
            )

    assert candidate.read_bytes() == replacement_bytes


@pytest.mark.parametrize("operation", ("verify", "require"))
def test_projection_reader_rejects_source_replacement_after_snapshot_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )
    concurrent = tmp_path / "concurrent.sqlite"
    _write_projection_fixture(concurrent)
    with sqlite3.connect(concurrent) as connection:
        connection.execute("UPDATE concept SET label = 'Concurrent'")
    concurrent_bytes = concurrent.read_bytes()
    validate = truth_module._validated_projection_state_on_connection

    def replace_after_validation(*args: object, **kwargs: object) -> object:
        result = validate(*args, **kwargs)
        concurrent.replace(database)
        return result

    monkeypatch.setattr(
        truth_module,
        "_validated_projection_state_on_connection",
        replace_after_validation,
    )

    if operation == "verify":
        assert not guard.verify(database, snapshot).clean
    else:
        with pytest.raises(ValueError, match="changed while it was validated"):
            guard.require_compatible(
                database,
                expected_schema_version=1,
                expected_builder_version="projection-builder/test",
            )

    assert database.read_bytes() == concurrent_bytes


@pytest.mark.parametrize(
    "failure",
    (
        "missing",
        "token",
        "open",
        "short-write",
        "partial-write",
        "fstat",
        "descriptor-stats",
        "authority",
        "copy",
        "connect",
    ),
)
def test_projection_reader_failure_cleans_private_validation_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    validation_root = tmp_path / "validation-root"
    validation_root.mkdir()
    created: list[Path] = []

    def make_validation_directory(*, prefix: str, dir: Path | None = None) -> str:
        assert dir is None
        directory = validation_root / f"{prefix}{len(created)}"
        directory.mkdir(mode=0o700)
        created.append(directory)
        return str(directory)

    monkeypatch.setattr(truth_module.tempfile, "mkdtemp", make_validation_directory)
    database = tmp_path / "query.sqlite"
    if failure != "missing":
        _write_projection_fixture(database)
    if failure == "token":
        monkeypatch.setattr(
            truth_module.secrets,
            "token_bytes",
            lambda length: (_ for _ in ()).throw(OSError("token generation failed")),
        )
    elif failure == "open":
        open_file = truth_module.os.open

        def fail_candidate_open(path: object, *args: object, **kwargs: object) -> int:
            if Path(path).name == "projection.sqlite":
                raise OSError("candidate open failed")
            return open_file(path, *args, **kwargs)

        monkeypatch.setattr(truth_module.os, "open", fail_candidate_open)
    elif failure in ("short-write", "partial-write"):
        write_file = truth_module.os.write
        writes = 0

        def bounded_candidate_write(file_descriptor: int, content: bytes) -> int:
            nonlocal writes
            writes += 1
            if failure == "partial-write" and writes == 2:
                raise OSError("candidate token write failed")
            return write_file(file_descriptor, content[:3])

        monkeypatch.setattr(truth_module.os, "write", bounded_candidate_write)
        if failure == "short-write":
            stat_file = truth_module.os.stat
            monkeypatch.setattr(
                truth_module.os,
                "fstat",
                lambda file_descriptor: (_ for _ in ()).throw(
                    OSError("candidate fstat failed")
                ),
            )
            monkeypatch.setattr(
                truth_module.os,
                "stat",
                lambda path, *args, **kwargs: (_ for _ in ()).throw(
                    OSError("candidate descriptor stat failed")
                )
                if isinstance(path, int)
                else stat_file(path, *args, **kwargs),
            )
    elif failure == "fstat":
        fstat = truth_module.os.fstat
        failed = False

        def fail_candidate_fstat(file_descriptor: int) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("candidate fstat failed")
            return fstat(file_descriptor)

        monkeypatch.setattr(truth_module.os, "fstat", fail_candidate_fstat)
    elif failure == "descriptor-stats":
        stat_file = truth_module.os.stat
        monkeypatch.setattr(
            truth_module.os,
            "fstat",
            lambda file_descriptor: (_ for _ in ()).throw(
                OSError("candidate fstat failed")
            ),
        )
        monkeypatch.setattr(
            truth_module.os,
            "stat",
            lambda path, *args, **kwargs: (_ for _ in ()).throw(
                OSError("candidate descriptor stat failed")
            )
            if isinstance(path, int)
            else stat_file(path, *args, **kwargs),
        )
    elif failure == "authority":
        monkeypatch.setattr(
            truth_module,
            "_CandidateAuthority",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("authority construction failed")
            ),
        )
    elif failure == "copy":
        monkeypatch.setattr(
            truth_module,
            "_copy_projection_candidate",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("copy failed")),
        )
    elif failure == "connect":
        monkeypatch.setattr(
            truth_module,
            "_connect_sqlite",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("connect failed")
            ),
        )

    expected_error = {
        "missing": ValueError,
        "token": OSError,
        "open": OSError,
        "short-write": OSError,
        "partial-write": OSError,
        "fstat": OSError,
        "descriptor-stats": OSError,
        "authority": ValueError,
        "copy": OSError,
        "connect": sqlite3.OperationalError,
    }[failure]
    with pytest.raises(expected_error):
        truth_module._open_stable_projection_connection(database)

    assert created
    assert tuple(validation_root.iterdir()) == ()


def test_projection_reader_rejects_sidecar_created_after_snapshot_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )
    validate = truth_module._validated_projection_state_on_connection

    def add_sidecar_after_validation(*args: object, **kwargs: object) -> object:
        result = validate(*args, **kwargs)
        Path(f"{database}-wal").write_bytes(b"concurrent")
        return result

    monkeypatch.setattr(
        truth_module,
        "_validated_projection_state_on_connection",
        add_sidecar_after_validation,
    )

    assert not guard.verify(database, snapshot).clean
    with pytest.raises(ValueError, match="changed while it was validated|active SQLite sidecar"):
        guard.require_compatible(
            database,
            expected_schema_version=1,
            expected_builder_version="projection-builder/test",
        )


def test_projection_reader_rejects_symlink_without_nofollow_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    _write_projection_fixture(database)
    outside = tmp_path / "outside.sqlite"
    database.replace(outside)
    database.symlink_to(outside)
    outside_bytes = outside.read_bytes()
    monkeypatch.setattr(truth_module, "_O_NOFOLLOW", 0)

    guard = SQLiteProjectionGuard(clock=lambda: instant)
    assert not guard.verify(database, snapshot).clean
    with pytest.raises(ValueError, match="missing or unsafe"):
        guard.require_compatible(
            database,
            expected_schema_version=1,
            expected_builder_version="projection-builder/test",
        )

    assert outside.read_bytes() == outside_bytes


def test_projection_sqlite_uri_escapes_query_and_fragment_characters(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query?mode=ro#fragment.sqlite"
    _write_projection_fixture(database)
    guard = SQLiteProjectionGuard(clock=lambda: instant)

    guard.stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )

    assert guard.verify(database, snapshot).clean
    assert guard.require_compatible(
        database,
        expected_schema_version=1,
        expected_builder_version="projection-builder/test",
    ).snapshot_id == snapshot.id


def test_verify_and_require_compatible_reject_foreign_key_corruption(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
            """
        )
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        snapshot,
        schema_version=1,
        builder_version="projection-builder/test",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("INSERT INTO child(parent_id) VALUES (99)")

    assert not guard.verify(database, snapshot).clean
    with pytest.raises(ValueError, match="foreign-key"):
        guard.require_compatible(
            database,
            expected_schema_version=1,
            expected_builder_version="projection-builder/test",
        )


def test_projection_stamp_rejects_compile_option_stat4_without_mutation(
    tmp_path: Path,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE concept(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE stat4(tbl, idx, neq, nlt, ndlt, sample)")
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET name = 'sqlite_stat4', "
            "tbl_name = 'sqlite_stat4', "
            "sql = 'CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)' "
            "WHERE name = 'stat4'"
        )
        connection.execute("PRAGMA writable_schema = OFF")
        connection.execute("PRAGMA schema_version = 2")
    original = database.read_bytes()

    with pytest.raises(ValueError, match="unexpected SQLite planner statistics"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


@pytest.mark.parametrize("profile", ("page-size", "auto-vacuum"))
def test_projection_stamp_rejects_nonportable_sqlite_profile_without_mutation(
    tmp_path: Path,
    profile: str,
) -> None:
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    snapshot = _manager(workspace, store, instant=instant).capture(
        created_by="operator:test"
    )
    database = tmp_path / "query.sqlite"
    with sqlite3.connect(database) as connection:
        if profile == "page-size":
            connection.execute("PRAGMA page_size = 8192")
        else:
            connection.execute("PRAGMA auto_vacuum = FULL")
        connection.execute("CREATE TABLE concept(id INTEGER PRIMARY KEY)")
    original = database.read_bytes()

    with pytest.raises(ValueError, match="non-portable SQLite file profile"):
        SQLiteProjectionGuard(clock=lambda: instant).stamp(
            database,
            snapshot,
            schema_version=1,
            builder_version="projection-builder/test",
        )

    assert database.read_bytes() == original
    assert _projection_candidate_paths(database) == ()


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


def test_projection_stale_against_expected_schema_and_builder(tmp_path: Path) -> None:
    """Catches a matching truth stamp from an incompatible projection schema."""
    workspace, store = _workspace(tmp_path)
    instant = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    manager = _manager(workspace, store, instant=instant)
    snapshot = manager.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    guard = SQLiteProjectionGuard(clock=lambda: instant)
    guard.stamp(
        database,
        snapshot,
        schema_version=8,
        builder_version="sqlite-knowledge-projection/9",
    )

    report = guard.verify(
        database,
        snapshot,
        truth_report=manager.verify(snapshot),
        expected_schema_version=9,
        expected_builder_version="sqlite-knowledge-projection/10",
    )

    assert not report.clean
    assert any(item.kind is DriftKind.PROJECTION_STALE for item in report.items)
    assert report.recommended_action == "discard_and_rebuild"


def test_policy_forbids_database_as_canonical_source() -> None:
    with pytest.raises(ValidationError):
        TruthPolicy.model_validate(
            {
                **_policy().model_dump(mode="json"),
                "database_to_canonical": "allowed",
            }
        )


def test_git_ontology_glob_matches_zero_or_many_directories() -> None:
    pattern = "ontology/**/*.yaml"

    assert TruthManager._glob_matches("ontology/knowledge.yaml", pattern)
    assert TruthManager._glob_matches(
        "ontology/topic/generated/project/bundle.yaml",
        pattern,
    )
    assert not TruthManager._glob_matches("ontology/topic/source.md", pattern)


def test_truth_inventory_excludes_rebuildable_artifact_manifests(tmp_path: Path) -> None:
    workspace, store = _workspace(tmp_path)
    artifact_manifest = workspace / "ontology" / "sample" / "artifacts.yaml"
    artifact_manifest.parent.mkdir()
    artifact_manifest.write_text("version: 1\nartifacts: []\n")
    policy = _policy().model_copy(
        update={"ontology_exclude_globs": ("ontology/**/artifacts.yaml",)}
    )
    manager = TruthManager(
        workspace_root=workspace,
        store_root=store.root,
        policy=policy,
        clock=lambda: datetime(2026, 8, 29, 17, 0, tzinfo=UTC),
    )

    snapshot = manager.capture(created_by="operator:test")

    locators = {item.locator for item in snapshot.artifacts}
    assert "workspace:ontology/knowledge.yaml" in locators
    assert "workspace:ontology/sample/artifacts.yaml" not in locators


def test_checked_and_packaged_truth_policies_exclude_artifact_manifests() -> None:
    repository = Path(__file__).resolve().parents[1]
    checked = TruthPolicy.from_yaml(repository / "config" / "truth-policy.yaml")
    packaged = TruthPolicy.from_yaml(
        repository / "src" / "research_agent" / "default_config" / "truth-policy.yaml"
    )

    assert checked == packaged
    assert checked.ontology_exclude_globs == ("ontology/**/artifacts.yaml",)
