from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.capabilities import Capability, CapabilityDecision, CapabilityRequest
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceAuthorizationError,
    SourceCandidate,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
    authorize_candidate,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _intent(locator: str = "https://issuer.example/report.pdf") -> SourceIntent:
    return SourceIntent(
        id="issuer-report",
        role="issuer_report",
        discovery=SourceDiscovery(kind=DiscoveryKind.DIRECT_URL, locator=locator),
        allowed_hosts=("issuer.example",),
        allowed_path_prefixes=("/",),
        accepted_media_types=("application/pdf", "text/plain"),
        document_patterns=("/*.pdf",),
        refresh=SourceRefreshPolicy(interval_seconds=60, max_items=10, max_depth=1),
        required=True,
        priority=1,
        associations=SourceAssociations(concepts=("concept:a", "concept:a")),
        temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
        created_at=NOW,
    )


def _candidate(locator: str) -> SourceCandidate:
    return SourceCandidate(intent_id="issuer-report", locator=locator, discovered_at=NOW)


def _host_decision(host: str) -> CapabilityDecision:
    request = CapabilityRequest(
        authority_repository="https://github.com/example/ontology",
        target_repository="https://github.com/example/ontology",
        capabilities=(Capability.SOURCE_FETCH,),
        ref="refs/heads/main",
        path="ontology/example",
        host=host,
        requested_at=NOW,
    )
    return CapabilityDecision(
        request=request,
        decision="allow",
        effective_capabilities=(Capability.SOURCE_FETCH,),
        reason="fixture",
        evaluator_version="fixture/1",
        decided_at=NOW,
    )


def test_authorize_candidate_rejects_a_host_outside_its_intent() -> None:
    """Dropping host scope enforcement would permit an intent to broaden itself."""
    with pytest.raises(SourceAuthorizationError, match="host"):
        authorize_candidate(_candidate("https://other.example/a"), _intent())


def test_intent_cannot_broaden_local_host_scope() -> None:
    """Ignoring a capability's host selector would let intent escape local authority."""
    with pytest.raises(SourceAuthorizationError, match="host"):
        authorize_candidate(_candidate("https://other.example/a"), _host_decision("issuer.example"))


def test_authorize_candidate_accepts_scoped_normalized_candidate() -> None:
    """Rejecting the normalized in-scope locator would prevent safe acquisition."""
    candidate = authorize_candidate(_candidate("https://issuer.example/a.pdf"), _intent())
    assert candidate.locator == "https://issuer.example/a.pdf"


@pytest.mark.parametrize(
    "locator",
    (
        "http://issuer.example/a",
        "https://user:password@issuer.example/a",
        "https://127.0.0.1/a",
        "https://issuer.example/a\n",
    ),
)
def test_candidate_rejects_unsafe_locator(locator: str) -> None:
    """Relaxing locator parsing would expose the transport to unsafe targets."""
    with pytest.raises(ValidationError):
        _candidate(locator)


@pytest.mark.parametrize(
    "locator",
    (
        "https://issuer.example/%2e%2e/private.pdf",
        "https://issuer.example/news%2fprivate.pdf",
        "https://issuer.example/news%5cprivate.pdf",
        "https://issuer.example/news\\private.pdf",
        "https://issuer.example/%252e%252e/private.pdf",
        "https://issuer.example/news%252fprivate.pdf",
    ),
)
def test_candidate_rejects_encoded_or_backslash_path_ambiguity(locator: str) -> None:
    """Accepting an ambiguous path could escape a checked-in path prefix."""
    with pytest.raises(ValidationError):
        _candidate(locator)


def test_percent_normalization_is_idempotent_without_raw_percent() -> None:
    """Leaving a raw percent in canonical output makes a second authorization differ."""
    first = _candidate("https://issuer.example/a%25b.pdf")
    second = _candidate(first.locator)
    assert first.locator == "https://issuer.example/a%25b.pdf"
    assert second.locator == first.locator


@pytest.mark.parametrize(
    ("raw", "canonical"),
    (
        ("https://issuer.example/a%2520b.pdf", "https://issuer.example/a%2520b.pdf"),
        ("https://issuer.example/a%2541b.pdf", "https://issuer.example/a%2541b.pdf"),
        ("https://issuer.example/a%25b.pdf", "https://issuer.example/a%25b.pdf"),
        ("https://issuer.example/a%2ab.pdf", "https://issuer.example/a%2Ab.pdf"),
    ),
)
def test_percent_normalization_preserves_semantic_layers_and_is_idempotent(
    raw: str, canonical: str
) -> None:
    """Canonicalization must not turn a server-visible literal escape into decoded data."""
    first = _candidate(raw)
    second = _candidate(first.locator)

    assert first.locator == canonical
    assert second.locator == canonical


@pytest.mark.parametrize(
    "locator",
    (
        "https://issuer.example/a%",
        "https://issuer.example/a%2",
        "https://issuer.example/a%zz",
        "https://issuer.example/news%252fprivate.pdf",
        "https://issuer.example/news%255cprivate.pdf",
        "https://issuer.example/%252e%252e/private.pdf",
        "https://issuer.example/a%2500b.pdf",
        "https://issuer.example/a%257fb.pdf",
    ),
)
def test_percent_normalization_rejects_malformed_or_eventually_unsafe_paths(
    locator: str,
) -> None:
    """Inspection must follow nested escapes without rewriting their resource semantics."""
    with pytest.raises(ValidationError):
        _candidate(locator)


def test_document_glob_is_segment_safe() -> None:
    """A star in one segment must never authorize a slash-delimited descendant."""
    authority = _intent().model_copy(update={"document_patterns": ("/news/*.pdf",)})
    allowed = _candidate("https://issuer.example/news/report.pdf")
    nested = _candidate("https://issuer.example/news/archive/report.pdf")

    assert authorize_candidate(allowed, authority) == allowed
    with pytest.raises(SourceAuthorizationError, match="document pattern"):
        authorize_candidate(nested, authority)


def test_intent_normalizes_associations_and_rejects_invalid_document_glob() -> None:
    """Allowing unsafe globs makes path selection ambiguous and broadens authority."""
    assert _intent().associations.concepts == ("concept:a",)
    with pytest.raises(ValidationError, match="document pattern"):
        _intent().model_copy(update={"document_patterns": ("../*.pdf",)}).model_validate(
            {**_intent().model_dump(), "document_patterns": ("../*.pdf",)}
        )
