from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.workload import WorkloadPolicy


def test_checked_in_workload_targets_local_single_user_cli() -> None:
    policy = WorkloadPolicy.from_yaml(Path("config/workload-policy.yaml"))

    assert policy.deployment_target == "local_single_user_cli"
    assert policy.concurrency.canonical_writers == 1
    assert policy.concurrency.writes_are_serialized
    assert policy.concurrency.research_workers == 4
    assert policy.concurrency.query_readers == 4
    assert [tier.claims for tier in policy.benchmark_tiers] == [
        10_000,
        100_000,
        1_000_000,
    ]


def test_policy_cannot_silently_enable_multiple_canonical_writers() -> None:
    policy = WorkloadPolicy.from_yaml(Path("config/workload-policy.yaml"))
    raw = policy.model_dump(mode="json")
    raw["concurrency"]["canonical_writers"] = 2

    with pytest.raises(ValidationError):
        WorkloadPolicy.model_validate(raw)


def test_benchmark_tiers_must_increase() -> None:
    policy = WorkloadPolicy.from_yaml(Path("config/workload-policy.yaml"))
    raw = policy.model_dump(mode="json")
    raw["benchmark_tiers"][2]["claims"] = 50_000

    with pytest.raises(ValidationError, match="must increase"):
        WorkloadPolicy.model_validate(raw)
