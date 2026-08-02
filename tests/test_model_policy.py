from pathlib import Path

import pytest

from research_agent.deposits import ModelRoute
from research_agent.model_policy import (
    DataClass,
    InputKind,
    ModelOperation,
    ModelUseContext,
    ModelUseGate,
    ModelUsePolicy,
)
from research_agent.models import ProviderConfig
from research_agent.providers import ModelClient, load_provider_configs


def _external_config(*, model: str = "gpt-5.2") -> ProviderConfig:
    return ProviderConfig(
        kind="openai_compatible",
        base_url="https://api.openai.com/v1",
        model=model,
        api_key_env="OPENAI_API_KEY",
        external=True,
        max_output_tokens=1024,
    )


def _gate(
    *,
    data_class: DataClass = DataClass.PUBLIC,
    input_kind: InputKind = InputKind.METADATA_ONLY,
    model_route: ModelRoute = ModelRoute.LOCAL_PREFERRED,
    human_approved: bool = False,
) -> ModelUseGate:
    return ModelUseGate(
        ModelUsePolicy.from_yaml(Path("config/model-policy.yaml")),
        ModelUseContext(
            operation=ModelOperation.QUERY_COMPILATION,
            data_class=data_class,
            input_kind=input_kind,
            model_route=model_route,
            human_approved=human_approved,
        ),
    )


def test_checked_in_policy_cannot_enable_automatic_external_calls_yet() -> None:
    policy = ModelUsePolicy.from_yaml(Path("config/model-policy.yaml"))

    assert policy.automatic_external_calls is False
    with pytest.raises(ValueError):
        ModelUsePolicy.model_validate(
            {**policy.model_dump(mode="json"), "automatic_external_calls": True}
        )


def test_external_client_cannot_be_constructed_without_gate() -> None:
    with pytest.raises(ValueError, match="authorization gate"):
        ModelClient("openai", _external_config())


def test_external_use_requires_human_approval_until_budget_policy_exists() -> None:
    with pytest.raises(ValueError, match="requires human approval"):
        _gate().authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_approved_external_call_is_bound_to_exact_context_and_input() -> None:
    authorization = _gate(human_approved=True).authorize(
        provider="openai",
        config=_external_config(),
        system="system",
        user="user",
        max_output_tokens=100,
    )

    assert authorization.provider == "openai"
    assert authorization.model == "gpt-5.2"
    assert authorization.operation is ModelOperation.QUERY_COMPILATION
    assert authorization.data_class is DataClass.PUBLIC
    assert authorization.max_output_tokens == 100
    assert authorization.input_sha256


def test_unknown_data_is_forbidden_even_with_human_approval() -> None:
    with pytest.raises(ValueError, match="unknown data classification"):
        _gate(data_class=DataClass.UNKNOWN, human_approved=True).authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="unclassified",
            max_output_tokens=100,
        )


def test_source_content_requires_external_allowed_route() -> None:
    with pytest.raises(ValueError, match="not marked external_allowed"):
        _gate(input_kind=InputKind.SOURCE_CONTENT, human_approved=True).authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="source text",
            max_output_tokens=100,
        )

    authorization = _gate(
        input_kind=InputKind.SOURCE_CONTENT,
        model_route=ModelRoute.EXTERNAL_ALLOWED,
        human_approved=True,
    ).authorize(
        provider="openai",
        config=_external_config(),
        system="system",
        user="source text",
        max_output_tokens=100,
    )
    assert authorization.model_route is ModelRoute.EXTERNAL_ALLOWED


def test_unallowlisted_model_is_rejected_before_network_use() -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        _gate(human_approved=True).authorize(
            provider="openai",
            config=_external_config(model="attacker-selected-model"),
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_allowlisted_name_and_model_cannot_use_an_unapproved_endpoint() -> None:
    config = _external_config().model_copy(update={"base_url": "https://attacker.invalid/v1"})
    with pytest.raises(ValueError, match="base URL is not allowlisted"):
        _gate(human_approved=True).authorize(
            provider="openai",
            config=config,
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_local_provider_remains_automatic_for_unknown_data() -> None:
    _, providers = load_provider_configs(Path("config/providers.toml"))
    gate = _gate(data_class=DataClass.UNKNOWN)

    authorization = gate.authorize(
        provider="deepseek_local",
        config=providers["deepseek_local"],
        system="system",
        user="unclassified local input",
        max_output_tokens=100,
    )

    assert authorization.external is False
    assert not authorization.human_approved
