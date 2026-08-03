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


def test_record_batches_remain_inspectable_and_iterable(tmp_path: Path) -> None:
    store = ImmutableStore(tmp_path / "store")
    store.initialize()

    batch_digest, item_digests = store.put_record_batch(
        "claim",
        [{"id": "claim:2", "value": "second"}, {"id": "claim:1", "value": "first"}],
    )

    assert len(item_digests) == 2
    assert store.record_path("claim-batch", batch_digest).is_file()
    assert {item["id"] for item in store.iter_records("claim")} == {"claim:1", "claim:2"}


def test_record_batch_index_tampering_fails_closed(tmp_path: Path) -> None:
    store = ImmutableStore(tmp_path / "store")
    store.initialize()
    digest, _ = store.put_record_batch("claim", [{"id": "claim:1"}])
    path = store.record_path("claim-batch", digest)
    path.write_text(path.read_text().replace("claim:1", "claim:tampered"))

    try:
        list(store.iter_records("claim"))
    except ValueError as error:
        assert "filename does not match content" in str(error)
    else:
        raise AssertionError("tampered batch must fail closed")
