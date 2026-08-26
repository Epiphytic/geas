from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, model_validator

from research_agent.models import ModelParameters, StrictModel


class OntologyBuildDefaults(StrictModel):
    """Globally reusable defaults for non-identity ontology build settings."""

    ontology_facets: tuple[str, ...] = (
        "identity",
        "scope",
        "architecture",
        "interfaces",
        "inputs",
        "outputs",
        "persistent state",
        "security",
        "evaluation",
        "limitations",
        "dissent",
        "knowledge gaps",
    )
    discovery_enabled: bool = True
    include_gap_queries: bool = True
    refresh_after_hours: int | None = Field(default=168, ge=1, le=87_600)
    max_queries: int | None = Field(default=None, ge=1, le=10_000)
    result_limit: int = Field(default=30, ge=1, le=200)
    approve_large_queries: bool = False
    repository_limit_per_query: int = Field(default=20, ge=1, le=100)
    provider: str = "deepseek_local"
    max_output_tokens: int = Field(default=65_536, ge=1024, le=524_288)
    model_parameters: ModelParameters = Field(default_factory=ModelParameters)
    debug_reasoning: bool = True
    timeout_seconds: float = Field(default=3600.0, ge=1.0, le=86_400.0)
    max_run_seconds: float = Field(default=1800.0, gt=0.0)
    minimum_model_window_seconds: float = Field(default=300.0, gt=0.0)
    finalization_reserve_seconds: float = Field(default=120.0, ge=0.0)
    work_claim_grace_seconds: float = Field(default=60.0, ge=0.0)
    connection_attempts: int = Field(default=10, ge=1, le=20)
    connection_retry_seconds: float = Field(default=2.0, ge=0.0, le=30.0)
    anchors_per_batch: int = Field(default=200, ge=1, le=200)
    max_batches_per_source: int | None = Field(default=None, ge=1, le=500)
    max_sources: int | None = Field(default=None, ge=1, le=10_000)
    model_parallelism: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def defaults_are_safe(self) -> OntologyBuildDefaults:
        if self.model_parallelism != 1:
            raise ValueError("ontology extraction currently requires model_parallelism: 1")
        if self.minimum_model_window_seconds >= self.max_run_seconds:
            raise ValueError(
                "minimum_model_window_seconds must leave time inside max_run_seconds"
            )
        if self.finalization_reserve_seconds >= self.max_run_seconds:
            raise ValueError(
                "finalization_reserve_seconds must be less than max_run_seconds"
            )
        return self

    def merge_ontology(self, value: Mapping[str, object]) -> dict[str, object]:
        """Overlay ontology-local values, preserving explicit null overrides."""
        merged = self.model_dump(mode="python", exclude_none=False)
        local_parameters = value.get("model_parameters")
        if isinstance(local_parameters, Mapping):
            parameters = self.model_parameters.model_dump(mode="python", exclude_none=False)
            parameters.update(local_parameters)
            merged["model_parameters"] = parameters
        merged.update(
            (key, item)
            for key, item in value.items()
            if key != "model_parameters" or not isinstance(item, Mapping)
        )
        return merged
