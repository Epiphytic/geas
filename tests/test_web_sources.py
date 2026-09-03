from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research_agent.capabilities import Capability, CapabilityDecision, CapabilityRequest
from research_agent.remote_acquisition import SourceFetchResult
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
)
from research_agent.web_sources import (
    DirectUrlAdapter,
    FeedAdapter,
    HtmlDiscoveryAdapter,
    SitemapAdapter,
    SourceEnumerationError,
    _feed_links,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)


class AllowEvaluator:
    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        return CapabilityDecision(
            request=request,
            decision="allow",
            effective_capabilities=request.capabilities,
            reason="fixture",
            evaluator_version="fixture/1",
            decided_at=NOW,
        )


def _capability_request(
    intent: SourceIntent, locator: str, capability: Capability
) -> CapabilityRequest:
    return CapabilityRequest(
        authority_repository="https://github.com/example/ontology",
        target_repository="https://github.com/example/ontology",
        capabilities=(capability,),
        ref="refs/heads/main",
        path="ontology/example",
        host="issuer.example",
        target=locator,
        requested_at=NOW,
    )


class FailIfCalledTransport:
    def fetch(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("direct URL discovery must not perform I/O")


def _intent(locator: str) -> SourceIntent:
    return SourceIntent(
        id="issuer-report",
        role="issuer_report",
        discovery=SourceDiscovery(kind=DiscoveryKind.DIRECT_URL, locator=locator),
        allowed_hosts=("issuer.example",),
        allowed_path_prefixes=("/",),
        accepted_media_types=("application/pdf",),
        document_patterns=("/*.pdf",),
        refresh=SourceRefreshPolicy(interval_seconds=60, max_items=10, max_depth=1),
        required=True,
        priority=1,
        associations=SourceAssociations(),
        temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
        created_at=NOW,
    )


class FixtureTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def fetch(self, *_: object, **__: object) -> SourceFetchResult:
        return SourceFetchResult(
            requested_url="https://issuer.example/discovery",
            final_url="https://issuer.example/discovery",
            status=200,
            media_type="application/xml",
            content=self.body,
        )


def _enumeration_intent(kind: DiscoveryKind, locator: str) -> SourceIntent:
    return _intent(locator).model_copy(
        update={"discovery": SourceDiscovery(kind=kind, locator=locator)}
    )


def test_direct_url_materialization_performs_no_network_io() -> None:
    """Fetching during direct materialization would bypass the fetch authority gate."""
    adapter = DirectUrlAdapter(transport=FailIfCalledTransport(), clock=lambda: NOW)
    candidates = adapter.discover(_intent("https://issuer.example/report.pdf"))
    assert [item.locator for item in candidates] == ["https://issuer.example/report.pdf"]


def test_xml_and_html_adapters_deduplicate_sort_and_ignore_unsafe_links() -> None:
    """Keeping untrusted enumeration order or script links would create nondeterministic scope."""
    feed = FeedAdapter(
        transport=FixtureTransport(
            b"<rss><channel><item><link>https://issuer.example/b.pdf</link></item>"
            b"<item><link>https://other.example/a.pdf</link></item>"
            b"<item><link>https://issuer.example/a.pdf</link></item></channel></rss>"
        ),
        clock=lambda: NOW,
        capability_evaluator=AllowEvaluator(),
        capability_request=_capability_request,
    )
    sitemap = SitemapAdapter(
        transport=FixtureTransport(
            b"<urlset><url><loc>https://issuer.example/a.pdf</loc></url></urlset>"
        ),
        clock=lambda: NOW,
        capability_evaluator=AllowEvaluator(),
        capability_request=_capability_request,
    )
    html = HtmlDiscoveryAdapter(
        transport=FixtureTransport(
            b'<a href="/a.pdf">a</a><form action="/b.pdf"></form><script>evil</script>'
        ),
        clock=lambda: NOW,
        capability_evaluator=AllowEvaluator(),
        capability_request=_capability_request,
    )

    feed_candidates = feed.discover(
        _enumeration_intent(DiscoveryKind.RSS_ATOM, "https://issuer.example/feed.xml")
    )
    sitemap_candidates = sitemap.discover(
        _enumeration_intent(DiscoveryKind.SITEMAP, "https://issuer.example/sitemap.xml")
    )
    html_candidates = html.discover(
        _enumeration_intent(DiscoveryKind.HTTPS_HTML, "https://issuer.example/news/")
    )

    assert [candidate.locator for candidate in feed_candidates] == [
        "https://issuer.example/a.pdf",
        "https://issuer.example/b.pdf",
    ]
    assert [candidate.locator for candidate in sitemap_candidates] == [
        "https://issuer.example/a.pdf"
    ]
    assert [candidate.locator for candidate in html_candidates] == ["https://issuer.example/a.pdf"]


def test_network_enumeration_without_an_evaluator_is_denied_before_io() -> None:
    """An optional evaluator would let an unapproved feed initiate a network request."""
    transport = FixtureTransport(b"<rss><channel /></rss>")
    adapter = FeedAdapter(transport=transport, clock=lambda: NOW)

    with pytest.raises(PermissionError, match="capability"):
        adapter.discover(
            _enumeration_intent(DiscoveryKind.RSS_ATOM, "https://issuer.example/feed.xml")
        )


def test_utf16_xml_entities_are_rejected_before_parsing() -> None:
    """Byte-only DTD checks miss hostile UTF-16 entity declarations."""
    payload = '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]><rss />'.encode("utf-16")
    with pytest.raises(SourceEnumerationError, match="entities"):
        _feed_links(payload)


def test_direct_url_is_depth_zero_but_feed_children_consume_one_edge() -> None:
    """Counting absolute path segments would drop a direct source at max depth zero."""
    direct = _intent("https://issuer.example/news/a.pdf").model_copy(
        update={
            "document_patterns": ("/news/*.pdf",),
            "refresh": SourceRefreshPolicy(interval_seconds=60, max_items=1, max_depth=0),
        }
    )
    feed = _enumeration_intent(
        DiscoveryKind.RSS_ATOM, "https://issuer.example/news/feed.xml"
    ).model_copy(
        update={"refresh": SourceRefreshPolicy(interval_seconds=60, max_items=1, max_depth=0)}
    )
    adapter = DirectUrlAdapter(transport=FailIfCalledTransport(), clock=lambda: NOW)
    feed_adapter = FeedAdapter(
        transport=FixtureTransport(b"<rss><item><link>/news/a.pdf</link></item></rss>"),
        clock=lambda: NOW,
        capability_evaluator=AllowEvaluator(),
        capability_request=_capability_request,
    )

    assert [candidate.locator for candidate in adapter.discover(direct)] == [
        "https://issuer.example/news/a.pdf"
    ]
    assert feed_adapter.discover(feed) == ()
