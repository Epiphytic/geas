from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from research_agent.models import StrictModel


class ConcurrencyProfile(StrictModel):
    canonical_writers: Literal[1]
    research_workers: int = Field(ge=1, le=32)
    query_readers: int = Field(ge=1, le=32)
    writes_are_serialized: Literal[True]


class BenchmarkTier(StrictModel):
    name: Literal["smoke", "standard", "scale"]
    claims: int = Field(gt=0)


class WorkloadPolicy(StrictModel):
    version: int = Field(ge=1)
    deployment_target: Literal["local_single_user_cli"]
    concurrency: ConcurrencyProfile
    benchmark_tiers: tuple[BenchmarkTier, ...]
    priorities: tuple[
        Literal[
            "inspectability",
            "deterministic_rebuilds",
            "crash_recovery",
            "portability",
            "local_query_latency",
        ],
        ...,
    ]
    backend_migration_requires_measurement: Literal[True]

    @model_validator(mode="after")
    def tiers_and_priorities_are_complete(self) -> WorkloadPolicy:
        tier_names = tuple(tier.name for tier in self.benchmark_tiers)
        if tier_names != ("smoke", "standard", "scale"):
            raise ValueError("benchmark tiers must be ordered smoke, standard, scale")
        claim_counts = tuple(tier.claims for tier in self.benchmark_tiers)
        if claim_counts != tuple(sorted(set(claim_counts))):
            raise ValueError("benchmark claim counts must increase without duplicates")
        if len(self.priorities) != len(set(self.priorities)):
            raise ValueError("workload priorities must not contain duplicates")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> WorkloadPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))
