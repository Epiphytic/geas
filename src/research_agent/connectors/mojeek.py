from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
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


class MojeekError(RuntimeError):
    pass


class MojeekTransport(Protocol):
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


class HttpsMojeekTransport:
    """Fixed-destination transport that never exposes the key in raised errors."""

    endpoint = "https://api.mojeek.com/search"

    def __init__(
        self,
        *,
        api_key_env: str = "MOJEEK_API_KEY",
        timeout: float = 20.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, parameters: Mapping[str, str]) -> bytes:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise MojeekError(f"missing required environment variable {self.api_key_env}")
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
                    raise MojeekError("Mojeek redirected away from its configured endpoint")
                content_type = response.headers.get_content_type()
                if content_type not in {"application/json", "text/json"}:
                    raise MojeekError("Mojeek returned an unsupported media type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise MojeekError(f"Mojeek returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise MojeekError("Mojeek request failed") from None
        if len(body) > self.max_response_bytes:
            raise MojeekError("Mojeek response exceeded the configured size limit")
        return body


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _MojeekResult(_ExternalModel):
    url: str
    title: str = ""
    desc: str = ""


class _MojeekHead(_ExternalModel):
    query: str
    start: int = Field(ge=0)
    results: int = Field(default=0, ge=0)


class _MojeekPayload(_ExternalModel):
    status: str
    head: _MojeekHead
    results: tuple[_MojeekResult, ...] = ()


class _MojeekEnvelope(_ExternalModel):
    response: _MojeekPayload


class MojeekDiscoveryConnector:
    """Discovery-only access to Mojeek's independent web index."""

    manifest = ConnectorManifest(
        id="connector:mojeek",
        version="1",
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVERY,
                ConnectorCapability.METADATA,
            }
        ),
        source_classes=frozenset(
            {
                SourceClass.GOVERNMENT,
                SourceClass.NEWS,
                SourceClass.SCHOLARLY,
                SourceClass.WEB,
            }
        ),
        allowed_schemes=frozenset({"https"}),
        allowed_hosts=frozenset({"api.mojeek.com"}),
        credential_env_vars=("MOJEEK_API_KEY",),
        query_fields=frozenset({"exact_terms", "match", "languages"}),
        filter_fields=frozenset({"language"}),
        max_results=200,
        max_pages=5,
        max_response_bytes=2_000_000,
        supported_media_types=frozenset({"application/json", "text/json"}),
        redistribution="storage rights depend on operator plan",
        parser_version="mojeek-json/1",
        normalization_version="mojeek-web-result/1",
        network_trust_zone="external-index",
        terms_note=(
            "Discovery only. Business storage rights must be confirmed before "
            "persisting normalized results beyond transient operation."
        ),
    )

    def __init__(self, transport: MojeekTransport | None = None) -> None:
        self.transport = transport or HttpsMojeekTransport()

    def normalize_query(self, request: DiscoveryRequest) -> str:
        terms = [self._quote(term) for term in request.exact_terms]
        separator = " OR " if request.match is TermMatch.ANY else " "
        language = request.languages[0].split("-", 1)[0].upper()
        return f"q={separator.join(terms)};lb={language};lbb=100"

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveryPage]:
        query = self._query_text(request)
        language = request.languages[0].split("-", 1)[0].upper()
        remaining = min(request.result_limit, self.manifest.max_results)
        start = 1
        for _page_number in range(min(request.page_limit, self.manifest.max_pages)):
            count = min(40, remaining)
            if count <= 0:
                break
            parameters = {
                "q": query,
                "fmt": "json",
                "s": str(start),
                "t": str(count),
                "lb": language,
                "lbb": "100",
                "date": "1",
                "cdate": "1",
                "dlen": "511",
            }
            body = self.transport.request(parameters)
            payload = self._parse(body)
            candidates: list[DiscoveryCandidate] = []
            rejected = 0
            for item in payload.response.results:
                try:
                    locator = self._canonical_locator(item.url)
                except ValueError:
                    rejected += 1
                    continue
                candidates.append(
                    DiscoveryCandidate(
                        upstream_id=(
                            f"mojeek:url:sha256:{hashlib.sha256(locator.encode()).hexdigest()}"
                        ),
                        canonical_locator=locator,
                        title=self._text(item.title) or locator,
                        media_type="text/html",
                        language=request.languages[0],
                        snippet=self._text(item.desc) or None,
                        score=0,
                    )
                )
            returned = len(payload.response.results)
            next_start = start + returned
            has_more = returned == count and next_start <= payload.response.head.results
            yield DiscoveryPage(
                candidates=tuple(candidates),
                cursor=str(start),
                next_cursor=str(next_start) if has_more else None,
                rejected_count=rejected,
                response_sha256=hashlib.sha256(body).hexdigest(),
            )
            remaining -= returned
            if not has_more or returned == 0:
                break
            start = next_start

    @staticmethod
    def _parse(body: bytes) -> _MojeekEnvelope:
        try:
            value = json.loads(body)
            envelope = _MojeekEnvelope.model_validate(value)
        except (json.JSONDecodeError, ValueError):
            raise MojeekError("Mojeek returned an invalid response") from None
        if envelope.response.status != "OK":
            raise MojeekError("Mojeek returned an API error")
        return envelope

    @staticmethod
    def _quote(term: str) -> str:
        escaped = term.replace('"', "")
        return f'"{escaped}"' if " " in escaped else escaped

    def _query_text(self, request: DiscoveryRequest) -> str:
        terms = [self._quote(term) for term in request.exact_terms]
        return (" OR " if request.match is TermMatch.ANY else " ").join(terms)

    @staticmethod
    def _text(value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(value)).strip()

    @staticmethod
    def _canonical_locator(value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("result locator must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("result locator must not contain user information")
        if parsed.hostname == "localhost" or parsed.hostname.endswith(".local"):
            raise ValueError("result locator must not use a local hostname")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("result locator must not use a non-public address")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        netloc = hostname
        if port and not (
            (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
        ):
            netloc = f"{hostname}:{port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
        )
