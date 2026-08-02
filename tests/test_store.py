from pathlib import Path

from research_agent.store import ImmutableStore


def test_source_ingestion_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("evidence")
    store = ImmutableStore(tmp_path / "store")
    store.initialize()

    first = store.ingest_file(source_path)
    second = store.ingest_file(source_path)

    assert first.id == second.id
    assert first.content_sha256 == second.content_sha256
    blob = store.blob_root / first.content_sha256[:2] / first.content_sha256
    assert blob.read_text() == "evidence"
