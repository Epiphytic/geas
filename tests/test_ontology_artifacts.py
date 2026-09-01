from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.ontology_artifacts as artifacts_module
from research_agent.ontology_artifacts import (
    ArtifactRole,
    OntologyArtifact,
    OntologyArtifactError,
    OntologyArtifactManager,
)
from research_agent.truth import SQLiteProjectionGuard, TruthSnapshot

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


def _index_leaf_layout(
    database: Path,
    index_name: str,
) -> tuple[bytearray, int, tuple[int, ...], tuple[int, ...]]:
    with sqlite3.connect(database) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        root_page = connection.execute(
            "SELECT rootpage FROM sqlite_schema WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()[0]
    content = bytearray(database.read_bytes())
    page_start = (root_page - 1) * page_size
    header_start = page_start + (100 if root_page == 1 else 0)
    assert content[header_start] == 0x0A
    cell_count = int.from_bytes(content[header_start + 3 : header_start + 5], "big")
    pointer_start = header_start + 8
    pointer_positions = tuple(pointer_start + 2 * index for index in range(cell_count))
    cell_offsets = tuple(
        page_start
        + int.from_bytes(content[position : position + 2], "big")
        for position in pointer_positions
    )
    return content, page_start, pointer_positions, cell_offsets

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


def _knowledge_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
            INSERT INTO parent VALUES (1);
            INSERT INTO child VALUES (1);
            """
        )
    snapshot = TruthSnapshot(
        id="truth-snapshot:test",
        state_digest="a" * 64,
        policy_sha256="b" * 64,
        artifacts=(),
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
        created_by="operator:test",
        builder_version="truth-manager/test",
    )
    SQLiteProjectionGuard(
        clock=lambda: datetime(2026, 8, 31, tzinfo=UTC)
    ).stamp(
        path,
        snapshot,
        schema_version=9,
        builder_version="sqlite-knowledge-projection/10",
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


@pytest.mark.parametrize("corruption", ("foreign-key", "header"))
def test_knowledge_projection_publication_rejects_full_guard_failure_before_upload(
    tmp_path: Path,
    corruption: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "knowledge.sqlite"
    _knowledge_database(database)
    if corruption == "foreign-key":
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("INSERT INTO child VALUES (99)")
    else:
        with database.open("r+b") as stream:
            stream.seek(24)
            stream.write(b"\x01\x00\x00\x00")
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)

    with pytest.raises(OntologyArtifactError, match="invalid SQLite|foreign-key|header"):
        manager.publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            knowledge_projection=database,
        )

    assert store.values == {}
    assert not manager.manifest_path.exists()


def test_projection_publication_binds_revision_scan_hash_and_upload_to_one_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "knowledge.sqlite"
    _knowledge_database(database)
    original = database.read_bytes()
    alternate = tmp_path / "alternate.sqlite"
    _knowledge_database(alternate)
    with sqlite3.connect(alternate) as connection:
        connection.execute("CREATE TABLE replacement(value TEXT)")
    revision = artifacts_module._sqlite_input_revision
    replaced = False

    def replace_source_after_revision(*args: object, **kwargs: object) -> str:
        nonlocal replaced
        result = revision(*args, **kwargs)
        if not replaced:
            alternate.replace(database)
            replaced = True
        return result

    monkeypatch.setattr(
        artifacts_module,
        "_sqlite_input_revision",
        replace_source_after_revision,
    )
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    receipt = OntologyArtifactManager(ontology).publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        knowledge_projection=database,
    )

    artifact = receipt.artifacts[0]
    uploaded = store.values[(artifact.release_tag, artifact.asset_name)]
    assert uploaded.read_bytes() == original
    assert artifact.content_sha256 == artifacts_module.hashlib.sha256(original).hexdigest()


def test_knowledge_projection_hydration_rejects_manifest_input_revision_mismatch(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "knowledge.sqlite"
    _knowledge_database(database)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    publication = manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        knowledge_projection=database,
    )
    assert publication.changed
    original_revision = manager.load().artifacts[0].input_revision
    manifest_text = manager.manifest_path.read_text()
    manager.manifest_path.write_text(
        manifest_text.replace(original_revision, "f" * 64)
    )

    with pytest.raises(OntologyArtifactError, match="input revision"):
        manager.hydrate(store=store)

    assert store.download_calls == 1


def test_projection_hydration_rejects_destination_replacement_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "knowledge.sqlite"
    _knowledge_database(database)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="operator-confirmed private storage",
        knowledge_projection=database,
    )
    revision = artifacts_module._sqlite_input_revision
    replaced = False

    def replace_destination_after_revision(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal replaced
        result = revision(path, *args, **kwargs)
        if not replaced and "geas-projection-validation-" in str(path):
            manager._cache_path(manager.load().artifacts[0]).write_bytes(b"replacement")
            replaced = True
        return result

    monkeypatch.setattr(
        artifacts_module,
        "_sqlite_input_revision",
        replace_destination_after_revision,
    )

    with pytest.raises(OntologyArtifactError, match="changed while it was hydrated"):
        manager.hydrate(store=store)

    assert replaced


@pytest.mark.parametrize(
    ("role", "artifact_format"),
    (
        (ArtifactRole.KNOWLEDGE_PROJECTION.value, "zip"),
        (ArtifactRole.SOURCE_LIBRARY.value, "zip"),
        (ArtifactRole.GENERATED_CONTENT.value, "sqlite"),
    ),
)
def test_hydration_rejects_incompatible_role_format_before_download(
    tmp_path: Path,
    role: str,
    artifact_format: str,
) -> None:
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
    payload = manager.load().model_dump(mode="json")
    payload["artifacts"][0]["role"] = role
    payload["artifacts"][0]["format"] = artifact_format
    manager.manifest_path.write_text(json.dumps(payload))
    store.download_calls = 0

    with pytest.raises(ValueError, match="role.*format|format.*role"):
        manager.hydrate(store=store)

    assert store.download_calls == 0


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


def test_hydration_prevalidates_later_artifact_file_kind_before_any_download(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    generated = tmp_path / "generated"
    generated.mkdir()
    generated.joinpath("index.md").write_text("# Routing\n")
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="public documentation fixture",
        source_library=database,
        generated_content=generated,
    )
    manager.cache.mkdir()
    manager.cache.joinpath("library.sqlite").mkdir()

    with pytest.raises(OntologyArtifactError, match="file target"):
        manager.hydrate(store=store)

    assert store.download_calls == 0
    assert not manager.cache.joinpath("generated.zip").exists()
    assert not manager.cache.joinpath("generated").exists()


def test_hydration_prevalidates_generated_directory_kind_before_any_download(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    generated = tmp_path / "generated"
    generated.mkdir()
    generated.joinpath("index.md").write_text("# Routing\n")
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="public documentation fixture",
        source_library=database,
        generated_content=generated,
    )
    manager.cache.mkdir()
    manager.cache.joinpath("generated").write_text("wrong target kind\n")

    with pytest.raises(OntologyArtifactError, match="directory target"):
        manager.hydrate(store=store)

    assert store.download_calls == 0
    assert not manager.cache.joinpath("generated.zip").exists()
    assert not manager.cache.joinpath("library.sqlite").exists()


def test_hydration_revalidates_ontology_root_after_constructor(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")
    manager = OntologyArtifactManager(ontology)
    manager.publish(
        store=store,
        published_by="test:operator",
        storage_rights_basis="public documentation fixture",
        source_library=database,
    )
    canonical = tmp_path / "canonical-routing"
    ontology.rename(canonical)
    canonical.joinpath("sentinel").write_bytes(b"canonical bytes\n")
    ontology.symlink_to(canonical, target_is_directory=True)
    before = canonical.joinpath("sentinel").read_bytes()

    with pytest.raises(OntologyArtifactError, match="ontology root"):
        manager.hydrate(store=store)

    assert store.download_calls == 0
    assert canonical.joinpath("sentinel").read_bytes() == before
    assert not canonical.joinpath(".geas-artifacts").exists()


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
        ("TEXT", "FIRECRAWL_KEY=your_firecrawl_key"),
        ("BLOB", b"FIRECRAWL_KEY=your_firecrawl_key"),
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


def test_sqlite_expression_index_payload_rejects_without_upload(
    tmp_path: Path,
) -> None:
    """Derived index records receive the same scan as other masked live bytes."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    assignment = b"FIRECRAWL_KEY=operator-secret-value-123"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE indexed_parts(prefix TEXT NOT NULL, suffix TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX indexed_assignment "
            "ON indexed_parts(prefix || '=' || suffix)"
        )
        connection.execute(
            "INSERT INTO indexed_parts VALUES (?, ?)",
            ("FIRECRAWL_KEY", "operator-secret-value-123"),
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert assignment in database.read_bytes()
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


def test_sqlite_expression_index_overflow_payload_rejects_without_upload(
    tmp_path: Path,
) -> None:
    """Index assignments split across local and overflow payload still reject."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    prefix = "x" * 475 + "\nFIRECRAWL_KEY"
    suffix = "operator-secret-value-123" + "y" * 5000
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE indexed_parts(prefix TEXT NOT NULL, suffix TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX indexed_assignment "
            "ON indexed_parts(prefix || '=' || suffix)"
        )
        connection.execute("INSERT INTO indexed_parts VALUES (?, ?)", (prefix, suffix))
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT prefix || '=' || suffix FROM indexed_parts"
        ).fetchone() == (prefix + "=" + suffix,)
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


def test_generated_sqlite_index_header_cannot_hide_credential_body(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "generated.sqlite"
    assignment = b"FIRECRAWL_KEY=operator-secret-value-123"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE indexed_parts(prefix TEXT NOT NULL, suffix TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX indexed_assignment "
            "ON indexed_parts(prefix || '=' || suffix)"
        )
        connection.execute(
            "INSERT INTO indexed_parts VALUES (?, ?)",
            ("FIRECRAWL_KEY", "operator-secret-value-123"),
        )
    content, _page_start, _pointer_positions, cell_offsets = _index_leaf_layout(
        database,
        "indexed_assignment",
    )
    cell_start = cell_offsets[0]
    payload_size = content[cell_start]
    assert payload_size < 0x80
    payload_start = cell_start + 1
    content[payload_start] = payload_size
    database.write_bytes(content)
    assert assignment in content
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="SQLite record"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize(
    ("serial_type", "message"),
    ((0x0A, "SQLite record serial type"), (0x7F, "SQLite record body extent")),
    ids=("reserved", "body-mismatch"),
)
def test_generated_sqlite_index_body_extent_must_match_serial_types(
    tmp_path: Path,
    serial_type: int,
    message: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "generated.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE indexed_values(value TEXT NOT NULL)")
        connection.execute("CREATE INDEX indexed_value ON indexed_values(value)")
        connection.execute("INSERT INTO indexed_values VALUES ('public-data')")
    content, _page_start, _pointer_positions, cell_offsets = _index_leaf_layout(
        database,
        "indexed_value",
    )
    cell_start = cell_offsets[0]
    assert content[cell_start] < 0x80
    payload_start = cell_start + 1
    assert content[payload_start] == 3
    content[payload_start + 1] = serial_type
    database.write_bytes(content)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match=message):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


@pytest.mark.parametrize("malformation", ("truncated", "overlong"))
def test_generated_sqlite_rejects_malformed_index_record_varint(
    tmp_path: Path,
    malformation: str,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "generated.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE indexed_values(value TEXT NOT NULL)")
        connection.execute("CREATE INDEX indexed_value ON indexed_values(value)")
        connection.execute("INSERT INTO indexed_values VALUES ('public-data')")
    content, _page_start, _pointer_positions, cell_offsets = _index_leaf_layout(
        database,
        "indexed_value",
    )
    cell_start = cell_offsets[0]
    assert content[cell_start] < 0x80
    payload_start = cell_start + 1
    assert content[payload_start] == 3
    if malformation == "truncated":
        content[payload_start + 2] = 0x80
    else:
        content[payload_start] = 4
        content[payload_start + 1] = 33
        content[payload_start + 2 : payload_start + 4] = b"\x80\x08"
    database.write_bytes(content)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="SQLite record varint"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_generated_sqlite_rejects_duplicate_index_cell_pointers(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "generated.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE indexed_values(value TEXT NOT NULL)")
        connection.execute("CREATE INDEX indexed_value ON indexed_values(value)")
        connection.executemany(
            "INSERT INTO indexed_values VALUES (?)",
            (("public-a",), ("public-b",)),
        )
    content, _page_start, pointer_positions, _cell_offsets = _index_leaf_layout(
        database,
        "indexed_value",
    )
    content[pointer_positions[1] : pointer_positions[1] + 2] = content[
        pointer_positions[0] : pointer_positions[0] + 2
    ]
    database.write_bytes(content)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="duplicate SQLite B-tree cell"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_generated_sqlite_rejects_overlapping_index_cell_ranges(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "generated.sqlite"
    nested_cell = b"\x02\x02\x08"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE indexed_values(value BLOB NOT NULL)")
        connection.execute("CREATE INDEX indexed_value ON indexed_values(value)")
        connection.executemany(
            "INSERT INTO indexed_values VALUES (?)",
            ((b"A" + nested_cell + b"Z",), (b"B-public",)),
        )
    content, page_start, pointer_positions, cell_offsets = _index_leaf_layout(
        database,
        "indexed_value",
    )
    nested_start = content.index(nested_cell, page_start)
    owner_index = next(
        index
        for index, cell_start in enumerate(cell_offsets)
        if cell_start < nested_start < cell_start + 1 + content[cell_start]
    )
    changed_index = 1 - owner_index
    relative_nested = nested_start - page_start
    content[pointer_positions[changed_index] : pointer_positions[changed_index] + 2] = (
        relative_nested.to_bytes(2, "big")
    )
    database.write_bytes(content)
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="overlapping SQLite B-tree cells"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="operator-confirmed private storage",
            generated_content=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


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


def test_live_placeholder_cannot_excuse_control_delimited_deleted_slack(
    tmp_path: Path,
) -> None:
    """A live safe row cannot authorize a prefix-overlapping deleted row."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    live_placeholder = b"FIRECRAWL_KEY=your_firecrawl_key"
    deleted_residue = live_placeholder[:-1] + b"\x01"
    assert len(deleted_residue) == len(live_placeholder)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute(
            "CREATE TABLE source_text(id INTEGER PRIMARY KEY, content BLOB NOT NULL)"
        )
        connection.execute("INSERT INTO source_text(content) VALUES (?)", (live_placeholder,))
        cursor = connection.execute(
            "INSERT INTO source_text(content) VALUES (?)", (deleted_residue,)
        )
        connection.execute("DELETE FROM source_text WHERE id = ?", (cursor.lastrowid,))
    database_bytes = database.read_bytes()
    assert live_placeholder in database_bytes
    assert deleted_residue in database_bytes
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="public documentation fixture",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_live_placeholder_cannot_excuse_long_deleted_varint_record(
    tmp_path: Path,
) -> None:
    """Occurrence accounting rejects a deleted record with a multibyte type."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    live_placeholder = b"FIRECRAWL_KEY=your_firecrawl_key"
    deleted_residue = (
        live_placeholder
        + b"\x01operator-secret-value-123"
        + b"x" * 256
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute(
            "CREATE TABLE source_text(id INTEGER PRIMARY KEY, content BLOB NOT NULL)"
        )
        connection.execute("INSERT INTO source_text(content) VALUES (?)", (live_placeholder,))
        cursor = connection.execute(
            "INSERT INTO source_text(content) VALUES (?)", (deleted_residue,)
        )
        connection.execute("DELETE FROM source_text WHERE id = ?", (cursor.lastrowid,))
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    database_bytes = database.read_bytes()
    assert live_placeholder in database_bytes
    assert deleted_residue in database_bytes
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="public documentation fixture",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_fragmented_sqlite_cannot_authorize_deleted_structural_finding(
    tmp_path: Path,
) -> None:
    """Only bytes in reachable live cells may be exempt from raw scanning."""
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA page_size=512")
        connection.execute("VACUUM")
    _library_database(database)
    live_placeholder = b"FIRECRAWL_KEY=your_firecrawl_key"
    deleted_residue = live_placeholder + b"\x01operator-secret-value-123" + b"x" * 700
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute(
            "CREATE TABLE source_text(id INTEGER PRIMARY KEY, content BLOB NOT NULL)"
        )
        connection.execute("CREATE INDEX source_text_content ON source_text(content)")
        for index in range(80):
            connection.execute(
                "INSERT INTO source_text(content) VALUES (?)",
                ((f"filler-{index:03d}-" + "z" * (40 + index % 17)).encode(),),
            )
        connection.execute("DELETE FROM source_text WHERE id % 3 = 0")
        connection.execute("INSERT INTO source_text(content) VALUES (?)", (live_placeholder,))
        cursor = connection.execute(
            "INSERT INTO source_text(content) VALUES (?)", (deleted_residue,)
        )
        connection.execute("DELETE FROM source_text WHERE id = ?", (cursor.lastrowid,))
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    database_bytes = database.read_bytes()
    assert live_placeholder in database_bytes
    assert live_placeholder + b"\x01" in database_bytes
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="public documentation fixture",
            source_library=database,
        )

    assert store.values == {}
    assert tuple((tmp_path / "remote-assets").iterdir()) == ()
    assert not (ontology / "artifacts.yaml").exists()


def test_deleted_prefixed_placeholder_residue_rejects_without_upload(
    tmp_path: Path,
) -> None:
    ontology = tmp_path / "routing"
    ontology.mkdir()
    database = tmp_path / "library.sqlite"
    _library_database(database)
    deleted_residue = b"documentation: FIRECRAWL_KEY=your_firecrawl_key\n"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute("CREATE TABLE source_text(content BLOB NOT NULL)")
        connection.execute("INSERT INTO source_text VALUES (?)", (deleted_residue,))
        connection.execute("DELETE FROM source_text")
    assert deleted_residue in database.read_bytes()
    store = MemoryArtifactStore().use_root(tmp_path / "remote-assets")

    with pytest.raises(OntologyArtifactError, match="possible credential"):
        OntologyArtifactManager(ontology).publish(
            store=store,
            published_by="test:operator",
            storage_rights_basis="public documentation fixture",
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
