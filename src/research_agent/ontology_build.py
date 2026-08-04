from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from research_agent.approvals import ApprovalRegistry
from research_agent.audit import DeterministicKnowledgeAuditor
from research_agent.budget import BudgetPolicy, UsageLedger
from research_agent.bundles import KnowledgeBundle, KnowledgeBundleImporter
from research_agent.candidate_bundles import (
    CandidateBundleError,
    CandidateBundleWriter,
    CandidateLicenseError,
)
from research_agent.connectors import MojeekDiscoveryConnector
from research_agent.discovery import (
    CompilerIdentity,
    ConnectorCapability,
    QueryPlan,
    SourceClass,
)
from research_agent.discovery_acquisition import (
    GitHubDiscoveryAcquirer,
    RepositorySnapshot,
)
from research_agent.extraction import (
    AnchorGroundedExtractionManager,
    ExtractionError,
    ExtractionRequest,
    ValidatedExtractionProposal,
)
from research_agent.knowledge import KnowledgeGap
from research_agent.model_policy import (
    DataClass,
    InputKind,
    ModelOperation,
    ModelUseContext,
    ModelUseGate,
    ModelUsePolicy,
)
from research_agent.models import (
    ModelParameters,
    StrictModel,
    ThreatObservation,
    canonical_json,
    utc_now,
)
from research_agent.operator_policy import ResearchPolicy
from research_agent.planning import (
    ConceptVocabulary,
    QueryPlanValidator,
    QueryProposal,
    deterministic_proposal,
)
from research_agent.projection import KnowledgeQueryEngine, SQLiteKnowledgeProjection
from research_agent.providers import ModelClient, ModelOutputTruncatedError, load_provider_configs
from research_agent.render import render_topic_markdown
from research_agent.research import DiscoveryExecutor
from research_agent.store import ImmutableStore
from research_agent.structure import AnchorKind, StructuralAnchor
from research_agent.truth import TruthManager, TruthPolicy


class OntologyBuildConfig(StrictModel):
    version: Literal[1]
    topic: str = Field(min_length=1)
    topic_concept_id: str = Field(pattern=r"^concept:[A-Za-z0-9][A-Za-z0-9._:-]*$")
    description: str = Field(default="", max_length=4000)
    seed_bundles: tuple[Path, ...] = ()
    seed_bundle_globs: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    include_gap_queries: bool = True
    max_queries: int | None = Field(default=None, ge=1, le=10_000)
    result_limit: int = Field(default=30, ge=1, le=200)
    approve_large_queries: bool = False
    repository_limit_per_query: int = Field(default=20, ge=1, le=100)
    provider: str = "deepseek_local"
    max_output_tokens: int = Field(default=65_536, ge=1024, le=524_288)
    model_parameters: ModelParameters = Field(default_factory=ModelParameters)
    debug_reasoning: bool = True
    timeout_seconds: float = Field(default=3600.0, ge=1.0, le=86_400.0)
    anchors_per_batch: int = Field(default=200, ge=1, le=200)
    max_batches_per_source: int | None = Field(default=None, ge=1, le=500)
    max_sources: int | None = Field(default=None, ge=1, le=10_000)
    model_parallelism: int = Field(default=1, ge=1, le=1)
    output_directory: Path
    tainted_source_index: Path | None = None

    @field_validator("seed_bundles", "output_directory", "tainted_source_index")
    @classmethod
    def paths_are_relative(cls, value: object) -> object:
        if value is None:
            return value
        values = value if isinstance(value, tuple) else (value,)
        if any(Path(item).is_absolute() or ".." in Path(item).parts for item in values):
            raise ValueError("ontology build paths must be workspace-relative")
        return value

    @field_validator("seed_bundle_globs")
    @classmethod
    def seed_globs_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            path = Path(pattern)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("ontology seed globs must be workspace-relative")
        return value

    @model_validator(mode="after")
    def defaults_are_safe(self) -> OntologyBuildConfig:
        if self.model_parallelism != 1:
            raise ValueError("ontology extraction currently requires model_parallelism: 1")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> OntologyBuildConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class TokenLimitExhaustion(StrictModel):
    source: str
    provider: str
    model: str
    requested_output_tokens: int
    provider_output_token_limit: int
    observed_output_tokens: int | None = None
    finish_reason: Literal["length"] = "length"
    recommendations: tuple[str, ...]


class TaintedSourceObservation(StrictModel):
    id: str
    threat_type: str
    status: str
    severity: str
    detected_at: datetime
    detector_kind: str
    detector_id: str
    detector_version: str | None = None
    evidence_fragment_ids: tuple[str, ...]


class TaintedSourceEntry(StrictModel):
    repository: str
    canonical_locator: str
    commit_sha: str
    source_version_id: str
    source_content_sha256: str
    license: str | None = None
    observed_at: datetime
    observations: tuple[TaintedSourceObservation, ...]


class TaintedSourceIndex(StrictModel):
    version: Literal[1] = 1
    topic: str
    generated_by: str = "deterministic-tainted-source-index/1"
    recorded_through: datetime | None = None
    entries: tuple[TaintedSourceEntry, ...] = ()


class OntologyBuildReceipt(StrictModel):
    version: Literal[1] = 1
    config_sha256: str
    checked_only: bool
    completed: bool
    queries_completed: tuple[str, ...] = ()
    repositories_acquired: tuple[str, ...] = ()
    proposals: tuple[str, ...] = ()
    candidate_bundles: tuple[str, ...] = ()
    skipped_tainted_sources: tuple[str, ...] = ()
    skipped_unlicensed_sources: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    token_limit_exhaustions: tuple[TokenLimitExhaustion, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    snapshot_path: str | None = None
    projection_path: str | None = None
    topic_path: str | None = None
    tainted_source_index_path: str | None = None
    audit_clean: bool | None = None


class BuildProgress:
    """Human progress on stderr plus structured, append-only JSONL events."""

    def __init__(self, *, root: Path, config_sha256: str) -> None:
        self.root = root
        self.config_sha256 = config_sha256
        self.path = root / "ontology-build.log.jsonl"

    def event(
        self,
        stage: str,
        status: str,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        **metadata: object,
    ) -> None:
        forbidden = re.compile(
            r"(?:api.?key|credential|secret|prompt|source.?text|model.?response|model.?output)",
            re.IGNORECASE,
        )
        unsafe = sorted(key for key in metadata if forbidden.search(key))
        if unsafe:
            raise ValueError(
                "operational log metadata contains forbidden sensitive fields: "
                + ", ".join(unsafe)
            )
        payload = {
            "timestamp": utc_now().isoformat(),
            "config_sha256": self.config_sha256,
            "stage": stage,
            "status": status,
            "message": message,
            **metadata,
        }
        if current is not None:
            payload["current"] = current
        if total is not None:
            payload["total"] = total
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        prefix = f"[{stage}]"
        if current is not None and total is not None:
            width = 20
            filled = min(width, math.floor(width * current / max(total, 1)))
            prefix += f" [{'#' * filled}{'-' * (width - filled)}] {current}/{total}"
        print(f"{prefix} {message}", file=sys.stderr, flush=True)


class OntologyBuilder:
    """Own the deterministic, resumable ontology-building control loop."""

    version = "ontology-builder/1"
    _words = re.compile(r"[a-z0-9][a-z0-9_.+-]{1,}", re.IGNORECASE)
    _anchor_terms = frozenset(
        {
            "agent",
            "architecture",
            "benchmark",
            "citation",
            "evaluation",
            "framework",
            "knowledge",
            "license",
            "model",
            "open",
            "research",
            "retrieval",
            "search",
            "security",
            "source",
            "workflow",
        }
    )

    def __init__(
        self,
        *,
        config: OntologyBuildConfig,
        root: Path,
        workspace: Path,
        providers_path: Path,
        research_policy_path: Path,
        model_policy_path: Path,
        budget_policy_path: Path,
        truth_policy_path: Path,
        vocabulary_path: Path,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self.workspace = workspace.resolve()
        self.providers_path = providers_path
        self.research_policy_path = research_policy_path
        self.model_policy_path = model_policy_path
        self.budget_policy_path = budget_policy_path
        self.truth_policy_path = truth_policy_path
        self.vocabulary_path = vocabulary_path
        self.store = ImmutableStore(self.root)
        self.config_sha256 = hashlib.sha256(canonical_json(config)).hexdigest()
        discovery_fields = config.model_dump(
            exclude={
                "provider",
                "max_output_tokens",
                "model_parameters",
                "debug_reasoning",
                "timeout_seconds",
                "anchors_per_batch",
                "max_batches_per_source",
                "max_sources",
                "model_parallelism",
                "output_directory",
                "tainted_source_index",
            }
        )
        self.discovery_config_sha256 = hashlib.sha256(
            canonical_json(discovery_fields)
        ).hexdigest()
        self.state_path = self.root / "ontology-build-state.json"
        self.progress = BuildProgress(root=self.root, config_sha256=self.config_sha256)

    def check(self) -> OntologyBuildReceipt:
        self._validate_inputs()
        return OntologyBuildReceipt(
            config_sha256=self.config_sha256,
            checked_only=True,
            completed=False,
        )

    def run(self) -> OntologyBuildReceipt:
        self._validate_inputs()
        self.store.initialize()
        self.progress.event("build", "started", "Starting or resuming ontology build")
        state = self._load_state()
        failures: list[str] = list(state.get("failures", []))
        token_exhaustions = [
            TokenLimitExhaustion.model_validate(item)
            for item in state.get("token_limit_exhaustions", [])
        ]
        bundles = list(state.get("candidate_bundles", []))
        proposals = list(state.get("proposals", []))
        tainted = list(state.get("skipped_tainted_sources", []))
        unlicensed = list(state.get("skipped_unlicensed_sources", []))

        seed_paths = self._seed_paths()
        for path in seed_paths:
            self._assert_seed_matches_head(path)
        for index, path in enumerate(seed_paths, start=1):
            key = str(path)
            if key not in state["imported_bundles"]:
                self.progress.event(
                    "seed",
                    "running",
                    f"Importing {key}",
                    current=index,
                    total=len(seed_paths),
                )
                KnowledgeBundleImporter(store=self.store).import_bundle(
                    self.workspace / path,
                    imported_by=f"ontology-builder:{self.config_sha256}",
                )
                state["imported_bundles"].append(key)
                self._save_state(state)
            else:
                self.progress.event(
                    "seed",
                    "resumed",
                    f"Already imported {key}",
                    current=index,
                    total=len(seed_paths),
                )

        query_strings = self._queries()
        snapshots = self._snapshots()
        acquired_by_source = {item.source_version_id: item for item in snapshots}
        known_locators = {item.canonical_locator.casefold() for item in snapshots}
        research_policy = ResearchPolicy.from_yaml(self.research_policy_path)
        provider_policy = research_policy.provider("connector:mojeek")
        connector = MojeekDiscoveryConnector()
        for index, question in enumerate(query_strings, start=1):
            if question in state["queries_completed"]:
                self.progress.event(
                    "discovery",
                    "resumed",
                    "Using completed search checkpoint",
                    current=index,
                    total=len(query_strings),
                    query=question,
                )
                continue
            self.progress.event(
                "discovery",
                "running",
                "Searching Mojeek discovery index",
                current=index,
                total=len(query_strings),
                query=question,
                result_limit=self.config.result_limit,
            )
            started = time.monotonic()
            execution = DiscoveryExecutor().run(
                self._query_plan(question, connector, provider_policy.max_requests_per_run),
                connector,
            )
            self.store.put_record("discovery-run", execution.discovery_run)
            novel_hits = tuple(
                hit
                for hit in execution.hits
                if hit.canonical_locator.casefold() not in known_locators
            )
            receipt = GitHubDiscoveryAcquirer(store=self.store).acquire_hits(
                novel_hits,
                discovery_label=f"ontology-build:{self.config_sha256}:{question}",
                limit=self.config.repository_limit_per_query,
            )
            for item in receipt.acquired:
                known_locators.add(item.snapshot.canonical_locator.casefold())
                acquired_by_source[item.snapshot.source_version_id] = item.snapshot
            state["queries_completed"].append(question)
            self._save_state(state)
            self.progress.event(
                "discovery",
                "completed",
                (
                    f"Found {len(execution.hits)} hits; acquired "
                    f"{len(receipt.acquired)} immutable repositories"
                ),
                current=index,
                total=len(query_strings),
                query=question,
                hit_count=len(execution.hits),
                acquired_count=len(receipt.acquired),
                constrained_count=len(receipt.access_constraints),
                duration_seconds=round(time.monotonic() - started, 3),
            )

        _, provider_configs = load_provider_configs(self.providers_path)
        configured_model = provider_configs[self.config.provider].model
        extraction_requests = {
            item.id: item
            for item in (
                ExtractionRequest.model_validate(value)
                for value in self.store.iter_records("extraction-request")
            )
        }
        existing_proposals = {
            item.source_version_id: item
            for item in (
                ValidatedExtractionProposal.model_validate(value)
                for value in self.store.iter_records("extraction-proposal")
            )
            if (
                request := extraction_requests.get(item.extraction_request_id)
            ) is not None
            and self._proposal_is_compatible(item, request, configured_model)
        }
        model_failed = bool(token_exhaustions)
        selected_snapshots = self._rank_snapshots(tuple(acquired_by_source.values()))
        if self.config.max_sources is not None:
            selected_snapshots = selected_snapshots[: self.config.max_sources]
        validation_failures = self._validation_failure_counts()
        for index, snapshot in enumerate(selected_snapshots, start=1):
            if snapshot.source_version_id in existing_proposals:
                proposal = existing_proposals[snapshot.source_version_id]
                self.progress.event(
                    "extraction",
                    "resumed",
                    f"Using validated proposal for {snapshot.repository}",
                    current=index,
                    total=len(selected_snapshots),
                    repository=snapshot.repository,
                    proposal_id=proposal.id,
                )
            else:
                receipt = self._parsed_receipt(snapshot)
                if receipt is None:
                    failures.append(f"{snapshot.repository}:missing-parsed-receipt")
                    self.progress.event(
                        "extraction",
                        "skipped",
                        f"Missing parsed receipt for {snapshot.repository}",
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                    )
                    continue
                if receipt["threat_observation_ids"]:
                    tainted.append(snapshot.repository)
                    self.progress.event(
                        "extraction",
                        "blocked",
                        f"Threat policy blocked {snapshot.repository}",
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                        threat_observation_count=len(receipt["threat_observation_ids"]),
                    )
                    continue
                if (
                    validation_failures.get(snapshot.source_version_id, 0)
                    >= 2
                ):
                    failures.append(
                        f"{snapshot.repository}:output-schema-quarantined"
                    )
                    self.progress.event(
                        "extraction",
                        "quarantined",
                        f"Skipped repeatedly invalid model output for {snapshot.repository}",
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                        validation_failure_count=validation_failures[
                            snapshot.source_version_id
                        ],
                    )
                    continue
                if model_failed:
                    self.progress.event(
                        "extraction",
                        "deferred",
                        f"Deferred {snapshot.repository} after model failure",
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                    )
                    continue
                try:
                    self.progress.event(
                        "extraction",
                        "running",
                        f"Extracting {snapshot.repository} with one tool-free model request",
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                        provider=self.config.provider,
                        max_output_tokens=self.config.max_output_tokens,
                        reasoning_effort=self.config.model_parameters.reasoning_effort,
                        thinking=self.config.model_parameters.thinking,
                        debug_reasoning=self.config.debug_reasoning,
                        timeout_seconds=self.config.timeout_seconds,
                    )
                    started = time.monotonic()
                    heartbeat_stop = threading.Event()
                    heartbeat = threading.Thread(
                        target=self._model_heartbeat,
                        args=(
                            heartbeat_stop,
                            snapshot,
                            index,
                            len(selected_snapshots),
                            started,
                        ),
                        daemon=True,
                    )
                    heartbeat.start()
                    proposal = self._extract(snapshot, receipt["structural_derivation_id"])
                except Exception as error:
                    # A timeout may leave a non-streaming local server occupied. Never
                    # enqueue another request during this run.
                    failures.append(f"{snapshot.repository}:model:{type(error).__name__}")
                    safe_to_continue = isinstance(error, (ValidationError, ExtractionError))
                    model_failed = not safe_to_continue
                    if isinstance(error, ModelOutputTruncatedError):
                        exhaustion = self._token_exhaustion(snapshot, error)
                        if exhaustion not in token_exhaustions:
                            token_exhaustions.append(exhaustion)
                    self.progress.event(
                        "extraction",
                        "failed",
                        (
                            f"Rejected invalid proposal for {snapshot.repository}; continuing"
                            if safe_to_continue
                            else f"Stopped model queue after {type(error).__name__}"
                        ),
                        current=index,
                        total=len(selected_snapshots),
                        repository=snapshot.repository,
                        error_type=type(error).__name__,
                        model_queue_stopped=model_failed,
                    )
                    self._save_state(
                        {
                            **state,
                            "failures": sorted(set(failures)),
                            "token_limit_exhaustions": [
                                item.model_dump(mode="json")
                                for item in token_exhaustions
                            ],
                            "skipped_tainted_sources": sorted(set(tainted)),
                        }
                    )
                    continue
                finally:
                    if "heartbeat_stop" in locals():
                        heartbeat_stop.set()
                        heartbeat.join(timeout=1)
                        del heartbeat_stop
                existing_proposals[snapshot.source_version_id] = proposal
                self.progress.event(
                    "extraction",
                    "completed",
                    (
                        f"Validated {len(proposal.claims)} claims from "
                        f"{snapshot.repository}"
                    ),
                    current=index,
                    total=len(selected_snapshots),
                    repository=snapshot.repository,
                    proposal_id=proposal.id,
                    claim_count=len(proposal.claims),
                    concept_count=len(proposal.concepts),
                    controversy_count=len(proposal.controversies),
                    gap_count=len(proposal.gaps),
                    duration_seconds=round(time.monotonic() - started, 3),
                )
            proposals.append(proposal.id)
            if not proposal.claims:
                continue
            try:
                relative = CandidateBundleWriter(
                    store=self.store,
                    workspace=self.workspace,
                ).write(
                    proposal,
                    snapshot,
                    topic=self.config.topic,
                    topic_concept_id=self.config.topic_concept_id,
                    output_root=self.config.output_directory,
                )
            except CandidateLicenseError:
                unlicensed.append(snapshot.repository)
                self.progress.event(
                    "bundle",
                    "blocked",
                    f"Did not redistribute {snapshot.repository}: license not allowlisted",
                    current=index,
                    total=len(selected_snapshots),
                    repository=snapshot.repository,
                    license=snapshot.license or "unknown",
                )
                continue
            except CandidateBundleError as error:
                failures.append(
                    f"{snapshot.repository}:candidate-bundle:{type(error).__name__}"
                )
                self.progress.event(
                    "bundle",
                    "failed",
                    f"Rejected invalid candidate bundle for {snapshot.repository}",
                    current=index,
                    total=len(selected_snapshots),
                    repository=snapshot.repository,
                    error_type=type(error).__name__,
                )
                continue
            relative_text = relative.as_posix()
            bundles.append(relative_text)
            state.update(
                {
                    "proposals": sorted(set(proposals)),
                    "candidate_bundles": sorted(set(bundles)),
                    "skipped_tainted_sources": sorted(set(tainted)),
                    "skipped_unlicensed_sources": sorted(set(unlicensed)),
                    "failures": sorted(set(failures)),
                }
            )
            self._save_state(state)
            self.progress.event(
                "bundle",
                "completed",
                f"Wrote review-only candidate {relative_text}",
                current=index,
                total=len(selected_snapshots),
                repository=snapshot.repository,
                bundle=relative_text,
                commit_authority="none",
            )

        state.update(
            {
                "proposals": sorted(set(proposals)),
                "candidate_bundles": sorted(set(bundles)),
                "skipped_tainted_sources": sorted(set(tainted)),
                "skipped_unlicensed_sources": sorted(set(unlicensed)),
                "failures": sorted(set(failures)),
                "token_limit_exhaustions": [
                    item.model_dump(mode="json") for item in token_exhaustions
                ],
            }
        )
        self._save_state(state)
        tainted_source_index_path = self._write_tainted_source_index(
            selected_snapshots
        )
        self.progress.event("finalize", "running", "Auditing and rebuilding projections")
        snapshot_path, database_path, topic_path, audit_clean = self._finalize()
        state["completed"] = (
            not model_failed
            and not token_exhaustions
            and not failures
            and audit_clean
        )
        self._save_state(state)
        completed = bool(state["completed"])
        self.progress.event(
            "build",
            "completed" if completed else "incomplete",
            (
                "Ontology build completed"
                if completed
                else "Ontology build is incomplete and requires operator action"
            ),
            proposal_count=len(set(proposals)),
            candidate_bundle_count=len(set(bundles)),
            failure_count=len(set(failures)),
            audit_clean=audit_clean,
        )
        return OntologyBuildReceipt(
            config_sha256=self.config_sha256,
            checked_only=False,
            completed=completed,
            queries_completed=tuple(state["queries_completed"]),
            repositories_acquired=tuple(
                item.repository for item in selected_snapshots
            ),
            proposals=tuple(sorted(set(proposals))),
            candidate_bundles=tuple(sorted(set(bundles))),
            skipped_tainted_sources=tuple(sorted(set(tainted))),
            skipped_unlicensed_sources=tuple(sorted(set(unlicensed))),
            failures=tuple(sorted(set(failures))),
            token_limit_exhaustions=tuple(token_exhaustions),
            recommended_actions=tuple(
                dict.fromkeys(
                    action
                    for item in token_exhaustions
                    for action in item.recommendations
                )
            ),
            snapshot_path=str(snapshot_path),
            projection_path=str(database_path),
            topic_path=str(topic_path),
            tainted_source_index_path=str(tainted_source_index_path),
            audit_clean=audit_clean,
        )

    def _validate_inputs(self) -> None:
        for path in (
            self.providers_path,
            self.research_policy_path,
            self.model_policy_path,
            self.budget_policy_path,
            self.truth_policy_path,
            self.vocabulary_path,
        ):
            path.resolve(strict=True)
        for path in self.config.seed_bundles:
            (self.workspace / path).resolve(strict=True)
        _, providers = load_provider_configs(self.providers_path)
        if self.config.provider not in providers:
            raise ValueError(f"unknown ontology build provider: {self.config.provider}")
        provider = providers[self.config.provider]
        if self.config.max_output_tokens > provider.max_output_tokens:
            raise ValueError(
                f"ontology requests {self.config.max_output_tokens} output tokens, but "
                f"provider {self.config.provider} supports only "
                f"{provider.max_output_tokens}; raise the provider capacity or select "
                "a model that accommodates the ontology"
            )
        required_context = self.config.model_parameters.minimum_context_tokens
        if required_context is not None:
            if provider.context_window_tokens is None:
                raise ValueError(
                    f"reasoning_effort=max requires a declared context window of at least "
                    f"{required_context} tokens; provider {self.config.provider} does not "
                    "declare context_window_tokens"
                )
            if provider.context_window_tokens < required_context:
                raise ValueError(
                    f"reasoning_effort=max requires at least {required_context} context "
                    f"tokens, but provider {self.config.provider} declares "
                    f"{provider.context_window_tokens}; increase the server context or "
                    "use reasoning_effort=high"
                )
        policy = ResearchPolicy.from_yaml(self.research_policy_path)
        if not policy.provider("connector:mojeek").enabled:
            raise ValueError("ontology build requires enabled Mojeek discovery")

    def _seed_paths(self) -> tuple[Path, ...]:
        paths = set(self.config.seed_bundles)
        if self.config.seed_bundle_globs:
            result = subprocess.run(
                (
                    "git",
                    "-C",
                    str(self.workspace),
                    "ls-tree",
                    "-r",
                    "--name-only",
                    "-z",
                    "HEAD",
                    "--",
                ),
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                raise ValueError(
                    "seed_bundle_globs require an accessible Git HEAD"
                )
            tracked = frozenset(
                item.decode("utf-8", errors="strict")
                for item in result.stdout.split(b"\0")
                if item
            )
            for pattern in self.config.seed_bundle_globs:
                paths.update(
                    path.relative_to(self.workspace)
                    for path in self.workspace.glob(pattern)
                    if path.is_file()
                    and path.relative_to(self.workspace).as_posix() in tracked
                )
        return tuple(sorted(paths, key=lambda item: item.as_posix()))

    def _assert_seed_matches_head(self, relative: Path) -> None:
        self._assert_path_matches_head(relative)
        bundle_path = (self.workspace / relative).resolve(strict=True)
        bundle = KnowledgeBundle.from_yaml(bundle_path)
        for source in bundle.sources:
            declared = Path(source.path)
            if declared.is_absolute() or ".." in declared.parts:
                raise ValueError("bundle source paths must be confined relative paths")
            source_relative = relative.parent / declared
            unresolved = self.workspace / source_relative
            if unresolved.is_symlink():
                raise ValueError("bundle source files must not be symbolic links")
            resolved = unresolved.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(bundle_path.parent):
                raise ValueError("bundle source path escapes its bundle")
            self._assert_path_matches_head(source_relative)

    def _assert_path_matches_head(self, relative: Path) -> None:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("canonical ontology paths must be workspace-relative")
        result = subprocess.run(
            (
                "git",
                "-C",
                str(self.workspace),
                "cat-file",
                "blob",
                f"HEAD:{relative.as_posix()}",
            ),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ValueError(
                f"canonical ontology path is absent from Git HEAD: {relative}"
            )
        path = self.workspace / relative
        if path.is_symlink() or path.read_bytes() != result.stdout:
            raise ValueError(
                f"canonical ontology path differs from Git HEAD: {relative}"
            )

    def _queries(self) -> tuple[str, ...]:
        values = list(self.config.queries or (self.config.topic,))
        if self.config.include_gap_queries:
            gaps = sorted(
                (
                    KnowledgeGap.model_validate(value)
                    for value in self.store.iter_records("knowledge-gap")
                ),
                key=lambda item: (-item.priority, item.question),
            )
            values.extend(item.question for item in gaps)
        queries = tuple(dict.fromkeys(item.strip() for item in values if item.strip()))
        return queries if self.config.max_queries is None else queries[: self.config.max_queries]

    def _query_plan(
        self,
        question: str,
        connector: MojeekDiscoveryConnector,
        max_requests: int,
    ) -> QueryPlan:
        base = deterministic_proposal(
            question,
            connector_id=connector.manifest.id,
            concept_ids=(),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "source_classes": [SourceClass.WEB],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": self.config.result_limit,
                "page_limit": min(math.ceil(self.config.result_limit / 40), max_requests),
            }
        )
        return QueryPlanValidator(
            vocabulary=ConceptVocabulary.from_yaml(self.vocabulary_path),
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=self.config.approve_large_queries,
        )

    def _extract(
        self,
        snapshot: RepositorySnapshot,
        structural_derivation_id: str,
    ) -> ValidatedExtractionProposal:
        _, providers = load_provider_configs(self.providers_path)
        provider = providers[self.config.provider]
        gate = ModelUseGate(
            ModelUsePolicy.from_yaml(self.model_policy_path),
            ModelUseContext(
                operation=ModelOperation.ONTOLOGY_EXTRACTION,
                data_class=DataClass.PUBLIC,
                input_kind=InputKind.SOURCE_CONTENT,
                run_id=f"run:ontology-build:{uuid4()}",
            ),
            budget_policy=BudgetPolicy.from_yaml(self.budget_policy_path),
            usage_ledger=UsageLedger(self.root / "usage.sqlite"),
            approval_registry=ApprovalRegistry(self.root / "usage.sqlite"),
        )
        client = ModelClient(
            self.config.provider,
            provider,
            gate=gate,
            timeout=self.config.timeout_seconds,
            parameters=self.config.model_parameters,
        )
        anchors = self._select_anchors(structural_derivation_id)
        result = AnchorGroundedExtractionManager(
            store=self.store,
            client=client,
            provider=self.config.provider,
            model=provider.model,
        ).propose(
            question=(
                f"Build reusable ontology knowledge about {snapshot.repository} as an "
                "open-source research agent. Extract only supported identity, scope, "
                "architecture, retrieval, inputs, outputs, persistent state, local-model "
                "support, licensing, security, evaluation, limitations, dissent, and gaps."
            ),
            structural_derivation_id=structural_derivation_id,
            anchor_ids=anchors,
            allowed_concept_ids=(self.config.topic_concept_id,),
            max_output_tokens=self.config.max_output_tokens,
            model_parameters=self.config.model_parameters,
            debug_reasoning=self.config.debug_reasoning,
            allow_partial_items=True,
        )
        if gate.last_authorization is not None:
            self.store.put_record("model-authorization", gate.last_authorization)
        if gate.last_settlement is not None:
            self.store.put_record("usage-settlement", gate.last_settlement)
        return result.proposal

    def _model_heartbeat(
        self,
        stop: threading.Event,
        snapshot: RepositorySnapshot,
        current: int,
        total: int,
        started: float,
    ) -> None:
        while not stop.wait(30):
            elapsed = round(time.monotonic() - started, 1)
            self.progress.event(
                "extraction",
                "running",
                f"Waiting for {snapshot.repository} ({elapsed:.0f}s elapsed)",
                current=current,
                total=total,
                repository=snapshot.repository,
                elapsed_seconds=elapsed,
                timeout_seconds=self.config.timeout_seconds,
            )

    def _select_anchors(self, derivation_id: str) -> tuple[str, ...]:
        text_by_digest: dict[str, str] = {}
        topic_terms = {
            item.casefold()
            for item in self._words.findall(
                f"{self.config.topic} {self.config.description}"
            )
        }
        anchors = [
            StructuralAnchor.model_validate(value)
            for value in self.store.iter_records("structural-anchor")
            if value.get("structural_derivation_id") == derivation_id
            and value.get("kind")
            in {
                AnchorKind.HEADING.value,
                AnchorKind.PARAGRAPH.value,
                AnchorKind.LIST_ITEM.value,
                AnchorKind.FOOTNOTE.value,
                AnchorKind.CAPTION.value,
            }
        ]
        scored: list[tuple[int, int, str]] = []
        for anchor in anchors:
            text = text_by_digest.get(anchor.source_content_sha256)
            if text is None:
                text = self.store.read_blob(anchor.source_content_sha256).decode("utf-8")
                text_by_digest[anchor.source_content_sha256] = text
            excerpt = text[anchor.start : anchor.end].casefold()
            words = set(self._words.findall(excerpt))
            score = 4 * len(words & topic_terms) + 2 * len(words & self._anchor_terms)
            score += 3 if "github.com/" in excerpt else 0
            score += 1 if anchor.kind is AnchorKind.HEADING else 0
            scored.append((-score, anchor.ordinal, anchor.id))
        configured_limit = (
            self.config.anchors_per_batch * self.config.max_batches_per_source
            if self.config.max_batches_per_source is not None
            else len(scored)
        )
        # Character and structural-record safety bounds still apply inside the
        # extraction validator; there is no ontology-wide semantic item cap.
        limit = configured_limit
        selected = sorted(scored)[:limit]
        if not selected:
            raise ValueError("source has no eligible extraction anchors")
        # The manager preserves caller order; ordinal order makes context coherent.
        return tuple(item[2] for item in sorted(selected, key=lambda item: item[1]))

    def _parsed_receipt(self, snapshot: RepositorySnapshot) -> dict[str, object] | None:
        derivations = [
            value
            for value in self.store.iter_records("structural-derivation")
            if value.get("source_version_id") == snapshot.source_version_id
        ]
        if not derivations:
            return None
        threat_ids = [
            value["id"]
            for value in self.store.iter_records("threat-observation")
            if value.get("target", {}).get("source_version") == snapshot.source_version_id
        ]
        return {
            "structural_derivation_id": derivations[-1]["id"],
            "threat_observation_ids": threat_ids,
        }

    def _snapshots(self) -> tuple[RepositorySnapshot, ...]:
        return tuple(
            RepositorySnapshot.model_validate(value)
            for value in self.store.iter_records("repository-snapshot")
        )

    def _rank_snapshots(
        self,
        snapshots: tuple[RepositorySnapshot, ...],
    ) -> tuple[RepositorySnapshot, ...]:
        topic_terms = {
            item.casefold()
            for item in self._words.findall(
                f"{self.config.topic} {self.config.description}"
            )
        }

        def score(item: RepositorySnapshot) -> tuple[int, str, str]:
            metadata = f"{item.repository} {item.description or ''}".casefold()
            words = set(self._words.findall(metadata))
            relevance = 5 * len(words & topic_terms) + 2 * len(
                words & self._anchor_terms
            )
            relevance += 12 if "deep" in words and "research" in words else 0
            relevance += 8 if "research" in words and "agent" in words else 0
            relevance -= 10 if item.archived else 0
            relevance -= 3 if item.fork else 0
            return (-relevance, item.repository.casefold(), item.commit_sha)

        return tuple(sorted(snapshots, key=score))

    def _validation_failure_counts(self) -> dict[str, int]:
        requests = {
            value["id"]: value["source_version_id"]
            for value in self.store.iter_records("extraction-request")
        }
        counts: dict[str, int] = {}
        for value in self.store.iter_records("extraction-attempt-failure"):
            if value.get("stage") != "output_validation":
                continue
            source_id = requests.get(value.get("extraction_request_id"))
            if source_id is not None:
                counts[source_id] = counts.get(source_id, 0) + 1
        return counts

    def _proposal_is_compatible(
        self,
        proposal: ValidatedExtractionProposal,
        request: ExtractionRequest,
        configured_model: str,
    ) -> bool:
        return (
            request.provider == self.config.provider
            and request.model == configured_model
            and request.max_output_tokens == self.config.max_output_tokens
            and request.model_parameters == self.config.model_parameters
            and request.debug_reasoning == self.config.debug_reasoning
            and proposal.model == configured_model
            and proposal.validator_version
            == AnchorGroundedExtractionManager.version
        )

    def _token_exhaustion(
        self,
        snapshot: RepositorySnapshot,
        error: ModelOutputTruncatedError,
    ) -> TokenLimitExhaustion:
        _, providers = load_provider_configs(self.providers_path)
        provider = providers[self.config.provider]
        recommendations = []
        if self.config.max_output_tokens < provider.max_output_tokens:
            recommendations.append(
                f"Increase max_output_tokens above {self.config.max_output_tokens} "
                f"and at most {provider.max_output_tokens} for provider "
                f"{self.config.provider}."
            )
        else:
            recommendations.append(
                f"Choose or configure a provider/model with more than "
                f"{provider.max_output_tokens} output tokens."
            )
        recommendations.append(
            "Alternatively split this source into smaller independently grounded "
            "extraction batches without reducing ontology-wide coverage."
        )
        return TokenLimitExhaustion(
            source=snapshot.repository,
            provider=self.config.provider,
            model=provider.model,
            requested_output_tokens=self.config.max_output_tokens,
            provider_output_token_limit=provider.max_output_tokens,
            observed_output_tokens=error.output_tokens,
            recommendations=tuple(recommendations),
        )

    def _write_tainted_source_index(
        self,
        snapshots: tuple[RepositorySnapshot, ...],
    ) -> Path:
        observations_by_source: dict[str, list[ThreatObservation]] = {}
        for value in self.store.iter_records("threat-observation"):
            observation = ThreatObservation.model_validate(value)
            observations_by_source.setdefault(
                observation.target.source_version, []
            ).append(observation)

        entries = []
        for snapshot in snapshots:
            observations = tuple(
                sorted(
                    observations_by_source.get(snapshot.source_version_id, ()),
                    key=lambda item: item.id,
                )
            )
            if not observations:
                continue
            entries.append(
                TaintedSourceEntry(
                    repository=snapshot.repository,
                    canonical_locator=snapshot.canonical_locator,
                    commit_sha=snapshot.commit_sha,
                    source_version_id=snapshot.source_version_id,
                    source_content_sha256=snapshot.source_content_sha256,
                    license=snapshot.license,
                    observed_at=snapshot.observed_at,
                    observations=tuple(
                        TaintedSourceObservation(
                            id=item.id,
                            threat_type=item.threat_type,
                            status=item.status.value,
                            severity=item.severity.value,
                            detected_at=item.detected_at,
                            detector_kind=item.detector.kind.value,
                            detector_id=item.detector.id,
                            detector_version=item.detector.version,
                            evidence_fragment_ids=item.evidence,
                        )
                        for item in observations
                    ),
                )
            )
        index = TaintedSourceIndex(
            topic=self.config.topic,
            recorded_through=max(
                (
                    item.detected_at
                    for entry in entries
                    for item in entry.observations
                ),
                default=None,
            ),
            entries=tuple(sorted(entries, key=lambda item: item.repository.casefold())),
        )
        relative = (
            self.config.tainted_source_index
            or self.config.output_directory.parent / "tainted-sources.yaml"
        )
        unresolved = self.workspace / relative
        if unresolved.is_symlink():
            raise ValueError("tainted source index path cannot replace a symlink")
        path = unresolved.resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise ValueError("tainted source index path escapes the workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            yaml.safe_dump(
                index.model_dump(mode="json", exclude_none=True),
                allow_unicode=True,
                sort_keys=False,
            )
        )
        os.replace(temporary, path)
        return path.relative_to(self.workspace.resolve())

    def _finalize(self) -> tuple[Path, Path, Path, bool]:
        audit = DeterministicKnowledgeAuditor().audit(self.store, as_of=utc_now())
        self.store.put_record("knowledge-audit-report", audit)
        truth_manager = TruthManager(
            workspace_root=self.workspace,
            store_root=self.root,
            policy=TruthPolicy.from_yaml(self.truth_policy_path),
        )
        snapshot = truth_manager.capture(created_by=f"ontology-builder:{self.config_sha256}")
        self.store.put_record("truth-snapshot", snapshot)
        snapshot_path = self.root / "truth-snapshot.json"
        snapshot_path.write_bytes(
            json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True).encode()
            + b"\n"
        )
        database_path = self.root / "query.sqlite"
        SQLiteKnowledgeProjection(store=self.store, workspace_root=self.workspace).build(
            database_path,
            snapshot=snapshot,
            truth_manager=truth_manager,
        )
        topic_path = self.root / "topic.md"
        topic = KnowledgeQueryEngine(database_path).topic(self.config.topic_concept_id)
        topic_path.write_text(render_topic_markdown(topic))
        return snapshot_path, database_path, topic_path, audit.clean

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {
                "version": 1,
                "config_sha256": self.config_sha256,
                "discovery_config_sha256": self.discovery_config_sha256,
                "imported_bundles": [],
                "queries_completed": [],
                "proposals": [],
                "candidate_bundles": [],
                "skipped_tainted_sources": [],
                "skipped_unlicensed_sources": [],
                "failures": [],
                "token_limit_exhaustions": [],
                "completed": False,
            }
        value = json.loads(self.state_path.read_bytes())
        if value.get("config_sha256") != self.config_sha256:
            if value.get("discovery_config_sha256") != self.discovery_config_sha256:
                raise ValueError(
                    "runtime root contains state for different discovery settings; "
                    "choose a new root"
                )
            value["config_sha256"] = self.config_sha256
            value["failures"] = []
            value["token_limit_exhaustions"] = []
            value["completed"] = False
        value.setdefault("discovery_config_sha256", self.discovery_config_sha256)
        return value

    def _save_state(self, state: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(json.dumps(state, indent=2, sort_keys=True).encode() + b"\n")
        os.replace(temporary, self.state_path)
