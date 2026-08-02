from __future__ import annotations

import hashlib
import json
import mimetypes
import os
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
