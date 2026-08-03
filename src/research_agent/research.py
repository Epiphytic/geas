from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import Field

from research_agent.discovery import (
    AccessConstraint,
    AccessConstraintReason,
    AcquisitionAttempt,
    AcquisitionRequest,
    AcquisitionState,
    ConnectorCapability,
    CoverageRun,
    DiscoveryCandidate,
    DiscoveryConnector,
    DiscoveryHit,
    DiscoveryRequest,
    DiscoveryRun,
    QueryPlan,
    ResearchConnector,
    SourceClass,
    identified,
)
from research_agent.models import SourceVersion, StrictModel, utc_now
from research_agent.store import ImmutableStore


class OfflineResearchResult(StrictModel):
    query_plan: QueryPlan
    discovery_run: DiscoveryRun
    hits: tuple[DiscoveryHit, ...]
    acquisition_attempts: tuple[AcquisitionAttempt, ...]
    source_versions: tuple[SourceVersion, ...]
    access_constraints: tuple[AccessConstraint, ...]
    coverage: CoverageRun
    record_hashes: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class DiscoveryExecution(StrictModel):
    discovery_run: DiscoveryRun
    hits: tuple[DiscoveryHit, ...]


class DiscoveryExecutor:
    """Executes a validated discovery plan without acquisition or model access."""

    version = "discovery-executor/1"

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock

    def run(self, plan: QueryPlan, connector: DiscoveryConnector) -> DiscoveryExecution:
        manifest = connector.manifest
        if manifest.id not in plan.connector_ids:
            raise ValueError(f"plan does not authorize connector {manifest.id}")
        if ConnectorCapability.DISCOVERY not in manifest.capabilities:
            raise ValueError(f"connector {manifest.id} does not declare discovery")
        started_at = self.clock()
        request = DiscoveryRequest(
            query_plan_id=plan.id,
            exact_terms=plan.exact_terms,
            match=plan.match,
            result_limit=min(plan.result_limit, manifest.max_results),
            page_limit=min(plan.page_limit, manifest.max_pages),
            languages=plan.languages,
        )

        candidates: list[DiscoveryCandidate] = []
        cursors: list[str] = []
        response_hashes: list[str] = []
        seen: set[tuple[str, str]] = set()
        duplicate_count = 0
        rejection_count = 0
        error_count = 0
        page_count = 0
        empty_pages = 0
        truncated = False
        reported_cost_microusd = 0
        termination = "connector_exhausted"
        for page in connector.discover(request):
            page_count += 1
            rejection_count += page.rejected_count
            error_count += page.error_count
            reported_cost_microusd += page.reported_cost_microusd
            if page.cursor is not None:
                cursors.append(page.cursor)
            if page.response_sha256 is not None:
                response_hashes.append(page.response_sha256)
            if not page.candidates:
                empty_pages += 1
                if empty_pages >= plan.stop_after_empty_pages:
                    termination = "empty_page_limit"
                    break
            else:
                empty_pages = 0
            for candidate in page.candidates:
                key = (candidate.upstream_id, candidate.canonical_locator)
                if key in seen:
                    duplicate_count += 1
                    continue
                seen.add(key)
                candidates.append(candidate)
                if len(candidates) >= plan.result_limit:
                    termination = "result_limit"
                    truncated = page.next_cursor is not None
                    break
            if len(candidates) >= plan.result_limit:
                break
            if page_count >= plan.page_limit:
                termination = "page_limit"
                truncated = page.next_cursor is not None
                break

        ended_at = self.clock()
        normalized_query = connector.normalize_query(request)
        run_fields = {
            "query_plan_id": plan.id,
            "connector_id": manifest.id,
            "connector_version": manifest.version,
            "normalized_query": normalized_query,
            "started_at": started_at,
            "ended_at": ended_at,
            "pagination_cursors": tuple(cursors),
            "response_sha256s": tuple(response_hashes),
            "termination_reason": termination,
            "result_count": len(candidates),
            "duplicate_count": duplicate_count,
            "rejection_count": rejection_count,
            "error_count": error_count,
            "truncated": truncated,
            "reported_cost_microusd": reported_cost_microusd,
            "executor_version": self.version,
        }
        discovery_run = DiscoveryRun(
            id=identified("discovery-run", run_fields),
            query_plan_id=plan.id,
            connector_id=manifest.id,
            connector_version=manifest.version,
            normalized_query=normalized_query,
            started_at=started_at,
            ended_at=ended_at,
            pagination_cursors=tuple(cursors),
            response_sha256s=tuple(response_hashes),
            termination_reason=termination,
            result_count=len(candidates),
            duplicate_count=duplicate_count,
            rejection_count=rejection_count,
            error_count=error_count,
            truncated=truncated,
            reported_cost_microusd=reported_cost_microusd,
        )
        hits = tuple(
            self._hit(candidate, discovery_run.id, rank)
            for rank, candidate in enumerate(candidates, start=1)
        )
        return DiscoveryExecution(discovery_run=discovery_run, hits=hits)

    @staticmethod
    def _hit(candidate: DiscoveryCandidate, run_id: str, rank: int) -> DiscoveryHit:
        fields = {
            "upstream_id": candidate.upstream_id,
            "canonical_locator": candidate.canonical_locator,
            "title": candidate.title,
            "authors": candidate.authors,
            "publisher": candidate.publisher,
            "published_at": candidate.published_at,
            "media_type": candidate.media_type,
            "language": candidate.language,
            "snippet": candidate.snippet,
            "known_entity_ids": candidate.known_entity_ids,
            "metadata": candidate.metadata,
            "discovery_run_id": run_id,
        }
        return DiscoveryHit(
            id=identified("discovery-hit", fields),
            upstream_id=candidate.upstream_id,
            canonical_locator=candidate.canonical_locator,
            title=candidate.title,
            authors=candidate.authors,
            publisher=candidate.publisher,
            published_at=candidate.published_at,
            media_type=candidate.media_type,
            language=candidate.language,
            upstream_rank=rank,
            snippet=candidate.snippet,
            discovery_run_id=run_id,
            known_entity_ids=candidate.known_entity_ids,
            acquisition_eligible=True,
            metadata=candidate.metadata,
        )


class OfflineResearchRunner:
    """Executes a validated plan without a model, network, credentials, or graph writes."""

    version = "offline-research-runner/1"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        connector: ResearchConnector,
        clock: Callable[[], datetime] = utc_now,
        freshness: timedelta = timedelta(days=7),
    ) -> None:
        self.store = store
        self.connector = connector
        self.clock = clock
        self.freshness = freshness

    def run(self, plan: QueryPlan, *, topic_branch: str = "topic:local") -> OfflineResearchResult:
        manifest = self.connector.manifest
        discovery = DiscoveryExecutor(clock=self.clock).run(plan, self.connector)
        discovery_run = discovery.discovery_run
        hits = discovery.hits
        attempts: list[AcquisitionAttempt] = []
        sources: list[SourceVersion] = []
        constraints: list[AccessConstraint] = []
        for hit in hits:
            attempted_at = self.clock()
            try:
                acquired = self.connector.acquire(
                    AcquisitionRequest(
                        discovery_hit_id=hit.id,
                        locator=hit.canonical_locator,
                        max_content_bytes=plan.max_content_bytes,
                    )
                )
                source = self.store.ingest_bytes(
                    acquired.content,
                    source_uri=acquired.locator,
                    media_type=acquired.media_type,
                    connector_id=manifest.id,
                    acquired_at=attempted_at,
                )
                sources.append(source)
                attempt_fields = {
                    "discovery_hit_id": hit.id,
                    "connector_id": manifest.id,
                    "resolved_locator": acquired.locator,
                    "outcome": "success",
                    "state": AcquisitionState.CONTENT_ACQUIRED,
                    "attempted_at": attempted_at,
                    "content_sha256": source.content_sha256,
                }
                attempts.append(
                    AcquisitionAttempt(
                        id=identified("acquisition-attempt", attempt_fields),
                        discovery_hit_id=hit.id,
                        connector_id=manifest.id,
                        resolved_locator=acquired.locator,
                        outcome="success",
                        state=AcquisitionState.CONTENT_ACQUIRED,
                        attempted_at=attempted_at,
                        content_length=len(acquired.content),
                        media_type=acquired.media_type,
                        content_sha256=source.content_sha256,
                        policy_outcome="allow_configured_local_root",
                    )
                )
            except (OSError, ValueError, RuntimeError) as error:
                reason = self._constraint_reason(error)
                constraint_fields = {
                    "target_id": hit.id,
                    "locator": hit.canonical_locator,
                    "reason": reason,
                    "observed_at": attempted_at,
                    "connector_id": manifest.id,
                }
                constraint = AccessConstraint(
                    id=identified("access-constraint", constraint_fields),
                    target_id=hit.id,
                    locator=hit.canonical_locator,
                    reason=reason,
                    observed_at=attempted_at,
                    connector_id=manifest.id,
                    human_resolvable=True,
                    detail=str(error),
                )
                constraints.append(constraint)
                attempt_fields = {
                    "discovery_hit_id": hit.id,
                    "connector_id": manifest.id,
                    "resolved_locator": hit.canonical_locator,
                    "outcome": reason,
                    "state": AcquisitionState.DISCOVERED,
                    "attempted_at": attempted_at,
                }
                attempts.append(
                    AcquisitionAttempt(
                        id=identified("acquisition-attempt", attempt_fields),
                        discovery_hit_id=hit.id,
                        connector_id=manifest.id,
                        resolved_locator=hit.canonical_locator,
                        outcome=reason,
                        state=AcquisitionState.DISCOVERED,
                        attempted_at=attempted_at,
                        policy_outcome="not_acquired",
                        retry_classification="operator_action",
                    )
                )

        measured_at = self.clock()
        gap_ids = self._gap_ids(plan, len(sources), len(hits))
        searched = plan.source_classes
        excluded = frozenset(set(SourceClass) - set(searched))
        coverage_fields = {
            "query_plan_id": plan.id,
            "topic_branch": topic_branch,
            "competency_questions": (plan.question,),
            "discovery_run_ids": (discovery_run.id,),
            "searched_source_classes": sorted(item.value for item in searched),
            "excluded_source_classes": sorted(item.value for item in excluded),
            "languages": plan.languages,
            "accessible_count": len(sources),
            "inaccessible_count": len(constraints),
            "metadata_only_count": max(0, len(hits) - len(sources) - len(constraints)),
            "unresolved_gap_ids": gap_ids,
            "measured_at": measured_at,
            "freshness_deadline": measured_at + self.freshness,
        }
        coverage = CoverageRun(
            id=identified("coverage-run", coverage_fields),
            query_plan_id=plan.id,
            topic_branch=topic_branch,
            competency_questions=(plan.question,),
            discovery_run_ids=(discovery_run.id,),
            searched_source_classes=searched,
            excluded_source_classes=excluded,
            languages=plan.languages,
            accessible_count=len(sources),
            inaccessible_count=len(constraints),
            metadata_only_count=max(0, len(hits) - len(sources) - len(constraints)),
            known_index_limitations=(
                "Only operator-selected local roots were searched.",
                "Binary formats were matched only by filename.",
            ),
            unresolved_gap_ids=gap_ids,
            measured_at=measured_at,
            freshness_deadline=measured_at + self.freshness,
        )

        records: dict[str, tuple[object, ...]] = {
            "query-plan": (plan,),
            "connector-manifest": (manifest,),
            "discovery-run": (discovery_run,),
            "discovery-hit": hits,
            "acquisition-attempt": tuple(attempts),
            "access-constraint": tuple(constraints),
            "coverage-run": (coverage,),
        }
        record_hashes = {
            kind: tuple(self.store.put_record(kind, record) for record in values)
            for kind, values in records.items()
        }
        return OfflineResearchResult(
            query_plan=plan,
            discovery_run=discovery_run,
            hits=hits,
            acquisition_attempts=tuple(attempts),
            source_versions=tuple(sources),
            access_constraints=tuple(constraints),
            coverage=coverage,
            record_hashes=record_hashes,
        )

    @staticmethod
    def _constraint_reason(error: Exception) -> AccessConstraintReason:
        message = str(error).casefold()
        if "size" in message or "limit" in message:
            return AccessConstraintReason.SIZE_LIMIT
        if "media type" in message:
            return AccessConstraintReason.UNSUPPORTED_MEDIA_TYPE
        if "escape" in message or "scheme" in message:
            return AccessConstraintReason.POLICY
        return AccessConstraintReason.NOT_FOUND

    @staticmethod
    def _gap_ids(plan: QueryPlan, accessible: int, discovered: int) -> tuple[str, ...]:
        gaps: list[str] = []
        if discovered == 0:
            gaps.append(identified("knowledge-gap", {"plan": plan.id, "reason": "no_results"}))
        if accessible < plan.minimum_independent_sources:
            gaps.append(
                identified(
                    "knowledge-gap",
                    {
                        "plan": plan.id,
                        "reason": "independent_source_minimum",
                        "required": plan.minimum_independent_sources,
                        "accessible": accessible,
                    },
                )
            )
        return tuple(gaps)
