from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
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
from research_agent.identifiers import (
    doi_locator,
    normalize_doi,
    normalize_issn,
    normalize_pmcid,
    normalize_pmid,
)


class EuropePmcError(RuntimeError):
    pass


class EuropePmcTransport(Protocol):
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


class HttpsEuropePmcTransport:
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, *, timeout: float = 20.0, max_response_bytes: int = 5_000_000) -> None:
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, parameters: Mapping[str, str]) -> bytes:
        request = urllib.request.Request(
            f"{self.endpoint}?{urllib.parse.urlencode(parameters)}",
            headers={
                "Accept": "application/json",
                "User-Agent": "Epiphytic-Research-Agent/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl().split("?", 1)[0] != self.endpoint:
                    raise EuropePmcError(
                        "Europe PMC redirected away from its configured endpoint"
                    )
                if response.headers.get_content_type() != "application/json":
                    raise EuropePmcError("Europe PMC returned an unsupported media type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise EuropePmcError(f"Europe PMC returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise EuropePmcError("Europe PMC request failed") from None
        if len(body) > self.max_response_bytes:
            raise EuropePmcError("Europe PMC response exceeded the configured size limit")
        return body


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Result(_ExternalModel):
    id: str
    source: str
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    title: str = ""
    author_string: str = Field(default="", alias="authorString")
    journal_title: str | None = Field(default=None, alias="journalTitle")
    journal_issn: str | None = Field(default=None, alias="journalIssn")
    pub_year: str | None = Field(default=None, alias="pubYear")
    first_publication_date: str | None = Field(
        default=None,
        alias="firstPublicationDate",
    )
    cited_by_count: int = Field(default=0, ge=0, alias="citedByCount")
    is_open_access: str = Field(default="N", alias="isOpenAccess")
    in_epmc: str = Field(default="N", alias="inEPMC")
    in_pmc: str = Field(default="N", alias="inPMC")


class _ResultList(_ExternalModel):
    result: tuple[_Result, ...] = ()


class _Envelope(_ExternalModel):
    hit_count: int = Field(default=0, ge=0, alias="hitCount")
    next_cursor_mark: str | None = Field(default=None, alias="nextCursorMark")
    result_list: _ResultList = Field(alias="resultList")


class EuropePmcDiscoveryConnector:
    manifest = ConnectorManifest(
        id="connector:europe-pmc",
        version="1",
        capabilities=frozenset(
            {ConnectorCapability.DISCOVERY, ConnectorCapability.METADATA}
        ),
        source_classes=frozenset({SourceClass.SCHOLARLY, SourceClass.REPOSITORY}),
        allowed_schemes=frozenset({"https"}),
        allowed_hosts=frozenset({"www.ebi.ac.uk"}),
        query_fields=frozenset({"exact_terms", "match", "languages"}),
        filter_fields=frozenset({"language"}),
        max_results=1_000,
        max_pages=20,
        max_response_bytes=5_000_000,
        supported_media_types=frozenset({"application/json"}),
        redistribution=(
            "lite bibliographic metadata only; linked material retains author or "
            "publisher rights"
        ),
        parser_version="europe-pmc-lite-json/1",
        normalization_version="europe-pmc-identifiers/1",
        network_trust_zone="external-index",
        terms_note=(
            "Only resultType=lite bibliographic metadata is retained. Abstracts, "
            "full text, and linked files require separate license-aware acquisition."
        ),
    )

    def __init__(self, transport: EuropePmcTransport | None = None) -> None:
        self.transport = transport or HttpsEuropePmcTransport()

    def normalize_query(self, request: DiscoveryRequest) -> str:
        return f"query={self._query_text(request)};cursorMark=*;resultType=lite"

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]:
        remaining = min(request.result_limit, self.manifest.max_results)
        cursor = "*"
        query = self._query_text(request)
        for _ in range(min(request.page_limit, self.manifest.max_pages)):
            page_size = min(100, remaining)
            if page_size <= 0:
                break
            body = self.transport.request(
                {
                    "query": query,
                    "format": "json",
                    "resultType": "lite",
                    "pageSize": str(page_size),
                    "cursorMark": cursor,
                }
            )
            envelope = self._parse(body)
            candidates: list[DiscoveryCandidate] = []
            rejected = 0
            for rank, result in enumerate(envelope.result_list.result):
                try:
                    candidates.append(self._candidate(result, request, rank))
                except ValueError:
                    rejected += 1
            next_cursor = envelope.next_cursor_mark
            yield DiscoveryPage(
                candidates=tuple(candidates),
                cursor=cursor,
                next_cursor=next_cursor if next_cursor and envelope.result_list.result else None,
                rejected_count=rejected,
                response_sha256=hashlib.sha256(body).hexdigest(),
            )
            remaining -= len(envelope.result_list.result)
            if not next_cursor or not envelope.result_list.result:
                break
            cursor = next_cursor

    def _candidate(
        self,
        result: _Result,
        request: DiscoveryRequest,
        rank: int,
    ) -> DiscoveryCandidate:
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,15}", result.source):
            raise ValueError("invalid Europe PMC source")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", result.id):
            raise ValueError("invalid Europe PMC record id")
        known_ids = [f"europe-pmc:{result.source}:{result.id}"]
        locator = f"https://europepmc.org/article/{result.source}/{result.id}"
        metadata: dict[str, str | int | float | bool] = {
            "europe_pmc_source": result.source,
            "cited_by_count": result.cited_by_count,
            "is_open_access": result.is_open_access == "Y",
            "in_europe_pmc": result.in_epmc == "Y",
            "in_pubmed_central": result.in_pmc == "Y",
        }
        if result.doi:
            try:
                doi = normalize_doi(result.doi)
            except ValueError:
                pass
            else:
                known_ids.append(f"doi:{doi}")
                metadata["doi"] = doi
                locator = doi_locator(doi)
        if result.pmid:
            self._append_identifier(
                known_ids,
                metadata,
                "pmid",
                result.pmid,
                normalize_pmid,
            )
        if result.pmcid:
            self._append_identifier(
                known_ids,
                metadata,
                "pmcid",
                result.pmcid,
                normalize_pmcid,
            )
        if result.journal_issn:
            self._append_identifier(
                known_ids,
                metadata,
                "issn",
                result.journal_issn,
                normalize_issn,
            )
        return DiscoveryCandidate(
            upstream_id=f"europe-pmc:{result.source}:{result.id}",
            canonical_locator=locator,
            title=result.title.strip() or result.id,
            authors=tuple(
                item.strip()
                for item in result.author_string.split(",")
                if item.strip()
            ),
            publisher=result.journal_title,
            published_at=self._publication_date(
                result.first_publication_date,
                result.pub_year,
            ),
            media_type="application/json",
            language=request.languages[0],
            snippet=(
                f"source={result.source}; cited_by={result.cited_by_count}; "
                f"open_access={result.is_open_access == 'Y'}"
            ),
            score=float(max(0, 100 - rank)),
            known_entity_ids=tuple(dict.fromkeys(known_ids)),
            metadata=metadata,
        )

    @staticmethod
    def _parse(body: bytes) -> _Envelope:
        try:
            return _Envelope.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            raise EuropePmcError("Europe PMC returned an invalid response") from None

    @staticmethod
    def _publication_date(value: str | None, year: str | None) -> datetime | None:
        candidate = value or year
        if candidate is None:
            return None
        try:
            if re.fullmatch(r"[0-9]{4}", candidate):
                return datetime(int(candidate), 1, 1, tzinfo=UTC)
            return datetime.fromisoformat(candidate).replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _append_identifier(
        identifiers: list[str],
        metadata: dict[str, str | int | float | bool],
        kind: str,
        value: str,
        normalizer: Callable[[str], str],
    ) -> None:
        try:
            normalized = normalizer(value)
        except ValueError:
            return
        identifiers.append(f"{kind}:{normalized}")
        metadata[kind] = normalized

    def _query_text(self, request: DiscoveryRequest) -> str:
        separator = " OR " if request.match is TermMatch.ANY else " AND "
        return separator.join(self._quote_term(term) for term in request.exact_terms)

    @staticmethod
    def _quote_term(term: str) -> str:
        escaped = term.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
