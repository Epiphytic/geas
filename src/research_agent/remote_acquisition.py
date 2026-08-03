from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Protocol

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
from research_agent.store import ImmutableStore


class RemoteFetchError(RuntimeError):
    pass


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
    ) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds

    def fetch(self, url: str) -> FetchedDocument:
        requested = self._validated_url(url)
        current = requested
        redirects: list[str] = []
        for _ in range(self.max_redirects + 1):
            address = self._public_ipv4(current)
            parsed = urllib.parse.urlsplit(current)
            assert parsed.hostname is not None
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
                    f"{parsed.hostname}:443:{address}",
                    "--dump-header",
                    str(headers_path),
                    "--output",
                    str(body_path),
                    "--write-out",
                    "%{http_code}",
                    current,
                ]
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self.timeout_seconds + 5,
                    check=False,
                    close_fds=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
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
        return urllib.parse.urlunsplit(
            ("https", hostname, parsed.path or "/", parsed.query, "")
        )

    @staticmethod
    def _public_ipv4(url: str) -> str:
        hostname = urllib.parse.urlsplit(url).hostname
        assert hostname is not None
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise RemoteFetchError("remote hostname did not resolve") from None
        public = sorted(
            {
                item[4][0]
                for item in addresses
                if isinstance(ipaddress.ip_address(item[4][0]), ipaddress.IPv4Address)
                and ipaddress.ip_address(item[4][0]).is_global
            }
        )
        if not public:
            raise RemoteFetchError("remote hostname has no public IPv4 address")
        return public[0]

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
            mimetypes.guess_type(urllib.parse.urlsplit(url).path)[0]
            or "application/octet-stream"
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
