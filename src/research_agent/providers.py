from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
            config.context_window_tokens is None or config.context_window_tokens < required_context
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
        if self.config.kind != "openai_compatible":
            return self._complete_oneshot(
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
                f"{self.name} stopped with unsupported finish reason {self.last_finish_reason!r}"
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(f"{self.name} returned no text")
        return content

    def _complete_oneshot(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int,
    ) -> str:
        """Run a schema-only coding CLI with deterministic tool denial.

        Source material exists only in stdin. The child starts in an empty
        temporary directory and cannot inherit repository instructions.
        """
        prompt = (
            f"{system}\n\n"
            "ONE-SHOT EXECUTION BOUNDARY:\n"
            "- Do not call tools, search, read files, or execute commands.\n"
            "- Use only the source anchors in the user payload below.\n"
            "- Return exactly one JSON object satisfying its output_schema.\n\n"
            f"{user}"
        )
        schema = self._schema_from_user_payload(user)
        with tempfile.TemporaryDirectory(prefix="research-agent-oneshot-") as raw_dir:
            directory = Path(raw_dir)
            schema_path = directory / "output-schema.json"
            output_path = directory / "last-message.json"
            schema_path.write_text(json.dumps(schema, sort_keys=True))
            if self.config.kind == "codex_oneshot":
                command = self._codex_command(
                    directory=directory,
                    schema_path=schema_path,
                    output_path=output_path,
                )
            else:
                command = self._claude_command(schema=schema)
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    cwd=directory,
                    timeout=self.timeout,
                    check=False,
                )
            except FileNotFoundError as error:
                raise ProviderError(f"{self.config.kind} executable is not installed") from error
            except subprocess.TimeoutExpired as error:
                raise ProviderError(
                    f"{self.name} one-shot exceeded {self.timeout:g} seconds"
                ) from error
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-2000:]
                if self.config.kind == "codex_oneshot":
                    event_errors = self._codex_error_messages(completed.stdout)
                    if event_errors:
                        detail = "; ".join(event_errors)[-2000:]
                raise ProviderError(f"{self.name} one-shot exited {completed.returncode}: {detail}")
            if self.config.kind == "codex_oneshot":
                self._audit_codex_events(completed.stdout)
                text = output_path.read_text() if output_path.exists() else ""
            else:
                text = self._claude_result(completed.stdout)
            self.last_finish_reason = "stop"
            if self.gate is not None:
                try:
                    self.gate.settle(input_tokens=None, output_tokens=None)
                except ValueError as error:
                    raise ProviderError(f"{self.name} usage settlement failed: {error}") from error
            if not text.strip():
                raise ProviderError(f"{self.name} returned no text")
            return text

    @staticmethod
    def _schema_from_user_payload(user: str) -> dict[str, Any]:
        try:
            payload = json.loads(user)
            schema = payload.get("output_schema")
        except (json.JSONDecodeError, AttributeError):
            schema = None
        if isinstance(schema, dict) and schema.get("type") == "object":
            return ModelClient._strict_output_schema(schema)
        return {"type": "object", "additionalProperties": True}

    @staticmethod
    def _strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """Normalize Pydantic JSON Schema for strict structured-output APIs."""
        normalized = json.loads(json.dumps(schema))

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                value.pop("default", None)
                properties = value.get("properties")
                if value.get("type") == "object" and isinstance(properties, dict):
                    value["required"] = list(properties)
                    value["additionalProperties"] = False
                elif value.get("type") == "object":
                    # Strict structured outputs cannot represent arbitrary-key
                    # mappings. Extraction qualifiers therefore remain the
                    # deterministic empty default and can be enriched later.
                    value["properties"] = {}
                    value["required"] = []
                    value["additionalProperties"] = False
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(normalized)
        return normalized

    def _codex_command(
        self,
        *,
        directory: Path,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        deny_path = directory / "deny-tool"
        deny_path.write_text(
            "#!/bin/sh\necho 'All tools are disabled for ontology one-shots.' >&2\nexit 2\n"
        )
        deny_path.chmod(0o700)
        hook = (
            f'[{{ matcher="*", hooks=[{{ type="command", command="{deny_path}", timeout=5 }}] }}]'
        )
        effort = self.parameters.reasoning_effort
        if effort in {"none", "minimal"}:
            effort = "low"
        return [
            "codex",
            "exec",
            "-",
            "--cd",
            str(directory),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--dangerously-bypass-hook-trust",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--model",
            self.config.model,
            "--config",
            "features.hooks=true",
            "--config",
            f"hooks.PreToolUse={hook}",
            "--config",
            'web_search="disabled"',
            "--config",
            f'model_reasoning_effort="{effort}"',
        ]

    def _claude_command(self, *, schema: dict[str, Any]) -> list[str]:
        effort = self.parameters.reasoning_effort
        if effort in {"none", "minimal", "xhigh", "max"}:
            effort = {"none": "low", "minimal": "low", "xhigh": "high", "max": "high"}[effort]
        return [
            "claude",
            "--print",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools=",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--model",
            self.config.model,
            "--effort",
            effort,
        ]

    def _audit_codex_events(self, output: str) -> None:
        reasoning: list[str] = []
        forbidden_markers = (
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "web_search",
            "tool_call",
        )
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProviderError("Codex emitted malformed JSONL audit output") from error
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type in forbidden_markers:
                raise ProviderError(
                    f"Codex attempted forbidden one-shot action {item_type!r}"
                )
            event_type = event.get("type")
            if event_type in forbidden_markers:
                raise ProviderError(
                    f"Codex attempted forbidden one-shot action {event_type!r}"
                )
            if isinstance(item, dict) and item_type == "reasoning":
                text = item.get("text")
                if isinstance(text, str):
                    reasoning.append(text)
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    output_tokens = usage.get("output_tokens")
                    self.last_output_tokens = (
                        output_tokens if isinstance(output_tokens, int) else None
                    )
        self.last_reasoning_content = "\n\n".join(reasoning) or None

    @staticmethod
    def _codex_error_messages(output: str) -> list[str]:
        messages: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in {"error", "turn.failed"}:
                continue
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            if not isinstance(message, str):
                message = event.get("message")
            if isinstance(message, str):
                messages.append(message)
        return messages

    def _claude_result(self, output: str) -> str:
        try:
            envelope = json.loads(output)
        except json.JSONDecodeError as error:
            raise ProviderError("Claude emitted malformed JSON output") from error
        if envelope.get("is_error"):
            raise ProviderError(f"Claude one-shot failed: {envelope.get('result', '')}")
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            output_tokens = usage.get("output_tokens")
            self.last_output_tokens = output_tokens if isinstance(output_tokens, int) else None
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return json.dumps(structured)
        result = envelope.get("result")
        return result if isinstance(result, str) else ""

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
