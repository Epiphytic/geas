from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from research_agent.discovery import (
    ConnectorCapability,
    ConnectorManifest,
    OpenAccessLocation,
    OpenAccessResolution,
    SourceClass,
    identified,
)
from research_agent.identifiers import doi_locator, normalize_doi


class UnpaywallError(RuntimeError):
    pass


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


class UnpaywallTransport:
    endpoint_prefix = "https://api.unpaywall.org/v2/"

    def __init__(
        self,
        *,
        email_env: str = "UNPAYWALL_EMAIL",
        timeout: float = 20.0,
        max_response_bytes: int = 5_000_000,
    ) -> None:
        self.email_env = email_env
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, doi: str) -> bytes:
        email = os.environ.get(self.email_env)
        if not email:
            raise UnpaywallError(f"missing required environment variable {self.email_env}")
        normalized = normalize_doi(doi)
        encoded_doi = urllib.parse.quote(normalized, safe="")
        endpoint = f"{self.endpoint_prefix}{encoded_doi}"
        request = urllib.request.Request(
            f"{endpoint}?{urllib.parse.urlencode({'email': email})}",
            headers={
                "Accept": "application/json",
                "User-Agent": "Epiphytic-Research-Agent/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl().split("?", 1)[0] != endpoint:
                    raise UnpaywallError(
                        "Unpaywall redirected away from its configured endpoint"
                    )
                if response.headers.get_content_type() != "application/json":
                    raise UnpaywallError("Unpaywall returned an unsupported media type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise UnpaywallError(f"Unpaywall returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise UnpaywallError("Unpaywall request failed") from None
        if len(body) > self.max_response_bytes:
            raise UnpaywallError("Unpaywall response exceeded the configured size limit")
        return body


class UnpaywallLookupTransport(Protocol):
    def request(self, doi: str) -> bytes: ...


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Location(_ExternalModel):
    url: str
    url_for_landing_page: str | None = None
    url_for_pdf: str | None = None
    host_type: str = "unknown"
    version: str = "unknown"
    license: str | None = None
    evidence: str | None = None
    repository_institution: str | None = None
    is_best: bool = False


class _Response(_ExternalModel):
    doi: str
    title: str = ""
    genre: str | None = None
    is_paratext: bool = False
    is_oa: bool = False
    oa_status: str = "closed"
    oa_locations: tuple[_Location, ...] = ()


class UnpaywallResolver:
    connector_id = "connector:unpaywall"
    automatic_license_prefixes = (
        "cc0",
        "public-domain",
        "cc-by",
        "cc by",
    )
    manifest = ConnectorManifest(
        id=connector_id,
        version="1",
        capabilities=frozenset({ConnectorCapability.METADATA}),
        source_classes=frozenset({SourceClass.REPOSITORY}),
        allowed_schemes=frozenset({"https"}),
        allowed_hosts=frozenset({"api.unpaywall.org"}),
        credential_env_vars=("UNPAYWALL_EMAIL",),
        query_fields=frozenset({"doi"}),
        max_results=50,
        max_pages=1,
        max_response_bytes=5_000_000,
        supported_media_types=frozenset({"application/json"}),
        redistribution=(
            "OA-location metadata retained; each linked manifestation retains "
            "its reported or unknown license"
        ),
        parser_version="unpaywall-v2-json/1",
        normalization_version="unpaywall-doi-location/1",
        network_trust_zone="external-index",
        terms_note=(
            "The required project contact is transport-only. Raw responses and "
            "contact identity are not persisted."
        ),
    )

    def __init__(
        self,
        transport: UnpaywallLookupTransport | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport or UnpaywallTransport()
        self.clock = clock or (lambda: datetime.now(UTC))

    def resolve(self, doi: str) -> OpenAccessResolution:
        normalized = normalize_doi(doi)
        body = self.transport.request(normalized)
        try:
            response = _Response.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            raise UnpaywallError("Unpaywall returned an invalid response") from None
        if normalize_doi(response.doi) != normalized:
            raise UnpaywallError("Unpaywall returned a mismatched DOI")
        locations: list[OpenAccessLocation] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for item in response.oa_locations:
            try:
                url = self._public_url(item.url)
                landing = (
                    self._public_url(item.url_for_landing_page)
                    if item.url_for_landing_page
                    else None
                )
                pdf = self._public_url(item.url_for_pdf) if item.url_for_pdf else None
            except ValueError:
                continue
            key = (url, landing, pdf)
            if key in seen:
                continue
            seen.add(key)
            license_value = item.license.strip().casefold() if item.license else None
            eligible = bool(
                license_value
                and license_value.startswith(self.automatic_license_prefixes)
                and "-nc" not in license_value
                and " nc" not in license_value
                and "-nd" not in license_value
                and " nd" not in license_value
            )
            locations.append(
                OpenAccessLocation(
                    url=url,
                    landing_page_url=landing,
                    pdf_url=pdf,
                    host_type=item.host_type,
                    version=item.version,
                    license=license_value,
                    license_status="known" if license_value else "unknown",
                    evidence=item.evidence,
                    repository_institution=item.repository_institution,
                    is_best=item.is_best,
                    automatic_acquisition_eligible=eligible,
                )
            )
        timestamp = self.clock()
        fields = {
            "doi": normalized,
            "connector_id": self.connector_id,
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "resolved_at": timestamp,
            "locations": locations,
        }
        return OpenAccessResolution(
            id=identified("open-access-resolution", fields),
            doi=normalized,
            canonical_locator=doi_locator(normalized),
            connector_id=self.connector_id,
            resolved_at=timestamp,
            response_sha256=hashlib.sha256(body).hexdigest(),
            is_open_access=response.is_oa,
            oa_status=response.oa_status,
            title=response.title.strip() or normalized,
            genre=response.genre,
            is_paratext=response.is_paratext,
            locations=tuple(locations),
        )

    @staticmethod
    def _public_url(value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OA location must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("OA location must not contain user information")
        if parsed.hostname == "localhost" or parsed.hostname.endswith(".local"):
            raise ValueError("OA location must not use a local hostname")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("OA location must not use a non-public address")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        netloc = hostname
        if port and not (
            (parsed.scheme == "http" and port == 80)
            or (parsed.scheme == "https" and port == 443)
        ):
            netloc = f"{hostname}:{port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
        )
