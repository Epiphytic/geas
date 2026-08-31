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


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Path] = {}
        self.root: Path | None = None

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
