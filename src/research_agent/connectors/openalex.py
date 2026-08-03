from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from research_agent.budget import BudgetPolicy, UsageLedger
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
    normalize_orcid,
    normalize_pmcid,
    normalize_pmid,
    normalize_ror,
)


class OpenAlexError(RuntimeError):
    pass


class OpenAlexTransport(Protocol):
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


class HttpsOpenAlexTransport:
    endpoint = "https://api.openalex.org/works"

    def __init__(
        self,
        *,
        api_key_env: str = "OPENALEX_API_KEY",
        timeout: float = 20.0,
        max_response_bytes: int = 5_000_000,
    ) -> None:
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, parameters: Mapping[str, str]) -> bytes:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise OpenAlexError(f"missing required environment variable {self.api_key_env}")
        query = urllib.parse.urlencode({**parameters, "api_key": api_key})
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
                    raise OpenAlexError("OpenAlex redirected away from its configured endpoint")
                if response.headers.get_content_type() != "application/json":
                    raise OpenAlexError("OpenAlex returned an unsupported media type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise OpenAlexError(f"OpenAlex returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise OpenAlexError("OpenAlex request failed") from None
        if len(body) > self.max_response_bytes:
            raise OpenAlexError("OpenAlex response exceeded the configured size limit")
        return body


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Author(_ExternalModel):
    id: str | None = None
    display_name: str = ""
    orcid: str | None = None


class _Institution(_ExternalModel):
    ror: str | None = None


class _Authorship(_ExternalModel):
    author: _Author
    institutions: tuple[_Institution, ...] = ()


class _Source(_ExternalModel):
    id: str | None = None
    display_name: str = ""
    issn_l: str | None = None
    issn: tuple[str, ...] = ()


class _Location(_ExternalModel):
    landing_page_url: str | None = None
    source: _Source | None = None
    license: str | None = None


class _OpenAccess(_ExternalModel):
    is_oa: bool = False
    oa_status: str | None = None
    oa_url: str | None = None


class _WorkIds(_ExternalModel):
    pmid: str | None = None
    pmcid: str | None = None


class _Work(_ExternalModel):
    id: str
    doi: str | None = None
    display_name: str = ""
    publication_date: str | None = None
    type: str = "unknown"
    language: str | None = None
    cited_by_count: int = Field(default=0, ge=0)
    is_retracted: bool = False
    referenced_works_count: int = Field(default=0, ge=0)
    relevance_score: float = Field(default=0, ge=0)
    authorships: tuple[_Authorship, ...] = ()
    primary_location: _Location | None = None
    open_access: _OpenAccess = Field(default_factory=_OpenAccess)
    ids: _WorkIds = Field(default_factory=_WorkIds)


class _Meta(_ExternalModel):
    count: int = Field(default=0, ge=0)
    next_cursor: str | None = None
    cost_usd: float = Field(default=0, ge=0)


class _Envelope(_ExternalModel):
    meta: _Meta
    results: tuple[_Work, ...] = ()


class OpenAlexDiscoveryConnector:
    manifest = ConnectorManifest(
        id="connector:openalex",
        version="1",
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVERY,
                ConnectorCapability.METADATA,
            }
        ),
        source_classes=frozenset({SourceClass.SCHOLARLY, SourceClass.REPOSITORY}),
        allowed_schemes=frozenset({"https"}),
        allowed_hosts=frozenset({"api.openalex.org"}),
        credential_env_vars=("OPENALEX_API_KEY",),
        query_fields=frozenset({"exact_terms", "match", "languages"}),
        filter_fields=frozenset({"language"}),
        max_results=1_000,
        max_pages=10,
        max_response_bytes=5_000_000,
        supported_media_types=frozenset({"application/json"}),
        redistribution="CC0-1.0 metadata; linked content retains its own rights",
        parser_version="openalex-json/1",
        normalization_version="openalex-work-doi/1",
        network_trust_zone="external-index",
        terms_note=(
            "OpenAlex metadata is CC0. API search is authenticated and "
            "provider-reported cost is preserved; linked full text is not acquired."
        ),
    )

    _work_id = re.compile(r"^https://openalex\.org/(W[1-9][0-9]*)$")
    _select = ",".join(
        (
            "id",
            "doi",
            "display_name",
            "publication_date",
            "type",
            "language",
            "cited_by_count",
            "is_retracted",
            "referenced_works_count",
            "relevance_score",
            "authorships",
            "primary_location",
            "open_access",
            "ids",
        )
    )

    def __init__(
        self,
        transport: OpenAlexTransport | None = None,
        *,
        usage_ledger: UsageLedger | None = None,
        budget_policy: BudgetPolicy | None = None,
        run_id: str | None = None,
        human_approved: bool = False,
        max_calls_per_run: int = 10,
        daily_cost_ceiling_microusd: int = 1_000_000,
    ) -> None:
        self.transport = transport or HttpsOpenAlexTransport()
        self.usage_ledger = usage_ledger
        self.budget_policy = budget_policy
        self.run_id = run_id
        self.human_approved = human_approved
        self.max_calls_per_run = max_calls_per_run
        self.daily_cost_ceiling_microusd = daily_cost_ceiling_microusd
        if (usage_ledger is None) != (budget_policy is None):
            raise ValueError("usage ledger and budget policy must be configured together")

    def normalize_query(self, request: DiscoveryRequest) -> str:
        return f"search={self._query_text(request)};cursor=*"

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]:
        remaining = min(request.result_limit, self.manifest.max_results)
        cursor = "*"
        query = self._query_text(request)
        for page_index in range(min(request.page_limit, self.manifest.max_pages)):
            per_page = min(100, remaining)
            if per_page <= 0:
                break
            parameters = {
                "search": query,
                "per_page": str(per_page),
                "cursor": cursor,
                "select": self._select,
            }
            reservation = None
            if self.usage_ledger is not None and self.budget_policy is not None:
                if self.run_id is None:
                    raise OpenAlexError("budgeted OpenAlex discovery requires a run id")
                reservation = self.usage_ledger.reserve_search(
                    policy=self.budget_policy,
                    provider="openalex",
                    run_id=self.run_id,
                    request_key=f"{request.query_plan_id}:page:{page_index}",
                    human_approved=self.human_approved,
                    max_calls_per_run=self.max_calls_per_run,
                    max_cost_microusd_per_day=self.daily_cost_ceiling_microusd,
                )
            body = self.transport.request(parameters)
            envelope = self._parse(body)
            reported_cost = self._cost_microusd(envelope.meta.cost_usd)
            if reservation is not None:
                assert self.usage_ledger is not None
                settlement = self.usage_ledger.settle_search(
                    reservation,
                    cost_microusd=reported_cost,
                )
                if settlement.status == "overrun":
                    raise OpenAlexError(
                        "OpenAlex reported a cost above the deterministic reservation"
                    )
            candidates: list[DiscoveryCandidate] = []
            rejected = 0
            for work in envelope.results:
                try:
                    candidate = self._candidate(work, request)
                except ValueError:
                    rejected += 1
                    continue
                candidates.append(candidate)
            next_cursor = envelope.meta.next_cursor
            yield DiscoveryPage(
                candidates=tuple(candidates),
                cursor=cursor,
                next_cursor=next_cursor if next_cursor and envelope.results else None,
                rejected_count=rejected,
                response_sha256=hashlib.sha256(body).hexdigest(),
                reported_cost_microusd=reported_cost,
            )
            remaining -= len(envelope.results)
            if not next_cursor or not envelope.results:
                break
            cursor = next_cursor

    def _candidate(self, work: _Work, request: DiscoveryRequest) -> DiscoveryCandidate:
        match = self._work_id.fullmatch(work.id)
        if match is None:
            raise ValueError("invalid OpenAlex work identifier")
        short_id = match.group(1)
        known_ids = [f"openalex:{short_id}"]
        if work.doi:
            doi = normalize_doi(work.doi)
            locator = doi_locator(doi)
            known_ids.append(f"doi:{doi}")
        else:
            locator = work.id
        self._append_identifier(known_ids, "pmid", work.ids.pmid, normalize_pmid)
        self._append_identifier(known_ids, "pmcid", work.ids.pmcid, normalize_pmcid)
        for authorship in work.authorships:
            self._append_identifier(
                known_ids,
                "orcid",
                authorship.author.orcid,
                normalize_orcid,
            )
            for institution in authorship.institutions:
                self._append_identifier(
                    known_ids,
                    "ror",
                    institution.ror,
                    normalize_ror,
                )
        authors = tuple(
            item.author.display_name.strip()
            for item in work.authorships
            if item.author.display_name.strip()
        )
        source = work.primary_location.source if work.primary_location else None
        if source:
            self._append_identifier(known_ids, "issn", source.issn_l, normalize_issn)
            for issn in source.issn:
                self._append_identifier(known_ids, "issn", issn, normalize_issn)
        publisher = source.display_name.strip() if source and source.display_name.strip() else None
        publication = self._publication_date(work.publication_date)
        metadata: dict[str, str | int | float | bool] = {
            "openalex_id": short_id,
            "work_type": work.type,
            "cited_by_count": work.cited_by_count,
            "referenced_works_count": work.referenced_works_count,
            "is_retracted": work.is_retracted,
            "is_open_access": work.open_access.is_oa,
        }
        if work.doi:
            metadata["doi"] = normalize_doi(work.doi)
        if work.open_access.oa_status:
            metadata["open_access_status"] = work.open_access.oa_status
        if work.primary_location and work.primary_location.license:
            metadata["location_license"] = work.primary_location.license
        snippet = (
            f"type={work.type}; cited_by={work.cited_by_count}; "
            f"references={work.referenced_works_count}; "
            f"open_access={work.open_access.oa_status or 'unknown'}; "
            f"retracted={str(work.is_retracted).lower()}"
        )
        return DiscoveryCandidate(
            upstream_id=f"openalex:{short_id}",
            canonical_locator=locator,
            title=work.display_name.strip() or short_id,
            authors=authors,
            publisher=publisher,
            published_at=publication,
            media_type="application/json",
            language=work.language or request.languages[0],
            snippet=snippet,
            score=work.relevance_score,
            known_entity_ids=tuple(dict.fromkeys(known_ids)),
            metadata=metadata,
        )

    @staticmethod
    def _parse(body: bytes) -> _Envelope:
        try:
            return _Envelope.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            raise OpenAlexError("OpenAlex returned an invalid response") from None

    @staticmethod
    def _publication_date(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("invalid OpenAlex publication date") from None
        return parsed.replace(tzinfo=UTC)

    @staticmethod
    def _cost_microusd(value: float) -> int:
        try:
            cost = Decimal(str(value))
        except InvalidOperation:
            raise OpenAlexError("OpenAlex returned invalid cost metadata") from None
        if not cost.is_finite() or cost < 0:
            raise OpenAlexError("OpenAlex returned invalid cost metadata")
        return int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))

    def _query_text(self, request: DiscoveryRequest) -> str:
        separator = " OR " if request.match is TermMatch.ANY else " AND "
        return separator.join(self._quote_term(term) for term in request.exact_terms)

    @staticmethod
    def _append_identifier(
        identifiers: list[str],
        kind: str,
        value: str | None,
        normalizer: Callable[[str], str],
    ) -> None:
        if value is None:
            return
        try:
            normalized = normalizer(value)
        except ValueError:
            return
        identifiers.append(f"{kind}:{normalized}")

    @staticmethod
    def _quote_term(term: str) -> str:
        escaped = term.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
