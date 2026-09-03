from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.publishing as publishing
from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    CapabilitySubject,
)
from research_agent.publishing import (
    PathRole,
    PublicationManifest,
    PublishMode,
    PublishPath,
    PublishRequest,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
REPOSITORY = "https://github.com/example/gold"


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Publisher Test",
            "GIT_AUTHOR_EMAIL": "publisher@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Publisher Test",
            "GIT_COMMITTER_EMAIL": "publisher@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=check,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    worktree = tmp_path / "worktree"
    _git(tmp_path, "clone", str(remote), str(worktree))
    _git(worktree, "switch", "-c", "main")
    (worktree / "README.md").write_text("fixture\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "initial")
    _git(worktree, "push", "-u", "origin", "main")
    return remote, worktree


def _manifest(worktree: Path, path: str, role: PathRole) -> PublicationManifest:
    return PublicationManifest(
        receipt_sha256="a" * 64,
        paths=(
            {
                "path": path,
                "role": role,
                "sha256": hashlib.sha256((worktree / path).read_bytes()).hexdigest(),
            },
        ),
    )


def _decision(
    *,
    path: str,
    capabilities: tuple[Capability, ...],
    target_ref: str = "refs/heads/main",
    grant_ids: tuple[str, ...] = (),
    delegation_chain: tuple[str, ...] = (),
) -> CapabilityDecision:
    request = CapabilityRequest(
        authority_repository=REPOSITORY,
        target_repository=REPOSITORY,
        capabilities=capabilities,
        ref=target_ref,
        path=path,
        requested_at=NOW,
    )
    return CapabilityDecision(
        request=request,
        decision="allow",
        effective_capabilities=capabilities,
        grant_ids=grant_ids,
        delegation_chain=delegation_chain,
        reason="fixture allow",
        evaluator_version="fixture/1",
        decided_at=NOW,
    )


def _request(
    path: str,
    role: PathRole,
    decision: CapabilityDecision,
    *,
    mode: PublishMode = PublishMode.PULL_REQUEST,
    target_ref: str = "refs/heads/main",
) -> PublishRequest:
    return PublishRequest(
        repository=REPOSITORY,
        target_ref=target_ref,
        mode=mode,
        paths=(PublishPath(path=path, role=role),),
        capability_decision_sha256=decision.sha256,
        created_at=NOW,
    )


class _Forge:
    def __init__(self) -> None:
        self.upserts: list[dict[str, str]] = []
        self.auto_merges: list[dict[str, str]] = []

    def upsert_pull_request(self, **values: str) -> str:
        self.upserts.append(values)
        return "https://github.com/example/gold/pull/7"

    def enable_auto_merge(self, **values: str) -> None:
        self.auto_merges.append(values)


class _PromotionVerifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def verify(self, **values: object) -> None:
        self.calls.append(values)


def _publisher(
    worktree: Path,
    manifest: PublicationManifest,
    decision: CapabilityDecision,
    *,
    direct_push: bool = False,
    forge: _Forge | None = None,
    grants: dict[str, CapabilityGrant] | None = None,
    promotion_verifier: _PromotionVerifier | None = None,
):
    publisher_type = (
        __import__(
            "research_agent.repository_publisher",
            fromlist=["GitRepositoryPublisher"],
        )
    ).GitRepositoryPublisher
    return publisher_type(
        repository=worktree,
        manifests=(manifest,),
        capability_decision=decision,
        forge=forge,
        now=lambda: NOW,
        direct_push=direct_push,
        grants=grants,
        promotion_verifier=promotion_verifier,
    )


ROLE_MATRIX = (
    (
        PathRole.GENERIC_SKILL,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: frozenset({Capability.GIT_PULL_REQUEST}),
            (PublishMode.DIRECT_PUSH, False): frozenset({Capability.GIT_DIRECT_PUSH}),
            (PublishMode.DIRECT_PUSH, True): frozenset({Capability.GIT_DIRECT_PUSH}),
            PublishMode.AUTO_MERGE: frozenset({Capability.GIT_AUTO_MERGE}),
        },
    ),
    (
        PathRole.EXPORTED_SKILL,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: frozenset({Capability.GIT_PULL_REQUEST}),
            (PublishMode.DIRECT_PUSH, False): frozenset({Capability.GIT_DIRECT_PUSH}),
            (PublishMode.DIRECT_PUSH, True): frozenset({Capability.GIT_DIRECT_PUSH}),
            PublishMode.AUTO_MERGE: frozenset({Capability.GIT_AUTO_MERGE}),
        },
    ),
    (
        PathRole.GENERATED_PROJECTION,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: frozenset({Capability.GIT_PULL_REQUEST}),
            (PublishMode.DIRECT_PUSH, False): frozenset({Capability.GIT_DIRECT_PUSH}),
            (PublishMode.DIRECT_PUSH, True): frozenset({Capability.GIT_DIRECT_PUSH}),
            PublishMode.AUTO_MERGE: frozenset({Capability.GIT_AUTO_MERGE}),
        },
    ),
    (
        PathRole.RUNTIME_STORE,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: None,
            (PublishMode.DIRECT_PUSH, False): None,
            (PublishMode.DIRECT_PUSH, True): None,
            PublishMode.AUTO_MERGE: None,
        },
    ),
    (
        PathRole.EXTRACTION_PROPOSAL,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: frozenset({Capability.GIT_PULL_REQUEST}),
            (PublishMode.DIRECT_PUSH, False): frozenset({Capability.GIT_DIRECT_PUSH}),
            (PublishMode.DIRECT_PUSH, True): None,
            PublishMode.AUTO_MERGE: None,
        },
    ),
    (
        PathRole.CANONICAL_KNOWLEDGE,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: frozenset({Capability.GIT_PULL_REQUEST}),
            (PublishMode.DIRECT_PUSH, False): frozenset({Capability.GIT_DIRECT_PUSH}),
            (PublishMode.DIRECT_PUSH, True): frozenset(
                {Capability.GIT_DIRECT_PUSH, Capability.KNOWLEDGE_AUTO_PROMOTE}
            ),
            PublishMode.AUTO_MERGE: frozenset(
                {Capability.GIT_AUTO_MERGE, Capability.KNOWLEDGE_AUTO_PROMOTE}
            ),
        },
    ),
    (
        PathRole.UNCLASSIFIED,
        {
            PublishMode.NONE: frozenset(),
            PublishMode.PULL_REQUEST: None,
            (PublishMode.DIRECT_PUSH, False): None,
            (PublishMode.DIRECT_PUSH, True): None,
            PublishMode.AUTO_MERGE: None,
        },
    ),
)


def test_literal_path_role_matrix_is_enforced_for_every_mode() -> None:
    requirements = publishing.required_capabilities

    for role, row in ROLE_MATRIX:
        for mode_key, expected in row.items():
            if isinstance(mode_key, tuple):
                mode, canonical = mode_key
            else:
                mode, canonical = mode_key, False
            assert requirements(role, mode, canonical_target=canonical) == expected


def test_classifier_uses_normalized_paths_and_exact_strict_manifest_entries() -> None:
    manifest_type = publishing.PublicationManifest
    classify = publishing.classify_managed_path
    manifest = manifest_type(
        receipt_sha256="a" * 64,
        paths=(
            {
                "path": ".agents/skills/gold/SKILL.md",
                "role": PathRole.EXPORTED_SKILL,
                "sha256": "1" * 64,
            },
            {
                "path": "ontology/gold/generated/query.ttl",
                "role": PathRole.GENERATED_PROJECTION,
                "sha256": "2" * 64,
            },
            {
                "path": "ontology/gold/candidates/run-1.yaml",
                "role": PathRole.EXTRACTION_PROPOSAL,
                "sha256": "3" * 64,
            },
            {
                "path": "ontology/gold/promotions/run-1.json",
                "role": PathRole.CANONICAL_KNOWLEDGE,
                "sha256": "4" * 64,
            },
            {
                "path": "config/source-policy.yaml",
                "role": PathRole.CANONICAL_KNOWLEDGE,
                "sha256": "5" * 64,
            },
        ),
    )

    assert classify(".agents/skills/geas/SKILL.md", manifests=(manifest,)) is PathRole.GENERIC_SKILL
    for item in manifest.paths:
        assert classify(item.path, manifests=(manifest,)) is item.role
    assert classify("config/operator-notes.yaml", manifests=(manifest,)) is PathRole.UNCLASSIFIED
    assert classify("README.md", manifests=(manifest,)) is PathRole.UNCLASSIFIED


def test_runtime_paths_cannot_be_reclassified_by_a_manifest() -> None:
    manifest_type = publishing.PublicationManifest
    classify = publishing.classify_managed_path
    runtime_paths = (
        "data/records/source.json",
        ".geas/runtime/checkpoint.json",
        "logs/model.jsonl",
        ".env",
        "query.sqlite",
    )

    for index, path in enumerate(runtime_paths):
        manifest = manifest_type(
            receipt_sha256="b" * 64,
            paths=(
                {
                    "path": path,
                    "role": PathRole.GENERATED_PROJECTION,
                    "sha256": f"{index + 1:x}" * 64,
                },
            ),
        )
        assert classify(path, manifests=(manifest,)) is PathRole.RUNTIME_STORE


def test_publish_none_preserves_local_changes_without_git_or_forge_mutation(
    tmp_path: Path,
) -> None:
    _remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_PULL_REQUEST,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.NONE)
    forge = _Forge()
    publisher = _publisher(
        worktree,
        _manifest(worktree, path, PathRole.EXPORTED_SKILL),
        decision,
        forge=forge,
    )
    head = _git(worktree, "rev-parse", "HEAD")

    result = publisher.publish(request)

    assert result.published is False
    assert result.reason == "publication-disabled"
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert (worktree / path).read_text() == "generated\n"
    assert forge.upserts == []


def test_pull_request_retries_converge_and_stage_only_manifest_owned_paths(
    tmp_path: Path,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    (worktree / "operator-notes.txt").write_text("never stage me\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_PULL_REQUEST,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision)
    forge = _Forge()
    publisher = _publisher(
        worktree,
        _manifest(worktree, path, PathRole.EXPORTED_SKILL),
        decision,
        forge=forge,
    )

    first = publisher.publish(request)
    second = publisher.publish(request)

    assert first == second
    assert first.published is True
    assert first.branch is not None and first.branch.startswith("geas/publish/")
    assert first.pull_request_url == "https://github.com/example/gold/pull/7"
    remote_ref = f"refs/heads/{first.branch}"
    assert _git(remote, "rev-parse", remote_ref) == first.commit_sha256
    changed = _git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", remote_ref)
    assert changed.splitlines() == [path]
    assert _git(remote, "show", f"{remote_ref}:{path}") == "generated"
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines() == [
        f"?? {path}",
        "?? operator-notes.txt",
    ]
    assert len(forge.upserts) == 2
    assert forge.upserts[0] == forge.upserts[1]
    assert forge.upserts[0]["head"] == first.branch
    assert request.id in forge.upserts[0]["body"]


def test_pull_request_identity_is_bound_to_the_producing_receipt_hash(
    tmp_path: Path,
) -> None:
    _remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    first_manifest = _manifest(worktree, path, PathRole.EXPORTED_SKILL)
    second_manifest = first_manifest.model_copy(update={"receipt_sha256": "b" * 64})
    decision = _decision(path=path, capabilities=(Capability.GIT_PULL_REQUEST,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision)

    first = _publisher(worktree, first_manifest, decision, forge=_Forge()).publish(request)
    second = _publisher(worktree, second_manifest, decision, forge=_Forge()).publish(request)

    assert first.branch != second.branch
    assert first.commit_sha256 != second.commit_sha256


def test_request_role_claim_cannot_override_manifest_classification(tmp_path: Path) -> None:
    remote, worktree = _repository(tmp_path)
    path = "operator-notes.txt"
    (worktree / path).write_text("operator authored\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_PULL_REQUEST,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision)
    forge = _Forge()
    publisher_type = (
        __import__(
            "research_agent.repository_publisher",
            fromlist=["GitRepositoryPublisher"],
        )
    ).GitRepositoryPublisher
    publisher = publisher_type(
        repository=worktree,
        manifests=(),
        capability_decision=decision,
        forge=forge,
        now=lambda: NOW,
    )
    before = _git(remote, "for-each-ref", "--format=%(refname):%(objectname)")

    with pytest.raises(PermissionError, match="classified"):
        publisher.publish(request)

    assert _git(remote, "for-each-ref", "--format=%(refname):%(objectname)") == before
    assert forge.upserts == []


def test_capability_decision_cannot_authorize_additional_manifest_paths(
    tmp_path: Path,
) -> None:
    remote, worktree = _repository(tmp_path)
    paths = (
        ".agents/skills/gold/SKILL.md",
        ".agents/skills/gold/references/guide.md",
    )
    for path in paths:
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_text(f"{path}\n")
    manifest = PublicationManifest(
        receipt_sha256="a" * 64,
        paths=tuple(
            {
                "path": path,
                "role": PathRole.EXPORTED_SKILL,
                "sha256": hashlib.sha256((worktree / path).read_bytes()).hexdigest(),
            }
            for path in paths
        ),
    )
    decision = _decision(path=paths[0], capabilities=(Capability.GIT_PULL_REQUEST,))
    request = PublishRequest(
        repository=REPOSITORY,
        target_ref="refs/heads/main",
        paths=tuple(PublishPath(path=path, role=PathRole.EXPORTED_SKILL) for path in paths),
        capability_decision_sha256=decision.sha256,
        created_at=NOW,
    )
    forge = _Forge()
    before = _git(remote, "for-each-ref", "--format=%(refname):%(objectname)")

    with pytest.raises(PermissionError, match="exact publication"):
        _publisher(worktree, manifest, decision, forge=forge).publish(request)

    assert _git(remote, "for-each-ref", "--format=%(refname):%(objectname)") == before
    assert forge.upserts == []


@pytest.mark.parametrize(
    ("capabilities", "direct_push", "target_ref", "message"),
    [
        ((Capability.GIT_PULL_REQUEST,), True, "refs/heads/main", "capability"),
        ((Capability.GIT_DIRECT_PUSH,), False, "refs/heads/main", "explicit"),
        ((Capability.GIT_DIRECT_PUSH,), True, "refs/tags/v1", "branch ref"),
    ],
)
def test_direct_push_requires_capability_explicit_flag_and_branch_ref(
    tmp_path: Path,
    capabilities: tuple[Capability, ...],
    direct_push: bool,
    target_ref: str,
    message: str,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    if target_ref.startswith("refs/tags/"):
        _git(worktree, "tag", target_ref.removeprefix("refs/tags/"))
        _git(worktree, "push", "origin", target_ref)
    decision = _decision(path=path, capabilities=capabilities, target_ref=target_ref)
    request = _request(
        path,
        PathRole.EXPORTED_SKILL,
        decision,
        mode=PublishMode.DIRECT_PUSH,
        target_ref=target_ref,
    )
    before = _git(remote, "for-each-ref", "--format=%(refname):%(objectname)")

    with pytest.raises(PermissionError, match=message):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXPORTED_SKILL),
            decision,
            direct_push=direct_push,
        ).publish(request)

    assert _git(remote, "for-each-ref", "--format=%(refname):%(objectname)") == before


def test_direct_push_rejects_dirty_unowned_paths_before_remote_mutation(
    tmp_path: Path,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    (worktree / "operator-notes.txt").write_text("not owned\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.DIRECT_PUSH)
    before = _git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(PermissionError, match="only manifest-owned"):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXPORTED_SKILL),
            decision,
            direct_push=True,
        ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == before


def test_direct_push_requires_head_to_be_the_exact_target_branch(tmp_path: Path) -> None:
    remote, worktree = _repository(tmp_path)
    _git(worktree, "switch", "-c", "topic")
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.DIRECT_PUSH)
    before = _git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(PermissionError, match="exact target branch"):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXPORTED_SKILL),
            decision,
            direct_push=True,
        ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == before


def _advance_remote(tmp_path: Path, remote: Path, name: str = "advancing") -> str:
    advancing = tmp_path / name
    _git(tmp_path, "clone", str(remote), str(advancing))
    (advancing / "advanced.txt").write_text(f"{name}\n")
    _git(advancing, "add", "advanced.txt")
    _git(advancing, "commit", "-m", name)
    _git(advancing, "push", "origin", "main")
    return _git(advancing, "rev-parse", "HEAD")


def test_direct_push_rejects_remote_movement_during_fresh_comparison(tmp_path: Path) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    advanced = _advance_remote(tmp_path, remote)
    decision = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.DIRECT_PUSH)

    with pytest.raises(PermissionError, match="moved"):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXPORTED_SKILL),
            decision,
            direct_push=True,
        ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == advanced


def test_direct_push_uses_exact_lease_and_preserves_commit_on_lease_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.DIRECT_PUSH)
    publisher = _publisher(
        worktree,
        _manifest(worktree, path, PathRole.EXPORTED_SKILL),
        decision,
        direct_push=True,
    )
    original_push = publisher._push
    observed: list[tuple[str, str, str | None]] = []

    def move_then_push(commit: str, ref: str, expected: str | None) -> None:
        observed.append((commit, ref, expected))
        _advance_remote(tmp_path, remote, name="lease-racer")
        original_push(commit, ref, expected)

    monkeypatch.setattr(publisher, "_push", move_then_push)

    with pytest.raises(PermissionError, match=r"exact lease; local commit ([0-9a-f]{40})") as error:
        publisher.publish(request)

    commit = error.value.args[0].split("local commit ", 1)[1].split(";", 1)[0]
    assert _git(worktree, "cat-file", "-t", commit) == "commit"
    assert observed == [(commit, "refs/heads/main", observed[0][2])]
    assert observed[0][2] is not None
    assert (
        f"git push --force-with-lease=refs/heads/main:{observed[0][2]} "
        f"origin {commit}:refs/heads/main"
    ) in str(error.value)


def test_delegated_direct_push_must_be_in_capability_and_delegable_sets(
    tmp_path: Path,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    grant = CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=REPOSITORY,
            refs=("refs/heads/main",),
            paths=(path,),
            bundle_sha256="*",
        ),
        capabilities=(Capability.GIT_DIRECT_PUSH,),
        delegable_capabilities=(),
        resources=CapabilityResources(git_refs=("refs/heads/main",)),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    decision = _decision(
        path=path,
        capabilities=(Capability.GIT_DIRECT_PUSH,),
        grant_ids=(grant.id,),
        delegation_chain=("https://github.com/example/parent",),
    )
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.DIRECT_PUSH)
    before = _git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(PermissionError, match="delegable"):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXPORTED_SKILL),
            decision,
            direct_push=True,
            grants={grant.id: grant},
        ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == before


def test_proposal_cannot_reach_canonical_ref(tmp_path: Path) -> None:
    remote, worktree = _repository(tmp_path)
    path = "ontology/gold/candidates/run-1.yaml"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("proposal: true\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    request = _request(path, PathRole.EXTRACTION_PROPOSAL, decision, mode=PublishMode.DIRECT_PUSH)
    before = _git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(PermissionError, match="forbidden"):
        _publisher(
            worktree,
            _manifest(worktree, path, PathRole.EXTRACTION_PROPOSAL),
            decision,
            direct_push=True,
        ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == before


def test_canonical_semantic_push_requires_both_capabilities_and_promotion_verification(
    tmp_path: Path,
) -> None:
    remote, worktree = _repository(tmp_path)
    path = "ontology/gold/promotions/run-1.json"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text('{"promotion":true}\n')
    manifest = _manifest(worktree, path, PathRole.CANONICAL_KNOWLEDGE)
    insufficient = _decision(path=path, capabilities=(Capability.GIT_DIRECT_PUSH,))
    denied = _request(
        path,
        PathRole.CANONICAL_KNOWLEDGE,
        insufficient,
        mode=PublishMode.DIRECT_PUSH,
    )
    before = _git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(PermissionError, match="capability"):
        _publisher(worktree, manifest, insufficient, direct_push=True).publish(denied)
    assert _git(remote, "rev-parse", "refs/heads/main") == before

    permitted = _decision(
        path=path,
        capabilities=(Capability.GIT_DIRECT_PUSH, Capability.KNOWLEDGE_AUTO_PROMOTE),
    )
    request = _request(path, PathRole.CANONICAL_KNOWLEDGE, permitted, mode=PublishMode.DIRECT_PUSH)
    verifier = _PromotionVerifier()
    result = _publisher(
        worktree,
        manifest,
        permitted,
        direct_push=True,
        promotion_verifier=verifier,
    ).publish(request)

    assert _git(remote, "rev-parse", "refs/heads/main") == result.commit_sha256
    assert len(verifier.calls) == 1
    assert verifier.calls[0]["commit"] == result.commit_sha256
    assert verifier.calls[0]["canonical_ref"] == "refs/heads/main"


def test_artifact_auto_merge_uses_distinct_capability_and_one_forge_request(
    tmp_path: Path,
) -> None:
    _remote, worktree = _repository(tmp_path)
    path = ".agents/skills/gold/SKILL.md"
    (worktree / path).parent.mkdir(parents=True)
    (worktree / path).write_text("generated\n")
    decision = _decision(path=path, capabilities=(Capability.GIT_AUTO_MERGE,))
    request = _request(path, PathRole.EXPORTED_SKILL, decision, mode=PublishMode.AUTO_MERGE)
    forge = _Forge()

    result = _publisher(
        worktree,
        _manifest(worktree, path, PathRole.EXPORTED_SKILL),
        decision,
        forge=forge,
    ).publish(request)

    assert result.reason == "auto-merge-requested"
    assert len(forge.upserts) == 1
    assert forge.auto_merges == [
        {
            "repository": REPOSITORY,
            "head": result.branch,
            "pull_request_url": result.pull_request_url,
        }
    ]
