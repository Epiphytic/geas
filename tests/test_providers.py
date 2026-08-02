from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.models import ProviderConfig
from research_agent.providers import load_provider_configs


def test_local_deepseek_is_default() -> None:
    default, providers = load_provider_configs(Path("config/providers.toml"))
    assert default == "deepseek_local"
    assert providers[default].external is False
    assert providers[default].model == "deepseek-v4-flash"
    assert str(providers[default].base_url).startswith("http://127.0.0.1:8000/")


def test_remote_endpoint_cannot_be_mislabeled_as_local() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ProviderConfig(
            kind="openai_compatible",
            base_url="https://attacker.invalid/v1",
            model="stolen-local-name",
            external=False,
            max_output_tokens=100,
        )


def test_external_endpoint_requires_https() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        ProviderConfig(
            kind="openai_compatible",
            base_url="http://api.example.test/v1",
            model="external",
            external=True,
            max_output_tokens=100,
        )
