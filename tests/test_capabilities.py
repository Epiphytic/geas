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
MANIFEST_SHA256 = "b" * 64
CATALOG_COMMIT = "c" * 40


def _verified_manifest(
    repository: str,
    manifest: DelegationManifest,
    *,
    manifest_sha256: str = MANIFEST_SHA256,
    catalog_commit: str = CATALOG_COMMIT,
) -> dict[str, object]:
    return {
        "repository": repository,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "catalog_commit": catalog_commit,
    }


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
    hosts: tuple[str, ...] | str = "*",
    path_prefixes: tuple[str, ...] | str = "*",
    connectors: tuple[str, ...] | str = "*",
    providers: tuple[str, ...] | str = "*",
    models: tuple[str, ...] | str = "*",
    data_classes: tuple[str, ...] | str = "*",
    git_refs: tuple[str, ...] | str = "*",
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
        resources=CapabilityResources(
            delegated_repositories=delegated_repositories,
            hosts=hosts,
            path_prefixes=path_prefixes,
            connectors=connectors,
            providers=providers,
            models=models,
            data_classes=data_classes,
            git_refs=git_refs,
        ),
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
    selectors: dict[str, str] = {}
    if capability in {
        Capability.SOURCE_DISCOVER,
        Capability.SOURCE_FETCH,
        Capability.SOURCE_ARCHIVE,
        Capability.SOURCE_EXTRACT,
    }:
        selectors = {
            "connector": "crossref",
            "host": "api.example.invalid",
            "target": "https://api.example.invalid/records/1",
        }
    elif capability is Capability.MODEL_EXTERNAL:
        selectors = {
            "provider": "openai",
            "model": "gpt-5",
            "data_class": "public",
        }
    return CapabilityRequest(
        authority_repository=authority_repository,
        target_repository=target_repository,
        capabilities=(capability,),
        ref=ref,
        path=path,
        bundle_sha256=bundle_sha256,
        dirty=dirty,
        requested_at=NOW,
        **selectors,
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
    "capability",
    (
        Capability.SOURCE_DISCOVER,
        Capability.SOURCE_FETCH,
        Capability.SOURCE_ARCHIVE,
        Capability.SOURCE_EXTRACT,
    ),
)
@pytest.mark.parametrize("missing", ("connector", "host", "target"))
def test_source_requests_require_every_normalized_resource_selector(
    capability: Capability,
    missing: str,
) -> None:
    raw = _request(capability).model_dump(mode="json")
    raw.pop(missing)

    with pytest.raises(ValidationError, match="supplied together"):
        CapabilityRequest.model_validate(raw)

    for selector in ("connector", "host", "target"):
        raw.pop(selector, None)
    request = CapabilityRequest.model_validate(raw)

    assert not _evaluator(_grant()).evaluate(request).allowed


def test_source_request_requires_canonical_wire_target_and_cross_checks_host() -> None:
    normalized = CapabilityRequest(
        authority_repository=ROOT,
        target_repository=ROOT,
        capabilities=(Capability.SOURCE_FETCH,),
        ref="refs/heads/main",
        path="ontology/a",
        bundle_sha256=DIGEST,
        connector="crossref",
        host="api.example.invalid",
        target="https://api.example.invalid/records/1",
        requested_at=NOW,
    )

    assert normalized.target == "https://api.example.invalid/records/1"

    for target in (
        "https://API.EXAMPLE.INVALID/records/1",
        "https://api.example.invalid:443/records/1",
        "https://api.example.invalid./records/1",
    ):
        with pytest.raises(ValidationError, match="canonical"):
            CapabilityRequest(
                **{
                    **normalized.model_dump(mode="python"),
                    "target": target,
                }
            )

    with pytest.raises(ValidationError, match="host.*target|target.*host"):
        CapabilityRequest(
            **{
                **normalized.model_dump(mode="python"),
                "host": "other.example.invalid",
            }
        )


def test_github_repository_request_accepts_exact_commit_pinned_readme_target() -> None:
    target = (
        "https://api.github.com/repos/Example/Research/readme?ref="
        + "a" * 40
    )
    request = CapabilityRequest(
        authority_repository=ROOT,
        target_repository=ROOT,
        capabilities=(Capability.SOURCE_FETCH,),
        ref="refs/heads/main",
        path="ontology/a",
        bundle_sha256=DIGEST,
        connector="source:github-repository",
        host="api.github.com",
        target=target,
        requested_at=NOW,
    )
    decision = _evaluator(
        _grant(
            hosts=("api.github.com",),
            path_prefixes=("/repos/Example/Research",),
            connectors=("source:github-repository",),
        )
    ).evaluate(request)

    assert request.target == target
    assert decision.allowed
    assert decision.request.target == target


@pytest.mark.parametrize(
    "target",
    (
        "https://api.github.com/repos/Example/Research/readme?ref=",
        "https://api.github.com/repos/Example/Research/readme?ref=" + "A" * 40,
        "https://api.github.com/repos/Example/Research/readme?ref=" + "g" * 40,
        "https://api.github.com/repos/Example/Research/readme?ref=" + "%61" * 40,
        "https://api.github.com/repos/Example/Research/readme?ref=" + "a" * 39,
        "https://api.github.com/repos/Example/Research/readme?ref=" + "a" * 41,
        "https://api.github.com/repos/Example/Research/readme?ref="
        + "a" * 40
        + "&other=value",
        "https://api.github.com/repos/Example/Research/readme?ref="
        + "a" * 40
        + "&ref="
        + "b" * 40,
        "https://api.github.com/repos/Example/Research/readme?other=" + "a" * 40,
        "https://api.github.com/repos/Example/Research/readme/?ref=" + "a" * 40,
        "https://api.github.com/repos/Example/Research/README?ref=" + "a" * 40,
        "https://api.github.com:443/repos/Example/Research/readme?ref=" + "a" * 40,
        "https://user@api.github.com/repos/Example/Research/readme?ref=" + "a" * 40,
        "https://api.github.com/repos/Example/Research/readme?ref="
        + "a" * 40
        + "#fragment",
    ),
)
def test_github_repository_request_rejects_noncanonical_readme_target(target: str) -> None:
    raw = _request(Capability.SOURCE_FETCH).model_dump(mode="python")
    raw.update(
        connector="source:github-repository",
        host="api.github.com",
        target=target,
    )

    with pytest.raises(ValidationError):
        CapabilityRequest.model_validate(raw)


@pytest.mark.parametrize(
    "target",
    (
        "https://user:secret@api.example.invalid/records/1",
        "https://api.example.invalid/records/1?token=secret",
        "ssh://git@api.example.invalid/records/1",
        "https://api.example.invalid/%41",
        "https://api.example.invalid/records\\1",
        "https://api.example.invalid/a/./b",
        "https://api.example.invalid/a/../b",
        "https://api.example.invalid/a/%ZZ",
        "https://api.example.invalid/a b",
        "https://api.example.invalid/ümlaut",
        "https://api.example.invalid/a;b",
        "https://api.example.invalid/a@b",
        "https://api.example.invalid/a%2fb",
    ),
)
def test_source_request_rejects_unsafe_targets(target: str) -> None:
    raw = _request(Capability.SOURCE_FETCH).model_dump(mode="python")
    raw["target"] = target

    with pytest.raises(ValidationError):
        CapabilityRequest.model_validate(raw)


@pytest.mark.parametrize(
    "target",
    (
        "https://api.example.invalid/a%20b",
        "https://api.example.invalid/%C3%BCmlaut",
        "https://api.example.invalid/a%3Bb",
        "https://api.example.invalid/a~b-_.",
    ),
)
def test_source_target_canonical_percent_encoding_is_idempotent_and_transport_exact(
    target: str,
) -> None:
    raw = _request(Capability.SOURCE_FETCH).model_dump(mode="python")
    raw["target"] = target
    first = CapabilityRequest.model_validate(raw)
    second = CapabilityRequest.model_validate(first.model_dump(mode="python"))
    decision = _evaluator(_grant()).evaluate(first)

    assert first.target == target
    assert second.target == target
    assert decision.allowed
    assert decision.request.target == target


def test_explicit_empty_source_resource_grants_no_authority() -> None:
    grant = _grant(hosts=(), path_prefixes=(), connectors=())

    assert not _evaluator(grant).evaluate(_request(Capability.SOURCE_FETCH)).allowed


def test_model_external_requires_and_matches_each_resource_dimension() -> None:
    request = _request(Capability.MODEL_EXTERNAL)
    matching = _grant(
        capabilities=(Capability.MODEL_EXTERNAL,),
        providers=("openai",),
        models=("gpt-5",),
        data_classes=("public",),
    )
    wrong_model = matching.model_copy(
        update={"resources": matching.resources.model_copy(update={"models": ("gpt-4",)})}
    )

    assert _evaluator(matching).evaluate(request).allowed
    assert not _evaluator(wrong_model).evaluate(request).allowed

    raw = request.model_dump(mode="json")
    for selector in ("provider", "model", "data_class"):
        missing = dict(raw)
        missing.pop(selector)
        with pytest.raises(ValidationError, match="supplied together"):
            CapabilityRequest.model_validate(missing)
        for key in ("provider", "model", "data_class"):
            missing.pop(key, None)
        denied = CapabilityRequest.model_validate(missing)
        assert not _evaluator(matching).evaluate(denied).allowed


def test_model_external_target_is_omitted_or_safe_and_never_retains_credentials() -> None:
    request = _request(Capability.MODEL_EXTERNAL)
    assert request.target is None

    raw = request.model_dump(mode="python")
    raw["target"] = "https://user:secret@example.invalid/v1"
    with pytest.raises(ValidationError):
        CapabilityRequest.model_validate(raw)


def test_irrelevant_git_ref_does_not_make_source_allow_more_specific_than_deny() -> None:
    allow = _grant(git_refs=("refs/heads/main",))
    deny = _grant("deny")

    assert not _evaluator(allow, deny).evaluate(_request(Capability.SOURCE_FETCH)).allowed


def test_irrelevant_additional_capability_does_not_defeat_equal_source_deny() -> None:
    allow = _grant(capabilities=(Capability.SOURCE_FETCH,))
    deny = _grant(
        "deny",
        capabilities=(Capability.REPOSITORY_READ, Capability.SOURCE_FETCH),
    )

    assert not _evaluator(allow, deny).evaluate(_request(Capability.SOURCE_FETCH)).allowed


def test_final_effective_resource_intersection_must_match_actual_request() -> None:
    source = _grant(
        capabilities=(Capability.SOURCE_FETCH,),
        hosts=("api.example.invalid",),
        path_prefixes=("/records",),
        connectors=("crossref",),
    )
    repository = _grant(
        capabilities=(Capability.REPOSITORY_READ,),
        hosts=(),
        path_prefixes=(),
        connectors=(),
    )
    request = CapabilityRequest(
        **{
            **_request(Capability.SOURCE_FETCH).model_dump(mode="python"),
            "capabilities": (Capability.REPOSITORY_READ, Capability.SOURCE_FETCH),
        }
    )

    assert not _evaluator(source, repository).evaluate(request).allowed


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
    before = _evaluator(
        _grant(expires_at=NOW + timedelta(microseconds=1))
    ).evaluate(_request(Capability.SOURCE_FETCH))
    at_boundary = _evaluator(_grant(expires_at=NOW)).evaluate(_request(Capability.SOURCE_FETCH))

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
    hosts: tuple[str, ...] | str = "*",
    path_prefixes: tuple[str, ...] | str = "*",
    connectors: tuple[str, ...] | str = "*",
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
        resources=CapabilityResources(
            delegated_repositories=delegated_repositories,
            hosts=hosts,
            path_prefixes=path_prefixes,
            connectors=connectors,
            providers="*",
            models="*",
            data_classes="*",
            git_refs="*",
        ),
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
            ROOT: _verified_manifest(
                ROOT,
                DelegationManifest(
                    delegations=(_entry(CHILD, max_delegation_depth=child_declared_depth),)
                ),
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
        {ROOT: _verified_manifest(ROOT, DelegationManifest(delegations=(_entry(CHILD),)))},
        clock=lambda: NOW,
    )

    decision = evaluator.evaluate(_request(Capability.SOURCE_FETCH, target_repository=CHILD))

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
        {ROOT: _verified_manifest(ROOT, DelegationManifest(delegations=(_entry(CHILD),)))},
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
        ROOT: _verified_manifest(
            ROOT,
            DelegationManifest(
                delegations=(
                    _entry(CHILD, delegated_repositories=(TARGET,), max_delegation_depth=1),
                )
            ),
        ),
        CHILD: _verified_manifest(
            CHILD,
            DelegationManifest(
                delegations=(
                    _entry(
                        TARGET,
                        capabilities=(Capability.SOURCE_FETCH,),
                        delegable_capabilities=(),
                        max_delegation_depth=0,
                    ),
                )
            ),
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
        ROOT: _verified_manifest(
            ROOT,
            DelegationManifest(
                delegations=(
                    _entry(CHILD, delegated_repositories=(TARGET,), max_delegation_depth=1),
                )
            ),
        ),
        CHILD: _verified_manifest(
            CHILD,
            DelegationManifest(
                delegations=(
                    _entry(
                        TARGET,
                        capabilities=(Capability.SOURCE_FETCH,),
                        delegable_capabilities=(),
                        max_delegation_depth=0,
                    ),
                )
            ),
        ),
    }

    decision = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=2), delegate_deny),
        manifests,
        clock=lambda: NOW,
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))

    assert not decision.allowed


def test_intermediate_effective_resources_must_match_actual_request() -> None:
    intermediate_delegate = _grant(
        repository=CHILD,
        capabilities=(Capability.SOURCE_FETCH, Capability.TRUST_DELEGATE),
        delegable_capabilities=(Capability.SOURCE_FETCH, Capability.TRUST_DELEGATE),
        delegated_repositories=(TARGET,),
        hosts=(),
        path_prefixes=(),
        connectors=(),
        max_delegation_depth=1,
    )
    manifests = {
        ROOT: _verified_manifest(
            ROOT,
            DelegationManifest(
                delegations=(
                    _entry(CHILD, delegated_repositories=(TARGET,), max_delegation_depth=1),
                )
            ),
        ),
        CHILD: _verified_manifest(
            CHILD,
            DelegationManifest(
                delegations=(
                    _entry(
                        TARGET,
                        capabilities=(Capability.SOURCE_FETCH,),
                        delegable_capabilities=(),
                        max_delegation_depth=0,
                    ),
                )
            ),
        ),
    }

    decision = DeterministicCapabilityEvaluator(
        (_delegating_root(depth=2), intermediate_delegate),
        manifests,
        clock=lambda: NOW,
    ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=TARGET))

    assert not decision.allowed


def test_expired_intermediate_and_attempted_widening_fail_closed() -> None:
    expired = DelegationManifest(delegations=(_entry(CHILD, expires_at=NOW),))
    widening = DelegationManifest(delegations=(_entry(CHILD, delegated_repositories=(OTHER,)),))
    root = _delegating_root(depth=2, repositories=(CHILD, TARGET))
    request = _request(Capability.SOURCE_FETCH, target_repository=CHILD)

    assert (
        not DeterministicCapabilityEvaluator(
            (root,), {ROOT: _verified_manifest(ROOT, expired)}, clock=lambda: NOW
        )
        .evaluate(request)
        .allowed
    )
    assert (
        not DeterministicCapabilityEvaluator(
            (root,), {ROOT: _verified_manifest(ROOT, widening)}, clock=lambda: NOW
        )
        .evaluate(request)
        .allowed
    )


def test_cycles_are_rejected_before_a_request_is_evaluated() -> None:
    manifests = {
        ROOT: _verified_manifest(ROOT, DelegationManifest(delegations=(_entry(CHILD),))),
        CHILD: _verified_manifest(CHILD, DelegationManifest(delegations=(_entry(ROOT),))),
    }

    with pytest.raises(ValueError, match="cycle|repeated"):
        DeterministicCapabilityEvaluator((_delegating_root(depth=2),), manifests, clock=lambda: NOW)


def test_multiple_valid_chains_choose_lexically_first_chain() -> None:
    root = _delegating_root(depth=2, repositories=(CHILD, OTHER, TARGET))
    manifests = {
        ROOT: _verified_manifest(
            ROOT,
            DelegationManifest(
                delegations=(
                    _entry(CHILD, delegated_repositories=(TARGET,)),
                    _entry(OTHER, delegated_repositories=(TARGET,)),
                )
            ),
        ),
        CHILD: _verified_manifest(
            CHILD,
            DelegationManifest(
                delegations=(
                    _entry(
                        TARGET,
                        capabilities=(Capability.SOURCE_FETCH,),
                        delegable_capabilities=(),
                        max_delegation_depth=0,
                    ),
                )
            ),
        ),
        OTHER: _verified_manifest(
            OTHER,
            DelegationManifest(
                delegations=(
                    _entry(
                        TARGET,
                        capabilities=(Capability.SOURCE_FETCH,),
                        delegable_capabilities=(),
                        max_delegation_depth=0,
                    ),
                )
            ),
        ),
    }

    decision = DeterministicCapabilityEvaluator((root,), manifests, clock=lambda: NOW).evaluate(
        _request(Capability.SOURCE_FETCH, target_repository=TARGET)
    )

    assert decision.allowed
    assert decision.delegation_chain == (ROOT, CHILD, TARGET)


def test_parsed_manifest_without_verified_bytes_and_commit_is_rejected() -> None:
    with pytest.raises(ValueError, match="verified|sha256|commit"):
        DeterministicCapabilityEvaluator(
            (_delegating_root(depth=1),),
            {ROOT: DelegationManifest(delegations=(_entry(CHILD),))},
            clock=lambda: NOW,
        )


def test_manifest_bytes_and_catalog_commit_bind_delegated_receipts() -> None:
    manifest = DelegationManifest(delegations=(_entry(CHILD),))

    def evaluate(*, manifest_sha256: str, catalog_commit: str):
        return DeterministicCapabilityEvaluator(
            (_delegating_root(depth=1),),
            {
                ROOT: _verified_manifest(
                    ROOT,
                    manifest,
                    manifest_sha256=manifest_sha256,
                    catalog_commit=catalog_commit,
                )
            },
            clock=lambda: NOW,
        ).evaluate(_request(Capability.SOURCE_FETCH, target_repository=CHILD))

    first = evaluate(manifest_sha256="1" * 64, catalog_commit="2" * 40)
    different_bytes = evaluate(manifest_sha256="3" * 64, catalog_commit="2" * 40)
    different_commit = evaluate(manifest_sha256="1" * 64, catalog_commit="4" * 40)

    assert first.allowed and different_bytes.allowed and different_commit.allowed
    assert first.manifest_sha256s == ("1" * 64,)
    assert first.catalog_commits == ("2" * 40,)
    assert len({first.id, different_bytes.id, different_commit.id}) == 3
