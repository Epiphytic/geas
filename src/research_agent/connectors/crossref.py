from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from research_agent.discovery import (
    ConnectorCapability,
    ConnectorManifest,
    DiscoveryCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    SourceClass,
    TermMatch,
)
from research_agent.identifiers import doi_locator, normalize_doi


class CrossrefError(RuntimeError):
    pass


class CrossrefTransport(Protocol):
    def request(self, parameters: Mapping[str, str]) -> bytes: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class HttpsCrossrefTransport:
    endpoint = "https://api.crossref.org/works"

    def __init__(self, *, timeout: float = 20.0, max_response_bytes: int = 5_000_000) -> None:
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, parameters: Mapping[str, str]) -> bytes:
        query = urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "Epiphytic-Research-Agent/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl().split("?", 1)[0] != self.endpoint:
                    raise CrossrefError("Crossref redirected away from its configured endpoint")
                if response.headers.get_content_type() not in {
                    "application/json",
                    "application/vnd.crossref-api-message+json",
                }:
                    raise CrossrefError("Crossref returned an unsupported media type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise CrossrefError(f"Crossref returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise CrossrefError("Crossref request failed") from None
        if len(body) > self.max_response_bytes:
            raise CrossrefError("Crossref response exceeded the configured size limit")
        return body


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Author(_ExternalModel):
    given: str = ""
    family: str = ""
    name: str = ""


class _DateParts(_ExternalModel):
    date_parts: tuple[tuple[int, ...], ...] = Field(default=(), alias="date-parts")


class _Work(_ExternalModel):
    doi: str = Field(alias="DOI")
    title: tuple[str, ...] = ()
    author: tuple[_Author, ...] = ()
    publisher: str | None = None
    published: _DateParts | None = None
    created: _DateParts | None = None
    language: str | None = None
    score: float = Field(default=0, ge=0)


class _Message(_ExternalModel):
    items: tuple[_Work, ...] = ()
    next_cursor: str | None = Field(default=None, alias="next-cursor")
    total_results: int = Field(default=0, ge=0, alias="total-results")


class _Envelope(_ExternalModel):
    status: str
    message: _Message


class CrossrefDiscoveryConnector:
    manifest = ConnectorManifest(
        id="connector:crossref",
        version="1",
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVERY,
                ConnectorCapability.METADATA,
            }
        ),
        source_classes=frozenset({SourceClass.SCHOLARLY}),
        allowed_schemes=frozenset({"https"}),
        allowed_hosts=frozenset({"api.crossref.org"}),
        query_fields=frozenset({"exact_terms", "match", "languages"}),
        filter_fields=frozenset({"language"}),
        max_results=1_000,
        max_pages=20,
        max_response_bytes=5_000_000,
        supported_media_types=frozenset(
            {
                "application/json",
                "application/vnd.crossref-api-message+json",
            }
        ),
        redistribution="metadata generally reusable; abstracts may carry separate copyright",
        parser_version="crossref-json/1",
        normalization_version="crossref-doi/1",
        network_trust_zone="external-index",
        terms_note=(
            "Crossref public REST metadata is used for scholarly discovery. "
            "Publisher abstracts are not retained by this connector."
        ),
    )

    def __init__(self, transport: CrossrefTransport | None = None) -> None:
        self.transport = transport or HttpsCrossrefTransport()

    def normalize_query(self, request: DiscoveryRequest) -> str:
        separator = " OR " if request.match is TermMatch.ANY else " "
        return f"query.bibliographic={separator.join(request.exact_terms)}"

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]:
        remaining = min(request.result_limit, self.manifest.max_results)
        cursor = "*"
        query = self._query_text(request)
        for _ in range(min(request.page_limit, self.manifest.max_pages)):
            rows = min(100, remaining)
            if rows <= 0:
                break
            body = self.transport.request(
                {
                    "query.bibliographic": query,
                    "rows": str(rows),
                    "cursor": cursor,
                }
            )
            envelope = self._parse(body)
            candidates: list[DiscoveryCandidate] = []
            rejected = 0
            for work in envelope.message.items:
                try:
                    doi = normalize_doi(work.doi)
                    published_at = self._published_at(work.published or work.created)
                except ValueError:
                    rejected += 1
                    continue
                candidates.append(
                    DiscoveryCandidate(
                        upstream_id=f"doi:{doi}",
                        canonical_locator=doi_locator(doi),
                        title=self._text(work.title[0]) if work.title else doi,
                        authors=tuple(
                            name for author in work.author if (name := self._author_name(author))
                        ),
                        publisher=self._text(work.publisher) if work.publisher else None,
                        published_at=published_at,
                        media_type="application/vnd.crossref-api-message+json",
                        language=work.language or request.languages[0],
                        score=work.score,
                        known_entity_ids=(f"doi:{doi}",),
                        metadata={"doi": doi},
                    )
                )
            next_cursor = envelope.message.next_cursor
            yield DiscoveryPage(
                candidates=tuple(candidates),
                cursor=cursor,
                next_cursor=next_cursor if next_cursor and candidates else None,
                rejected_count=rejected,
                response_sha256=hashlib.sha256(body).hexdigest(),
            )
            remaining -= len(envelope.message.items)
            if not next_cursor or not envelope.message.items:
                break
            cursor = next_cursor

    @staticmethod
    def _parse(body: bytes) -> _Envelope:
        try:
            envelope = _Envelope.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            raise CrossrefError("Crossref returned an invalid response") from None
        if envelope.status != "ok":
            raise CrossrefError("Crossref returned an API error")
        return envelope

    def _query_text(self, request: DiscoveryRequest) -> str:
        separator = " OR " if request.match is TermMatch.ANY else " "
        return separator.join(request.exact_terms)

    @staticmethod
    def _author_name(author: _Author) -> str:
        return " ".join(part for part in (author.given, author.family, author.name) if part).strip()

    @staticmethod
    def _published_at(value: _DateParts | None) -> datetime | None:
        if value is None or not value.date_parts:
            return None
        parts = value.date_parts[0]
        if not parts:
            return None
        year, month, day = (*parts, 1, 1)[:3]
        return datetime(year, month, day, tzinfo=UTC)

    @staticmethod
    def _text(value: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", html.unescape(value))
        return re.sub(r"\s+", " ", without_tags).strip()
