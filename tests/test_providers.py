import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from research_agent.models import ModelParameters, ProviderConfig
from research_agent.providers import (
    ModelClient,
    ModelJsonProtocolError,
    ModelOutputTruncatedError,
    load_provider_configs,
)


class _Gate:
    def __init__(self) -> None:
        self.authorized = False
        self.settled = False

    def authorize(self, **_kwargs) -> None:
        self.authorized = True

    def settle(self, **_kwargs) -> None:
        self.settled = True


def _oneshot_config(kind: str) -> ProviderConfig:
    return ProviderConfig(
        kind=kind,
        base_url=(
            "https://chatgpt.com/codex-cli"
            if kind == "codex_oneshot"
            else "https://api.anthropic.com/claude-code"
        ),
        model="fixture",
        external=True,
        max_output_tokens=4096,
    )


def test_codex_oneshot_is_ephemeral_schema_bound_and_tool_denied(monkeypatch, tmp_path) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"version":1}')
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"thread.started","thread_id":"t"}\n'
                '{"type":"turn.completed","usage":{"output_tokens":7}}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", run)
    gate = _Gate()
    client = ModelClient(
        "codex",
        _oneshot_config("codex_oneshot"),
        gate=gate,
        parameters=ModelParameters(reasoning_effort="xhigh"),
    )
    user = json.dumps(
        {
            "output_schema": {
                "type": "object",
                "properties": {"version": {"type": "integer"}},
                "required": ["version"],
            }
        }
    )

    assert client.complete_json(system="trusted", user=user) == {"version": 1}
    command = captured["command"]
    assert command[:3] == ["codex", "exec", "-"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") : command.index("--sandbox") + 2] == [
        "--sandbox",
        "read-only",
    ]
    assert any("hooks.PreToolUse=" in item for item in command)
    assert 'web_search="disabled"' in command
    assert captured["kwargs"]["input"].startswith("trusted")
    assert gate.authorized and gate.settled
    assert client.last_output_tokens == 7


def test_oneshot_schema_normalization_requires_all_nested_properties() -> None:
    schema = ModelClient._strict_output_schema(
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "child": {
                    "type": "object",
                    "properties": {"optional": {"type": ["string", "null"]}},
                },
                "mapping": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name"],
        }
    )

    assert schema["required"] == ["name", "child", "mapping"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["child"]["required"] == ["optional"]
    assert schema["properties"]["child"]["additionalProperties"] is False
    assert schema["properties"]["mapping"]["properties"] == {}
    assert schema["properties"]["mapping"]["required"] == []


def test_codex_oneshot_rejects_audited_tool_attempt(monkeypatch) -> None:
    def run(command, **_kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"version":1}')
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"item.started","item":{"type":"command_execution","command":"pwd"}}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", run)
    client = ModelClient(
        "codex",
        _oneshot_config("codex_oneshot"),
        gate=_Gate(),
    )
    with pytest.raises(Exception, match="forbidden one-shot action"):
        client.complete_json(system="trusted", user="{}")


def test_codex_audit_does_not_confuse_text_about_tools_with_tool_events() -> None:
    client = ModelClient(
        "codex",
        _oneshot_config("codex_oneshot"),
        gate=_Gate(),
    )
    client._audit_codex_events(
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"I made no tool_call or web_search."}}\n'
    )


def test_oneshot_timeout_fails_closed(monkeypatch) -> None:
    def run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=12)

    monkeypatch.setattr("subprocess.run", run)
    client = ModelClient(
        "codex",
        _oneshot_config("codex_oneshot"),
        gate=_Gate(),
        timeout=12,
    )
    with pytest.raises(Exception, match="exceeded 12 seconds"):
        client.complete_json(system="trusted", user="{}")


def test_claude_oneshot_disables_tools_and_reads_structured_output(monkeypatch) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "is_error": False,
                    "structured_output": {"version": 1},
                    "usage": {"output_tokens": 9},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", run)
    client = ModelClient(
        "claude",
        _oneshot_config("claude_oneshot"),
        gate=_Gate(),
    )
    assert client.complete_json(system="trusted", user="{}") == {"version": 1}
    assert "--tools=" in captured["command"]
    assert "--safe-mode" in captured["command"]
    assert captured["command"][
        captured["command"].index("--permission-mode") : captured["command"].index(
            "--permission-mode"
        )
        + 2
    ] == ["--permission-mode", "dontAsk"]
    assert client.last_output_tokens == 9


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
                        "content": ('{"version":1,"claims":[{"key":"nested","subject":"x"}]')
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
