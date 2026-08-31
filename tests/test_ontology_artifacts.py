from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from research_agent.ontology_artifacts import (
    ArtifactRole,
    OntologyArtifact,
    OntologyArtifactError,
    OntologyArtifactManager,
)

_FORBIDDEN_CONTROL_VALUES = (
    *range(0x00, 0x09),
    0x0B,
    0x0C,
    *range(0x0E, 0x20),
    0x7F,
)
_FORBIDDEN_CONTROL_PARAMS = tuple(
    pytest.param(bytes((value,)), id=f"0x{value:02x}")
    for value in _FORBIDDEN_CONTROL_VALUES
)
_CONTROL_POSITION_PARAMS = (
    pytest.param(
        b"",
        b"FIRECRAWL_KEY=your_firecrawl_key\n",
        id="prefix",
    ),
    pytest.param(
        b"FIRE",
        b"CRAWL_KEY=your_firecrawl_key\n",
        id="inside-name",
    ),
    pytest.param(
        b"FIRECRAWL_KEY",
        b"=your_firecrawl_key\n",
        id="before-operator",
    ),
    pytest.param(
        b"FIRECRAWL_KEY=",
        b"your_firecrawl_key\n",
        id="before-rhs",
    ),
    pytest.param(
        b"FIRECRAWL_KEY=your_firecrawl_key",
        b"\n",
        id="suffix",
    ),
)


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Path] = {}
        self.root: Path | None = None
        self.download_calls = 0

    def use_root(self, root: Path) -> MemoryArtifactStore:
        self.root = root
        root.mkdir()
        return self

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        assert self.root is not None
        key = (artifact.release_tag, artifact.asset_name)
        if key in self.values:
            return False
        destination = self.root / artifact.asset_name
        shutil.copyfile(source, destination)
        self.values[key] = destination
        return True

    def available(self, artifact: OntologyArtifact) -> bool:
        return (artifact.release_tag, artifact.asset_name) in self.values

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        self.download_calls += 1
        source = self.values[(artifact.release_tag, artifact.asset_name)]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _library_database(path: Path) -> None:
    metadata = {
        "schema_version": 1,
        "builder_version": "source-library-projection/1",
        "snapshot": {
            "id": "source-library-snapshot:sha256:" + "a" * 64,
            "library_id": "library:test",
            "manifest_sha256": "b" * 64,
            "created_at": "2026-08-26T00:00:00Z",
            "source_version_ids": ["source:sha256:" + "c" * 64],
            "text_derivation_ids": ["text-derivation:sha256:" + "d" * 64],
            "repository_snapshot_ids": ["repository-snapshot:sha256:" + "e" * 64],
            "version": 1,
        },
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE library_metadata(singleton INTEGER PRIMARY KEY, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO library_metadata VALUES (1, ?)",
            (json.dumps(metadata),),
        )


def test_artifacts_publish_by_input_revision_and_hydrate_lazily(tmp_path: Path) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "index.md").write_text("# Routing\n")
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)

    first = manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        source_library=database,
        generated_content=generated,
    )
    second = manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        source_library=database,
        generated_content=generated,
    )

    assert first.changed is True
    assert set(first.published) == {
        ArtifactRole.SOURCE_LIBRARY,
        ArtifactRole.GENERATED_CONTENT,
    }
    assert second.changed is False
    assert set(second.reused) == {
        ArtifactRole.SOURCE_LIBRARY,
        ArtifactRole.GENERATED_CONTENT,
    }

    hydrated = manager.hydrate(store=store)
    assert all(item.downloaded for item in hydrated.hydrated)
    assert (ontology / ".geas-artifacts" / "library.sqlite").is_file()
    assert (ontology / ".geas-artifacts" / "generated" / "index.md").read_text() == (
        "# Routing\n"
    )

    cached = manager.hydrate(store=store)
    assert all(not item.downloaded for item in cached.hydrated)


def test_hydration_rejects_symlinked_cache_root_before_download_or_canonical_write(
    tmp_path: Path,
) -> None:
    """A cache alias back into the ontology must not overwrite canonical bytes."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        source_library=database,
    )
    canonical = ontology / "library.sqlite"
    canonical.write_bytes(b"canonical ontology bytes\n")
    (ontology / ".geas-artifacts").symlink_to(ontology, target_is_directory=True)
    before = canonical.read_bytes()

    with pytest.raises(OntologyArtifactError, match="symbolic link"):
        manager.hydrate(store=store)

    assert store.download_calls == 0
    assert canonical.read_bytes() == before


def test_hydration_prevalidates_every_selected_output_before_first_download(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "index.md").write_text("# Routing\n")
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        source_library=database,
        generated_content=generated,
    )
    manager.cache.mkdir()
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"outside bytes\n")
    (manager.cache / "library.sqlite").symlink_to(outside)

    with pytest.raises(OntologyArtifactError, match="symbolic link"):
        manager.hydrate(store=store)

    assert store.download_calls == 0
    assert outside.read_bytes() == b"outside bytes\n"
    assert not (manager.cache / "generated.zip").exists()
    assert not (manager.cache / "generated").exists()


def test_artifact_publication_rejects_possible_credentials(tmp_path: Path) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "instructions.md").write_text(
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
    )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=generated,
        )


def test_artifact_publication_accepts_documented_public_placeholders(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "example.env.md").write_text(
        'FIRECRAWL_KEY="your_firecrawl_key"\n'
        'OPENAI_KEY="your_openai_key"\n'
    )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    receipt = OntologyArtifactManager(ontology).publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="public documentation fixture",
        generated_content=generated,
    )

    assert receipt.published == (ArtifactRole.GENERATED_CONTENT,)


def test_artifact_publication_still_rejects_nonplaceholder_assignments(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "instructions.md").write_text(
        'FIRECRAWL_KEY="operator-secret-value-123"\n'
    )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=generated,
        )


def test_artifact_publication_rejects_placeholder_concatenation_without_upload(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "instructions.md").write_text(
        'FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\n'
    )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=generated,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    "assignment",
    (
        b"FIRECRAWL_KEY='your_''firecrawl_key''operator-secret-value-123'\n",
        b"FIRECRAWL_KEY=your_${K}\n",
        b"FIRECRAWL_KEY=your_$(x)\n",
        b"FIRECRAWL_KEY=your_;id\n",
        b"FIRECRAWL_KEY=operator-secret-value-123\rNEXT=value\n",
        b"FIRECRAWL_KEY=operator-secret-value-123\r\rNEXT=value\n",
        b"prefix=\x00\rFIRECRAWL_KEY=operator-secret-value-123\r\x01NEXT=value\n",
        b"\x0bFIRECRAWL_KEY=your_firecrawl_key\n",
        b"FIRE\x0cCRAWL_KEY=your_firecrawl_key\n",
        b"FIRE\x00CRAWL_KEY=your_firecrawl_key\n",
        b"FIRECRAWL_KEY=your_firecrawl_key\x7f\n",
    ),
)
def test_raw_artifact_rejects_credential_bypass_without_upload(
    tmp_path: Path,
    assignment: bytes,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    generated = tmp_path / "instructions.md"
    generated.write_bytes(assignment)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=generated,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_sqlite_artifact_rejects_placeholder_concatenation_without_upload(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE source_text(content TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO source_text VALUES (?)",
            ('FIRECRAWL_KEY="your_firecrawl_key"operator-secret-value-123\n',),
        )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    ("column_type", "assignment"),
    (
        ("TEXT", "FIRECRAWL_KEY=your_${K}\n"),
        ("BLOB", b"FIRECRAWL_KEY=your_$(x)\n"),
        ("TEXT", "FIRECRAWL_KEY=operator-secret-value-123\rNEXT=value\n"),
        (
            "BLOB",
            b"prefix=\x00\rFIRECRAWL_KEY=operator-secret-value-123\r\r\x01NEXT=value\n",
        ),
    ),
)
def test_sqlite_artifact_scans_every_text_and_blob_value_without_upload(
    tmp_path: Path,
    column_type: str,
    assignment: str | bytes,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE source_text(content {column_type} NOT NULL)")
        connection.execute("INSERT INTO source_text VALUES (?)", (assignment,))
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize("encoding", ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"))
def test_sqlite_artifact_rejects_encoded_residue_without_upload(
    tmp_path: Path,
    encoding: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    assignment = "FIRECRAWL_KEY=operator-secret-value-123".encode(encoding)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE source_text(content BLOB NOT NULL)")
        connection.execute("INSERT INTO source_text VALUES (?)", (assignment,))
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert store.download_calls == 0
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize("column_type", ("TEXT", "BLOB"))
@pytest.mark.parametrize(("before", "after"), _CONTROL_POSITION_PARAMS)
@pytest.mark.parametrize("control", _FORBIDDEN_CONTROL_PARAMS)
def test_sqlite_text_and_blob_reject_every_forbidden_control_position(
    tmp_path: Path,
    column_type: str,
    before: bytes,
    after: bytes,
    control: bytes,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    assignment_bytes = before + control + after
    assignment = (
        assignment_bytes.decode("ascii")
        if column_type == "TEXT"
        else assignment_bytes
    )
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE source_text(content {column_type} NOT NULL)")
        connection.execute("INSERT INTO source_text VALUES (?)", (assignment,))
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    ("column_type", "assignment"),
    (
        ("TEXT", "FIRECRAWL_KEY=your_firecrawl_key\rNEXT=value\n"),
        ("BLOB", b"FIRECRAWL_KEY=your_firecrawl_key\r\rNEXT=value\n"),
    ),
)
def test_sqlite_stored_values_keep_normal_placeholder_classification(
    tmp_path: Path,
    column_type: str,
    assignment: str | bytes,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE source_text(content {column_type} NOT NULL)")
        connection.execute("INSERT INTO source_text VALUES (?)", (assignment,))
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    receipt = OntologyArtifactManager(ontology).publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="public documentation fixture",
        source_library=database,
    )

    assert receipt.published == (ArtifactRole.SOURCE_LIBRARY,)
    assert len(store.values) == 1
    assert (ontology / "artifacts.yaml").is_file()


@pytest.mark.parametrize("residue_kind", ("page-slack", "freelist"))
def test_sqlite_artifact_rejects_deleted_assignment_residue_without_upload(
    tmp_path: Path,
    residue_kind: str,
) -> None:
    """Outgoing bytes remain sensitive after SQLite no longer exposes the row."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    secret = b"FIRECRAWL_KEY=operator-secret-value-123\n"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute("CREATE TABLE deleted_residue(content TEXT NOT NULL)")
        if residue_kind == "page-slack":
            connection.execute("INSERT INTO deleted_residue VALUES (?)", (secret.decode(),))
            connection.execute("DELETE FROM deleted_residue")
            assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)
        else:
            padded = "x" * 5000 + secret.decode() + "y" * 5000
            connection.execute("INSERT INTO deleted_residue VALUES (?)", (padded,))
            connection.execute("DROP TABLE deleted_residue")
            assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
    assert secret in database.read_bytes()
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    "schema_sql",
    (
        """CREATE TABLE leaked_default(
            content TEXT DEFAULT 'FIRECRAWL_KEY=operator-secret-value-123'
        )""",
        """CREATE VIEW leaked_view AS
            SELECT 'FIRECRAWL_KEY=operator-secret-value-123' AS content""",
        """CREATE TRIGGER leaked_trigger AFTER INSERT ON source_text BEGIN
            SELECT 'FIRECRAWL_KEY=operator-secret-value-123';
        END""",
        'CREATE INDEX "FIRECRAWL_KEY=operator-secret-value-123" '
        "ON source_text(content)",
    ),
    ids=("default", "view", "trigger", "index"),
)
def test_sqlite_artifact_scans_every_schema_sql_surface_without_upload(
    tmp_path: Path,
    schema_sql: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE source_text(content TEXT NOT NULL)")
        connection.execute(schema_sql)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    "schema_sql",
    (
        """CREATE TABLE split_default(
            content TEXT DEFAULT ('FIRECRAWL_KEY=operator-' || 'secret-value-123')
        )""",
        """CREATE VIEW split_view AS
            SELECT 'FIRECRAWL_KEY=' || 'operator-' || 'secret-value-123' AS content""",
        """CREATE VIEW printf_view AS
            SELECT printf('FIRECRAWL_KEY=%s', 'operator-secret-value-123') AS content""",
        """CREATE VIEW char_view AS
            SELECT 'FIRECRAWL_KEY=' || char(111, 112, 101, 114, 97, 116, 111, 114)
            AS content""",
        """CREATE TABLE comment_marker(
            content TEXT /* FIRECRAWL_KEY=operator-secret-value-123 */
        )""",
        """CREATE TABLE placeholder_default(
            content TEXT DEFAULT 'FIRECRAWL_KEY=your_firecrawl_key'
        )""",
    ),
    ids=(
        "concatenated-default",
        "multiple-view-literals",
        "printf-view",
        "char-view",
        "comment",
        "placeholder",
    ),
)
def test_sqlite_schema_rejects_assignment_marker_without_evaluating_sql(
    tmp_path: Path,
    schema_sql: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(schema_sql)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_malformed_sqlite_artifact_fails_without_upload(tmp_path: Path) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="invalid SQLite"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()
