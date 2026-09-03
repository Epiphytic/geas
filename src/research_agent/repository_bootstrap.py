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
    RepositoryRemovalPhase,
    RepositoryUpdateEffect,
    RepositoryUpdateEffectReceipt,
    RepositoryUpdateJournal,
    RepositoryUpdatePhase,
    VerifiedRepositoryBootstrap,
    repository_update_operation_id,
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
_UPDATE_PHASES = tuple(RepositoryUpdatePhase)
_UPDATE_PHASE_INDEX = {phase: index for index, phase in enumerate(_UPDATE_PHASES)}


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
        replace_trust: Callable[
            [BootstrapOperation, CapabilityGrant | None, CapabilityGrant | None], None
        ]
        | None = None,
        subscribe: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        replace_subscription: Callable[
            [BootstrapOperation, BootstrapOperation], tuple[ManagedPath, ...]
        ]
        | None = None,
        hydrate_artifacts: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        install_generic_skill: Callable[[BootstrapOperation], tuple[ManagedPath, ...]]
        | None = None,
        export_catalog_skills: Callable[[BootstrapOperation], tuple[ManagedPath, ...]]
        | None = None,
        link_agents: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None = None,
        remove_trust: Callable[[BootstrapOperation, CapabilityGrant], None] | None = None,
        unsubscribe: Callable[[BootstrapOperation], None] | None = None,
        remove_skills: Callable[[BootstrapOperation], None] | None = None,
        remove_obsolete_paths: Callable[[BootstrapOperation], None] | None = None,
        verify_software_provenance: Callable[[], None] | None = None,
    ) -> None:
        self.root = Path(os.path.abspath(os.fspath(root.expanduser())))
        self.announce, self.now, self.verify = announce, now, verify
        self.record_trust, self.replace_trust = record_trust, replace_trust
        self.subscribe, self.replace_subscription = subscribe, replace_subscription
        self.hydrate_artifacts = hydrate_artifacts
        self.install_generic_skill, self.export_catalog_skills = (
            install_generic_skill,
            export_catalog_skills,
        )
        self.link_agents, self.remove_trust = link_agents, remove_trust
        self.unsubscribe, self.remove_skills = unsubscribe, remove_skills
        self.remove_obsolete_paths = remove_obsolete_paths
        self.verify_software_provenance = verify_software_provenance

    def install(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt:
        self._announce_install(request)
        self._require_install_dependencies()
        verified = self._verified(request)
        if self._load_update(request.name) is not None:
            raise ValueError("repository update transaction is active; resume it before install")
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
        self._require_update_dependencies()
        if self.verify_software_provenance is None:
            raise ValueError("repository bootstrap dependency is missing: software provenance")
        self.verify_software_provenance()
        candidate = self._verified(request)
        existing = self._load(request.name)
        if existing is None or existing.removed or existing.verified is None:
            raise ValueError("unknown active repository bootstrap")
        journal = self._load_update(request.name)
        if journal is None:
            self._assert_complete_install(existing)
            self._assert_owned_paths(existing.managed_paths)
            if existing.request == request and existing.verified == candidate:
                return existing
            candidate_grant = repository_trust_grant(
                request, verified=candidate, created_at=existing.created_at
            )
            if not _grant_narrows(existing.trust_grant, candidate_grant):
                raise ValueError("repository update expands the existing owned trust scope")
            if existing.trust_grant != candidate_grant and self.replace_trust is None:
                raise ValueError(
                    "repository bootstrap dependency is missing: atomic trust replacement"
                )
            now = self.now()
            journal = RepositoryUpdateJournal(
                old_receipt_sha256=existing.id.rsplit(":", 1)[-1],
                old_request=existing.request,
                old_managed_paths=existing.managed_paths,
                old_grant=existing.trust_grant,
                candidate_request=request,
                candidate_verified=candidate,
                candidate_grant=candidate_grant,
                phase=RepositoryUpdatePhase.VERIFIED,
                created_at=now,
                updated_at=now,
            )
            self._write_update(journal)
        else:
            completed = self._validate_loaded_update(existing, journal, request, candidate)
            if completed is not None:
                self._remove_update_journal(request.name)
                return completed
            self._assert_update_journal_files(journal)
            if (
                self._update_before(journal, RepositoryUpdatePhase.TRUST_REPLACED)
                and journal.old_grant != journal.candidate_grant
                and self.replace_trust is None
            ):
                raise ValueError(
                    "repository bootstrap dependency is missing: atomic trust replacement"
                )
        return self._resume_update(existing, journal)

    def remove(self, request: RepositoryBootstrapRequest) -> RepositoryBootstrapReceipt:
        self.announce(f"Geas will remove only managed bootstrap paths for {request.name}.")
        if self._load_update(request.name) is not None:
            raise ValueError("repository update transaction is active; resume it before removal")
        receipt = self._load(request.name)
        if receipt is not None and receipt.removed:
            if receipt.request != request:
                raise ValueError(
                    "repository bootstrap removal request does not match ownership receipt"
                )
            return receipt
        self._require_removal_dependencies()
        if receipt is None or receipt.verified is None:
            raise ValueError("unknown active repository bootstrap")
        if receipt.request != request:
            raise ValueError(
                "repository bootstrap removal request does not match ownership receipt"
            )
        if receipt.trust_grant is not None and self.remove_trust is None:
            raise ValueError("repository bootstrap dependency is missing: trust removal")
        if not receipt.removal_pending:
            self._assert_owned_paths(receipt.managed_paths)
            receipt = receipt.model_copy(
                update={
                    "removal_pending": True,
                    "removal_phase": RepositoryRemovalPhase.PENDING,
                    "updated_at": self.now(),
                }
            )
            self._assert_owned_paths(receipt.managed_paths)
            self._write(receipt)
        if receipt.removal_phase is None:
            raise ValueError("repository bootstrap removal journal is invalid")
        assert self.remove_skills is not None and self.unsubscribe is not None
        if receipt.removal_phase is RepositoryRemovalPhase.PENDING:
            skill_paths = _skill_paths(receipt.managed_paths)
            other_paths = _non_skill_paths(receipt.managed_paths)
            self._assert_paths_exact_or_absent(skill_paths)
            self._assert_owned_paths(other_paths)
            self.remove_skills(
                self._removal_operation(receipt, "skills", owned_paths=skill_paths)
            )
            self._assert_paths_absent(skill_paths)
            self._assert_owned_paths(other_paths)
            receipt = receipt.model_copy(
                update={
                    "removal_phase": RepositoryRemovalPhase.SKILLS_REMOVED,
                    "updated_at": self.now(),
                }
            )
            self._write(receipt)
        if receipt.removal_phase is RepositoryRemovalPhase.SKILLS_REMOVED:
            remaining = _non_skill_paths(receipt.managed_paths)
            self._assert_paths_absent(_skill_paths(receipt.managed_paths))
            self._assert_paths_exact_or_absent(remaining)
            self.unsubscribe(
                self._removal_operation(receipt, "subscription", owned_paths=remaining)
            )
            self._assert_paths_absent(receipt.managed_paths)
            receipt = receipt.model_copy(
                update={
                    "removal_phase": RepositoryRemovalPhase.SUBSCRIPTION_REMOVED,
                    "updated_at": self.now(),
                }
            )
            self._write(receipt)
        if receipt.removal_phase is RepositoryRemovalPhase.SUBSCRIPTION_REMOVED:
            self._assert_paths_absent(receipt.managed_paths)
            if receipt.trust_grant is not None:
                assert self.remove_trust is not None
                self.remove_trust(
                    self._removal_operation(receipt, "trust", owned_paths=()),
                    receipt.trust_grant,
                )
            receipt = receipt.model_copy(
                update={
                    "removal_phase": RepositoryRemovalPhase.TRUST_REMOVED,
                    "updated_at": self.now(),
                }
            )
            self._assert_paths_absent(receipt.managed_paths)
            self._write(receipt)
        if receipt.removal_phase is not RepositoryRemovalPhase.TRUST_REMOVED:
            raise ValueError("repository bootstrap removal journal has an invalid phase")
        self._assert_paths_absent(receipt.managed_paths)
        removed = receipt.model_copy(
            update={
                "removal_pending": False,
                "removal_phase": None,
                "removed": True,
                "managed_paths": (),
                "trust_grant": None,
                "updated_at": self.now(),
            }
        )
        self._write(removed)
        return removed

    def _resume_update(
        self,
        old_receipt: RepositoryBootstrapReceipt,
        journal: RepositoryUpdateJournal,
    ) -> RepositoryBootstrapReceipt:
        candidate = journal.candidate_verified
        old_operation = self._update_operation(
            journal,
            request=journal.old_request,
            verified=old_receipt.verified,
            phase=BootstrapPhase.SUBSCRIBED,
            step="subscription",
            owned_paths=journal.old_managed_paths,
        )
        candidate_operation = self._update_operation(
            journal,
            request=journal.candidate_request,
            verified=candidate,
            phase=BootstrapPhase.SUBSCRIBED,
            step="subscription",
        )

        if self._update_before(journal, RepositoryUpdatePhase.TRUST_REPLACED):
            trust_changed = journal.old_grant != journal.candidate_grant
            if trust_changed:
                journal = self._prepare_update(journal, RepositoryUpdatePhase.TRUST_PENDING)
                self._assert_update_journal_files(journal)
                assert self.replace_trust is not None
                self.replace_trust(
                    self._update_operation(
                        journal,
                        request=journal.candidate_request,
                        verified=candidate,
                        phase=BootstrapPhase.TRUST_COMMITTED,
                        step="trust",
                    ),
                    journal.old_grant,
                    journal.candidate_grant,
                )
            journal = self._commit_update_effect(
                journal,
                phase=RepositoryUpdatePhase.TRUST_REPLACED,
                effect=RepositoryUpdateEffect.TRUST,
                affected_paths=(),
                mutation_performed=trust_changed,
            )

        if self._update_before(journal, RepositoryUpdatePhase.SUBSCRIPTION_REPLACED):
            journal = self._prepare_update(journal, RepositoryUpdatePhase.SUBSCRIPTION_PENDING)
            self._assert_update_journal_files(journal)
            assert self.replace_subscription is not None
            produced = self.replace_subscription(old_operation, candidate_operation)
            journal = self._commit_update_effect(
                journal,
                phase=RepositoryUpdatePhase.SUBSCRIPTION_REPLACED,
                effect=RepositoryUpdateEffect.SUBSCRIPTION,
                affected_paths=produced,
                mutation_performed=True,
            )

        journal = self._run_update_path_step(
            journal,
            pending=RepositoryUpdatePhase.ARTIFACTS_PENDING,
            completed=RepositoryUpdatePhase.ARTIFACTS_HYDRATED,
            step="artifacts",
            phase=BootstrapPhase.SKILLS_INSTALLED,
            callback=self.hydrate_artifacts,
        )
        journal = self._run_update_path_step(
            journal,
            pending=RepositoryUpdatePhase.GENERIC_SKILL_PENDING,
            completed=RepositoryUpdatePhase.GENERIC_SKILL_INSTALLED,
            step="generic-skill",
            phase=BootstrapPhase.SKILLS_INSTALLED,
            callback=self.install_generic_skill,
        )
        journal = self._run_update_path_step(
            journal,
            pending=RepositoryUpdatePhase.CATALOG_SKILLS_PENDING,
            completed=RepositoryUpdatePhase.CATALOG_SKILLS_EXPORTED,
            step="catalog-skills",
            phase=BootstrapPhase.SKILLS_INSTALLED,
            callback=self.export_catalog_skills,
        )
        journal = self._run_update_path_step(
            journal,
            pending=RepositoryUpdatePhase.AGENT_LINKS_PENDING,
            completed=RepositoryUpdatePhase.AGENT_LINKS_INSTALLED,
            step="agent-links",
            phase=BootstrapPhase.SKILLS_INSTALLED,
            callback=self.link_agents,
        )

        if self._update_before(journal, RepositoryUpdatePhase.OBSOLETE_PATHS_REMOVED):
            journal = self._prepare_update(journal, RepositoryUpdatePhase.OBSOLETE_PATHS_PENDING)
            current = {item.path for item in journal.candidate_managed_paths}
            obsolete = tuple(
                item for item in journal.old_managed_paths if item.path not in current
            )
            if obsolete:
                self._assert_update_journal_files(journal)
                assert self.remove_obsolete_paths is not None
                self.remove_obsolete_paths(
                    self._update_operation(
                        journal,
                        request=journal.candidate_request,
                        verified=candidate,
                        phase=BootstrapPhase.COMPLETED,
                        step="obsolete-paths",
                        owned_paths=obsolete,
                    )
                )
                self._assert_paths_absent(obsolete)
            journal = self._commit_update_effect(
                journal,
                phase=RepositoryUpdatePhase.OBSOLETE_PATHS_REMOVED,
                effect=RepositoryUpdateEffect.OBSOLETE_PATHS,
                affected_paths=obsolete,
                mutation_performed=bool(obsolete),
            )

        journal = self._prepare_update(journal, RepositoryUpdatePhase.FINALIZING)
        self._assert_update_journal_files(journal)
        replacement = RepositoryBootstrapReceipt(
            request=journal.candidate_request,
            verified=journal.candidate_verified,
            completed_phases=_PHASES,
            trust_grant=journal.candidate_grant,
            managed_paths=journal.candidate_managed_paths,
            created_at=old_receipt.created_at,
            updated_at=self.now(),
        )
        self._write(replacement)
        self._remove_update_journal(journal.candidate_request.name)
        return replacement

    def _run_update_path_step(
        self,
        journal: RepositoryUpdateJournal,
        *,
        pending: RepositoryUpdatePhase,
        completed: RepositoryUpdatePhase,
        step: str,
        phase: BootstrapPhase,
        callback: Callable[[BootstrapOperation], tuple[ManagedPath, ...]] | None,
    ) -> RepositoryUpdateJournal:
        if not self._update_before(journal, completed):
            return journal
        journal = self._prepare_update(journal, pending)
        self._assert_update_journal_files(journal)
        assert callback is not None
        produced = callback(
            self._update_operation(
                journal,
                request=journal.candidate_request,
                verified=journal.candidate_verified,
                phase=phase,
                step=step,
                owned_paths=journal.candidate_managed_paths,
            )
        )
        return self._commit_update_effect(
            journal,
            phase=completed,
            effect=RepositoryUpdateEffect(step),
            affected_paths=produced,
            mutation_performed=True,
        )

    def _commit_update_effect(
        self,
        journal: RepositoryUpdateJournal,
        *,
        phase: RepositoryUpdatePhase,
        effect: RepositoryUpdateEffect,
        affected_paths: tuple[ManagedPath, ...],
        mutation_performed: bool,
    ) -> RepositoryUpdateJournal:
        paths = {item.path: item for item in journal.candidate_managed_paths}
        if effect is not RepositoryUpdateEffect.OBSOLETE_PATHS:
            for item in affected_paths:
                previous = paths.get(item.path)
                if previous is not None and previous != item:
                    raise ValueError(f"update adapters disagree about managed path: {item.path}")
                paths[item.path] = item
        effect_receipt = RepositoryUpdateEffectReceipt(
            effect=effect,
            idempotency_key=repository_update_operation_id(
                old_receipt_sha256=journal.old_receipt_sha256,
                candidate_request=journal.candidate_request,
                candidate_verified=journal.candidate_verified,
                effect=effect,
            ),
            mutation_performed=mutation_performed,
            affected_paths=affected_paths,
        )
        updated = journal.model_copy(
            update={
                "candidate_managed_paths": tuple(paths[path] for path in sorted(paths)),
                "effect_receipts": (*journal.effect_receipts, effect_receipt),
                "phase": phase,
                "updated_at": self.now(),
            }
        )
        self._assert_update_journal_files(updated)
        self._write_update(updated)
        return updated

    def _prepare_update(
        self, journal: RepositoryUpdateJournal, phase: RepositoryUpdatePhase
    ) -> RepositoryUpdateJournal:
        current = _UPDATE_PHASE_INDEX[journal.phase]
        requested = _UPDATE_PHASE_INDEX[phase]
        if requested < current:
            return journal
        if requested > current + 1 and phase is not RepositoryUpdatePhase.TRUST_REPLACED:
            raise ValueError("repository update journal phase transition is invalid")
        updated = journal.model_copy(update={"phase": phase, "updated_at": self.now()})
        self._write_update(updated)
        return updated

    def _update_before(
        self, journal: RepositoryUpdateJournal, phase: RepositoryUpdatePhase
    ) -> bool:
        return _UPDATE_PHASE_INDEX[journal.phase] < _UPDATE_PHASE_INDEX[phase]

    def _assert_update_journal_files(self, journal: RepositoryUpdateJournal) -> None:
        self._assert_owned_paths(journal.candidate_managed_paths)
        obsolete = tuple(
            item
            for item in journal.old_managed_paths
            if item.path not in {candidate.path for candidate in journal.candidate_managed_paths}
        )
        if self._update_before(journal, RepositoryUpdatePhase.OBSOLETE_PATHS_PENDING):
            self._assert_owned_paths(obsolete)
        elif journal.phase is RepositoryUpdatePhase.OBSOLETE_PATHS_PENDING:
            self._assert_paths_exact_or_absent(obsolete)
        else:
            self._assert_paths_absent(obsolete)

    def _assert_paths_exact_or_absent(self, managed: tuple[ManagedPath, ...]) -> None:
        for item in managed:
            candidate = _confined_owned_path(self.root, item.path)
            if candidate.exists() or candidate.is_symlink():
                self._assert_owned_paths((item,))

    def _assert_paths_absent(self, managed: tuple[ManagedPath, ...]) -> None:
        for item in managed:
            candidate = _confined_owned_path(self.root, item.path)
            if candidate.exists() or candidate.is_symlink():
                raise ValueError(f"managed path still exists after removal: {item.path}")

    def _validate_loaded_update(
        self,
        receipt: RepositoryBootstrapReceipt,
        journal: RepositoryUpdateJournal,
        request: RepositoryBootstrapRequest,
        candidate: VerifiedRepositoryBootstrap,
    ) -> RepositoryBootstrapReceipt | None:
        if journal.candidate_request != request:
            raise ValueError("repository update request conflicts with the active transaction")
        if journal.candidate_verified != candidate:
            raise ValueError("repository update candidate verification changed")
        expected_grant = repository_trust_grant(
            request, verified=candidate, created_at=receipt.created_at
        )
        if expected_grant != journal.candidate_grant:
            raise ValueError("repository update candidate grant changed")
        old_hash = receipt.id.rsplit(":", 1)[-1]
        if old_hash == journal.old_receipt_sha256:
            if (
                receipt.request != journal.old_request
                or receipt.managed_paths != journal.old_managed_paths
                or receipt.trust_grant != journal.old_grant
            ):
                raise ValueError("repository update old ownership receipt changed")
            self._assert_complete_install(receipt)
            return None
        if self._receipt_is_completed_candidate(receipt, journal):
            if journal.phase is not RepositoryUpdatePhase.FINALIZING:
                raise ValueError(
                    "completed candidate receipt requires a FINALIZING update journal"
                )
            self._assert_update_journal_files(journal)
            return receipt
        raise ValueError("repository update ownership receipt conflicts with its journal")

    def _receipt_is_completed_candidate(
        self, receipt: RepositoryBootstrapReceipt, journal: RepositoryUpdateJournal
    ) -> bool:
        return (
            receipt.request == journal.candidate_request
            and receipt.verified == journal.candidate_verified
            and receipt.trust_grant == journal.candidate_grant
            and receipt.managed_paths == journal.candidate_managed_paths
            and receipt.completed_phases == _PHASES
            and receipt.pending_phase is None
            and not receipt.removed
            and not receipt.removal_pending
        )

    def _assert_complete_install(self, receipt: RepositoryBootstrapReceipt) -> None:
        if (
            receipt.completed_phases != _PHASES
            or receipt.pending_phase is not None
            or receipt.update_candidate is not None
            or receipt.removal_pending
        ):
            raise ValueError("repository bootstrap must be complete before update")

    def _update_operation(
        self,
        journal: RepositoryUpdateJournal,
        *,
        request: RepositoryBootstrapRequest,
        verified: VerifiedRepositoryBootstrap | None,
        phase: BootstrapPhase,
        step: str,
        owned_paths: tuple[ManagedPath, ...] = (),
    ) -> BootstrapOperation:
        if verified is None:
            raise ValueError("repository update is missing verified old ownership")
        key = repository_update_operation_id(
            old_receipt_sha256=journal.old_receipt_sha256,
            candidate_request=journal.candidate_request,
            candidate_verified=journal.candidate_verified,
            effect=RepositoryUpdateEffect(step),
        )
        return BootstrapOperation(
            request=request,
            verified=verified,
            phase=phase,
            idempotency_key=key,
            owned_paths=owned_paths,
        )

    def _removal_operation(
        self,
        receipt: RepositoryBootstrapReceipt,
        step: str,
        *,
        owned_paths: tuple[ManagedPath, ...] | None = None,
    ) -> BootstrapOperation:
        assert receipt.verified is not None
        key = content_id(
            "repository-bootstrap-removal-operation",
            {
                "request": receipt.request.model_dump(mode="json"),
                "verified": receipt.verified.id,
                "step": step,
            },
        )
        return BootstrapOperation(
            request=receipt.request,
            verified=receipt.verified,
            phase=BootstrapPhase.COMPLETED,
            idempotency_key=key,
            owned_paths=receipt.managed_paths if owned_paths is None else owned_paths,
        )

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

    def _require_update_dependencies(self) -> None:
        required = {
            "subscription replacement": self.replace_subscription,
            "obsolete path removal": self.remove_obsolete_paths,
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
        journal = RepositoryUpdateJournal.model_validate(journal.model_dump(mode="python"))
        path = self._update_path(journal.candidate_request.name)
        _reject_symlink_ancestry(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestry(path.parent)
        if path.is_symlink():
            raise ValueError("repository update journal must be a regular file")
        data = canonical_json(journal.model_dump(mode="json")) + b"\n"
        temporary = path.with_name(f".{path.name}.{hashlib.sha256(data).hexdigest()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _load_update(self, name: str) -> RepositoryUpdateJournal | None:
        path = self._update_path(name)
        if path.is_symlink():
            raise ValueError("repository update journal must be a regular file")
        if not path.exists():
            return None
        _reject_symlink_ancestry(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("repository update journal must be a regular file")
        try:
            journal = RepositoryUpdateJournal.model_validate_json(path.read_bytes())
        except Exception as error:
            raise ValueError("repository update journal is invalid") from error
        if journal.candidate_request.name != name:
            raise ValueError("repository update journal name does not match its path")
        return journal

    def _remove_update_journal(self, name: str) -> None:
        path = self._update_path(name)
        _reject_symlink_ancestry(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("repository update journal must be a regular file")
        path.unlink()
        _fsync_directory(path.parent)

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


def _non_skill_paths(managed: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
    return tuple(item for item in managed if item.role not in {"skill", "link"})


def _skill_paths(managed: tuple[ManagedPath, ...]) -> tuple[ManagedPath, ...]:
    return tuple(item for item in managed if item.role in {"skill", "link"})


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
