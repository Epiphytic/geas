"""Repository bootstrap lifecycle contract tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.bootstrap_models import (
    BootstrapPhase,
    ManagedPath,
    RepositoryBootstrapReceipt,
    RepositoryBootstrapRequest,
    RepositoryUpdateJournal,
    VerifiedRepositoryBootstrap,
)
from research_agent.repository_bootstrap import (
    BootstrapOperation,
    RepositoryBootstrapManager,
    repository_trust_grant,
)


def _request(**changes: object) -> RepositoryBootstrapRequest:
    values: dict[str, object] = {
        "name": "example",
        "repository": "https://example.test/ontology.git",
        "ref": "refs/heads/main",
        "catalog": "geas.yaml",
        "commit_sha256": "a" * 40,
        "trust": "read_only",
    }
    values.update(changes)
    return RepositoryBootstrapRequest(**values)


def _verified(**changes: object) -> VerifiedRepositoryBootstrap:
    values: dict[str, object] = {
        "repository": "https://example.test/ontology.git",
        "ref": "refs/heads/main",
        "catalog": "geas.yaml",
        "commit_sha256": "a" * 40,
        "ontology_paths": ("ontology/example",),
        "bundle_sha256": ("b" * 64,),
        "source_hosts": ("news.example.test",),
        "source_path_prefixes": ("/disclosures/",),
        "source_connectors": ("connector:fixture",),
        "delegated_repositories": ("https://example.test/child.git",),
    }
    values.update(changes)
    return VerifiedRepositoryBootstrap(**values)


def _manager(tmp_path: Path, **changes: object) -> RepositoryBootstrapManager:
    values: dict[str, object] = {
        "root": tmp_path / "state",
        "announce": lambda _message: None,
        "now": lambda: datetime(2026, 9, 2, tzinfo=UTC),
        "verify": lambda _request: _verified(),
        "record_trust": lambda _operation, _grant: None,
        "subscribe": lambda _operation: (),
        "replace_subscription": lambda _old, _candidate: (),
        "hydrate_artifacts": lambda _operation: (),
        "install_generic_skill": lambda _operation: (),
        "export_catalog_skills": lambda _operation: (),
        "link_agents": lambda _operation: (),
        "remove_trust": lambda _operation, _grant: None,
        "unsubscribe": lambda _operation: None,
        "remove_skills": lambda _operation: None,
        "remove_obsolete_paths": lambda _operation: None,
        "verify_software_provenance": lambda: None,
    }
    values.update(changes)
    return RepositoryBootstrapManager(**values)  # type: ignore[arg-type]


def test_trust_grant_uses_verified_scope_not_request_claims(tmp_path: Path) -> None:
    """Catches caller-supplied source scopes becoming local trust authority."""
    request = _request(
        trust="trust_repository",
        ontology_paths=("unverified/path",),
        bundle_sha256=("c" * 64,),
        source_hosts=("attacker.example.test",),
        source_path_prefixes=("/widened/",),
        source_connectors=("connector:attacker",),
        delegated_repositories=("https://example.test/attacker.git",),
    )

    grant = repository_trust_grant(
        request,
        verified=_verified(),
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert grant is not None
    assert grant.subject.paths == ("ontology/example",)
    assert grant.subject.bundle_sha256 == ("b" * 64,)
    assert grant.resources.hosts == ("news.example.test",)
    assert grant.resources.connectors == ("connector:fixture",)


def test_trust_repository_rejects_an_incomplete_verified_source_scope(tmp_path: Path) -> None:
    """Catches an empty verified selector becoming an implicit wildcard grant."""
    with pytest.raises(ValueError, match="verified source scope"):
        repository_trust_grant(
            _request(trust="trust_repository"),
            verified=_verified(source_hosts=()),
            created_at=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_install_refuses_to_claim_subscription_or_skill_success_without_dependencies(
    tmp_path: Path,
) -> None:
    """Catches success phases produced by absent subscription or skill services."""
    manager = RepositoryBootstrapManager(
        root=tmp_path / "state",
        announce=lambda _message: None,
        verify=lambda _request: _verified(),
        record_trust=lambda _operation, _grant: None,
    )

    with pytest.raises(ValueError, match="dependency"):
        manager.install(_request())

    assert not (tmp_path / "state").exists()


def test_install_announces_every_mutation_before_first_write(tmp_path: Path) -> None:
    """Catches a bootstrap write occurring before its explicit operator announcement."""
    events: list[str] = []
    manager = _manager(tmp_path, announce=events.append)

    receipt = manager.install(_request())

    assert events[0].startswith("Geas will bind repository")
    assert receipt.completed_phases[0] == BootstrapPhase.VERIFIED
    assert receipt.trust_grant is not None
    assert receipt.trust_grant.capabilities == ("repository.read",)


def test_trust_repository_grant_is_scoped_to_verified_snapshot_without_model_or_git(
    tmp_path: Path,
) -> None:
    """Catches a trust install widening delegated source or publication authority."""
    request = _request(
        trust="trust_repository",
        delegate_depth=1,
        ontology_paths=("ontology/example",),
        bundle_sha256=("b" * 64,),
        source_hosts=("news.example.test",),
        source_path_prefixes=("/disclosures/",),
        source_connectors=("connector:fixture",),
        delegated_repositories=("https://example.test/child.git",),
    )

    grant = repository_trust_grant(
        request,
        verified=_verified(),
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert grant is not None
    assert grant.subject.refs == ("refs/heads/main",)
    assert grant.subject.paths == ("ontology/example",)
    assert grant.resources.hosts == ("news.example.test",)
    assert grant.resources.path_prefixes == ("/disclosures/",)
    assert grant.resources.providers == ()
    assert grant.resources.git_refs == ()
    assert "model.external" not in grant.capabilities
    assert not any(capability.value.startswith("git.") for capability in grant.capabilities)
    assert "trust.delegate" not in grant.delegable_capabilities


def test_install_resumes_after_post_commit_subscription_failure(tmp_path: Path) -> None:
    """Catches an interrupted transaction repeating a committed trust mutation on resume."""
    calls: list[str] = []

    def fail_subscription(_operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        raise RuntimeError("subscription unavailable")

    manager = _manager(
        tmp_path,
        record_trust=lambda _operation, _grant: calls.append("trust"),
        subscribe=fail_subscription,
    )
    with pytest.raises(RuntimeError, match="subscription unavailable"):
        manager.install(_request())

    resumed = _manager(
        tmp_path,
        record_trust=lambda _operation, _grant: calls.append("trust"),
    ).install(_request())

    assert calls == ["trust"]
    assert resumed.completed_phases == (
        BootstrapPhase.VERIFIED,
        BootstrapPhase.TRUST_COMMITTED,
        BootstrapPhase.SUBSCRIBED,
        BootstrapPhase.SKILLS_INSTALLED,
        BootstrapPhase.COMPLETED,
    )


def test_update_rejects_software_provenance_before_bootstrap_receipt_write(tmp_path: Path) -> None:
    """Catches an update making an ontology write before Geas provenance is trusted."""
    manager = _manager(
        tmp_path,
        verify_software_provenance=lambda: (_ for _ in ()).throw(ValueError("untrusted Geas")),
    )

    with pytest.raises(ValueError, match="untrusted Geas"):
        manager.update(_request())

    assert not (tmp_path / "state").exists()


def test_install_requires_an_exact_checkout_verifier_before_writing_receipts(
    tmp_path: Path,
) -> None:
    """Catches a service recording VERIFIED without an exact checkout verification boundary."""
    manager = RepositoryBootstrapManager(root=tmp_path / "state", announce=lambda _message: None)

    with pytest.raises(ValueError, match="verified checkout"):
        manager.install(_request())

    assert not (tmp_path / "state").exists()


def test_remove_refuses_modified_managed_path_without_touching_unrelated_files(
    tmp_path: Path,
) -> None:
    """Catches removal deleting a user-modified owned path or unrelated repository file."""
    root = tmp_path / "state"
    owned = root / ".agents" / "skills" / "example" / "SKILL.md"
    owned.parent.mkdir(parents=True)
    owned.write_text("original\n")
    unrelated = root / "README.md"
    unrelated.write_text("keep\n")
    managed = ManagedPath(
        path=".agents/skills/example/SKILL.md",
        sha256=hashlib.sha256(b"original\n").hexdigest(),
        role="skill",
    )
    manager = _manager(tmp_path, root=root)
    receipt = manager.install(_request())
    journal = root / "repository-bootstrap" / "example.json"
    replacement = receipt.model_copy(update={"managed_paths": (managed,)})
    journal.write_bytes(replacement.model_dump_json().encode())
    owned.write_text("operator changed\n")

    with pytest.raises(ValueError, match="modified"):
        manager.remove(_request())

    assert owned.read_text() == "operator changed\n"
    assert unrelated.read_text() == "keep\n"


def test_remove_preflights_grant_removal_before_any_owned_mutation(tmp_path: Path) -> None:
    """Catches removal deleting skills before discovering its trust remover is unavailable."""
    mutations: list[str] = []
    manager = _manager(
        tmp_path,
        remove_trust=None,
        remove_skills=lambda _operation: mutations.append("skills"),
        unsubscribe=lambda _operation: mutations.append("subscription"),
    )
    manager.install(_request())

    with pytest.raises(ValueError, match="trust removal"):
        manager.remove(_request())

    assert mutations == []


def test_remove_refuses_an_absolute_managed_link_target(tmp_path: Path) -> None:
    """Catches link removal validating only a target hash while escaping the managed root."""
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    link = root / ".agents" / "skills" / "example"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    managed = ManagedPath(
        path=".agents/skills/example",
        role="link",
        sha256=hashlib.sha256(str(outside).encode()).hexdigest(),
    )
    manager = _manager(tmp_path, root=root)
    receipt = manager.install(_request())
    journal = root / "repository-bootstrap" / "example.json"
    journal.write_bytes(
        receipt.model_copy(update={"managed_paths": (managed,)}).model_dump_json().encode()
    )

    with pytest.raises(ValueError, match="target escapes"):
        manager.remove(_request())

    assert outside.read_text() == "keep\n"


def test_update_journal_binds_complete_old_ownership_and_candidate_identity(tmp_path: Path) -> None:
    """Catches an update transaction clearing receipt-owned paths before replacement commits."""
    manager = _manager(tmp_path)
    old = manager.install(_request())
    candidate = _verified(commit_sha256="c" * 40)
    journal = RepositoryUpdateJournal(
        old_receipt_sha256=old.id.rsplit(":", 1)[-1],
        old_request=old.request,
        old_managed_paths=old.managed_paths,
        old_grant=old.trust_grant,
        candidate_request=_request(commit_sha256="c" * 40),
        candidate_verified=candidate,
        candidate_grant=old.trust_grant,
        phase=BootstrapPhase.VERIFIED,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert journal.old_managed_paths == old.managed_paths


def test_update_journal_rejects_candidate_worktree_identity_mismatch(tmp_path: Path) -> None:
    """Catches a persisted candidate binding a different checkout than its request."""
    request = _request(current_worktree=(tmp_path / "requested").resolve())
    with pytest.raises(ValueError, match="candidate verified identity"):
        RepositoryUpdateJournal(
            old_receipt_sha256="d" * 64,
            old_request=request,
            old_managed_paths=(),
            candidate_request=request,
            candidate_verified=_verified(current_worktree=(tmp_path / "different").resolve()),
            phase=BootstrapPhase.VERIFIED,
            created_at=datetime(2026, 9, 2, tzinfo=UTC),
            updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        )


def _written_path(root: Path, relative: str, content: str, role: str) -> ManagedPath:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return ManagedPath(
        path=relative,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        role=role,
    )


def test_interrupted_update_keeps_old_receipt_and_reloads_verified_candidate(
    tmp_path: Path,
) -> None:
    """Catches replacement ownership becoming authoritative before all callbacks finish."""
    root = tmp_path / "state"
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)
    verification_scopes = {"candidate": ("news.example.test",)}

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(
            commit_sha256=request.commit_sha256,
            source_hosts=verification_scopes["candidate"],
        )

    def subscribe(operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        suffix = operation.request.commit_sha256[0]
        return (_written_path(root, f"subscriptions/{suffix}.json", suffix, "manifest"),)

    old = _manager(tmp_path, root=root, verify=verify, subscribe=subscribe).install(old_request)

    def interrupted_export(_operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        raise RuntimeError("stop after candidate subscription")

    with pytest.raises(RuntimeError, match="candidate subscription"):
        _manager(
            tmp_path,
            root=root,
            verify=verify,
            subscribe=subscribe,
            export_catalog_skills=interrupted_export,
        ).update(candidate_request)

    receipt_path = root / "repository-bootstrap" / "example.json"
    persisted = RepositoryBootstrapReceipt.model_validate_json(receipt_path.read_bytes())
    assert persisted.id == old.id
    assert persisted.request == old_request
    assert persisted.managed_paths == old.managed_paths

    verification_scopes["candidate"] = ("changed.example.test",)
    with pytest.raises(ValueError, match="candidate verification changed"):
        _manager(tmp_path, root=root, verify=verify, subscribe=subscribe).update(
            candidate_request
        )

    assert RepositoryBootstrapReceipt.model_validate_json(receipt_path.read_bytes()).id == old.id


def test_update_reconciles_obsolete_paths_and_subscription_without_unioning_ownership(
    tmp_path: Path,
) -> None:
    """Catches obsolete snapshot ownership surviving a completed replacement."""
    root = tmp_path / "state"
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)
    subscription_replacements: list[tuple[str, str, str]] = []
    obsolete_removals: list[tuple[str, ...]] = []

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    def subscribe(operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        suffix = operation.request.commit_sha256[0]
        return (_written_path(root, f"subscriptions/{suffix}.json", suffix, "manifest"),)

    def hydrate(operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        suffix = operation.request.commit_sha256[0]
        return (_written_path(root, f"snapshots/{suffix}.json", suffix, "snapshot"),)

    def replace_subscription(
        old: BootstrapOperation, candidate: BootstrapOperation
    ) -> tuple[ManagedPath, ...]:
        subscription_replacements.append(
            (
                old.request.commit_sha256,
                candidate.request.commit_sha256,
                candidate.idempotency_key,
            )
        )
        return subscribe(candidate)

    def remove_obsolete(operation: BootstrapOperation) -> None:
        obsolete_removals.append(tuple(item.path for item in operation.owned_paths))
        for item in operation.owned_paths:
            (root / item.path).unlink()

    manager = _manager(
        tmp_path,
        root=root,
        verify=verify,
        subscribe=subscribe,
        hydrate_artifacts=hydrate,
    )
    old = manager.install(old_request)
    updated = _manager(
        tmp_path,
        root=root,
        verify=verify,
        subscribe=subscribe,
        hydrate_artifacts=hydrate,
        replace_subscription=replace_subscription,
        remove_obsolete_paths=remove_obsolete,
    ).update(candidate_request)

    assert subscription_replacements == [("a" * 40, "c" * 40, subscription_replacements[0][2])]
    assert obsolete_removals == [tuple(item.path for item in old.managed_paths)]
    assert {item.path for item in updated.managed_paths} == {
        "snapshots/c.json",
        "subscriptions/c.json",
    }
    assert not any((root / item.path).exists() for item in old.managed_paths)


@pytest.mark.parametrize(
    "interrupted_step",
    ("trust", "subscription", "hydrate", "generic", "export", "link", "obsolete"),
)
def test_update_resumes_every_mutation_with_stable_idempotency_keys(
    tmp_path: Path, interrupted_step: str
) -> None:
    """Catches restart replay using a new key or repeating a semantic mutation."""
    root = tmp_path / "state"
    old_request = _request(
        trust="trust_repository",
        ontology_paths=("ontology/example",),
        bundle_sha256=("b" * 64,),
        source_hosts=("news.example.test",),
        source_path_prefixes=("/disclosures/",),
        source_connectors=("connector:fixture",),
        delegated_repositories=("https://example.test/child.git",),
    )
    candidate_request = _request(commit_sha256="c" * 40, trust="read_only")
    invocations: dict[str, list[str]] = {}
    semantic_applications: dict[str, set[str]] = {}
    should_interrupt = {interrupted_step: True}

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    def apply(step: str, key: str) -> None:
        invocations.setdefault(step, []).append(key)
        applied = semantic_applications.setdefault(step, set())
        first_application = key not in applied
        applied.add(key)
        if first_application and should_interrupt.pop(step, False):
            raise RuntimeError(f"interrupted {step}")

    def produced(operation: BootstrapOperation, step: str) -> tuple[ManagedPath, ...]:
        apply(step, operation.idempotency_key)
        suffix = operation.request.commit_sha256[0]
        return (_written_path(root, f"generated/{step}-{suffix}", step, "skill"),)

    def replace_trust(
        operation: BootstrapOperation, _old: object, _new: object
    ) -> None:
        apply("trust", operation.idempotency_key)

    def replace_subscription(
        _old: BootstrapOperation, candidate: BootstrapOperation
    ) -> tuple[ManagedPath, ...]:
        return produced(candidate, "subscription")

    def remove_obsolete(operation: BootstrapOperation) -> None:
        apply("obsolete", operation.idempotency_key)
        for item in operation.owned_paths:
            path = root / item.path
            if path.exists() and not path.is_symlink():
                path.unlink()

    install_manager = _manager(
        tmp_path,
        root=root,
        verify=verify,
        subscribe=lambda operation: produced(operation, "install-subscription"),
        hydrate_artifacts=lambda operation: produced(operation, "install-hydrate"),
        install_generic_skill=lambda operation: produced(operation, "install-generic"),
        export_catalog_skills=lambda operation: produced(operation, "install-export"),
        link_agents=lambda operation: produced(operation, "install-link"),
    )
    install_manager.install(old_request)

    update_adapters: dict[str, object] = {
        "replace_trust": replace_trust,
        "replace_subscription": replace_subscription,
        "remove_obsolete_paths": remove_obsolete,
        "hydrate_artifacts": lambda operation: produced(operation, "hydrate"),
        "install_generic_skill": lambda operation: produced(operation, "generic"),
        "export_catalog_skills": lambda operation: produced(operation, "export"),
        "link_agents": lambda operation: produced(operation, "link"),
    }
    with pytest.raises(RuntimeError, match=f"interrupted {interrupted_step}"):
        _manager(tmp_path, root=root, verify=verify, **update_adapters).update(
            candidate_request
        )

    receipt_path = root / "repository-bootstrap" / "example.json"
    persisted = RepositoryBootstrapReceipt.model_validate_json(receipt_path.read_bytes())
    assert persisted.request == old_request

    resumed = _manager(tmp_path, root=root, verify=verify, **update_adapters).update(
        candidate_request
    )

    keys = invocations[interrupted_step]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    update_steps = {
        "trust",
        "subscription",
        "hydrate",
        "generic",
        "export",
        "link",
        "obsolete",
    }
    assert all(len(semantic_applications[step]) == 1 for step in update_steps)
    assert all(len(set(invocations[step])) == 1 for step in update_steps)
    assert len({next(iter(semantic_applications[step])) for step in update_steps}) == 7
    assert resumed.request == candidate_request
    assert not (root / "repository-bootstrap" / "example.update.json").exists()


def test_update_with_unchanged_grant_performs_no_trust_mutation(tmp_path: Path) -> None:
    """Catches update re-recording or replacing a byte-identical trust grant."""
    trust_calls: list[str] = []
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    manager = _manager(
        tmp_path,
        verify=verify,
        record_trust=lambda _operation, _grant: trust_calls.append("record"),
        replace_trust=lambda _operation, _old, _new: trust_calls.append("replace"),
        replace_subscription=lambda _old, _new: (),
        remove_obsolete_paths=lambda _operation: None,
    )
    manager.install(old_request)
    manager.update(candidate_request)

    assert trust_calls == ["record"]


def test_update_preflights_changed_trust_adapter_before_creating_journal(
    tmp_path: Path,
) -> None:
    """Catches a changed grant reaching durable update intent without atomic replacement."""
    old_request = _request(
        trust="trust_repository",
        ontology_paths=("ontology/example",),
        bundle_sha256=("b" * 64,),
        source_hosts=("news.example.test",),
        source_path_prefixes=("/disclosures/",),
        source_connectors=("connector:fixture",),
        delegated_repositories=("https://example.test/child.git",),
    )
    candidate_request = _request(commit_sha256="c" * 40, trust="read_only")

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    manager = _manager(tmp_path, verify=verify)
    manager.install(old_request)

    with pytest.raises(ValueError, match="trust replacement"):
        _manager(tmp_path, verify=verify, replace_trust=None).update(candidate_request)

    assert not (
        tmp_path / "state" / "repository-bootstrap" / "example.update.json"
    ).exists()


def test_update_recovers_when_final_receipt_was_installed_before_journal_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a crash after atomic receipt replacement making the transaction unrecoverable."""
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    manager = _manager(tmp_path, verify=verify)
    manager.install(old_request)
    monkeypatch.setattr(
        manager,
        "_remove_update_journal",
        lambda _name: (_ for _ in ()).throw(RuntimeError("stop after receipt")),
    )
    with pytest.raises(RuntimeError, match="stop after receipt"):
        manager.update(candidate_request)

    receipt_path = tmp_path / "state" / "repository-bootstrap" / "example.json"
    assert RepositoryBootstrapReceipt.model_validate_json(
        receipt_path.read_bytes()
    ).request == candidate_request

    recovered = _manager(tmp_path, verify=verify).update(candidate_request)

    assert recovered.request == candidate_request
    assert not receipt_path.with_name("example.update.json").exists()


def test_remove_refuses_to_cross_an_active_update_transaction(tmp_path: Path) -> None:
    """Catches removal mutating old ownership while candidate replacement is resumable."""
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)
    mutations: list[str] = []

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    manager = _manager(tmp_path, verify=verify)
    manager.install(old_request)
    with pytest.raises(RuntimeError, match="interrupt update"):
        _manager(
            tmp_path,
            verify=verify,
            hydrate_artifacts=lambda _operation: (_ for _ in ()).throw(
                RuntimeError("interrupt update")
            ),
        ).update(candidate_request)

    with pytest.raises(ValueError, match="update transaction is active"):
        _manager(
            tmp_path,
            verify=verify,
            remove_skills=lambda _operation: mutations.append("skills"),
            unsubscribe=lambda _operation: mutations.append("subscription"),
            remove_trust=lambda _operation, _grant: mutations.append("trust"),
        ).remove(old_request)

    assert mutations == []


def test_update_refuses_a_broken_symbolic_link_at_the_journal_path(tmp_path: Path) -> None:
    """Catches atomic journal creation replacing an ambiguous pre-existing link."""
    old_request = _request()
    candidate_request = _request(commit_sha256="c" * 40)

    def verify(request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        return _verified(commit_sha256=request.commit_sha256)

    manager = _manager(tmp_path, verify=verify)
    manager.install(old_request)
    journal_path = tmp_path / "state" / "repository-bootstrap" / "example.update.json"
    journal_path.symlink_to(tmp_path / "missing-outside-journal")

    with pytest.raises(ValueError, match="journal must be a regular file"):
        manager.update(candidate_request)

    assert journal_path.is_symlink()


def test_resumed_remove_rechecks_changed_link_after_pending_receipt(tmp_path: Path) -> None:
    """Catches resumed removal trusting a link checked before removal intent was durable."""
    root = tmp_path / "state"
    target = root / ".agents" / "skills" / "snapshot"
    target.parent.mkdir(parents=True)
    target.write_text("owned\n")
    link = root / ".agents" / "skills" / "example"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("snapshot")
    managed = ManagedPath(
        path=".agents/skills/example",
        role="link",
        sha256=hashlib.sha256(b"snapshot").hexdigest(),
    )
    manager = _manager(
        tmp_path,
        root=root,
        remove_skills=lambda _operation: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    installed = manager.install(_request())
    receipt_path = root / "repository-bootstrap" / "example.json"
    receipt_path.write_bytes(
        installed.model_copy(update={"managed_paths": (managed,)}).model_dump_json().encode()
    )
    with pytest.raises(RuntimeError, match="stop"):
        manager.remove(_request())

    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    link.unlink()
    link.symlink_to(outside)
    mutations: list[str] = []
    with pytest.raises(ValueError, match="target escapes"):
        _manager(
            tmp_path,
            root=root,
            remove_skills=lambda _operation: mutations.append("skills"),
            unsubscribe=lambda _operation: mutations.append("subscription"),
            remove_trust=lambda _operation, _grant: mutations.append("trust"),
        ).remove(_request())

    assert mutations == []
    assert outside.read_text() == "keep\n"


def test_repeated_remove_returns_completed_receipt_without_replaying_mutations(
    tmp_path: Path,
) -> None:
    """Catches a retry of a completed removal replaying destructive adapters."""
    mutations: list[str] = []
    manager = _manager(
        tmp_path,
        remove_skills=lambda _operation: mutations.append("skills"),
        unsubscribe=lambda _operation: mutations.append("subscription"),
        remove_trust=lambda _operation, _grant: mutations.append("trust"),
    )
    manager.install(_request())
    first = manager.remove(_request())
    second = _manager(
        tmp_path,
        remove_skills=lambda _operation: mutations.append("skills"),
        unsubscribe=lambda _operation: mutations.append("subscription"),
        remove_trust=lambda _operation, _grant: mutations.append("trust"),
    ).remove(_request())

    assert second == first
    assert mutations == ["skills", "subscription", "trust"]
