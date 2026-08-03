from __future__ import annotations

import hashlib
import resource
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from research_agent.knowledge import Concept
from research_agent.models import (
    Claim,
    EvidenceFragment,
    EvidenceSelector,
    ReviewState,
    StrictModel,
    content_id,
)
from research_agent.projection import KnowledgeQueryEngine, SQLiteKnowledgeProjection
from research_agent.store import ImmutableStore
from research_agent.truth import TruthManager, TruthPolicy


class ProjectionBenchmarkResult(StrictModel):
    tier: str
    claim_count: int = Field(gt=0)
    canonical_write_seconds: float = Field(ge=0)
    snapshot_seconds: float = Field(ge=0)
    projection_build_seconds: float = Field(ge=0)
    query_median_ms: float = Field(ge=0)
    query_max_ms: float = Field(ge=0)
    database_bytes: int = Field(ge=0)
    peak_rss_kib: int = Field(ge=0)
    projected_counts: dict[str, int]
    benchmark_version: str


class ProjectionBenchmark:
    version = "projection-benchmark/1"

    def __init__(self, *, workspace_root: Path, truth_policy: TruthPolicy) -> None:
        self.workspace_root = workspace_root.resolve()
        self.truth_policy = truth_policy

    def run(self, *, tier: str, claim_count: int) -> ProjectionBenchmarkResult:
        if claim_count < 1:
            raise ValueError("benchmark claim count must be positive")
        with tempfile.TemporaryDirectory(prefix="research-agent-benchmark-") as directory:
            root = Path(directory)
            store = ImmutableStore(root / "data")
            store.initialize()
            started = time.perf_counter()
            source_content = b"Deterministic benchmark evidence for ontology retrieval."
            source = store.ingest_bytes(
                source_content,
                source_uri="urn:benchmark:source",
                media_type="text/plain",
                connector_id="connector:benchmark",
                acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            selector = EvidenceSelector(
                type="text_quote",
                exact=source_content.decode(),
            )
            fragment_fields = {
                "source_version": source.id,
                "selector": selector,
                "content_sha256": hashlib.sha256(source_content).hexdigest(),
            }
            fragment = EvidenceFragment(
                id=content_id("evidence-fragment", fragment_fields),
                source_version=source.id,
                selector=selector,
                content_sha256=fragment_fields["content_sha256"],
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            store.put_record("evidence-fragment", fragment)
            concept = Concept(
                id="concept:benchmark",
                label="Benchmark topic",
                description="Synthetic deterministic projection workload.",
                synonyms=("projection workload",),
                recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
                recorded_by="system:benchmark",
            )
            store.put_record("concept", concept)
            pending: list[Claim] = []
            for index in range(claim_count):
                fields = {
                    "subject": concept.id,
                    "predicate": f"ep:benchmark_property_{index % 100}",
                    "object": f"benchmark retrieval value {index}",
                    "qualifiers": {"bucket": index % 100},
                    "stance": "reports",
                    "epistemic_status": "observed",
                    "asserted_by": "system:benchmark",
                    "evidence": (fragment.id,),
                    "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "review_state": ReviewState.ACCEPTED,
                }
                pending.append(Claim(id=content_id("claim", fields), **fields))
                if len(pending) == 10_000:
                    store.put_record_batch("claim", pending)
                    pending = []
            if pending:
                store.put_record_batch("claim", pending)
            canonical_write_seconds = time.perf_counter() - started

            manager = TruthManager(
                workspace_root=self.workspace_root,
                store_root=store.root,
                policy=self.truth_policy,
            )
            started = time.perf_counter()
            snapshot = manager.capture(created_by="system:benchmark")
            snapshot_seconds = time.perf_counter() - started

            database = root / "query.sqlite"
            started = time.perf_counter()
            build = SQLiteKnowledgeProjection(
                store=store,
                workspace_root=self.workspace_root,
            ).build(
                database,
                snapshot=snapshot,
                truth_manager=manager,
            )
            projection_build_seconds = time.perf_counter() - started

            engine = KnowledgeQueryEngine(database)
            timings: list[float] = []
            for _ in range(7):
                started = time.perf_counter()
                result = engine.query("benchmark retrieval value", limit=25)
                timings.append((time.perf_counter() - started) * 1000)
                if not result.hits:
                    raise RuntimeError("benchmark query returned no results")
            return ProjectionBenchmarkResult(
                tier=tier,
                claim_count=claim_count,
                canonical_write_seconds=canonical_write_seconds,
                snapshot_seconds=snapshot_seconds,
                projection_build_seconds=projection_build_seconds,
                query_median_ms=statistics.median(timings),
                query_max_ms=max(timings),
                database_bytes=database.stat().st_size,
                peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                projected_counts=build.counts,
                benchmark_version=self.version,
            )
