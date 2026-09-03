from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    CapabilitySubject,
    DelegationEntry,
    DelegationManifest,
    DeterministicCapabilityEvaluator,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
ROOT = "https://github.com/example/root"
CHILD = "https://github.com/example/child"
OTHER = "https://github.com/example/other"
TARGET = "https://github.com/example/target"
DIGEST = "a" * 64


def _grant(
    decision: str = "allow",
    *,
    repository: str = ROOT,
    refs: tuple[str, ...] | str = "*",
    paths: tuple[str, ...] | str = "*",
    digests: tuple[str, ...] | str = "*",
    capabilities: tuple[Capability, ...] = (Capability.SOURCE_FETCH,),
    delegable_capabilities: tuple[Capability, ...] = (),
    delegated_repositories: tuple[str, ...] | str = (),
    max_delegation_depth: int = 0,
    expires_at: datetime | None = None,
) -> CapabilityGrant:
    return CapabilityGrant(
        decision=decision,
        subject=CapabilitySubject(
            repository=repository,
            refs=refs,
            paths=paths,
            bundle_sha256=digests,
        ),
        capabilities=capabilities,
        delegable_capabilities=delegable_capabilities,
        resources=CapabilityResources(delegated_repositories=delegated_repositories),
        max_delegation_depth=max_delegation_depth,
        expires_at=expires_at,
        created_at=NOW - timedelta(days=1),
        created_via="manual",
    )


def _request(
    capability: Capability,
    *,
    authority_repository: str = ROOT,
    target_repository: str = ROOT,
    ref: str = "refs/heads/main",
    path: str = "ontology/a",
    bundle_sha256: str | None = DIGEST,
    dirty: bool = False,
) -> CapabilityRequest:
    return CapabilityRequest(
        authority_repository=authority_repository,
        target_repository=target_repository,
        capabilities=(capability,),
        ref=ref,
        path=path,
        bundle_sha256=bundle_sha256,
        dirty=dirty,
        requested_at=NOW,
    )


def _evaluator(*grants: CapabilityGrant, yolo: bool = False) -> DeterministicCapabilityEvaluator:
    return DeterministicCapabilityEvaluator(grants, {}, clock=lambda: NOW, yolo=yolo)


def test_equal_specificity_deny_wins_for_one_atomic_capability() -> None:
    decision = _evaluator(
        _grant(paths=("ontology/a",)),
        _grant("deny", paths=("ontology/a",)),
    ).evaluate(_request(Capability.SOURCE_FETCH, path="ontology/a"))

    assert decision.allowed is False


@pytest.mark.parametrize(
    ("allow_selector", "deny_selector"),
    [
        ({"digests": (DIGEST,)}, {"paths": ("ontology/a",), "refs": ("refs/heads/main",)}),
        ({"paths": ("ontology/a",)}, {"refs": ("refs/heads/main",)}),
        ({"refs": ("refs/heads/main",)}, {}),
    ],
)
def test_digest_then_path_then_ref_specificity_wins(
    allow_selector: dict[str, object], deny_selector: dict[str, object]
) -> None:
    decision = _evaluator(
        _grant("deny", **deny_selector),  # type: ignore[arg-type]
        _grant("allow", **allow_selector),  # type: ignore[arg-type]
    ).evaluate(_request(Capability.SOURCE_FETCH))

    assert decision.allowed


def test_target_local_deny_blocks_an_origin_allow() -> None:
    decision = _evaluator(
        _grant(repository=ROOT),
        _grant("deny", repository=CHILD),
    ).evaluate(
        _request(
            Capability.SOURCE_FETCH,
            authority_repository=ROOT,
            target_repository=CHILD,
        )
    )

    assert not decision.allowed


def test_dirty_bytes_are_not_covered_by_branch_only_allow() -> None:
    decision = _evaluator(
        _grant(refs=("refs/heads/main",)),
    ).evaluate(_request(Capability.SOURCE_FETCH, dirty=True))

    assert not decision.allowed


def test_expiry_is_exclusive_at_the_clock_boundary() -> None:
    before = _evaluator(_grant(expires_at=NOW + timedelta(microseconds=1))).evaluate(
        _request(Capability.SOURCE_FETCH)
    )
    at_boundary = _evaluator(_grant(expires_at=NOW)).evaluate(
        _request(Capability.SOURCE_FETCH)
    )

    assert before.allowed
    assert not at_boundary.allowed


def test_unknown_capability_fails_before_evaluation() -> None:
    raw = _request(Capability.REPOSITORY_READ).model_dump(mode="json")
    raw["capabilities"] = ["shell.execute"]

    with pytest.raises(ValidationError, match="capabilities"):
        CapabilityRequest.model_validate(raw)


@pytest.mark.parametrize(
    "capability",
    tuple(item for item in Capability if item is not Capability.REPOSITORY_READ),
)
def test_yolo_refuses_every_non_read_capability(capability: Capability) -> None:
    decision = _evaluator(yolo=True).evaluate(_request(capability))

    assert not decision.allowed


def test_yolo_allows_only_unresolved_repository_read() -> None:
    decision = _evaluator(yolo=True).evaluate(_request(Capability.REPOSITORY_READ))

    assert decision.allowed
    assert decision.grant_ids == ()


def _entry(
    repository: str,
    *,
    capabilities: tuple[Capability, ...] = (
        Capability.SOURCE_FETCH,
        Capability.TRUST_DELEGATE,
    ),
    delegable_capabilities: tuple[Capability, ...] = (
        Capability.SOURCE_FETCH,
        Capability.TRUST_DELEGATE,
    ),
    delegated_repositories: tuple[str, ...] = (),
    max_delegation_depth: int = 1,
    expires_at: datetime | None = None,
) -> DelegationEntry:
    return DelegationEntry(
        subject=CapabilitySubject(
            repository=repository,
            refs="*",
            paths="*",
            bundle_sha256="*",
        ),
        capabilities=capabilities,
        delegable_capabilities=delegable_capabilities,
        resources=CapabilityResources(delegated_repositories=delegated_repositories),
        max_delegation_depth=max_delegation_depth,
        expires_at=expires_at,
    )


def _delegating_root(
    *,
    depth: int,
    capabilities: tuple[Capability, ...] = (
        Capability.SOURCE_FETCH,
        Capability.TRUST_DELEGATE,
    ),
    delegable_capabilities: tuple[Capability, ...] = (
        Capability.SOURCE_FETCH,
        Capability.TRUST_DELEGATE,
    ),
    repositories: tuple[str, ...] | str = (CHILD, TARGET),
) -> CapabilityGrant:
    return _grant(
        capabilities=capabilities,
        delegable_capabilities=delegable_capabilities,
        delegated_repositories=repositories,
        max_delegation_depth=depth,
    )


def _evaluate_chain(*, root_depth: int, child_declared_depth: int):
    evaluator = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=root_depth),),
        {
            ROOT: DelegationManifest(
                delegations=(
                    _entry(CHILD, max_delegation_depth=child_declared_depth),
                )
            )
        },
        clock=lambda: NOW,
    )
    return evaluator.evaluate(
        _request(
            Capability.SOURCE_FETCH,
            target_repository=CHILD,
        )
    )


def test_one_hop_consumes_the_only_delegation_edge() -> None:
    decision = _evaluate_chain(root_depth=1, child_declared_depth=8)

    assert decision.allowed
    assert decision.effective_remaining_depth == 0


def test_child_depth_is_parent_minus_one_intersected_with_declaration() -> None:
    decision = _evaluate_chain(root_depth=4, child_declared_depth=2)

    assert decision.allowed
    assert decision.effective_remaining_depth == 2


def test_wildcard_delegated_repository_scope_allows_declared_child() -> None:
    evaluator = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=1, repositories="*"),),
        {ROOT: DelegationManifest(delegations=(_entry(CHILD),))},
        clock=lambda: NOW,
    )

    decision = evaluator.evaluate(
        _request(Capability.SOURCE_FETCH, target_repository=CHILD)
    )

    assert decision.allowed


@pytest.mark.parametrize(
    "root",
    [
        _delegating_root(
            depth=2,
            capabilities=(Capability.SOURCE_FETCH,),
            delegable_capabilities=(Capability.SOURCE_FETCH,),
        ),
        _delegating_root(
            depth=2,
            delegable_capabilities=(Capability.TRUST_DELEGATE,),
        ),
        _delegating_root(depth=2, repositories=(TARGET,)),
    ],
    ids=("missing-trust-delegate", "missing-delegable-capability", "undeclared-child"),
)
def test_delegation_edge_requires_all_parent_authority(root: CapabilityGrant) -> None:
    evaluator = DeterministicCapabilityEvaluator(
        (root,),
        {ROOT: DelegationManifest(delegations=(_entry(CHILD),))},
        clock=lambda: NOW,
    )

    assert not evaluator.evaluate(
        _request(Capability.SOURCE_FETCH, target_repository=CHILD)
    ).allowed


def test_intermediate_local_allow_intersects_and_local_deny_blocks() -> None:
    broad_deny = _grant("deny", repository=CHILD)
    specific_allow = _grant(
        repository=CHILD,
        paths=("ontology/a",),
        capabilities=(Capability.SOURCE_FETCH, Capability.TRUST_DELEGATE),
        delegable_capabilities=(Capability.SOURCE_FETCH,),
        delegated_repositories=(TARGET,),
        max_delegation_depth=1,
    )
    manifests = {
        ROOT: DelegationManifest(
            delegations=(
                _entry(CHILD, delegated_repositories=(TARGET,), max_delegation_depth=1),
            )
        ),
        CHILD: DelegationManifest(
            delegations=(
                _entry(
                    TARGET,
                    capabilities=(Capability.SOURCE_FETCH,),
                    delegable_capabilities=(),
                    max_delegation_depth=0,
                ),
            )
        ),
    }

    allowed = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=2), broad_deny, specific_allow),
        manifests,
        clock=lambda: NOW,
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))
    denied = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=2), broad_deny), manifests, clock=lambda: NOW
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))

    assert allowed.allowed
    assert allowed.effective_remaining_depth == 0
    assert not denied.allowed


def test_intermediate_local_trust_delegate_deny_stops_onward_traversal() -> None:
    delegate_deny = _grant(
        "deny",
        repository=CHILD,
        capabilities=(Capability.TRUST_DELEGATE,),
        delegated_repositories=(TARGET,),
    )
    manifests = {
        ROOT: DelegationManifest(
            delegations=(
                _entry(CHILD, delegated_repositories=(TARGET,), max_delegation_depth=1),
            )
        ),
        CHILD: DelegationManifest(
            delegations=(
                _entry(
                    TARGET,
                    capabilities=(Capability.SOURCE_FETCH,),
                    delegable_capabilities=(),
                    max_delegation_depth=0,
                ),
            )
        ),
    }

    decision = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=2), delegate_deny),
        manifests,
        clock=lambda: NOW,
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))

    assert not decision.allowed


def test_expired_intermediate_and_attempted_widening_fail_closed() -> None:
    expired = DelegationManifest(
        delegations=(_entry(CHILD, expires_at=NOW),)
    )
    widening = DelegationManifest(
        delegations=(_entry(CHILD, delegated_repositories=(OTHER,)),)
    )
    root = _delegating_root(depth=2, repositories=(CHILD, TARGET))
    request = _request(Capability.SOURCE_FETCH, target_repository=CHILD)

    assert not DeterministicCapabilityEvaluator(
        (root,), {ROOT: expired}, clock=lambda: NOW
    ).evaluate(request).allowed
    assert not DeterministicCapabilityEvaluator(
        (root,), {ROOT: widening}, clock=lambda: NOW
    ).evaluate(request).allowed


def test_cycles_are_rejected_before_a_request_is_evaluated() -> None:
    manifests = {
        ROOT: DelegationManifest(delegations=(_entry(CHILD),)),
        CHILD: DelegationManifest(delegations=(_entry(ROOT),)),
    }

    with pytest.raises(ValueError, match="cycle|repeated"):
        DeterministicCapabilityEvaluator(
            (_delegating_root(depth=2),), manifests, clock=lambda: NOW
        )


def test_multiple_valid_chains_choose_lexically_first_chain() -> None:
    root = _delegating_root(depth=2, repositories=(CHILD, OTHER, TARGET))
    manifests = {
        ROOT: DelegationManifest(
            delegations=(
                _entry(CHILD, delegated_repositories=(TARGET,)),
                _entry(OTHER, delegated_repositories=(TARGET,)),
            )
        ),
        CHILD: DelegationManifest(
            delegations=(
                _entry(
                    TARGET,
                    capabilities=(Capability.SOURCE_FETCH,),
                    delegable_capabilities=(),
                    max_delegation_depth=0,
                ),
            )
        ),
        OTHER: DelegationManifest(
            delegations=(
                _entry(
                    TARGET,
                    capabilities=(Capability.SOURCE_FETCH,),
                    delegable_capabilities=(),
                    max_delegation_depth=0,
                ),
            )
        ),
    }

    decision = DeterministicCapabilityEvaluator(
        (root,), manifests, clock=lambda: NOW
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))

    assert decision.allowed
    assert decision.delegation_chain == (ROOT, CHILD, TARGET)
