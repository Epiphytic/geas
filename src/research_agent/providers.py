from __future__ import annotations

import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from research_agent.model_policy import ModelUseGate
from research_agent.models import ModelParameters, ProviderConfig


class ProviderError(RuntimeError):
    pass


class ModelOutputTruncatedError(ProviderError):
    def __init__(self, *, output_tokens: int | None) -> None:
        super().__init__("model output reached its configured token limit")
        self.finish_reason = "length"
        self.output_tokens = output_tokens


class ModelJsonProtocolError(ProviderError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def load_provider_configs(path: Path) -> tuple[str, dict[str, ProviderConfig]]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    default = raw["default_provider"]
    providers = {
        name: ProviderConfig.model_validate(value)
        for name, value in raw.get("providers", {}).items()
    }
    if default not in providers:
        raise ValueError(f"default provider {default!r} is not configured")
    return default, providers


class ModelClient:
    """Tool-free OpenAI-compatible text client.

    The client deliberately exposes no tool definitions. Its output is always
    untrusted data and must pass a typed validator before use.
    """

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        *,
        gate: ModelUseGate | None = None,
        timeout: float = 180.0,
        parameters: ModelParameters | None = None,
        connection_attempts: int = 3,
        connection_retry_seconds: float = 1.0,
    ) -> None:
        if config.external and gate is None:
            raise ValueError("external model clients require a deterministic authorization gate")
        self.name = name
        self.config = config
        self.gate = gate
        self.timeout = timeout
        self.parameters = parameters or ModelParameters()
        if not 1 <= connection_attempts <= 20:
            raise ValueError("connection_attempts must be between 1 and 20")
        if not 0.0 <= connection_retry_seconds <= 30.0:
            raise ValueError("connection_retry_seconds must be between 0 and 30")
        self.connection_attempts = connection_attempts
        self.connection_retry_seconds = connection_retry_seconds
        required_context = self.parameters.minimum_context_tokens
        if required_context is not None and (
            config.context_window_tokens is None
            or config.context_window_tokens < required_context
        ):
            declared = (
                "unknown"
                if config.context_window_tokens is None
                else str(config.context_window_tokens)
            )
            raise ValueError(
                f"reasoning_effort=max requires at least {required_context} context "
                f"tokens; provider {name} declares {declared}"
            )
        self.last_finish_reason: str | None = None
        self.last_output_tokens: int | None = None
        self.last_reasoning_content: str | None = None

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int | None = None,
    ) -> str:
        token_limit = min(
            max_output_tokens or self.config.max_output_tokens,
            self.config.max_output_tokens,
        )
        if self.gate is not None:
            self.gate.authorize(
                provider=self.name,
                config=self.config,
                system=system,
                user=user,
                max_output_tokens=token_limit,
            )
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "max_tokens": token_limit,
        }
        parameters = self.parameters.model_dump(mode="json", exclude_none=True)
        stop = parameters.pop("stop", [])
        body.update(parameters)
        if stop:
            body["stop"] = stop
        headers = {"Content-Type": "application/json"}
        if self.config.api_key_env:
            api_key = os.environ.get(self.config.api_key_env)
            if not api_key:
                raise ProviderError(
                    f"provider {self.name!r} requires environment variable "
                    f"{self.config.api_key_env}"
                )
            headers["Authorization"] = f"Bearer {api_key}"
        elif not self.config.external:
            headers["Authorization"] = "Bearer local"

        endpoint = f"{str(self.config.base_url).rstrip('/')}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        for attempt in range(1, self.connection_attempts + 1):
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                break
            except urllib.error.URLError as error:
                refused = isinstance(error.reason, ConnectionRefusedError)
                if not refused or attempt == self.connection_attempts:
                    raise ProviderError(f"{self.name} request failed: {error}") from error
                time.sleep(self.connection_retry_seconds * attempt)
            except (TimeoutError, json.JSONDecodeError) as error:
                # A timeout or malformed response may follow accepted work.
                # Retrying could create concurrent duplicate generations.
                raise ProviderError(f"{self.name} request failed: {error}") from error

        if self.gate is not None:
            usage = payload.get("usage") if isinstance(payload, dict) else None
            input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            try:
                self.gate.settle(
                    input_tokens=input_tokens if isinstance(input_tokens, int) else None,
                    output_tokens=output_tokens if isinstance(output_tokens, int) else None,
                )
            except ValueError as error:
                raise ProviderError(f"{self.name} usage settlement failed: {error}") from error

        try:
            choice = payload["choices"][0]
            message = choice["message"]
            if message.get("tool_calls"):
                raise ProviderError("model attempted an unauthorized tool call")
            content = message["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"unexpected response shape from {self.name}") from error
        finish_reason = choice.get("finish_reason")
        self.last_finish_reason = finish_reason if isinstance(finish_reason, str) else None
        reasoning = message.get("reasoning_content")
        self.last_reasoning_content = reasoning if isinstance(reasoning, str) else None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        self.last_output_tokens = output_tokens if isinstance(output_tokens, int) else None
        if self.last_finish_reason == "length":
            raise ModelOutputTruncatedError(output_tokens=self.last_output_tokens)
        if self.last_finish_reason not in {None, "stop"}:
            raise ProviderError(
                f"{self.name} stopped with unsupported finish reason "
                f"{self.last_finish_reason!r}"
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(f"{self.name} returned no text")
        return content

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        text = self.complete(
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
        )
        stripped = text.strip()
        try:
            value, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            raise ModelJsonProtocolError(
                "model output did not contain one complete top-level JSON object"
            ) from None
        if not isinstance(value, dict) or stripped[end:].strip():
            raise ModelJsonProtocolError(
                "model output must be exactly one complete top-level JSON object"
            )
        return value
