from pathlib import Path

from research_agent.providers import load_provider_configs


def test_local_deepseek_is_default() -> None:
    default, providers = load_provider_configs(Path("config/providers.toml"))
    assert default == "deepseek_local"
    assert providers[default].external is False
    assert providers[default].model == "deepseek-v4-flash"
    assert str(providers[default].base_url).startswith("http://127.0.0.1:8000/")
