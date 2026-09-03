from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import mimetypes
import shutil
import socket
import ssl
import subprocess
import tempfile
import unicodedata
import urllib.parse
import zlib
from collections.abc import Callable, Mapping
from datetime import datetime
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

from pydantic import Field, field_validator

from research_agent.capabilities import Capability, CapabilityEvaluator, CapabilityRequest
from research_agent.discovery import (
    AccessConstraint,
    AccessConstraintReason,
    AcquisitionAttempt,
    AcquisitionState,
    OpenAccessLocation,
    OpenAccessResolution,
    identified,
)
from research_agent.models import SourceVersion, StrictModel, utc_now
from research_agent.parsing import ParsedDocumentManager, ParsedIngestReceipt, ParserError
from research_agent.source_intent import _source_url
from research_agent.store import ImmutableStore


class RemoteFetchError(RuntimeError):
    pass


class SourceFetchConstraint(StrEnum):
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"


class SourceValidator(StrictModel):
    """Only replay-safe cache validators may be carried across source runs."""

    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=512)

    @field_validator("etag", "last_modified")
    @classmethod
    def no_controls(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("conditional validator contains control characters")
        return value


class SourceFetchRequest(StrictModel):
    """A fully bounded fetch request assembled from trusted source intent."""

    locator: str
    allowed_hosts: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]
    accepted_media_types: tuple[str, ...] = ()
    max_wire_bytes: int = Field(default=5_000_000, gt=0, le=100_000_000)
    max_decoded_bytes: int = Field(default=25_000_000, gt=0, le=100_000_000)
    max_redirects: int = Field(default=3, ge=0, le=10)
    connect_timeout_seconds: int = Field(default=10, gt=0, le=120)
    read_timeout_seconds: int = Field(default=30, gt=0, le=300)
    capability_request: CapabilityRequest | None = None

    @property
    def url(self) -> str:
        return self.locator


class SourceFetchResult(StrictModel):
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()
    status: int
    media_type: str | None = None
    content: bytes = b""
    validator: SourceValidator = Field(default_factory=SourceValidator)
    retry_after: int | None = None
    constraint: SourceFetchConstraint | None = None


class ConditionalHttpResponse(StrictModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""


class ConditionalHttpClient(Protocol):
    def request(
        self,
        *,
        url: str,
        address: str,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        max_wire_bytes: int,
    ) -> ConditionalHttpResponse: ...


class PinnedHttpsClient:
    """Minimal production client: connect to the chosen address, validate TLS hostname."""

    def request(
        self,
        *,
        url: str,
        address: str,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        max_wire_bytes: int,
    ) -> ConditionalHttpResponse:
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname is not None
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection = _PinnedHttpsConnection(
            hostname=parsed.hostname,
            address=address,
            timeout=connect_timeout_seconds,
        )
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            connection.sock.settimeout(read_timeout_seconds)  # type: ignore[union-attr]
            chunks: list[bytes] = []
            size = 0
            while chunk := response.read(min(65_536, max_wire_bytes + 1)):
                size += len(chunk)
                if size > max_wire_bytes:
                    raise RemoteFetchError("HTTPS response exceeded the compressed size limit")
                chunks.append(chunk)
            return ConditionalHttpResponse(
                status=response.status,
                headers={key: value for key, value in response.getheaders()},
                body=b"".join(chunks),
            )
        except TimeoutError as error:
            raise RemoteFetchError("bounded HTTPS request timed out") from error
        except (OSError, http.client.HTTPException) as error:
            raise RemoteFetchError("bounded HTTPS request failed") from error
        finally:
            connection.close()


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, *, hostname: str, address: str, timeout: int) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class ConditionalHttpsTransport:
    """HTTPS transport that validates every DNS answer and every redirect.

    A concrete client is deliberately injected: production embedding selects a
    connection implementation capable of pinning ``address`` while preserving
    hostname TLS validation.  This layer owns the deterministic policy checks.
    """

    version = "conditional-https-transport/1"

    def __init__(
        self,
        *,
        dns_resolver: Callable[[str], tuple[str, ...]] | None = None,
        http_client: ConditionalHttpClient | None = None,
        capability_evaluator: CapabilityEvaluator | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.dns_resolver = dns_resolver or self._resolve_all
        self.http_client = http_client or PinnedHttpsClient()
        self.capability_evaluator = capability_evaluator
        self.clock = clock

    def fetch(
        self, request: SourceFetchRequest, *, prior: SourceValidator | None = None
    ) -> SourceFetchResult:
        current = self._authorize_url(request, request.locator)
        requested = current
        redirects: list[str] = []
        headers: dict[str, str] = {"accept-encoding": "gzip, deflate"}
        if prior is not None:
            if prior.etag:
                headers["if-none-match"] = prior.etag
            if prior.last_modified:
                headers["if-modified-since"] = prior.last_modified
        for _ in range(request.max_redirects + 1):
            parsed = urllib.parse.urlsplit(current)
            assert parsed.hostname is not None
            self._require_capability(request, current)
            address = self._public_address(parsed.hostname)
            try:
                response = self.http_client.request(
                    url=current,
                    address=address,
                    headers=headers,
                    connect_timeout_seconds=request.connect_timeout_seconds,
                    read_timeout_seconds=request.read_timeout_seconds,
                    max_wire_bytes=request.max_wire_bytes,
                )
            except TimeoutError:
                raise RemoteFetchError("bounded HTTPS request timed out") from None
            clean_headers = {key.casefold(): value for key, value in response.headers.items()}
            validator = SourceValidator(
                etag=self._safe_metadata(clean_headers.get("etag")),
                last_modified=self._safe_metadata(clean_headers.get("last-modified")),
            )
            if response.status == 304:
                if prior is None or (prior.etag is None and prior.last_modified is None):
                    raise RemoteFetchError("received unsolicited not-modified response")
                return SourceFetchResult(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    status=response.status,
                    validator=SourceValidator(
                        etag=validator.etag or (prior.etag if prior else None),
                        last_modified=validator.last_modified
                        or (prior.last_modified if prior else None),
                    ),
                )
            if 300 <= response.status < 400:
                location = clean_headers.get("location")
                if not location:
                    raise RemoteFetchError("redirect omitted its destination")
                current = self._authorize_url(request, urllib.parse.urljoin(current, location))
                redirects.append(current)
                continue
            retry_after = self._retry_after(clean_headers.get("retry-after"))
            constraint = self._constraint(response.status)
            if constraint is not None:
                return SourceFetchResult(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    status=response.status,
                    validator=validator,
                    retry_after=retry_after,
                    constraint=constraint,
                )
            if not 200 <= response.status < 300:
                raise RemoteFetchError(f"HTTPS fetch returned status {response.status}")
            if len(response.body) > request.max_wire_bytes:
                raise RemoteFetchError("HTTPS response exceeded the compressed size limit")
            content = self._decoded(
                response.body,
                clean_headers.get("content-encoding"),
                request.max_decoded_bytes,
            )
            claimed = clean_headers.get("content-type", "application/octet-stream").split(";", 1)[0]
            media_type = self._sniff_media(claimed, current, content)
            if self._is_xml_family(claimed) and media_type == "application/xml":
                media_type = claimed.casefold()
            if media_type == "application/octet-stream":
                return SourceFetchResult(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    status=response.status,
                    media_type=media_type,
                    validator=validator,
                    constraint=SourceFetchConstraint.UNSUPPORTED_MEDIA_TYPE,
                )
            if self._claimed_type_conflicts(claimed, media_type):
                return SourceFetchResult(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    status=response.status,
                    media_type=media_type,
                    validator=validator,
                    constraint=SourceFetchConstraint.UNSUPPORTED_MEDIA_TYPE,
                )
            if request.accepted_media_types and media_type not in request.accepted_media_types:
                return SourceFetchResult(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    status=response.status,
                    media_type=media_type,
                    validator=validator,
                    constraint=SourceFetchConstraint.UNSUPPORTED_MEDIA_TYPE,
                )
            return SourceFetchResult(
                requested_url=requested,
                final_url=current,
                redirect_chain=tuple(redirects),
                status=response.status,
                media_type=media_type,
                content=content,
                validator=validator,
            )
        raise RemoteFetchError("HTTPS redirect limit exceeded")

    def _public_address(self, hostname: str) -> str:
        try:
            addresses = tuple(sorted(set(self.dns_resolver(hostname))))
        except (OSError, socket.gaierror):
            raise RemoteFetchError("remote hostname did not resolve") from None
        if not addresses:
            raise RemoteFetchError("remote hostname did not resolve")
        try:
            parsed = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError:
            raise RemoteFetchError("remote hostname resolved to an invalid address") from None
        if any(not address.is_global for address in parsed):
            raise RemoteFetchError("remote hostname resolved to a non-public address")
        return str(parsed[0])

    @staticmethod
    def _resolve_all(hostname: str) -> tuple[str, ...]:
        return tuple(
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        )

    @staticmethod
    def _authorize_url(request: SourceFetchRequest, value: str) -> str:
        try:
            normalized = _source_url(value)
        except ValueError as error:
            raise RemoteFetchError("remote URL is unsafe") from error
        parsed = urllib.parse.urlsplit(normalized)
        assert parsed.hostname is not None
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise RemoteFetchError("remote URL must name a hostname, not an IP address")
        if parsed.hostname not in request.allowed_hosts:
            raise RemoteFetchError("remote URL host is outside the source authority")
        if not any(
            prefix == "/"
            or parsed.path == prefix.rstrip("/")
            or parsed.path.startswith(f"{prefix.rstrip('/')}/")
            for prefix in request.allowed_path_prefixes
        ):
            raise RemoteFetchError("remote URL path is outside the source authority")
        return normalized

    def _require_capability(self, request: SourceFetchRequest, locator: str) -> None:
        if self.capability_evaluator is None or request.capability_request is None:
            raise RemoteFetchError("conditional HTTPS fetch requires a capability evaluator")
        parsed = urllib.parse.urlsplit(locator)
        decision = self.capability_evaluator.evaluate(
            request.capability_request.model_copy(
                update={
                    "capabilities": (Capability.SOURCE_FETCH,),
                    "host": parsed.hostname,
                    "target": locator,
                }
            )
        )
        if (
            decision.decision != "allow"
            or Capability.SOURCE_FETCH not in decision.effective_capabilities
        ):
            raise RemoteFetchError("conditional HTTPS fetch denied by capability evaluator")

    @staticmethod
    def _decoded(body: bytes, encoding: str | None, maximum: int) -> bytes:
        try:
            if encoding is None or encoding.casefold() in {"", "identity"}:
                decoded = body
            elif encoding.casefold() == "gzip":
                decoded = ConditionalHttpsTransport._stream_decode(
                    body, 16 + zlib.MAX_WBITS, maximum
                )
            elif encoding.casefold() == "deflate":
                decoded = ConditionalHttpsTransport._stream_decode(body, zlib.MAX_WBITS, maximum)
            else:
                raise RemoteFetchError("unsupported content encoding")
        except (OSError, zlib.error):
            raise RemoteFetchError("invalid compressed HTTPS response") from None
        if len(decoded) > maximum:
            raise RemoteFetchError("HTTPS response exceeded the decoded size limit")
        return decoded

    @staticmethod
    def _stream_decode(body: bytes, wbits: int, maximum: int) -> bytes:
        decoder = zlib.decompressobj(wbits)
        pieces: list[bytes] = []
        size = 0
        for start in range(0, len(body), 65_536):
            remaining = maximum - size
            piece = decoder.decompress(body[start : start + 65_536], remaining + 1)
            size += len(piece)
            if size > maximum or decoder.unconsumed_tail:
                raise RemoteFetchError("HTTPS response exceeded the decoded size limit")
            pieces.append(piece)
        if not decoder.eof or decoder.unused_data:
            raise RemoteFetchError("invalid compressed HTTPS response")
        tail = decoder.flush(maximum - size + 1)
        if size + len(tail) > maximum:
            raise RemoteFetchError("HTTPS response exceeded the decoded size limit")
        pieces.append(tail)
        return b"".join(pieces)

    @staticmethod
    def _constraint(status: int) -> SourceFetchConstraint | None:
        if status in {401, 403}:
            return SourceFetchConstraint.DENIED
        if status in {429, 503}:
            return SourceFetchConstraint.RATE_LIMITED
        if status == 415:
            return SourceFetchConstraint.UNSUPPORTED_MEDIA_TYPE
        return None

    def _retry_after(self, value: str | None) -> int | None:
        if value is None:
            return None
        try:
            result = int(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                return None
            return max(0, int((parsed - self.clock()).total_seconds()))
        return result if result >= 0 else None

    @staticmethod
    def _sniff_media(claimed: str, url: str, content: bytes) -> str:
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        xml_text = ConditionalHttpsTransport._decoded_xml_text(content)
        if xml_text is not None and xml_text.lstrip().startswith("<"):
            lowered = xml_text.lstrip()[:512].casefold()
            if lowered.startswith(("<!doctype html", "<html")):
                return (
                    "text/html"
                    if ConditionalHttpsTransport._valid_text(xml_text)
                    else "application/octet-stream"
                )
            return (
                "application/xml"
                if ConditionalHttpsTransport._secure_xml(content)
                else "application/octet-stream"
            )
        text = ConditionalHttpsTransport._decoded_utf8_text(content)
        if text is None:
            return "application/octet-stream"
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return "application/octet-stream"
            return "application/json"
        if content and ConditionalHttpsTransport._valid_text(text):
            return "text/plain"
        return "application/octet-stream"

    @staticmethod
    def _decoded_utf8_text(content: bytes) -> str | None:
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _decoded_xml_text(content: bytes) -> str | None:
        encoding = "utf-8-sig"
        if content.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif content.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _valid_text(text: str) -> bool:
        return all(
            character in {"\t", "\n", "\r"}
            or unicodedata.category(character) != "Cc"
            for character in text
        )

    @staticmethod
    def _claimed_type_conflicts(claimed: str, sniffed: str) -> bool:
        normalized = claimed.casefold()
        if normalized in {"", "application/octet-stream", "text/plain"}:
            return False
        return normalized != sniffed

    @staticmethod
    def _is_xml_family(value: str) -> bool:
        normalized = value.casefold()
        return normalized in {
            "application/xml",
            "text/xml",
            "application/rss+xml",
            "application/atom+xml",
        } or normalized.endswith("+xml")

    @staticmethod
    def _secure_xml(content: bytes) -> bool:
        text = ConditionalHttpsTransport._decoded_xml_text(content)
        if text is None or not ConditionalHttpsTransport._valid_text(text):
            return False
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            return False
        try:
            ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return False
        return True

    @staticmethod
    def _safe_metadata(value: str | None) -> str | None:
        if value is None or len(value) > 512:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return None
        return value


class FetchedDocument(StrictModel):
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    media_type: str
    content: bytes


class RemoteDocumentFetcher(Protocol):
    def fetch(self, url: str) -> FetchedDocument: ...


class RemoteAcquisitionReceipt(StrictModel):
    acquisition_attempt: AcquisitionAttempt | None = None
    parsed_ingest: ParsedIngestReceipt | None = None
    access_constraint: AccessConstraint | None = None
    selected_location: OpenAccessLocation | None = None
    record_hashes: dict[str, tuple[str, ...]]


class PinnedHttpsFetcher:
    version = "pinned-curl-fetcher/1"

    def __init__(
        self,
        *,
        max_bytes: int = 25_000_000,
        max_redirects: int = 3,
        timeout_seconds: int = 30,
        dns_resolver: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds
        self.dns_resolver = dns_resolver or self._resolve_all

    def fetch(
        self,
        url: str,
        *,
        before_request: Callable[[str], None] | None = None,
    ) -> FetchedDocument:
        requested = self._validated_url(url)
        current = requested
        redirects: list[str] = []
        for _ in range(self.max_redirects + 1):
            if before_request is not None:
                before_request(current)
            address = self._public_address(current)
            parsed = urllib.parse.urlsplit(current)
            assert parsed.hostname is not None
            pinned_address = f"[{address}]" if ":" in address else address
            with tempfile.TemporaryDirectory(prefix="research-agent-fetch-") as directory:
                root = Path(directory)
                headers_path = root / "headers"
                body_path = root / "body"
                executable = shutil.which("curl")
                if executable is None:
                    raise RemoteFetchError("curl is unavailable")
                command = [
                    executable,
                    "--silent",
                    "--show-error",
                    "--noproxy",
                    "*",
                    "--proto",
                    "=https",
                    "--max-redirs",
                    "0",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    str(self.timeout_seconds),
                    "--max-filesize",
                    str(self.max_bytes),
                    "--resolve",
                    f"{parsed.hostname}:443:{pinned_address}",
                    "--dump-header",
                    str(headers_path),
                    "--output",
                    str(body_path),
                    "--write-out",
                    "%{http_code}",
                    current,
                ]
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        timeout=self.timeout_seconds + 5,
                        check=False,
                        close_fds=True,
                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    )
                except subprocess.TimeoutExpired:
                    raise RemoteFetchError("bounded HTTPS fetch timed out") from None
                if completed.returncode != 0:
                    raise RemoteFetchError("bounded HTTPS fetch failed")
                try:
                    status = int(completed.stdout)
                    header_bytes = headers_path.read_bytes()
                    _, _, header_fields = header_bytes.partition(b"\r\n")
                    headers = BytesHeaderParser().parsebytes(header_fields)
                except (ValueError, OSError):
                    raise RemoteFetchError("invalid HTTPS response metadata") from None
                if 300 <= status < 400:
                    location = headers.get("Location")
                    if not location:
                        raise RemoteFetchError("redirect omitted its destination")
                    current = self._validated_url(urllib.parse.urljoin(current, location))
                    redirects.append(current)
                    continue
                if not 200 <= status < 300:
                    raise RemoteFetchError(f"HTTPS fetch returned status {status}")
                content = body_path.read_bytes()
                if len(content) > self.max_bytes:
                    raise RemoteFetchError("HTTPS response exceeded the size limit")
                media_type = self._media_type(
                    headers.get_content_type(),
                    current,
                    content,
                )
                return FetchedDocument(
                    requested_url=requested,
                    final_url=current,
                    redirect_chain=tuple(redirects),
                    media_type=media_type,
                    content=content,
                )
        raise RemoteFetchError("HTTPS redirect limit exceeded")

    @staticmethod
    def _validated_url(value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise RemoteFetchError("remote acquisition requires HTTPS")
        if parsed.username or parsed.password:
            raise RemoteFetchError("remote URL must not contain credentials")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        if hostname == "localhost" or hostname.endswith(".local"):
            raise RemoteFetchError("remote URL must not use a local hostname")
        if parsed.port not in {None, 443}:
            raise RemoteFetchError("remote URL must use the default HTTPS port")
        return urllib.parse.urlunsplit(("https", hostname, parsed.path or "/", parsed.query, ""))

    def _public_address(self, url: str) -> str:
        hostname = urllib.parse.urlsplit(url).hostname
        assert hostname is not None
        try:
            addresses = tuple(sorted(set(self.dns_resolver(hostname))))
        except (OSError, socket.gaierror):
            raise RemoteFetchError("remote hostname did not resolve") from None
        if not addresses:
            raise RemoteFetchError("remote hostname did not resolve")
        try:
            parsed = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError:
            raise RemoteFetchError("remote hostname resolved to an invalid address") from None
        if any(not address.is_global for address in parsed):
            raise RemoteFetchError("remote hostname resolved to a non-public address")
        selected = min(parsed, key=lambda address: (address.version, address.packed))
        return str(selected)

    @staticmethod
    def _resolve_all(hostname: str) -> tuple[str, ...]:
        return tuple(
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        )

    @staticmethod
    def _media_type(header_type: str, url: str, content: bytes) -> str:
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"PK\x03\x04"):
            guessed = mimetypes.guess_type(urllib.parse.urlsplit(url).path)[0]
            return guessed or "application/zip"
        if header_type and header_type != "application/octet-stream":
            return header_type.casefold()
        return (
            mimetypes.guess_type(urllib.parse.urlsplit(url).path)[0] or "application/octet-stream"
        )


class LicenseGatedAcquirer:
    version = "license-gated-acquirer/1"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        fetcher: RemoteDocumentFetcher | None = None,
        parser: ParsedDocumentManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.fetcher = fetcher or PinnedHttpsFetcher()
        self.parser = parser or ParsedDocumentManager(store=store, clock=clock)
        self.clock = clock

    def acquire(self, resolution: OpenAccessResolution) -> RemoteAcquisitionReceipt:
        eligible = sorted(
            (
                location
                for location in resolution.locations
                if location.automatic_acquisition_eligible
            ),
            key=lambda item: (
                not item.is_best,
                item.host_type != "repository",
                item.pdf_url is None,
                item.url,
            ),
        )
        if not eligible:
            observed_at = self.clock()
            fields = {
                "target_id": resolution.id,
                "locator": resolution.canonical_locator,
                "connector_id": "connector:license-gated-http",
                "observed_at": observed_at,
            }
            constraint = AccessConstraint(
                id=identified("access-constraint", fields),
                target_id=resolution.id,
                locator=resolution.canonical_locator,
                reason=AccessConstraintReason.LICENSING_UNCERTAIN,
                observed_at=observed_at,
                connector_id="connector:license-gated-http",
                lawful_alternatives=tuple(item.url for item in resolution.locations),
                human_resolvable=True,
                detail="No manifestation has a deterministically permitted license",
            )
            digest = self.store.put_record("access-constraint", constraint)
            return RemoteAcquisitionReceipt(
                access_constraint=constraint,
                record_hashes={"access-constraint": (digest,)},
            )
        selected = None
        target = ""
        fetched = None
        failed_routes = 0
        for candidate in eligible:
            candidate_target = candidate.pdf_url or candidate.url
            try:
                candidate_fetched = self.fetcher.fetch(candidate_target)
            except RemoteFetchError:
                failed_routes += 1
                continue
            selected = candidate
            target = candidate_target
            fetched = candidate_fetched
            break
        if selected is None or fetched is None:
            observed_at = self.clock()
            fields = {
                "target_id": resolution.id,
                "locator": resolution.canonical_locator,
                "connector_id": "connector:license-gated-http",
                "observed_at": observed_at,
                "failed_routes": failed_routes,
            }
            constraint = AccessConstraint(
                id=identified("access-constraint", fields),
                target_id=resolution.id,
                locator=resolution.canonical_locator,
                reason=AccessConstraintReason.DENIED,
                observed_at=observed_at,
                connector_id="connector:license-gated-http",
                lawful_alternatives=tuple(item.url for item in eligible),
                human_resolvable=True,
                detail="Every deterministically licensed acquisition route failed",
            )
            digest = self.store.put_record("access-constraint", constraint)
            return RemoteAcquisitionReceipt(
                access_constraint=constraint,
                record_hashes={"access-constraint": (digest,)},
            )
        attempted_at = self.clock()
        try:
            parsed = self.parser.ingest(
                fetched.content,
                source_uri=fetched.final_url,
                media_type=fetched.media_type,
                connector_id="connector:license-gated-http",
                license=selected.license,
            )
        except ParserError:
            original = SourceVersion.from_bytes(
                source_uri=fetched.final_url,
                content=fetched.content,
                media_type=fetched.media_type,
                connector_id="connector:license-gated-http",
                license=selected.license,
                acquired_at=attempted_at,
                trust_zone="quarantined",
            )
            fields = {
                "target_id": original.id,
                "locator": fetched.final_url,
                "connector_id": "connector:license-gated-http",
                "observed_at": attempted_at,
            }
            constraint = AccessConstraint(
                id=identified("access-constraint", fields),
                target_id=original.id,
                locator=fetched.final_url,
                reason=AccessConstraintReason.UNSUPPORTED_MEDIA_TYPE,
                observed_at=attempted_at,
                connector_id="connector:license-gated-http",
                lawful_alternatives=(resolution.canonical_locator,),
                human_resolvable=True,
                detail=(
                    "Original bytes were quarantined; no deterministic text parser "
                    "is registered for the media type"
                ),
            )
            attempt_fields = {
                "explicit_locator": target,
                "resolved_locator": fetched.final_url,
                "content_sha256": original.content_sha256,
                "attempted_at": attempted_at,
            }
            attempt = AcquisitionAttempt(
                id=identified("acquisition-attempt", attempt_fields),
                explicit_locator=target,
                connector_id="connector:license-gated-http",
                resolved_locator=fetched.final_url,
                redirect_chain=fetched.redirect_chain,
                outcome="original_quarantined_parser_unavailable",
                state=AcquisitionState.CONTENT_ACQUIRED,
                attempted_at=attempted_at,
                content_length=len(fetched.content),
                media_type=fetched.media_type,
                content_sha256=original.content_sha256,
                licensing_outcome=f"permitted:{selected.license}",
                policy_outcome="allow-original-only:parser-unavailable",
            )
            constraint_hash = self.store.put_record("access-constraint", constraint)
            attempt_hash = self.store.put_record("acquisition-attempt", attempt)
            return RemoteAcquisitionReceipt(
                acquisition_attempt=attempt,
                access_constraint=constraint,
                selected_location=selected,
                record_hashes={
                    "acquisition-attempt": (attempt_hash,),
                    "access-constraint": (constraint_hash,),
                },
            )
        fields = {
            "explicit_locator": target,
            "resolved_locator": fetched.final_url,
            "content_sha256": hashlib.sha256(fetched.content).hexdigest(),
            "attempted_at": attempted_at,
        }
        attempt = AcquisitionAttempt(
            id=identified("acquisition-attempt", fields),
            explicit_locator=target,
            connector_id="connector:license-gated-http",
            resolved_locator=fetched.final_url,
            redirect_chain=fetched.redirect_chain,
            outcome="parsed_to_quarantined_text",
            state=AcquisitionState.PARSED,
            attempted_at=attempted_at,
            content_length=len(fetched.content),
            media_type=fetched.media_type,
            content_sha256=hashlib.sha256(fetched.content).hexdigest(),
            licensing_outcome=f"permitted:{selected.license}",
            policy_outcome="allow:explicit-permissive-license",
        )
        digest = self.store.put_record("acquisition-attempt", attempt)
        return RemoteAcquisitionReceipt(
            acquisition_attempt=attempt,
            parsed_ingest=parsed,
            selected_location=selected,
            record_hashes={
                **parsed.record_hashes,
                "acquisition-attempt": (digest,),
            },
        )
