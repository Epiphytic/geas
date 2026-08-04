import io
import json
import urllib.error
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.models import ModelParameters, ProviderConfig
from research_agent.providers import (
    ModelClient,
    ModelJsonProtocolError,
    ModelOutputTruncatedError,
    load_provider_configs,
)


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


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _local_client(
    monkeypatch,
    payload,
    *,
    parameters: ModelParameters | None = None,
    captured=None,
) -> ModelClient:
    class _Opener:
        def open(self, request, timeout):
            assert timeout == 180.0
            if captured is not None:
                captured.append(json.loads(request.data))
            return _Response(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: _Opener())
    return ModelClient(
        "local",
        ProviderConfig(
            kind="openai_compatible",
            base_url="http://127.0.0.1:8000/v1",
            model="fixture",
            external=False,
            max_output_tokens=4096,
            context_window_tokens=393_216,
        ),
        parameters=parameters,
    )


def test_model_client_detects_token_limit_before_json_parsing(monkeypatch) -> None:
    client = _local_client(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"content": '{"version":1,"claims":[{"key":"partial"}'},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 4096},
        },
    )

    with pytest.raises(ModelOutputTruncatedError, match="token limit"):
        client.complete_json(system="system", user="user", max_output_tokens=4096)
    assert client.last_finish_reason == "length"
    assert client.last_output_tokens == 4096


def test_model_client_never_salvages_nested_json_from_truncated_outer_object(
    monkeypatch,
) -> None:
    client = _local_client(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"version":1,"claims":[{"key":"nested","subject":"x"}]'
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )

    with pytest.raises(ModelJsonProtocolError, match="complete top-level"):
        client.complete_json(system="system", user="user")


def test_model_client_accepts_only_one_complete_json_object(monkeypatch) -> None:
    client = _local_client(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"content": '  {"version":1,"claims":[]}  '},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        },
    )
    assert client.complete_json(system="system", user="user") == {
        "version": 1,
        "claims": [],
    }


def test_schema_extraction_can_disable_hidden_thinking(monkeypatch) -> None:
    captured = []
    client = _local_client(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"content": '{"version":1}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        parameters=ModelParameters(thinking=False, reasoning_effort="none"),
        captured=captured,
    )
    client.complete_json(system="system", user="user")
    assert captured[0]["thinking"] is False
    assert captured[0]["reasoning_effort"] == "none"
    assert captured[0]["max_tokens"] == 4096


def test_model_client_sends_all_configured_generation_parameters(monkeypatch) -> None:
    captured = []
    client = _local_client(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": '{"version":1}',
                        "reasoning_content": "private scratch work",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        parameters=ModelParameters(
            thinking=True,
            reasoning_effort="max",
            temperature=0.2,
            top_p=0.9,
            top_k=20,
            min_p=0.05,
            seed=42,
            stop=("DONE",),
        ),
        captured=captured,
    )

    client.complete_json(system="system", user="user")

    assert captured[0]["reasoning_effort"] == "max"
    assert captured[0]["temperature"] == 0.2
    assert captured[0]["top_p"] == 0.9
    assert captured[0]["top_k"] == 20
    assert captured[0]["min_p"] == 0.05
    assert captured[0]["seed"] == 42
    assert captured[0]["stop"] == ["DONE"]
    assert client.last_reasoning_content == "private scratch work"


def test_max_reasoning_cannot_silently_downgrade_for_small_context() -> None:
    with pytest.raises(ValueError, match="393216"):
        ModelClient(
            "local",
            ProviderConfig(
                kind="openai_compatible",
                base_url="http://127.0.0.1:8000/v1",
                model="fixture",
                external=False,
                max_output_tokens=4096,
                context_window_tokens=384_000,
            ),
            parameters=ModelParameters(thinking=True, reasoning_effort="max"),
        )


def test_connection_refusal_is_retried_before_generation(monkeypatch) -> None:
    attempts = 0

    class _Opener:
        def open(self, _request, timeout):
            nonlocal attempts
            assert timeout == 180.0
            attempts += 1
            if attempts == 1:
                raise urllib.error.URLError(ConnectionRefusedError())
            return _Response(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": '{"version":1}'},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: _Opener())
    client = ModelClient(
        "local",
        ProviderConfig(
            kind="openai_compatible",
            base_url="http://127.0.0.1:8000/v1",
            model="fixture",
            external=False,
            max_output_tokens=4096,
        ),
        connection_retry_seconds=0,
    )
    assert client.complete_json(system="system", user="user") == {"version": 1}
    assert attempts == 2
