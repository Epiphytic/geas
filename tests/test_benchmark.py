from pathlib import Path

from research_agent.benchmark import ProjectionBenchmark
from research_agent.truth import TruthPolicy


def test_projection_benchmark_exercises_canonical_and_query_paths() -> None:
    result = ProjectionBenchmark(
        workspace_root=Path("."),
        truth_policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
    ).run(tier="test", claim_count=25)

    assert result.claim_count == 25
    assert result.projected_counts["claims"] == 25
    assert result.database_bytes > 0
    assert result.query_median_ms > 0
