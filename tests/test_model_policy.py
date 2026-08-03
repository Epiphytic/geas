import io
import json
from pathlib import Path

import pytest

from research_agent.budget import BudgetPolicy, UsageLedger
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
    tmp_path: Path,
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
            run_id="run:test",
        ),
        budget_policy=BudgetPolicy.from_yaml(Path("config/budget-policy.yaml")),
        usage_ledger=UsageLedger(tmp_path / "usage.sqlite"),
    )


def test_checked_in_policy_enables_only_budget_gated_automatic_external_calls() -> None:
    policy = ModelUsePolicy.from_yaml(Path("config/model-policy.yaml"))

    assert policy.automatic_external_calls is True


def test_external_client_cannot_be_constructed_without_gate() -> None:
    with pytest.raises(ValueError, match="authorization gate"):
        ModelClient("openai", _external_config())


def test_external_use_requires_budget_policy_and_ledger() -> None:
    gate = ModelUseGate(
        ModelUsePolicy.from_yaml(Path("config/model-policy.yaml")),
        ModelUseContext(
            operation=ModelOperation.QUERY_COMPILATION,
            data_class=DataClass.PUBLIC,
            input_kind=InputKind.METADATA_ONLY,
            run_id="run:test",
        ),
    )
    with pytest.raises(ValueError, match="budget policy and usage ledger"):
        gate.authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_automatic_external_call_is_bound_to_context_input_and_reservation(
    tmp_path: Path,
) -> None:
    authorization = _gate(tmp_path).authorize(
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
    assert authorization.usage_reservation_id


def test_unknown_data_is_forbidden_even_with_human_approval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown data classification"):
        _gate(tmp_path, data_class=DataClass.UNKNOWN, human_approved=True).authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="unclassified",
            max_output_tokens=100,
        )


def test_source_content_requires_external_allowed_route(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not marked external_allowed"):
        _gate(
            tmp_path,
            input_kind=InputKind.SOURCE_CONTENT,
            human_approved=True,
        ).authorize(
            provider="openai",
            config=_external_config(),
            system="system",
            user="source text",
            max_output_tokens=100,
        )

    authorization = _gate(
        tmp_path,
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


def test_unallowlisted_model_is_rejected_before_network_use(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        _gate(tmp_path, human_approved=True).authorize(
            provider="openai",
            config=_external_config(model="attacker-selected-model"),
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_allowlisted_name_and_model_cannot_use_an_unapproved_endpoint(
    tmp_path: Path,
) -> None:
    config = _external_config().model_copy(update={"base_url": "https://attacker.invalid/v1"})
    with pytest.raises(ValueError, match="base URL is not allowlisted"):
        _gate(tmp_path, human_approved=True).authorize(
            provider="openai",
            config=config,
            system="system",
            user="user",
            max_output_tokens=100,
        )


def test_local_provider_remains_automatic_for_unknown_data(tmp_path: Path) -> None:
    _, providers = load_provider_configs(Path("config/providers.toml"))
    gate = _gate(tmp_path, data_class=DataClass.UNKNOWN)

    authorization = gate.authorize(
        provider="deepseek_local",
        config=providers["deepseek_local"],
        system="system",
        user="unclassified local input",
        max_output_tokens=100,
    )

    assert authorization.external is False
    assert not authorization.human_approved


def test_model_client_settles_provider_reported_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            payload = {
                "choices": [{"message": {"content": "complete"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            }
            return Response(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    gate = _gate(tmp_path)
    client = ModelClient("openai", _external_config(), gate=gate)

    assert client.complete(system="system", user="user", max_output_tokens=100) == "complete"
    assert gate.last_settlement is not None
    assert gate.last_settlement.status == "settled"
    assert gate.last_settlement.input_tokens_actual == 20
    assert gate.last_settlement.output_tokens_actual == 5
