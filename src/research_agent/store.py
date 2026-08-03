from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from research_agent.models import SourceVersion, canonical_json


class ImmutableStore:
    """Content-addressed blobs and immutable typed records.

    This component accepts already validated objects. Model output must be parsed
    and validated before it reaches this boundary.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.blob_root = self.root / "blobs" / "sha256"
        self.record_root = self.root / "records"

    def initialize(self) -> None:
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.record_root.mkdir(parents=True, exist_ok=True)

    def put_blob(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self.blob_root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"content-address collision at {path}") from None
        return digest

    def put_record(self, kind: str, record: BaseModel | dict[str, Any]) -> str:
        if not kind.replace("-", "").replace("_", "").isalnum():
            raise ValueError("record kind must be alphanumeric with '-' or '_'")
        payload = canonical_json(record)
        digest = hashlib.sha256(payload).hexdigest()
        directory = self.record_root / kind / digest[:2]
        path = directory / f"{digest}.json"
        directory.mkdir(parents=True, exist_ok=True)
        rendered = (
            json.dumps(
                json.loads(payload),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        try:
            with path.open("xb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != rendered:
                raise RuntimeError(f"immutable record mismatch at {path}") from None
        return digest

    def put_record_batch(
        self,
        kind: str,
        records: list[BaseModel | dict[str, Any]],
    ) -> tuple[str, tuple[str, ...]]:
        """Persist many immutable records with one durable content-addressed write."""
        if not records:
            raise ValueError("record batch must not be empty")
        canonical_records: dict[str, dict[str, Any]] = {}
        for record in records:
            payload = canonical_json(record)
            digest = hashlib.sha256(payload).hexdigest()
            canonical_records[digest] = json.loads(payload)
        item_digests = tuple(sorted(canonical_records))
        batch = {
            "version": 1,
            "record_kind": kind,
            "item_sha256s": item_digests,
            "records": [canonical_records[digest] for digest in item_digests],
        }
        return self.put_record(f"{kind}-batch", batch), item_digests

    def record_path(self, kind: str, digest: str) -> Path:
        if not kind.replace("-", "").replace("_", "").isalnum():
            raise ValueError("record kind must be alphanumeric with '-' or '_'")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("record digest must be a lowercase SHA-256 value")
        return self.record_root / kind / digest[:2] / f"{digest}.json"

    def iter_records(self, kind: str) -> Iterator[dict[str, Any]]:
        """Yield immutable records of one kind in content-hash order."""
        root = self.record_root / kind
        seen: set[str] = set()
        if root.exists():
            for path in sorted(root.glob("*/*.json")):
                value = self._read_record_path(path, root)
                digest = hashlib.sha256(canonical_json(value)).hexdigest()
                seen.add(digest)
                yield value
        batch_root = self.record_root / f"{kind}-batch"
        if batch_root.exists():
            for path in sorted(batch_root.glob("*/*.json")):
                batch = self._read_record_path(path, batch_root)
                if set(batch) != {
                    "version",
                    "record_kind",
                    "item_sha256s",
                    "records",
                }:
                    raise ValueError(f"invalid immutable record batch envelope: {path}")
                if batch["version"] != 1 or batch["record_kind"] != kind:
                    raise ValueError(f"immutable record batch kind/version mismatch: {path}")
                records = batch["records"]
                item_digests = batch["item_sha256s"]
                actual_digests = [
                    hashlib.sha256(canonical_json(record)).hexdigest() for record in records
                ]
                if actual_digests != item_digests or actual_digests != sorted(set(actual_digests)):
                    raise ValueError(f"immutable record batch index mismatch: {path}")
                for digest, value in zip(actual_digests, records, strict=True):
                    if digest not in seen:
                        seen.add(digest)
                        yield value

    @staticmethod
    def _read_record_path(path: Path, root: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"immutable record escapes its kind root: {path}")
        value = json.loads(path.read_bytes())
        if hashlib.sha256(canonical_json(value)).hexdigest() != path.stem:
            raise ValueError(f"immutable record filename does not match content: {path}")
        if not isinstance(value, dict):
            raise ValueError(f"immutable record must contain a JSON object: {path}")
        return value

    def read_blob(self, digest: str) -> bytes:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("blob digest must be a lowercase SHA-256 value")
        path = self.blob_root / digest[:2] / digest
        if path.is_symlink() or not path.resolve().is_relative_to(self.blob_root):
            raise ValueError("blob escapes the content-addressed store")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"blob filename does not match content: {path}")
        return content

    def ingest_file(
        self,
        path: Path,
        *,
        source_uri: str | None = None,
        connector_id: str = "connector:local-file",
        license: str | None = None,
        acquired_at: datetime | None = None,
    ) -> SourceVersion:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {resolved}")
        content = resolved.read_bytes()
        media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return self.ingest_bytes(
            content,
            source_uri=source_uri or resolved.as_uri(),
            media_type=media_type,
            connector_id=connector_id,
            license=license,
            acquired_at=acquired_at,
        )

    def ingest_bytes(
        self,
        content: bytes,
        *,
        source_uri: str,
        media_type: str,
        connector_id: str,
        license: str | None = None,
        acquired_at: datetime | None = None,
    ) -> SourceVersion:
        digest = self.put_blob(content)
        source = SourceVersion.from_bytes(
            source_uri=source_uri,
            content=content,
            media_type=media_type,
            connector_id=connector_id,
            license=license,
            acquired_at=acquired_at,
        )
        if source.content_sha256 != digest:
            raise RuntimeError("source and blob hashes diverged")
        self.put_record("source-version", source)
        return source
