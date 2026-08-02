from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Iterable
from pathlib import Path

from research_agent.discovery import (
    AcquisitionRequest,
    AcquisitionResult,
    ConnectorCapability,
    ConnectorManifest,
    DiscoveryCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    SourceClass,
    TermMatch,
    locator_path,
)


class LocalFileConnector:
    """Deterministic, path-confined discovery and acquisition."""

    manifest = ConnectorManifest(
        id="connector:local-file",
        version="1",
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVERY,
                ConnectorCapability.METADATA,
                ConnectorCapability.FULL_TEXT,
                ConnectorCapability.LOCAL_FILE,
            }
        ),
        source_classes=frozenset({SourceClass.LOCAL_FILE}),
        allowed_schemes=frozenset({"file"}),
        query_fields=frozenset({"exact_terms", "match", "languages"}),
        filter_fields=frozenset({"media_type"}),
        max_results=1_000,
        max_pages=100,
        max_response_bytes=10_000_000,
        supported_media_types=frozenset(
            {
                "application/json",
                "application/pdf",
                "application/xml",
                "text/csv",
                "text/html",
                "text/markdown",
                "text/plain",
                "text/xml",
            }
        ),
        redistribution="operator-controlled",
        parser_version="local-bytes/1",
        normalization_version="unicode-casefold/1",
        network_trust_zone="offline",
        terms_note="Only operator-selected local roots are accessible.",
    )

    def __init__(self, roots: Iterable[Path]) -> None:
        resolved = tuple(sorted({path.resolve(strict=True) for path in roots}))
        if not resolved:
            raise ValueError("at least one local root is required")
        if any(not path.is_dir() for path in resolved):
            raise ValueError("local roots must be directories")
        self.roots = resolved

    def _resolve_allowed(self, locator: str) -> Path:
        candidate = locator_path(locator).resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("local acquisition target must be a regular file")
        if not any(candidate.is_relative_to(root) for root in self.roots):
            raise ValueError("local acquisition target escapes configured roots")
        return candidate

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]:
        files = sorted(
            {
                path.resolve()
                for root in self.roots
                for path in root.rglob("*")
                if path.is_file() and path.resolve().is_relative_to(root)
            },
            key=lambda item: item.as_posix(),
        )
        candidates: list[DiscoveryCandidate] = []
        rejected = 0
        for path in files:
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if media_type not in self.manifest.supported_media_types:
                rejected += 1
                continue
            try:
                if path.stat().st_size > self.manifest.max_response_bytes:
                    rejected += 1
                    continue
                text = (
                    path.read_text(errors="replace")
                    if media_type.startswith("text/")
                    or media_type in {"application/json", "application/xml"}
                    else ""
                )
            except OSError:
                rejected += 1
                continue
            haystack = f"{path.name}\n{text}".casefold()
            matched = tuple(
                term
                for term in request.exact_terms
                if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack)
            )
            qualifies = bool(matched)
            if request.match is TermMatch.ALL:
                qualifies = len(matched) == len(request.exact_terms)
            if not qualifies:
                continue
            snippet = self._snippet(text, matched)
            candidates.append(
                DiscoveryCandidate(
                    upstream_id=f"sha256:{hashlib.sha256(path.as_uri().encode()).hexdigest()}",
                    canonical_locator=path.as_uri(),
                    title=path.name,
                    media_type=media_type,
                    snippet=snippet,
                    score=len(matched),
                )
            )

        ranked = sorted(
            candidates,
            key=lambda item: (-item.score, item.canonical_locator),
        )[: request.result_limit]
        page_size = max(1, (request.result_limit + request.page_limit - 1) // request.page_limit)
        pages = [ranked[index : index + page_size] for index in range(0, len(ranked), page_size)]
        for index, page in enumerate(pages[: request.page_limit]):
            yield DiscoveryPage(
                candidates=tuple(page),
                cursor=str(index),
                next_cursor=str(index + 1) if index + 1 < len(pages) else None,
                rejected_count=rejected if index == 0 else 0,
            )

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        path = self._resolve_allowed(request.locator)
        limit = min(request.max_content_bytes, self.manifest.max_response_bytes)
        size = path.stat().st_size
        if size > limit:
            raise ValueError(f"content size {size} exceeds limit {limit}")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media_type not in self.manifest.supported_media_types:
            raise ValueError(f"unsupported media type: {media_type}")
        content = path.read_bytes()
        if len(content) != size:
            raise RuntimeError("local file changed during acquisition")
        return AcquisitionResult(locator=path.as_uri(), content=content, media_type=media_type)

    @staticmethod
    def _snippet(text: str, matched: tuple[str, ...], *, width: int = 240) -> str:
        normalized = text.casefold()
        offsets = [normalized.find(term) for term in matched]
        offset = min((item for item in offsets if item >= 0), default=0)
        start = max(0, offset - width // 3)
        return " ".join(text[start : start + width].split())
