from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakes.automatic_acquisition import FakeCapabilityEvaluator, FakeClock
from pydantic import ValidationError

from research_agent.bootstrap_models import BootstrapPhase, ManagedPath, RepositoryInstallReceipt
from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    CapabilitySubject,
)
from research_agent.publishing import PathRole, PublishMode, PublishPath, PublishRequest
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
)
from research_agent.source_work import SourceWorkItem, SourceWorkPhase

NOW = datetime(2026, 9, 2, tzinfo=UTC)
REPOSITORY = "https://github.com/example/ontology"


def _subject() -> CapabilitySubject:
    return CapabilitySubject(
        repository=REPOSITORY,
        refs=("refs/heads/main",),
        paths=("ontology/example",),
        bundle_sha256=("a" * 64,),
    )


def _work(*, capability_decision_sha256: str) -> SourceWorkItem:
    return SourceWorkItem(
        ontology_bundle_sha256="a" * 64,
        source_intent_id="issuer-news-example",
        source_intent_sha256="b" * 64,
        locator="https://example.com/news/announcement.pdf",
        adapter_id="direct-https",
        adapter_version="1",
        capability_decision_sha256=capability_decision_sha256,
        phase=SourceWorkPhase.CANDIDATE,
        predecessor_id=None,
        created_at=NOW,
    )


def test_capability_grant_rejects_non_delegable_capability() -> None:
    """Dropping the subset check would let a child gain authority."""
    with pytest.raises(ValidationError, match="delegable_capabilities"):
        CapabilityGrant(
            decision="allow",
            subject=_subject(),
            capabilities=(Capability.REPOSITORY_READ,),
            delegable_capabilities=(Capability.SOURCE_FETCH,),
            resources=CapabilityResources(),
            max_delegation_depth=1,
            expires_at=None,
            created_at=NOW,
            created_via="manual",
        )


def test_capability_contract_is_strict_and_serializes_empty_values() -> None:
    """Removing strictness or omitted empty selectors would hide authority scope."""
    grant = CapabilityGrant(
        decision="allow",
        subject=_subject(),
        capabilities=(Capability.REPOSITORY_READ,),
        delegable_capabilities=(),
        resources=CapabilityResources(),
        max_delegation_depth=1,
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    dumped = grant.model_dump(mode="json")
    assert dumped["expires_at"] is None
    assert dumped["delegable_capabilities"] == []
    assert dumped["resources"]["hosts"] == []
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CapabilityResources(unknown_scope=True)  # type: ignore[call-arg]


def test_capability_selectors_normalize_to_sorted_unique_tuples() -> None:
    """Changing selector normalization would make equivalent grants hash differently."""
    subject = CapabilitySubject(
        repository=REPOSITORY,
        refs=("refs/heads/z", "refs/heads/a", "refs/heads/z"),
        paths=("z", "a", "z"),
        bundle_sha256=("f" * 64, "a" * 64, "f" * 64),
    )
    resources = CapabilityResources(
        hosts=("z.example", "a.example", "z.example"),
        path_prefixes=("/z/", "/a/", "/z/"),
    )
    assert subject.refs == ("refs/heads/a", "refs/heads/z")
    assert subject.paths == ("a", "z")
    assert subject.bundle_sha256 == ("a" * 64, "f" * 64)
    assert resources.hosts == ("a.example", "z.example")
    assert resources.path_prefixes == ("/a/", "/z/")


@pytest.mark.parametrize(
    ("repository", "ref", "path"),
    [
        ("http://github.com/example/ontology", "refs/heads/main", "ontology/example"),
        ("https://user@github.com/example/ontology", "refs/heads/main", "ontology/example"),
        (REPOSITORY, "main", "ontology/example"),
        (REPOSITORY, "refs/heads/main", "../ontology/example"),
    ],
)
def test_capability_subject_rejects_unsafe_repository_selectors(
    repository: str, ref: str, path: str
) -> None:
    """Relaxing repository selectors could make a grant match an unsafe target."""
    with pytest.raises(ValidationError):
        CapabilitySubject(
            repository=repository,
            refs=(ref,),
            paths=(path,),
            bundle_sha256="a" * 64,
        )


def test_source_intent_rejects_unsupported_version_and_naive_dates() -> None:
    """Removing version/date checks would make intent compatibility ambiguous."""
    with pytest.raises(ValidationError, match="version"):
        SourceIntent(
            version=2,
            id="issuer-news-example",
            role="issuer_news",
            discovery=SourceDiscovery(
                kind=DiscoveryKind.RSS_ATOM,
                locator="https://example.com/feed.xml",
            ),
            allowed_hosts=("example.com",),
            allowed_path_prefixes=("/",),
            accepted_media_types=("application/atom+xml",),
            document_patterns=(),
            refresh=SourceRefreshPolicy(interval_seconds=900, max_items=40, max_depth=1),
            required=True,
            priority=10,
            associations=SourceAssociations(),
            temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
            created_at=datetime(2026, 9, 2),
        )


def test_source_intent_rejects_naive_created_at() -> None:
    """Removing timezone validation would make refresh ordering host-dependent."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceIntent(
            id="issuer-news-example",
            role="issuer_news",
            discovery=SourceDiscovery(
                kind=DiscoveryKind.RSS_ATOM,
                locator="https://example.com/feed.xml",
            ),
            allowed_hosts=("example.com",),
            allowed_path_prefixes=("/",),
            accepted_media_types=("application/atom+xml",),
            document_patterns=(),
            refresh=SourceRefreshPolicy(interval_seconds=900, max_items=40, max_depth=1),
            required=True,
            priority=10,
            associations=SourceAssociations(),
            temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
            created_at=datetime(2026, 9, 2),
        )


def test_source_intent_rejects_unsafe_urls_and_has_canonical_id() -> None:
    """Removing URL checks could send an adapter to an unapproved location."""
    with pytest.raises(ValidationError):
        SourceDiscovery(kind=DiscoveryKind.DIRECT_URL, locator="http://example.com/x")
    left = SourceIntent(
        id="issuer-news-example",
        role="issuer_news",
        discovery=SourceDiscovery(
            kind=DiscoveryKind.DIRECT_URL, locator="https://example.com/news/a"
        ),
        allowed_hosts=("example.com",),
        allowed_path_prefixes=("/news/",),
        accepted_media_types=("text/html",),
        document_patterns=("/news/*.html",),
        refresh=SourceRefreshPolicy(interval_seconds=900, max_items=40, max_depth=1),
        required=True,
        priority=10,
        associations=SourceAssociations(concepts=("concept:example",)),
        temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
        created_at=NOW,
    )
    right = left.model_copy(update={"allowed_hosts": ("example.com",)})
    assert left.canonical_id == right.canonical_id
    assert left.canonical_id.startswith("source-intent:sha256:")


def test_source_work_identity_changes_with_authority_receipt() -> None:
    """Omitting the decision receipt would reuse work authorized under different scope."""
    left = _work(capability_decision_sha256="1" * 64)
    right = _work(capability_decision_sha256="2" * 64)
    assert left.id != right.id


def test_source_work_rejects_phase_regression() -> None:
    """Allowing a phase regression would make immutable checkpoints inconsistent."""
    with pytest.raises(ValidationError, match="predecessor"):
        SourceWorkItem(
            ontology_bundle_sha256="a" * 64,
            source_intent_id="issuer-news-example",
            source_intent_sha256="b" * 64,
            locator="https://example.com/news/announcement.pdf",
            adapter_id="direct-https",
            adapter_version="1",
            capability_decision_sha256="1" * 64,
            phase=SourceWorkPhase.AUTHORIZED,
            predecessor_phase=SourceWorkPhase.FETCHED,
            predecessor_id="source-work:sha256:" + "a" * 64,
            created_at=NOW,
        )


def test_bootstrap_receipt_validates_phase_owned_paths_and_canonical_identity() -> None:
    """Changing the owned path or phase must produce a distinct immutable receipt."""
    receipt = RepositoryInstallReceipt(
        repository=REPOSITORY,
        ref="refs/heads/main",
        commit_sha256="a" * 40,
        phase=BootstrapPhase.PLANNED,
        managed_paths=(
            ManagedPath(path=".agents/skills/geas/SKILL.md", sha256="b" * 64, role="skill"),
        ),
        created_at=NOW,
        recovery_command=None,
    )
    assert receipt.id.startswith("repository-install:sha256:")
    with pytest.raises(ValidationError):
        ManagedPath(path="../outside", sha256="b" * 64, role="skill")


def test_publish_request_rejects_unclassified_paths_for_remote_modes() -> None:
    """Allowing an unclassified remote path would stage operator-authored content."""
    with pytest.raises(ValidationError, match="unclassified"):
        PublishRequest(
            repository=REPOSITORY,
            target_ref="refs/heads/main",
            mode=PublishMode.PULL_REQUEST,
            paths=(PublishPath(path="notes.txt", role=PathRole.UNCLASSIFIED),),
            capability_decision_sha256="a" * 64,
            created_at=NOW,
        )


def test_deterministic_fakes_deny_unconfigured_operations_without_wall_time() -> None:
    """Replacing the deny default or clock fixture would hide unintended effects."""
    clock = FakeClock(NOW)
    request = CapabilityRequest(
        authority_repository=REPOSITORY,
        target_repository=REPOSITORY,
        capabilities=(Capability.REPOSITORY_READ,),
        ref="refs/heads/main",
        path="ontology/example",
        requested_at=clock.now(),
    )
    with pytest.raises(PermissionError, match="unconfigured"):
        FakeCapabilityEvaluator().evaluate(request)
    assert clock.now() == NOW
