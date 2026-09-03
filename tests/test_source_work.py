from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityRequest,
)
from research_agent.models import canonical_json
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceAuthorizationError,
    SourceCandidate,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
)
from research_agent.source_work import (
    FetchedSourcePayload,
    ImmutableSourceWorkStore,
    SourceCheckpoint,
    SourceWorkCoordinator,
    SourceWorkInterruption,
    SourceWorkItem,
    SourceWorkLimits,
    SourceWorkPhase,
)
from research_agent.store import ImmutableStore

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
REPOSITORY = "https://github.com/example/ontology"
BUNDLE = "a" * 64


def _intent(
    identifier: str = "issuer-news",
    *,
    required: bool = True,
    priority: int = 10,
    interval_seconds: int = 900,
) -> SourceIntent:
    return SourceIntent(
        id=identifier,
        role="issuer_news",
        discovery=SourceDiscovery(
            kind=DiscoveryKind.DIRECT_URL,
            locator=f"https://issuer.example/news/{identifier}.txt",
        ),
        allowed_hosts=("issuer.example",),
        allowed_path_prefixes=("/news/",),
        accepted_media_types=("text/plain",),
        refresh=SourceRefreshPolicy(
            interval_seconds=interval_seconds,
            max_items=4,
            max_depth=0,
        ),
        required=required,
        priority=priority,
        associations=SourceAssociations(topics=("issuer",)),
        temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
        created_at=NOW,
    )


def _request(
    intent: SourceIntent,
    candidate: SourceCandidate,
    capabilities: tuple[Capability, ...],
    now: datetime,
) -> CapabilityRequest:
    return CapabilityRequest(
        authority_repository=REPOSITORY,
        target_repository=REPOSITORY,
        capabilities=capabilities,
        ref="refs/heads/main",
        path="ontology/example",
        bundle_sha256=BUNDLE,
        connector="source:test",
        host=urlsplit(candidate.locator).hostname,
        target=candidate.locator,
        requested_at=now,
    )


class _AllowEvaluator:
    def __init__(self, denied: frozenset[Capability] = frozenset()) -> None:
        self.denied = denied
        self.requests: list[CapabilityRequest] = []

    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        self.requests.append(request)
        allowed = not bool(set(request.capabilities) & self.denied)
        return CapabilityDecision(
            request=request,
            decision="allow" if allowed else "deny",
            effective_capabilities=request.capabilities if allowed else (),
            reason="fixture decision",
            evaluator_version="fixture/1",
            decided_at=request.requested_at,
        )


class _Adapter:
    adapter_id = "source:test"
    version = "1"

    def __init__(
        self,
        *,
        content: bytes = b"# Issuer update\n\nProduction increased.\n",
        phase: SourceWorkPhase = SourceWorkPhase.FETCHED,
    ) -> None:
        self.content = content
        self.phase = phase
        self.discovery_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.payload_calls: list[str] = []

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self.discovery_calls.append(intent.id)
        return (
            SourceCandidate(
                intent_id=intent.id,
                locator=intent.discovery.locator,
                media_type="text/plain",
                discovered_at=NOW,
            ),
        )

    def fetch(
        self,
        candidate: SourceCandidate,
        *,
        prior: SourceCheckpoint | None,
    ) -> SourceCheckpoint:
        del prior
        self.fetch_calls.append(candidate.id)
        return SourceCheckpoint(
            work_item_id=candidate.id,
            phase=self.phase,
            result_sha256=(
                hashlib.sha256(self.content).hexdigest()
                if self.phase is SourceWorkPhase.FETCHED
                else None
            ),
            recorded_at=NOW,
        )

    def payload(
        self,
        candidate: SourceCandidate,
        checkpoint: SourceCheckpoint,
    ) -> FetchedSourcePayload:
        del checkpoint
        self.payload_calls.append(candidate.id)
        return FetchedSourcePayload(
            content=self.content,
            source_uri=candidate.locator,
            media_type="text/plain",
            connector_id="connector:test",
            license="CC-BY-4.0",
            observed_at=NOW,
            published_at=NOW - timedelta(hours=1),
            valid_at=NOW - timedelta(hours=1),
        )


def _coordinator(
    root: Path,
    *,
    adapter: _Adapter | None = None,
    evaluator: _AllowEvaluator | None = None,
    after_phase=None,
    now: datetime = NOW,
    limits: SourceWorkLimits | None = None,
    extractor: object | None = None,
    external_model: bool = False,
) -> SourceWorkCoordinator:
    immutable = ImmutableStore(root)
    return SourceWorkCoordinator(
        store=immutable,
        work_store=ImmutableSourceWorkStore(immutable),
        adapter=adapter or _Adapter(),
        capability_evaluator=evaluator or _AllowEvaluator(),
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        clock=lambda: now,
        monotonic=lambda: 0.0,
        limits=limits or SourceWorkLimits(),
        after_phase=after_phase,
        extractor=extractor,
        external_model=external_model,
    )


def _item(
    phase: SourceWorkPhase,
    *,
    predecessor: SourceWorkItem | None = None,
    capability_digest: str = "b" * 64,
    parser_version: str = "1",
    validator_version: str = "anchor-grounded-extraction-validator/1",
    bundle_digest: str = BUNDLE,
    adapter_version: str = "1",
) -> SourceWorkItem:
    return SourceWorkItem(
        ontology_bundle_sha256=bundle_digest,
        source_intent_id="issuer-news",
        source_intent_sha256="c" * 64,
        locator="https://issuer.example/news/a.txt",
        adapter_id="source:test",
        adapter_version=adapter_version,
        parser_id="document-parser-registry",
        parser_version=parser_version,
        extraction_validator_version=validator_version,
        capability_decision_sha256=capability_digest,
        phase=phase,
        predecessor_id=predecessor.id if predecessor else None,
        predecessor_phase=predecessor.phase if predecessor else None,
        created_at=NOW,
    )


def test_store_rejects_a_successor_whose_predecessor_is_not_current(tmp_path: Path) -> None:
    """Accepting a stale predecessor would fork one mutable checkpoint lineage."""
    work = ImmutableSourceWorkStore(ImmutableStore(tmp_path))
    candidate = work.put(_item(SourceWorkPhase.CANDIDATE))
    authorized = work.put(_item(SourceWorkPhase.AUTHORIZED, predecessor=candidate))
    with pytest.raises(ValueError, match="current predecessor"):
        work.put(_item(SourceWorkPhase.AUTHORIZED, predecessor=candidate))
    assert work.current(candidate.lineage_id) == authorized


def test_store_atomically_replaces_only_the_current_index(tmp_path: Path) -> None:
    """Overwriting immutable records instead of the locator index loses audit history."""
    work = ImmutableSourceWorkStore(ImmutableStore(tmp_path))
    candidate = work.put(_item(SourceWorkPhase.CANDIDATE))
    authorized = work.put(_item(SourceWorkPhase.AUTHORIZED, predecessor=candidate))
    index = json.loads((tmp_path / "source-work-current.json").read_bytes())
    assert index == {candidate.lineage_id: authorized.id}
    assert len(tuple((tmp_path / "records" / "source-work").glob("*/*.json"))) == 2
    assert not tuple(tmp_path.glob(".source-work-current.*.tmp"))


def test_incompatible_identity_starts_an_immutable_successor_lineage(tmp_path: Path) -> None:
    """Reusing parser/authority/bundle-incompatible work would bless stale derivations."""
    work = ImmutableSourceWorkStore(ImmutableStore(tmp_path))
    original = work.put(_item(SourceWorkPhase.CANDIDATE))
    changed = work.put(
        _item(
            SourceWorkPhase.CANDIDATE,
            capability_digest="d" * 64,
            parser_version="2",
        )
    )
    assert original.id != changed.id
    assert original.lineage_id != changed.lineage_id
    assert work.get(original.id) == original
    assert work.get(changed.id) == changed


@pytest.mark.parametrize(
    ("predecessor", "successor"),
    [
        (SourceWorkPhase.CANDIDATE, SourceWorkPhase.AUTHORIZED),
        (SourceWorkPhase.AUTHORIZED, SourceWorkPhase.FETCHED),
        (SourceWorkPhase.AUTHORIZED, SourceWorkPhase.NOT_MODIFIED),
        (SourceWorkPhase.AUTHORIZED, SourceWorkPhase.ACCESS_CONSTRAINED),
        (SourceWorkPhase.FETCHED, SourceWorkPhase.ACCESS_CONSTRAINED),
        (SourceWorkPhase.FETCHED, SourceWorkPhase.ARCHIVED),
        (SourceWorkPhase.ARCHIVED, SourceWorkPhase.PARSED),
        (SourceWorkPhase.ARCHIVED, SourceWorkPhase.PARSER_CONSTRAINED),
        (SourceWorkPhase.PARSED, SourceWorkPhase.STRUCTURED),
        (SourceWorkPhase.STRUCTURED, SourceWorkPhase.INDEXED),
        (SourceWorkPhase.INDEXED, SourceWorkPhase.ANCHORS_SELECTED),
        (SourceWorkPhase.ANCHORS_SELECTED, SourceWorkPhase.EXTRACTION_PROPOSED),
        (SourceWorkPhase.ANCHORS_SELECTED, SourceWorkPhase.EXTRACTION_CONSTRAINED),
        (SourceWorkPhase.NOT_MODIFIED, SourceWorkPhase.FINALIZED),
        (SourceWorkPhase.ACCESS_CONSTRAINED, SourceWorkPhase.FINALIZED),
        (SourceWorkPhase.PARSER_CONSTRAINED, SourceWorkPhase.FINALIZED),
        (SourceWorkPhase.EXTRACTION_PROPOSED, SourceWorkPhase.FINALIZED),
        (SourceWorkPhase.EXTRACTION_CONSTRAINED, SourceWorkPhase.FINALIZED),
    ],
)
def test_every_declared_source_work_transition_is_legal(
    predecessor: SourceWorkPhase,
    successor: SourceWorkPhase,
) -> None:
    prior = SimpleNamespace(id="source-work:test", phase=predecessor)
    SourceWorkItem.model_validate(
        _item(SourceWorkPhase.CANDIDATE)
        .model_copy(
            update={
                "phase": successor,
                "predecessor_id": prior.id,
                "predecessor_phase": predecessor,
            }
        )
        .model_dump()
    )


def test_illegal_source_work_jump_is_rejected() -> None:
    with pytest.raises(ValueError, match="predecessor phase"):
        SourceWorkItem.model_validate(
            _item(SourceWorkPhase.CANDIDATE)
            .model_copy(
                update={
                    "phase": SourceWorkPhase.FINALIZED,
                    "predecessor_id": "source-work:test",
                    "predecessor_phase": SourceWorkPhase.CANDIDATE,
                }
            )
            .model_dump()
        )


def test_work_lineage_binds_every_compatibility_contract() -> None:
    original = _item(SourceWorkPhase.CANDIDATE)
    variants = (
        _item(SourceWorkPhase.CANDIDATE, capability_digest="d" * 64),
        _item(SourceWorkPhase.CANDIDATE, parser_version="2"),
        _item(SourceWorkPhase.CANDIDATE, validator_version="validator/2"),
        _item(SourceWorkPhase.CANDIDATE, bundle_digest="e" * 64),
        _item(SourceWorkPhase.CANDIDATE, adapter_version="2"),
    )
    assert all(item.lineage_id != original.lineage_id for item in variants)


def test_resume_reuses_completed_fetch_after_interruption(tmp_path: Path) -> None:
    """Fetching again after archive would duplicate external effects on resume."""

    def interrupt(phase: SourceWorkPhase) -> None:
        if phase is SourceWorkPhase.ARCHIVED:
            raise SourceWorkInterruption("fixture interruption")

    first_adapter = _Adapter()
    first = _coordinator(tmp_path, adapter=first_adapter, after_phase=interrupt)
    with pytest.raises(SourceWorkInterruption, match="fixture"):
        first.run_due((_intent(),), now=NOW)
    assert len(first_adapter.fetch_calls) == 1

    second_adapter = _Adapter()
    second = _coordinator(tmp_path, adapter=second_adapter)
    receipt = second.run_due((_intent(),), now=NOW)
    assert second_adapter.fetch_calls == []
    assert receipt.completed_phases[-1] is SourceWorkPhase.FINALIZED
    assert receipt.complete is True


@pytest.mark.parametrize(
    "interrupted_phase",
    (
        SourceWorkPhase.PARSED,
        SourceWorkPhase.STRUCTURED,
        SourceWorkPhase.INDEXED,
        SourceWorkPhase.ANCHORS_SELECTED,
    ),
)
def test_resume_preserves_parsed_identity_and_exact_anchors(
    tmp_path: Path,
    interrupted_phase: SourceWorkPhase,
) -> None:
    def interrupt(phase: SourceWorkPhase) -> None:
        if phase is interrupted_phase:
            raise SourceWorkInterruption("fixture interruption")

    with pytest.raises(SourceWorkInterruption, match="fixture"):
        _coordinator(tmp_path, after_phase=interrupt).run_due((_intent(),), now=NOW)

    extractor = _Extractor(ImmutableStore(tmp_path))
    adapter = _Adapter()
    receipt = _coordinator(tmp_path, adapter=adapter, extractor=extractor).run_due(
        (_intent(),), now=NOW
    )

    assert adapter.fetch_calls == []
    assert extractor.calls[0]["anchor_ids"]
    assert receipt.completed_phases[-1] is SourceWorkPhase.FINALIZED


def test_not_modified_records_refresh_without_a_new_blob(tmp_path: Path) -> None:
    """Treating 304 as content would invent a new immutable source version."""
    adapter = _Adapter(phase=SourceWorkPhase.NOT_MODIFIED)
    receipt = _coordinator(tmp_path, adapter=adapter).run_due((_intent(),), now=NOW)
    assert receipt.complete is True
    assert receipt.completed_phases == (
        SourceWorkPhase.CANDIDATE,
        SourceWorkPhase.AUTHORIZED,
        SourceWorkPhase.NOT_MODIFIED,
        SourceWorkPhase.FINALIZED,
    )
    assert tuple((tmp_path / "blobs" / "sha256").glob("*/*")) == ()
    observations = tuple(ImmutableStore(tmp_path).iter_records("source-refresh-observation"))
    assert len(observations) == 1
    assert observations[0]["status"] == "not_modified"


def test_fresh_coordinator_restores_conditional_validators_from_checkpoint(
    tmp_path: Path,
) -> None:
    class ValidatorAdapter(_Adapter):
        def __init__(self, *, not_modified: bool = False) -> None:
            super().__init__()
            self.not_modified = not_modified
            self.priors: list[SourceCheckpoint | None] = []

        def fetch(
            self,
            candidate: SourceCandidate,
            *,
            prior: SourceCheckpoint | None,
        ) -> SourceCheckpoint:
            self.fetch_calls.append(candidate.id)
            self.priors.append(prior)
            return SourceCheckpoint(
                work_item_id=candidate.id,
                phase=(
                    SourceWorkPhase.NOT_MODIFIED
                    if self.not_modified
                    else SourceWorkPhase.FETCHED
                ),
                result_sha256=(
                    None
                    if self.not_modified
                    else hashlib.sha256(self.content).hexdigest()
                ),
                etag='"version-1"',
                last_modified="Thu, 03 Sep 2026 12:00:00 GMT",
                recorded_at=NOW,
            )

    first = ValidatorAdapter()
    _coordinator(tmp_path, adapter=first).run_due(
        (_intent(interval_seconds=1),), now=NOW
    )
    second = ValidatorAdapter(not_modified=True)
    _coordinator(tmp_path, adapter=second).run_due(
        (_intent(interval_seconds=1),), now=NOW + timedelta(seconds=1)
    )

    assert second.priors[0] is not None
    assert second.priors[0].etag == '"version-1"'
    assert second.priors[0].last_modified == "Thu, 03 Sep 2026 12:00:00 GMT"


def test_capability_constraint_never_writes_a_blob(tmp_path: Path) -> None:
    """A denied archive must not persist transient source bytes."""
    evaluator = _AllowEvaluator(frozenset({Capability.SOURCE_ARCHIVE}))
    receipt = _coordinator(tmp_path, evaluator=evaluator).run_due((_intent(),), now=NOW)
    assert receipt.complete is False
    assert SourceWorkPhase.ACCESS_CONSTRAINED in receipt.completed_phases
    assert tuple((tmp_path / "blobs" / "sha256").glob("*/*")) == ()
    assert all(
        request.capabilities != (Capability.SOURCE_EXTRACT,) for request in evaluator.requests
    )


def test_typed_fetch_constraint_is_preserved_durably_without_a_blob(
    tmp_path: Path,
) -> None:
    class ConstrainedAdapter(_Adapter):
        def fetch(
            self,
            candidate: SourceCandidate,
            *,
            prior: SourceCheckpoint | None,
        ) -> SourceCheckpoint:
            del prior
            return SourceCheckpoint(
                work_item_id=candidate.id,
                phase=SourceWorkPhase.ACCESS_CONSTRAINED,
                constraint="rate_limited",
                retry_after=60,
                recorded_at=NOW,
            )

    store = ImmutableStore(tmp_path)
    receipt = _coordinator(tmp_path, adapter=ConstrainedAdapter()).run_due(
        (_intent(),), now=NOW
    )

    checkpoint = next(store.iter_records("source-checkpoint"))
    constraint = next(store.iter_records("source-work-constraint"))
    assert checkpoint["constraint"] == "rate_limited"
    assert checkpoint["retry_after"] == 60
    assert constraint["constraint_type"] == "rate_limited"
    assert receipt.complete is False
    assert tuple((tmp_path / "blobs" / "sha256").glob("*/*")) == ()


def test_discovered_child_is_checked_against_intent_before_fetch(tmp_path: Path) -> None:
    class EscapingAdapter(_Adapter):
        def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
            return (
                SourceCandidate(
                    intent_id=intent.id,
                    locator="https://other.example/escaped.txt",
                    media_type="text/plain",
                    discovered_at=NOW,
                ),
            )

    adapter = EscapingAdapter()

    with pytest.raises(SourceAuthorizationError, match="host"):
        _coordinator(tmp_path, adapter=adapter).run_due((_intent(),), now=NOW)

    assert adapter.fetch_calls == []
    assert tuple((tmp_path / "blobs" / "sha256").glob("*/*")) == ()


def test_duplicate_bytes_reuse_blob_but_create_temporal_successor_observation(
    tmp_path: Path,
) -> None:
    """Conflating byte identity with observation identity erases source history."""
    first = _coordinator(tmp_path)
    first.run_due((_intent(interval_seconds=1),), now=NOW)
    later = NOW + timedelta(seconds=1)
    second = _coordinator(tmp_path, now=later)
    second.run_due((_intent(interval_seconds=1),), now=later)
    assert len(tuple((tmp_path / "blobs" / "sha256").glob("*/*"))) == 1
    observations = tuple(ImmutableStore(tmp_path).iter_records("source-temporal-observation"))
    assert len(observations) == 2
    assert observations[0]["source_content_sha256"] == observations[1]["source_content_sha256"]
    assert observations[0]["id"] != observations[1]["id"]


def test_work_identity_binds_canonical_intent_bytes() -> None:
    """Dropping checked-in intent bytes from identity could reuse broadened scope."""
    intent = _intent()
    assert hashlib.sha256(canonical_json(intent)).hexdigest() != "c" * 64


class _Extractor:
    def __init__(self, store: ImmutableStore, *, error: Exception | None = None) -> None:
        self.store = store
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.provider_calls = 0

    def propose(self, **values: object) -> object:
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        self.provider_calls += 1
        return SimpleNamespace(id="extraction-proposal:sha256:" + "e" * 64)


def test_local_extraction_uses_only_exact_leaf_anchors_without_model_capability(
    tmp_path: Path,
) -> None:
    """Selecting containers or requiring model.external for local work breaks grounding."""
    store = ImmutableStore(tmp_path)
    evaluator = _AllowEvaluator()
    extractor = _Extractor(store)
    coordinator = SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=_Adapter(),
        capability_evaluator=evaluator,
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        extractor=extractor,
        external_model=False,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    receipt = coordinator.run_due((_intent(),), now=NOW)
    assert receipt.proposal_ids == ("extraction-proposal:sha256:" + "e" * 64,)
    selected = set(extractor.calls[0]["anchor_ids"])
    kinds = {value["id"]: value["kind"] for value in store.iter_records("structural-anchor")}
    assert selected
    assert {kinds[item] for item in selected} <= {
        "heading",
        "paragraph",
        "list_item",
        "footnote",
        "caption",
    }
    assert all(
        request.capabilities != (Capability.MODEL_EXTERNAL,) for request in evaluator.requests
    )


def test_external_model_denial_happens_before_extractor_call(tmp_path: Path) -> None:
    """Calling the provider before model.external authorization leaks source text."""
    store = ImmutableStore(tmp_path)
    evaluator = _AllowEvaluator(frozenset({Capability.MODEL_EXTERNAL}))
    extractor = _Extractor(store)
    coordinator = SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=_Adapter(),
        capability_evaluator=evaluator,
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        extractor=extractor,
        external_model=True,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    receipt = coordinator.run_due((_intent(),), now=NOW)
    assert extractor.calls == []
    assert SourceWorkPhase.EXTRACTION_CONSTRAINED in receipt.completed_phases
    assert receipt.complete is False


def test_model_policy_denial_occurs_after_capability_allow_and_before_provider_call(
    tmp_path: Path,
) -> None:
    """Skipping the existing model gate would let capability authority spend budget."""
    store = ImmutableStore(tmp_path)
    evaluator = _AllowEvaluator()
    extractor = _Extractor(store, error=PermissionError("model policy denied"))
    coordinator = SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=_Adapter(),
        capability_evaluator=evaluator,
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        extractor=extractor,
        external_model=True,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    with pytest.raises(PermissionError, match="model policy"):
        coordinator.run_due((_intent(),), now=NOW)
    assert any(
        request.capabilities == (Capability.MODEL_EXTERNAL,) for request in evaluator.requests
    )
    assert extractor.provider_calls == 0


def test_due_intents_run_by_descending_priority_then_utf8_id(tmp_path: Path) -> None:
    """Input ordering must not change source request order or bounded outcomes."""
    adapter = _Adapter(phase=SourceWorkPhase.NOT_MODIFIED)
    intents = (
        _intent("zeta", priority=3),
        _intent("beta", priority=8),
        _intent("alpha", priority=8),
    )
    _coordinator(tmp_path, adapter=adapter).run_due(intents, now=NOW)
    assert adapter.discovery_calls == ["alpha", "beta", "zeta"]


def test_refresh_is_due_at_the_exact_fake_clock_boundary(tmp_path: Path) -> None:
    """Using wall time or a strict greater-than comparison makes refresh nondeterministic."""
    adapter = _Adapter(phase=SourceWorkPhase.NOT_MODIFIED)
    intent = _intent(interval_seconds=10)
    _coordinator(tmp_path, adapter=adapter).run_due((intent,), now=NOW)
    before = _Adapter(phase=SourceWorkPhase.NOT_MODIFIED)
    _coordinator(tmp_path, adapter=before, now=NOW + timedelta(seconds=9)).run_due(
        (intent,), now=NOW + timedelta(seconds=9)
    )
    boundary = _Adapter(phase=SourceWorkPhase.NOT_MODIFIED)
    _coordinator(tmp_path, adapter=boundary, now=NOW + timedelta(seconds=10)).run_due(
        (intent,), now=NOW + timedelta(seconds=10)
    )
    assert before.discovery_calls == []
    assert boundary.discovery_calls == [intent.id]


def test_request_and_byte_limits_stop_before_unbounded_persistence(tmp_path: Path) -> None:
    """Ignoring run ledgers permits declarations to drive unbounded I/O or storage."""
    adapter = _Adapter(content=b"0123456789")
    limits = SourceWorkLimits(
        max_requests_per_run=1,
        max_bytes_per_run=5,
        refresh_interval_seconds=1,
        max_run_seconds=100,
        finalization_reserve_seconds=10,
    )
    receipt = _coordinator(tmp_path, adapter=adapter, limits=limits).run_due(
        (_intent("first"), _intent("second")), now=NOW
    )
    assert len(adapter.fetch_calls) == 1
    assert tuple((tmp_path / "blobs" / "sha256").glob("*/*")) == ()
    assert receipt.complete is False


def test_finalization_reserve_stops_before_the_first_due_request(tmp_path: Path) -> None:
    """Spending the reserve on acquisition can leave the run unfinalizable."""
    values = iter((0.0, 95.0))
    adapter = _Adapter()
    coordinator = SourceWorkCoordinator(
        store=ImmutableStore(tmp_path),
        work_store=ImmutableSourceWorkStore(ImmutableStore(tmp_path)),
        adapter=adapter,
        capability_evaluator=_AllowEvaluator(),
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        clock=lambda: NOW,
        monotonic=lambda: next(values),
        limits=SourceWorkLimits(
            max_run_seconds=100,
            finalization_reserve_seconds=10,
        ),
    )
    receipt = coordinator.run_due((_intent(),), now=NOW)
    assert adapter.discovery_calls == []
    assert receipt.complete is False


def test_finalization_reserve_stops_before_external_model_call(tmp_path: Path) -> None:
    values = iter((0.0, 0.0, 0.0, 95.0))
    store = ImmutableStore(tmp_path)
    extractor = _Extractor(store)
    coordinator = SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=_Adapter(),
        capability_evaluator=_AllowEvaluator(),
        capability_request=_request,
        ontology_bundle_sha256=BUNDLE,
        extractor=extractor,
        external_model=True,
        clock=lambda: NOW,
        monotonic=lambda: next(values),
        limits=SourceWorkLimits(
            max_run_seconds=100,
            finalization_reserve_seconds=10,
        ),
    )

    receipt = coordinator.run_due((_intent(),), now=NOW)

    assert extractor.calls == []
    assert receipt.complete is False
    assert SourceWorkPhase.EXTRACTION_CONSTRAINED in receipt.completed_phases


def test_ontology_depth_limit_is_intersected_before_discovery(tmp_path: Path) -> None:
    class DepthAdapter(_Adapter):
        def __init__(self) -> None:
            super().__init__(phase=SourceWorkPhase.NOT_MODIFIED)
            self.depths: list[int] = []

        def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
            self.depths.append(intent.refresh.max_depth)
            return super().discover(intent)

    adapter = DepthAdapter()
    deep = _intent().model_copy(
        update={"refresh": _intent().refresh.model_copy(update={"max_depth": 7})}
    )
    limits = SourceWorkLimits(max_depth=2)

    _coordinator(tmp_path, adapter=adapter, limits=limits).run_due((deep,), now=NOW)

    assert adapter.depths == [2]


def test_optional_constraint_finalizes_without_failing_the_batch(tmp_path: Path) -> None:
    """Treating optional and required constraints alike defeats bounded best-effort updates."""
    optional = _coordinator(
        tmp_path / "optional",
        evaluator=_AllowEvaluator(frozenset({Capability.SOURCE_FETCH})),
    ).run_due((_intent(required=False),), now=NOW)
    required = _coordinator(
        tmp_path / "required",
        evaluator=_AllowEvaluator(frozenset({Capability.SOURCE_FETCH})),
    ).run_due((_intent(required=True),), now=NOW)
    assert optional.complete is True
    assert required.complete is False
