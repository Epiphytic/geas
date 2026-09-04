"""Explicit-deny deterministic fakes; none consult wall time or the network."""

from __future__ import annotations

from datetime import datetime

from research_agent.capabilities import CapabilityDecision, CapabilityEvaluator, CapabilityRequest
from research_agent.publishing import PublishRequest, PublishResult, RepositoryPublisher
from research_agent.source_intent import SourceAdapter, SourceCandidate, SourceIntent
from research_agent.source_work import SourceCheckpoint, SourceWorkItem, SourceWorkStore


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeCapabilityEvaluator(CapabilityEvaluator):
    def __init__(self, decisions: dict[str, CapabilityDecision] | None = None) -> None:
        self.decisions = decisions or {}
        self.requests: list[CapabilityRequest] = []

    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        self.requests.append(request)
        try:
            return self.decisions[request.id]
        except KeyError as error:
            raise PermissionError(
                "fake capability evaluator denies unconfigured requests"
            ) from error


class FakeSourceAdapter(SourceAdapter):
    def __init__(
        self,
        *,
        adapter_id: str = "fake-source",
        version: str = "1",
        discoveries: dict[str, tuple[SourceCandidate, ...]] | None = None,
        checkpoints: dict[str, SourceCheckpoint] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.version = version
        self.discoveries = discoveries or {}
        self.checkpoints = checkpoints or {}
        self.discovery_calls: list[SourceIntent] = []
        self.fetch_calls: list[tuple[SourceCandidate, SourceCheckpoint | None]] = []

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        self.discovery_calls.append(intent)
        try:
            return self.discoveries[intent.id]
        except KeyError as error:
            raise PermissionError(
                "fake source adapter denies unconfigured discovery"
            ) from error

    def fetch(
        self, candidate: SourceCandidate, *, prior: SourceCheckpoint | None
    ) -> SourceCheckpoint:
        self.fetch_calls.append((candidate, prior))
        try:
            return self.checkpoints[candidate.id]
        except KeyError as error:
            raise PermissionError("fake source adapter denies unconfigured fetches") from error


class FakeSourceWorkStore(SourceWorkStore):
    def __init__(self) -> None:
        self.items: dict[str, SourceWorkItem] = {}
        self.checkpoints: dict[str, SourceCheckpoint] = {}

    def get(self, work_item_id: str) -> SourceWorkItem | None:
        return self.items.get(work_item_id)

    def put(self, item: SourceWorkItem) -> SourceWorkItem:
        self.items.setdefault(item.id, item)
        return self.items[item.id]

    def checkpoint(self, checkpoint: SourceCheckpoint) -> SourceCheckpoint:
        self.checkpoints.setdefault(checkpoint.id, checkpoint)
        return self.checkpoints[checkpoint.id]


class FakeRepositoryPublisher(RepositoryPublisher):
    def __init__(self, results: dict[str, PublishResult] | None = None) -> None:
        self.results = results or {}
        self.requests: list[PublishRequest] = []

    def publish(self, request: PublishRequest) -> PublishResult:
        self.requests.append(request)
        try:
            return self.results[request.id]
        except KeyError as error:
            raise PermissionError(
                "fake repository publisher denies unconfigured requests"
            ) from error
