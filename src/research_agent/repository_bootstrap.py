"""Fail-closed, resumable repository-agent bootstrap lifecycle."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from research_agent.bootstrap_models import (
    BootstrapPhase,
    ManagedPath,
    RepositoryBootstrapReceipt,
    RepositoryBootstrapRequest,
    RepositoryUpdateJournal,
    VerifiedRepositoryBootstrap,
)
from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResources,
    CapabilitySubject,
)
from research_agent.models import canonical_json, content_id, utc_now

_PHASES = (
    BootstrapPhase.VERIFIED,
    BootstrapPhase.TRUST_COMMITTED,
    BootstrapPhase.SUBSCRIBED,
    BootstrapPhase.SKILLS_INSTALLED,
    BootstrapPhase.COMPLETED,
)
_SOURCE_CAPABILITIES = (
    Capability.SOURCE_ARCHIVE,
    Capability.SOURCE_DISCOVER,
    Capability.SOURCE_EXTRACT,
    Capability.SOURCE_FETCH,
)


@dataclass(frozen=True)
class BootstrapOperation:
    """Immutable input to one idempotent external mutation adapter."""

    request: RepositoryBootstrapRequest
    verified: VerifiedRepositoryBootstrap
    phase: BootstrapPhase
    idempotency_key: str
    owned_paths: tuple[ManagedPath, ...] = ()


def repository_trust_grant(
    request: RepositoryBootstrapRequest,
    *,
    verified: VerifiedRepositoryBootstrap,
    created_at: datetime,
) -> CapabilityGrant | None:
    """Build authority exclusively from an exact verified repository snapshot."""
    _verify_identity(request, verified)
    if request.trust == "none":
        return None
    if request.trust == "read_only":
        capabilities = (Capability.REPOSITORY_READ,)
        delegable: tuple[Capability, ...] = ()
    else:
        if not (
            verified.source_hosts and verified.source_path_prefixes and verified.source_connectors
        ):
            raise ValueError("verified source scope must be complete and non-empty")
        capabilities = (
            Capability.REPOSITORY_READ,
            Capability.SOURCE_ARCHIVE,
            Capability.SOURCE_DISCOVER,
            Capability.SOURCE_EXTRACT,
            Capability.SOURCE_FETCH,
            Capability.TRUST_DELEGATE,
        )
        delegable = (
            Capability.REPOSITORY_READ,
            *_SOURCE_CAPABILITIES,
            *((Capability.TRUST_DELEGATE,) if request.delegate_depth > 1 else ()),
        )
    return CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=verified.repository,
            refs=(verified.ref,),
            paths=verified.ontology_paths,
            bundle_sha256=verified.bundle_sha256,
        ),
        capabilities=capabilities,
        delegable_capabilities=delegable,
        resources=CapabilityResources(
            delegated_repositories=verified.delegated_repositories,
            hosts=verified.source_hosts,
            path_prefixes=verified.source_path_prefixes,
            connectors=verified.source_connectors,
        ),
        max_delegation_depth=request.delegate_depth,
        expires_at=None,
        created_at=created_at,
        created_via="repository_install",
    )


class RepositoryBootstrapManager:
    """Compose explicit, idempotent lifecycle dependencies with an fsynced journal."""

    def __init__(
        self,
        *,
        root: Path,
        announce: Callable[[str], None],
        now: Callable[[], datetime] = utc_now,
        verify: Callable[[RepositoryBootstrapRequest], VerifiedRepositoryBootstrap] | None = None,
        record_trust: Callable[[BootstrapOperation, CapabilityGrant], None] | None = None,
        replace_trust: Callable[[BootstrapOperation, CapabilityGrant, CapabilityGrant], None]
        | None = None,
        subscribe: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        hydrate_artifacts: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        install_generic_skill: Callable[[BootstrapOperation], tuple[ManagedPath, ...]]
        | None = None,
        export_catalog_skills: Callable[[BootstrapOperation], tuple[ManagedPath, ...]]
        | None = None,
        link_agents: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        remove_trust: Callable[[BootstrapOperation, CapabilityGrant], None] | None = None,
        unsubscribe: Callable[[BootstrapOperation], None] | None = None,
        remove_skills: Callable[[BootstrapOperation], None] | None = None,
        verify_software_provenance: Callable[[], None] | None = None,
    ) -> None:
        self.root = Path(os.path.abspath(os.fspath(root.expanduser())))
        self.announce, self.now, self.verify = announce, now, verify
        self.record_trust, self.replace_trust = record_trust, replace_trust
        self.subscribe, self.hydrate_artifacts = subscribe, hydrate_artifacts
        self.install_generic_skill, self.export_catalog_skills = (
            install_generic_skill,
            export_catalog_skills,
        )
        self.link_agents, self.remove_trust = link_agents, remove_trust
        self.unsubscribe, self.remove_skills = unsubscribe, remove_skills
        self.verify_software_provenance = verify_software_provenance

    def install(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt:
        self._announce_install(request)
        self._require_install_dependencies()
        verified = self._verified(request)
        existing = self._load(request.name)
        if existing is not None and not existing.removed:
            if existing.request != request or existing.verified != verified:
                raise ValueError("repository bootstrap name is already owned by another snapshot")
            return self._resume(existing, verified)
        now = self.now()
        return self._resume(
            RepositoryBootstrapReceipt(
                request=request, verified=verified, created_at=now, updated_at=now
            ),
            verified,
        )

    def update(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt:
        self._announce_update(request)
        self._require_install_dependencies()
        if self.verify_software_provenance is None:
            raise ValueError("repository bootstrap dependency is missing: software provenance")
        self.verify_software_provenance()
        candidate = self._verified(request)
        existing = self._load(request.name)
        if existing is None or existing.removed or existing.verified is None:
            raise ValueError("unknown active repository bootstrap")
        old_grant = existing.trust_grant
        new_grant = repository_trust_grant(
            request, verified=candidate, created_at=existing.created_at
        )
        if not _grant_narrows(old_grant, new_grant):
            raise ValueError("repository update expands the existing owned trust scope")
        journal = RepositoryUpdateJournal(
            old_receipt_sha256=existing.id.rsplit(":", 1)[-1],
            old_request=existing.request,
            old_managed_paths=existing.managed_paths,
            old_grant=old_grant,
            candidate_request=request,
            candidate_verified=candidate,
            candidate_grant=new_grant,
            phase=BootstrapPhase.VERIFIED,
            created_at=self.now(),
        )
        self._write_update(journal)
        if old_grant != new_grant:
            if self.replace_trust is None or old_grant is None or new_grant is None:
                raise ValueError(
                    "repository bootstrap dependency is missing: atomic trust replacement"
                )
            intent = existing.model_copy(
                update={"update_candidate": candidate, "updated_at": self.now()}
            )
            self._write(intent)
            self.replace_trust(
                self._operation(intent, candidate, BootstrapPhase.TRUST_COMMITTED),
                old_grant,
                new_grant,
            )
        replacement = existing.model_copy(
            update={
                "request": request,
                "verified": candidate,
                "completed_phases": (
                    BootstrapPhase.VERIFIED,
                    BootstrapPhase.TRUST_COMMITTED,
                ),
                "pending_phase": None,
                "update_candidate": None,
                "trust_grant": new_grant,
                "managed_paths": existing.managed_paths,
                "updated_at": self.now(),
            }
        )
        self._write(replacement)
        result = self._resume(replacement, candidate)
        update_path = self._update_path(request.name)
        if update_path.exists() and not update_path.is_symlink():
            update_path.unlink()
        return result

    def remove(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt:
        self.announce(f"Geas will remove only managed bootstrap paths for {request.name}.")
        self._require_removal_dependencies()
        receipt = self._load(request.name)
        if receipt is None or receipt.removed or receipt.verified is None:
            raise ValueError("unknown active repository bootstrap")
        if receipt.request != request:
            raise ValueError(
                "repository bootstrap removal request does not match ownership receipt"
            )
        if receipt.trust_grant is not None and self.remove_trust is None:
            raise ValueError("repository bootstrap dependency is missing: trust removal")
        operation = self._operation(receipt, receipt.verified, BootstrapPhase.COMPLETED)
        if not receipt.removal_pending:
            self._assert_owned_paths(receipt.managed_paths)
            receipt = receipt.model_copy(update={"removal_pending": True, "updated_at": self.now()})
            self._write(receipt)
        assert self.remove_skills is not None and self.unsubscribe is not None
        self.remove_skills(operation)
        self.unsubscribe(operation)
        if receipt.trust_grant is not None:
            assert self.remove_trust is not None
            self.remove_trust(operation, receipt.trust_grant)
        removed = receipt.model_copy(
            update={
                "removal_pending": False,
                "removed": True,
                "managed_paths": (),
                "trust_grant": None,
                "updated_at": self.now(),
            }
        )
        self._write(removed)
        return removed

    def _resume(
        self, receipt: RepositoryBootstrapReceipt, verified: VerifiedRepositoryBootstrap
    ) -> RepositoryBootstrapReceipt:
        if receipt.verified != verified:
            raise ValueError("repository checkout identity changed during bootstrap resumption")
        for phase in _PHASES:
            if phase in receipt.completed_phases:
                continue
            receipt = self._prepare(receipt, phase)
            operation = self._operation(receipt, verified, phase)
            produced: tuple[ManagedPath, ...] = ()
            if phase is BootstrapPhase.TRUST_COMMITTED:
                grant = repository_trust_grant(
                    receipt.request, verified=verified, created_at=receipt.created_at
                )
                if grant is not None:
                    assert self.record_trust is not None
                    self.record_trust(operation, grant)
                receipt = receipt.model_copy(update={"trust_grant": grant})
            elif phase is BootstrapPhase.SUBSCRIBED:
                assert self.subscribe is not None
                produced = self.subscribe(operation)
            elif phase is BootstrapPhase.SKILLS_INSTALLED:
                assert self.hydrate_artifacts is not None and self.install_generic_skill is not None
                assert self.export_catalog_skills is not None and self.link_agents is not None
                produced = (
                    self.hydrate_artifacts(operation)
                    + self.install_generic_skill(operation)
                    + self.export_catalog_skills(operation)
                    + self.link_agents(operation)
                )
            receipt = self._commit(receipt, phase, produced)
        return receipt

    def _prepare(
        self, receipt: RepositoryBootstrapReceipt, phase: BootstrapPhase
    ) -> RepositoryBootstrapReceipt:
        if receipt.pending_phase not in {None, phase}:
            raise ValueError("repository bootstrap has a conflicting pending phase")
        prepared = receipt.model_copy(update={"pending_phase": phase, "updated_at": self.now()})
        self._write(prepared)
        return prepared

    def _commit(
        self,
        receipt: RepositoryBootstrapReceipt,
        phase: BootstrapPhase,
        produced: tuple[ManagedPath, ...],
    ) -> RepositoryBootstrapReceipt:
        if receipt.pending_phase != phase:
            raise ValueError("repository bootstrap phase intent is missing")
        paths = {item.path: item for item in (*receipt.managed_paths, *produced)}
        committed = receipt.model_copy(
            update={
                "completed_phases": (*receipt.completed_phases, phase),
                "pending_phase": None,
                "managed_paths": tuple(paths[path] for path in sorted(paths)),
                "updated_at": self.now(),
            }
        )
        self._write(committed)
        return committed

    def _verified(self, request: RepositoryBootstrapRequest) -> VerifiedRepositoryBootstrap:
        if self.verify is None:
            raise ValueError("repository bootstrap dependency is missing: verified checkout")
        verified = self.verify(request)
        _verify_identity(request, verified)
        return verified

    def _operation(
        self,
        receipt: RepositoryBootstrapReceipt,
        verified: VerifiedRepositoryBootstrap,
        phase: BootstrapPhase,
    ) -> BootstrapOperation:
        key = content_id(
            "repository-bootstrap-operation",
            {
                "request": receipt.request.model_dump(mode="json"),
                "verified": verified.id,
                "phase": phase.value,
            },
        )
        return BootstrapOperation(
            request=receipt.request,
            verified=verified,
            phase=phase,
            idempotency_key=key,
            owned_paths=receipt.managed_paths,
        )

    def _require_install_dependencies(self) -> None:
        required = {
            "verified checkout": self.verify,
            "trust recorder": self.record_trust,
            "subscription": self.subscribe,
            "artifact hydration": self.hydrate_artifacts,
            "generic skill installation": self.install_generic_skill,
            "catalog skill export": self.export_catalog_skills,
            "agent linking": self.link_agents,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"repository bootstrap dependency is missing: {', '.join(missing)}")

    def _require_removal_dependencies(self) -> None:
        required = {"skill removal": self.remove_skills, "subscription removal": self.unsubscribe}
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"repository bootstrap dependency is missing: {', '.join(missing)}")

    def _announce_install(self, request: RepositoryBootstrapRequest) -> None:
        self.announce(
            "Geas will bind repository "
            f"{request.repository} at {request.ref} ({request.commit_sha256}) "
            "and write its receipt."
        )

    def _announce_update(self, request: RepositoryBootstrapRequest) -> None:
        self.announce(f"Geas will verify software provenance before updating {request.name}.")

    def _receipt_path(self, name: str) -> Path:
        return self.root / "repository-bootstrap" / f"{name}.json"

    def _update_path(self, name: str) -> Path:
        return self.root / "repository-bootstrap" / f"{name}.update.json"

    def _write_update(self, journal: RepositoryUpdateJournal) -> None:
        path = self._update_path(journal.candidate_request.name)
        _reject_symlink_ancestry(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_json(journal.model_dump(mode="json")) + b"\n"
        temporary = path.with_name(f".{path.name}.{hashlib.sha256(data).hexdigest()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _load(self, name: str) -> RepositoryBootstrapReceipt | None:
        path = self._receipt_path(name)
        if not path.exists():
            return None
        _reject_symlink_ancestry(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("repository bootstrap receipt must be a regular file")
        try:
            return RepositoryBootstrapReceipt.model_validate_json(path.read_bytes())
        except Exception as error:
            raise ValueError("repository bootstrap receipt is invalid") from error

    def _write(self, receipt: RepositoryBootstrapReceipt) -> None:
        path = self._receipt_path(receipt.request.name)
        _reject_symlink_ancestry(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestry(path.parent)
        if path.is_symlink():
            raise ValueError("repository bootstrap receipt must not be a symbolic link")
        data = canonical_json(receipt.model_dump(mode="json")) + b"\n"
        temporary = path.with_name(f".{path.name}.{hashlib.sha256(data).hexdigest()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _assert_owned_paths(self, managed: tuple[ManagedPath, ...]) -> None:
        for item in managed:
            candidate = _confined_owned_path(self.root, item.path)
            if item.role == "link":
                if not candidate.is_symlink():
                    raise ValueError(f"managed link is missing or unsafe: {item.path}")
                target = os.readlink(candidate)
                target_path = Path(target)
                if target_path.is_absolute() or ".." in target_path.parts:
                    raise ValueError(f"managed link target escapes bootstrap root: {item.path}")
                lexical_target = candidate.parent / target_path
                _reject_symlink_ancestry(lexical_target.parent)
                try:
                    lexical_target.resolve(strict=False).relative_to(
                        self.root.resolve(strict=False)
                    )
                except ValueError as error:
                    raise ValueError(
                        f"managed link target escapes bootstrap root: {item.path}"
                    ) from error
                if hashlib.sha256(target.encode()).hexdigest() != item.sha256:
                    raise ValueError(f"managed link was modified: {item.path}")
            elif candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"managed path is missing or unsafe: {item.path}")
            elif hashlib.sha256(candidate.read_bytes()).hexdigest() != item.sha256:
                raise ValueError(f"managed path was modified: {item.path}")


def _verify_identity(
    request: RepositoryBootstrapRequest, verified: VerifiedRepositoryBootstrap
) -> None:
    if any(
        getattr(request, item) != getattr(verified, item)
        for item in ("repository", "ref", "catalog", "commit_sha256")
    ):
        raise ValueError("verified repository snapshot does not match the requested identity")
    if (
        request.current_worktree is not None
        and request.current_worktree != verified.current_worktree
    ):
        raise ValueError("verified current worktree does not match the requested worktree")


def _grant_narrows(old: CapabilityGrant | None, new: CapabilityGrant | None) -> bool:
    if old is None:
        return new is None
    if new is None:
        return True
    if old.subject.repository != new.subject.repository:
        return False
    for before, after in (
        (old.subject.refs, new.subject.refs),
        (old.subject.paths, new.subject.paths),
        (old.subject.bundle_sha256, new.subject.bundle_sha256),
        (old.resources.delegated_repositories, new.resources.delegated_repositories),
        (old.resources.hosts, new.resources.hosts),
        (old.resources.path_prefixes, new.resources.path_prefixes),
        (old.resources.connectors, new.resources.connectors),
    ):
        if before == "*" or after == "*" or not set(after).issubset(before):
            return False
    return (
        set(new.capabilities).issubset(old.capabilities)
        and set(new.delegable_capabilities).issubset(old.delegable_capabilities)
        and new.max_delegation_depth <= old.max_delegation_depth
    )


def _reject_symlink_ancestry(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("managed path must not traverse symbolic links")


def _confined_owned_path(root: Path, relative: str) -> Path:
    candidate = root / relative
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("managed path escapes bootstrap root") from error
    _reject_symlink_ancestry(candidate.parent)
    return candidate
