from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import yaml

from research_agent.ontology_build import OntologyBuildConfig, OntologyUpdateService
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
)
from research_agent.source_work import SourceUpdateReceipt

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def _intent(identifier: str = "issuer-news") -> SourceIntent:
    return SourceIntent(
        id=identifier,
        role="issuer_news",
        discovery=SourceDiscovery(
            kind=DiscoveryKind.DIRECT_URL,
            locator="https://issuer.example/news/latest.txt",
        ),
        allowed_hosts=("issuer.example",),
        allowed_path_prefixes=("/news/",),
        accepted_media_types=("text/plain",),
        refresh=SourceRefreshPolicy(interval_seconds=900, max_items=2, max_depth=0),
        required=True,
        priority=10,
        associations=SourceAssociations(topics=("issuer",)),
        temporal=SourceTemporalPolicy(field="published_at", retention="append_only"),
        created_at=NOW,
    )


def _config(**updates: object) -> OntologyBuildConfig:
    values: dict[str, object] = {
        "version": 1,
        "topic": "Issuer",
        "topic_concept_id": "concept:issuer",
        "output_directory": "ontology/issuer",
    }
    values.update(updates)
    return OntologyBuildConfig.model_validate(values)


def test_absent_source_intent_preserves_existing_build_configuration() -> None:
    config = _config()

    assert config.source_intent == ()


def test_explicit_yaml_writes_source_intents_and_every_update_default() -> None:
    rendered = yaml.safe_load(_config(source_intent=[_intent()]).explicit_yaml())

    assert rendered["source_intent"][0]["id"] == "issuer-news"
    assert rendered["source_work"] == {
        "max_requests_per_run": 50,
        "max_bytes_per_run": 100_000_000,
        "max_depth": 1,
        "refresh_interval_seconds": 3600,
        "max_run_seconds": 1800.0,
        "finalization_reserve_seconds": 120.0,
    }


def test_update_dispatches_named_ontology_with_caller_clock() -> None:
    config = _config(source_intent=[_intent()])
    calls: list[tuple[tuple[SourceIntent, ...], datetime]] = []
    receipt = SourceUpdateReceipt(
        source_intent_id="issuer-news",
        complete=True,
        finalized_at=NOW,
    )

    class FakeCoordinator:
        def run_due(self, intents, *, now):
            calls.append((intents, now))
            return receipt

    service = OntologyUpdateService(
        configs={"issuer": config},
        coordinators={"issuer": FakeCoordinator()},
    )

    assert service.update("issuer", now=NOW) == receipt
    assert calls == [((config.source_intent[0],), NOW)]


def test_update_with_no_source_intent_is_a_deterministic_noop() -> None:
    config = _config()
    coordinator = SimpleNamespace(
        run_due=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coordinator must not run")
        )
    )

    receipt = OntologyUpdateService(
        configs={"issuer": config},
        coordinators={"issuer": coordinator},
    ).update("issuer", now=NOW)

    assert receipt.complete
    assert receipt.source_intent_ids == ()
    assert receipt.work_item_ids == ()
