"""Deterministic, bounded enumeration adapters for checked-in web sources."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree

from research_agent.capabilities import Capability, CapabilityEvaluator, CapabilityRequest
from research_agent.models import utc_now
from research_agent.remote_acquisition import (
    ConditionalHttpsTransport,
    SourceFetchRequest,
    SourceFetchResult,
    SourceValidator,
)
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAuthorizationError,
    SourceCandidate,
    SourceIntent,
    authorize_candidate,
)
from research_agent.source_work import SourceCheckpoint, SourceOperationError, SourceWorkPhase


class SourceEnumerationError(ValueError):
    pass


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value:
                self.links.append(value)


class _BaseAdapter:
    adapter_id = "source:web"
    version = "1"
    max_fetch_requests = 4
    max_discovery_requests = 4
    discovery_kind: DiscoveryKind

    def __init__(
        self,
        *,
        transport: ConditionalHttpsTransport,
        clock: Callable[[], datetime] = utc_now,
        capability_evaluator: CapabilityEvaluator | None = None,
        capability_request: Callable[[SourceIntent, str, Capability], CapabilityRequest]
        | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.capability_evaluator = capability_evaluator
        self.capability_request = capability_request
        self._intents: dict[str, SourceIntent] = {}
        self.last_fetch: dict[str, SourceFetchResult] = {}
        self.last_discovery_request_count = 0

    def _check_kind(self, intent: SourceIntent) -> None:
        if intent.discovery.kind is not self.discovery_kind:
            raise SourceEnumerationError(
                f"{self.adapter_id} does not support {intent.discovery.kind.value} discovery"
            )

    def _request(self, intent: SourceIntent, locator: str | None = None) -> SourceFetchRequest:
        target = locator or intent.discovery.locator
        return SourceFetchRequest(
            locator=target,
            allowed_hosts=intent.allowed_hosts,
            allowed_path_prefixes=intent.allowed_path_prefixes,
            accepted_media_types=(),
            capability_request=self._capability_request(intent, target, Capability.SOURCE_FETCH),
        )

    def _capability_request(
        self, intent: SourceIntent, locator: str, capability: Capability
    ) -> CapabilityRequest:
        if self.capability_evaluator is None:
            raise PermissionError("source adapter requires a capability evaluator")
        if self.capability_request is None:
            raise SourceAuthorizationError("source adapter lacks a capability request factory")
        return self.capability_request(intent, locator, capability)

    def _require_capability(
        self, intent: SourceIntent, locator: str, capability: Capability
    ) -> None:
        request = self._capability_request(intent, locator, capability)
        assert self.capability_evaluator is not None
        decision = self.capability_evaluator.evaluate(request)
        if decision.decision != "allow" or capability not in decision.effective_capabilities:
            raise SourceAuthorizationError(f"source adapter lacks {capability.value} authority")

    def _materialize(
        self,
        intent: SourceIntent,
        links: Iterable[str],
        *,
        discovered_at: datetime,
        edge_depth: int,
    ) -> tuple[SourceCandidate, ...]:
        candidates: dict[str, SourceCandidate] = {}
        for link in links:
            try:
                candidate = SourceCandidate(
                    intent_id=intent.id,
                    locator=urljoin(intent.discovery.locator, link),
                    discovered_at=discovered_at,
                )
                authorized = authorize_candidate(candidate, intent)
                if not _within_depth(intent, edge_depth):
                    continue
                candidates.setdefault(authorized.locator, authorized)
            except (SourceAuthorizationError, ValueError):
                # Enumeration data is untrusted. Out-of-scope and malformed links
                # are ignored; they never gain an outbound request.
                continue
        return tuple(
            candidates[key]
            for key in sorted(candidates, key=lambda locator: locator.encode("utf-8"))
        )[: intent.refresh.max_items]

    def fetch(
        self, candidate: SourceCandidate, *, prior: SourceCheckpoint | None
    ) -> SourceCheckpoint:
        try:
            intent = self._intents[candidate.intent_id]
        except KeyError:
            raise SourceAuthorizationError("candidate was not emitted by this adapter") from None
        authorize_candidate(candidate, intent)
        self._require_capability(intent, candidate.locator, Capability.SOURCE_FETCH)
        result = self._transport_fetch(
            SourceFetchRequest(
                locator=candidate.locator,
                allowed_hosts=intent.allowed_hosts,
                allowed_path_prefixes=intent.allowed_path_prefixes,
                accepted_media_types=intent.accepted_media_types,
                capability_request=self._capability_request(
                    intent, candidate.locator, Capability.SOURCE_FETCH
                ),
            ),
            prior=(
                SourceValidator(etag=prior.etag, last_modified=prior.last_modified)
                if prior is not None
                and prior.prior_source_version_id is not None
                and prior.prior_source_record_sha256 is not None
                and (prior.etag or prior.last_modified)
                else None
            ),
        )
        self.last_fetch[candidate.id] = result
        phase = (
            SourceWorkPhase.NOT_MODIFIED
            if result.status == 304
            else SourceWorkPhase.ACCESS_CONSTRAINED
            if result.constraint is not None
            else SourceWorkPhase.FETCHED
        )
        return SourceCheckpoint(
            work_item_id=candidate.id,
            phase=phase,
            result_sha256=hashlib.sha256(result.content).hexdigest() if result.content else None,
            etag=result.validator.etag,
            last_modified=result.validator.last_modified,
            constraint=result.constraint.value if result.constraint is not None else None,
            retry_after=result.retry_after,
            request_count=1 + len(result.redirect_chain),
            recorded_at=self.clock(),
        )

    def _transport_fetch(
        self,
        request: SourceFetchRequest,
        *,
        prior: SourceValidator | None = None,
    ) -> SourceFetchResult:
        try:
            return self.transport.fetch(request, prior=prior)
        except Exception as error:
            attempted = int(getattr(error, "request_count", 0))
            raise SourceOperationError(
                "source transport failed", request_count=attempted
            ) from error


class DirectUrlAdapter(_BaseAdapter):
    adapter_id = "source:direct-url"
    discovery_kind = DiscoveryKind.DIRECT_URL
    max_discovery_requests = 0

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self._check_kind(intent)
        self._intents[intent.id] = intent
        self.last_discovery_request_count = 0
        # This intentionally performs no I/O. Fetch remains separately authorized.
        return self._materialize(
            intent,
            (intent.discovery.locator,),
            discovered_at=self.clock(),
            edge_depth=0,
        )


class FeedAdapter(_BaseAdapter):
    adapter_id = "source:feed"
    discovery_kind = DiscoveryKind.RSS_ATOM

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self._check_kind(intent)
        discovered_at = self.clock()
        self._require_capability(intent, intent.discovery.locator, Capability.SOURCE_DISCOVER)
        result = self._transport_fetch(self._request(intent))
        self.last_discovery_request_count = 1 + len(result.redirect_chain)
        if result.status != 200 or result.constraint is not None:
            return ()
        self._intents[intent.id] = intent
        return self._materialize(
            intent,
            _feed_links(result.content),
            discovered_at=discovered_at,
            edge_depth=1,
        )


class SitemapAdapter(_BaseAdapter):
    adapter_id = "source:sitemap"
    discovery_kind = DiscoveryKind.SITEMAP

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self._check_kind(intent)
        discovered_at = self.clock()
        self._require_capability(intent, intent.discovery.locator, Capability.SOURCE_DISCOVER)
        result = self._transport_fetch(self._request(intent))
        self.last_discovery_request_count = 1 + len(result.redirect_chain)
        if result.status != 200 or result.constraint is not None:
            return ()
        self._intents[intent.id] = intent
        return self._materialize(
            intent,
            _sitemap_links(result.content),
            discovered_at=discovered_at,
            edge_depth=1,
        )


class HtmlDiscoveryAdapter(_BaseAdapter):
    adapter_id = "source:https-html"
    discovery_kind = DiscoveryKind.HTTPS_HTML

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self._check_kind(intent)
        discovered_at = self.clock()
        self._require_capability(intent, intent.discovery.locator, Capability.SOURCE_DISCOVER)
        result = self._transport_fetch(self._request(intent))
        self.last_discovery_request_count = 1 + len(result.redirect_chain)
        if result.status != 200 or result.constraint is not None:
            return ()
        self._intents[intent.id] = intent
        return self._materialize(
            intent,
            _html_links(result.content),
            discovered_at=discovered_at,
            edge_depth=1,
        )


class MojeekSourceAdapter(_BaseAdapter):
    """Adapt pre-authorized Mojeek result locators; no result body is persisted."""

    adapter_id = "source:mojeek"
    discovery_kind = DiscoveryKind.MOJEEK
    max_discovery_requests = 1

    def __init__(
        self,
        *,
        search: Callable[[SourceIntent], Iterable[str]],
        transport: ConditionalHttpsTransport,
        clock: Callable[[], datetime] = utc_now,
        capability_evaluator: CapabilityEvaluator | None = None,
        capability_request: Callable[[SourceIntent, str, Capability], CapabilityRequest]
        | None = None,
    ) -> None:
        super().__init__(
            transport=transport,
            clock=clock,
            capability_evaluator=capability_evaluator,
            capability_request=capability_request,
        )
        self.search = search

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self._check_kind(intent)
        discovered_at = self.clock()
        self._require_capability(intent, intent.discovery.locator, Capability.SOURCE_DISCOVER)
        self._intents[intent.id] = intent
        self.last_discovery_request_count = 1
        try:
            links = self.search(intent)
            candidates = self._materialize(
                intent,
                links,
                discovered_at=discovered_at,
                edge_depth=1,
            )
        except Exception as error:
            attempted = int(
                getattr(error, "request_count", self.last_discovery_request_count)
            )
            self.last_discovery_request_count = attempted
            raise SourceOperationError(
                "source discovery failed",
                request_count=attempted,
            ) from error
        self.last_discovery_request_count = int(getattr(self.search, "last_request_count", 1))
        return candidates


def _xml_root(content: bytes) -> ElementTree.Element:
    text = _decode_xml(content)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise SourceEnumerationError("XML discovery documents may not declare entities")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise SourceEnumerationError("invalid XML discovery document") from error


def _decode_xml(content: bytes) -> str:
    encoding = "utf-8"
    if content.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        return content.decode(encoding)
    except UnicodeDecodeError as error:
        raise SourceEnumerationError("XML discovery document has invalid encoding") from error


def _within_depth(intent: SourceIntent, edge_depth: int) -> bool:
    return edge_depth <= intent.refresh.max_depth


def _feed_links(content: bytes) -> tuple[str, ...]:
    root = _xml_root(content)
    links: list[str] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1].casefold()
        if local != "link":
            continue
        href = element.attrib.get("href")
        if href:
            links.append(href.strip())
        elif element.text and element.text.strip():
            links.append(element.text.strip())
    return tuple(links)


def _sitemap_links(content: bytes) -> tuple[str, ...]:
    root = _xml_root(content)
    return tuple(
        element.text.strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].casefold() == "loc"
        and element.text
        and element.text.strip()
    )


def _html_links(content: bytes) -> tuple[str, ...]:
    parser = _LinkCollector()
    parser.feed(content.decode("utf-8", errors="replace"))
    parser.close()
    return tuple(parser.links)


def json_links(content: bytes) -> tuple[str, ...]:
    """Extract common locator fields from a bounded JSON discovery response."""
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceEnumerationError("invalid JSON discovery document") from error
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.casefold() in {
                    "url",
                    "link",
                    "locator",
                    "download_url",
                } and isinstance(child, str):
                    found.append(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)
