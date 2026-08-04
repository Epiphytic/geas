from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from research_agent.cli import _ontology_build_exit_code
from research_agent.ontology_build import (
    BuildProgress,
    OntologyBuildConfig,
    OntologyBuilder,
    OntologyBuildReceipt,
    TokenLimitExhaustion,
)


def _builder(tmp_path: Path, config: OntologyBuildConfig) -> OntologyBuilder:
    return OntologyBuilder(
        config=config,
        root=tmp_path / "runtime",
        workspace=Path("."),
        providers_path=Path("config/providers.toml"),
        research_policy_path=Path("config/research-policy.yaml"),
        model_policy_path=Path("config/model-policy.yaml"),
        budget_policy_path=Path("config/budget-policy.yaml"),
        truth_policy_path=Path("config/truth-policy.yaml"),
        vocabulary_path=Path("config/concept-vocabulary.yaml"),
    )


def test_shipped_build_config_keeps_serial_128k_capacity_without_coverage_caps() -> None:
    config = OntologyBuildConfig.from_yaml(
        Path("ontology/open-source-research-agents/build.yaml")
    )

    assert config.model_parallelism == 1
    assert config.max_output_tokens == 131_072
    assert config.timeout_seconds == 14_400
    assert config.include_gap_queries
    assert config.max_queries is None
    assert config.max_sources is None
    assert config.max_batches_per_source is None
    assert config.approve_large_queries is True
    assert config.model_parameters.reasoning_effort == "high"
    assert config.debug_reasoning is True


def test_shipped_reasoning_decision_is_backed_by_recorded_metrics() -> None:
    evaluation = yaml.safe_load(
        Path(
            "ontology/open-source-research-agents/model-evaluation.yaml"
        ).read_text()
    )
    assert evaluation["decision"]["reasoning_effort"] == "high"
    assert evaluation["high"]["claims"] > evaluation["max"]["claims"]
    assert (
        evaluation["high"]["distinct_predicates"]
        > evaluation["max"]["distinct_predicates"]
    )
    assert evaluation["high"]["unique_claim_ratio"] == 1.0
    assert evaluation["max"]["unique_claim_ratio"] == 1.0


def test_general_ontology_default_is_64k() -> None:
    config = OntologyBuildConfig.model_validate(
        {
            "version": 1,
            "topic": "Test",
            "topic_concept_id": "concept:test",
            "output_directory": "ontology/test/generated",
        }
    )
    assert config.max_output_tokens == 65_536


def test_max_reasoning_requires_384_kib_context() -> None:
    config = OntologyBuildConfig.model_validate(
        {
            "version": 1,
            "topic": "Test",
            "topic_concept_id": "concept:test",
            "output_directory": "ontology/test/generated",
            "model_parameters": {
                "thinking": True,
                "reasoning_effort": "max",
            },
        }
    )
    assert config.model_parameters.minimum_context_tokens == 393_216


def test_model_tuning_does_not_invalidate_discovery_checkpoint(tmp_path) -> None:
    base = {
        "version": 1,
        "topic": "Test",
        "topic_concept_id": "concept:test",
        "queries": ["one", "two"],
        "output_directory": "ontology/test/generated",
    }
    high = _builder(tmp_path, OntologyBuildConfig.model_validate(base))
    maximum = _builder(
        tmp_path,
        OntologyBuildConfig.model_validate(
            {
                **base,
                "max_output_tokens": 131_072,
                "model_parameters": {
                    "thinking": True,
                    "reasoning_effort": "max",
                },
            }
        ),
    )
    changed_discovery = _builder(
        tmp_path,
        OntologyBuildConfig.model_validate({**base, "queries": ["different"]}),
    )

    assert high.config_sha256 != maximum.config_sha256
    assert high.discovery_config_sha256 == maximum.discovery_config_sha256
    assert high.discovery_config_sha256 != changed_discovery.discovery_config_sha256


def test_seed_globs_only_resolve_git_tracked_promoted_bundles(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    generated = workspace / "ontology" / "topic" / "generated"
    tracked = generated / "tracked" / "bundle.yaml"
    untracked = generated / "untracked" / "bundle.yaml"
    tracked.parent.mkdir(parents=True)
    untracked.parent.mkdir(parents=True)
    tracked.write_text("version: 1\n")
    untracked.write_text("version: 1\n")
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(
        ("git", "-C", str(workspace), "add", "ontology/topic/generated/tracked/bundle.yaml"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "promote tracked bundle",
        ),
        check=True,
    )
    config = OntologyBuildConfig.model_validate(
        {
            "version": 1,
            "topic": "Test",
            "topic_concept_id": "concept:test",
            "seed_bundle_globs": ["ontology/topic/generated/*/bundle.yaml"],
            "output_directory": "ontology/topic/generated",
        }
    )
    builder = OntologyBuilder(
        config=config,
        root=tmp_path / "runtime",
        workspace=workspace,
        providers_path=Path("config/providers.toml"),
        research_policy_path=Path("config/research-policy.yaml"),
        model_policy_path=Path("config/model-policy.yaml"),
        budget_policy_path=Path("config/budget-policy.yaml"),
        truth_policy_path=Path("config/truth-policy.yaml"),
        vocabulary_path=Path("config/concept-vocabulary.yaml"),
    )

    assert builder._seed_paths() == (
        Path("ontology/topic/generated/tracked/bundle.yaml"),
    )


def test_parallel_model_calls_are_rejected() -> None:
    value = {
        "version": 1,
        "topic": "Test",
        "topic_concept_id": "concept:test",
        "output_directory": "ontology/test/generated",
        "model_parallelism": 2,
    }
    with pytest.raises(ValueError, match="model_parallelism"):
        OntologyBuildConfig.model_validate(value)


def test_documented_check_command_is_executable(tmp_path) -> None:
    result = subprocess.run(
        (
            "uv",
            "run",
            "research-agent",
            "--env-file",
            str(tmp_path / "absent.env"),
            "ontology-build",
            "ontology/open-source-research-agents/build.yaml",
            "--root",
            str(tmp_path / "runtime"),
            "--check",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"checked_only": true' in result.stdout
    assert '"completed": false' in result.stdout


def test_reasoning_evaluation_runner_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ("bash", "-n", "scripts/evaluate_reasoning_modes.sh"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_progress_is_human_readable_and_logged_as_jsonl(tmp_path, capsys) -> None:
    progress = BuildProgress(root=tmp_path, config_sha256="a" * 64)
    progress.event(
        "discovery",
        "running",
        "Searching index",
        current=2,
        total=8,
        hit_count=30,
    )

    captured = capsys.readouterr()
    assert "[#####---------------] 2/8" in captured.err
    assert captured.out == ""
    event = json.loads(
        (tmp_path / "ontology-build.log.jsonl").read_text().splitlines()[0]
    )
    assert event["stage"] == "discovery"
    assert event["hit_count"] == 30


def test_progress_rejects_sensitive_log_fields(tmp_path) -> None:
    progress = BuildProgress(root=tmp_path, config_sha256="a" * 64)
    with pytest.raises(ValueError, match="sensitive"):
        progress.event(
            "model",
            "running",
            "unsafe fixture",
            model_prompt="must not be logged",
        )
    assert not (tmp_path / "ontology-build.log.jsonl").exists()


def test_cli_reports_token_exhaustion_with_actionable_remedies(capsys) -> None:
    receipt = OntologyBuildReceipt(
        config_sha256="a" * 64,
        checked_only=False,
        completed=False,
        token_limit_exhaustions=(
            TokenLimitExhaustion(
                source="Example/Research",
                provider="deepseek_local",
                model="deepseek-v4-flash",
                requested_output_tokens=131_072,
                provider_output_token_limit=131_072,
                observed_output_tokens=131_072,
                recommendations=(
                    "Choose a model with more output tokens.",
                    "Split the source into grounded batches.",
                ),
            ),
        ),
    )
    assert _ontology_build_exit_code(receipt) == 3
    error = capsys.readouterr().err
    assert "ran out of output tokens" in error
    assert "Requested 131072" in error
    assert "Choose a model" in error


def test_cli_rejects_incomplete_build_with_non_token_failures() -> None:
    receipt = OntologyBuildReceipt(
        config_sha256="a" * 64,
        checked_only=False,
        completed=False,
        failures=("Example/Research:output-schema-quarantined",),
    )

    assert _ontology_build_exit_code(receipt) == 2
