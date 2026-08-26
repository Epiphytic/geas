import json
import subprocess
from pathlib import Path

from research_agent.onboarding import build_setup_guide, render_setup_guide_markdown
from research_agent.operator_policy import ResearchPolicy
from research_agent.providers import load_provider_configs
from research_agent.user_config import GeasUserConfig, UserConfigManager


def test_setup_guide_lists_paths_credentials_and_end_to_end_steps(tmp_path: Path) -> None:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    profile_name, profile = GeasUserConfig.default().profile()
    default_provider, providers = load_provider_configs(Path("config/providers.toml"))

    guide = build_setup_guide(
        manager=manager,
        profile_name=profile_name,
        profile=profile,
        default_provider=default_provider,
        providers=providers,
        research_policy=ResearchPolicy.from_yaml(Path("config/research-policy.yaml")),
    )
    markdown = render_setup_guide_markdown(guide)

    assert guide.config_exists is False
    assert guide.default_provider == "deepseek_local"
    assert {item.environment_variable for item in guide.credentials} == {
        "MOJEEK_API_KEY",
        "OPENALEX_API_KEY",
        "OPENAI_API_KEY",
        "UNPAYWALL_EMAIL",
        "ZAI_API_KEY",
    }
    assert len(guide.steps) == 10
    assert "ontology-build project-expertise" in markdown
    assert "ontology-artifact-publish project-expertise" in markdown
    assert "--format agent-instructions" in markdown
    assert "secret-value" not in markdown


def test_setup_guide_cli_supports_json_and_markdown_without_creating_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "geas" / "config.yaml"
    base = ("uv", "run", "geas", "--geas-config", str(config), "setup-guide")

    as_json = subprocess.run(
        base,
        check=True,
        capture_output=True,
        text=True,
    )
    as_markdown = subprocess.run(
        (*base, "--format", "markdown"),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(as_json.stdout)["config_exists"] is False
    assert as_markdown.stdout.startswith("# Geas quick setup walkthrough")
    assert not config.exists()
