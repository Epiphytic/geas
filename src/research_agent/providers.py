from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from research_agent.model_policy import ModelUseGate
from research_agent.models import ProviderConfig


class ProviderError(RuntimeError):
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
    ) -> None:
        if config.external and gate is None:
            raise ValueError("external model clients require a deterministic authorization gate")
        self.name = name
        self.config = config
        self.gate = gate
        self.timeout = timeout

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
            "temperature": 0,
            "max_tokens": token_limit,
        }
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
        try:
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"{self.name} request failed: {error}") from error

        try:
            message = payload["choices"][0]["message"]
            if message.get("tool_calls"):
                raise ProviderError("model attempted an unauthorized tool call")
            content = message["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"unexpected response shape from {self.name}") from error
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
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ProviderError("model output did not contain a JSON object")
