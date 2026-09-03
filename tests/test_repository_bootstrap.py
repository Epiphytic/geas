"""Repository bootstrap lifecycle contract tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.bootstrap_models import (
    BootstrapPhase,
    ManagedPath,
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
        "hydrate_artifacts": lambda _operation: (),
        "install_generic_skill": lambda _operation: (),
        "export_catalog_skills": lambda _operation: (),
        "link_agents": lambda _operation: (),
        "remove_trust": lambda _operation, _grant: None,
        "unsubscribe": lambda _operation: None,
        "remove_skills": lambda _operation: None,
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
    link.parent.mkdir(parents=True)
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
    )

    assert journal.old_managed_paths == old.managed_paths
