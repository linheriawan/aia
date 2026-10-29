"""OpenAI-compatible adapter (ARCHITECTURE.md §5.5): any `chat/completions`
server — Ollama, LM Studio, llama.cpp, Cloudflare Workers AI, or any other
OpenAI-API-shaped endpoint. The user can configure any number of these
(Settings → Providers → Custom); each gets its own id, so usage/pricing stay
distinguishable per endpoint even though they share one adapter class.

httpx is async-native, so unlike Bedrock this needs no thread/executor bridge.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from deskai.core.errors import ProviderAuthError, ProviderConnectionError
from deskai.core.events import Done, Error, StreamEvent, TextDelta, ToolCallComplete, Usage
from deskai.core.messages import (
    ImagePart,
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from deskai.providers.base import ModelInfo
from deskai.storage import repositories, secrets

KIND = "openai_compat"
DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama's default


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    display_name: str = "OpenAI-compatible"
    extra_headers: dict[str, str] = field(default_factory=dict)
    models: Sequence[ModelInfo] = ()


def _image_data_url(part: ImagePart) -> str:
    encoded = base64.b64encode(part.data).decode("ascii")
    return f"data:{part.media_type};base64,{encoded}"


def _message_to_openai(message: Message) -> list[dict[str, Any]]:
    if message.role is Role.TOOL:
        return [
            {
                "role": "tool",
                "tool_call_id": part.tool_call_id,
                "content": "\n".join(p.text for p in part.content if isinstance(p, TextPart)),
            }
            for part in message.parts
            if isinstance(part, ToolResultPart)
        ]

    role = "assistant" if message.role is Role.ASSISTANT else "user"
    text_parts = [p for p in message.parts if isinstance(p, TextPart)]
    image_parts = [p for p in message.parts if isinstance(p, ImagePart)]
    tool_call_parts = [p for p in message.parts if isinstance(p, ToolCallPart)]

    entry: dict[str, Any] = {"role": role}
    if image_parts:
        content: list[dict[str, Any]] = [{"type": "text", "text": p.text} for p in text_parts]
        content.extend(
            {"type": "image_url", "image_url": {"url": _image_data_url(p)}} for p in image_parts
        )
        entry["content"] = content
    else:
        entry["content"] = "\n".join(p.text for p in text_parts) or None

    if tool_call_parts:
        entry["tool_calls"] = [
            {
                "id": p.id,
                "type": "function",
                "function": {"name": p.name, "arguments": json.dumps(p.arguments)},
            }
            for p in tool_call_parts
        ]

    return [entry]


def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            continue  # sent separately as the leading system message, like Bedrock
        result.extend(_message_to_openai(message))
    return result


def _tools_to_openai(tools: list[ToolSpec]) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _build_request(
    model: ModelInfo, system: str, messages: list[Message], tools: list[ToolSpec]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [],
    }
    if system:
        payload["messages"].append({"role": "system", "content": system})
    payload["messages"].extend(_messages_to_openai(messages))
    if model.supports_tools:
        tool_defs = _tools_to_openai(tools)
        if tool_defs:
            payload["tools"] = tool_defs
    return payload


async def _parse_sse(response: httpx.Response) -> AsyncIterator[StreamEvent]:
    tool_calls: dict[int, dict[str, str]] = {}
    finish_reason = "stop"

    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices") or []
        if choices:
            choice = choices[0]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                yield TextDelta(text)
            for tool_call_delta in delta.get("tool_calls") or []:
                index = tool_call_delta.get("index", 0)
                entry = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tool_call_delta.get("id"):
                    entry["id"] = tool_call_delta["id"]
                function = tool_call_delta.get("function") or {}
                if function.get("name"):
                    entry["name"] = function["name"]
                if function.get("arguments"):
                    entry["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        usage = chunk.get("usage")
        if usage:
            yield Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    for entry in tool_calls.values():
        try:
            arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
        except json.JSONDecodeError:
            arguments = {}
        yield ToolCallComplete(entry["id"], entry["name"], arguments)

    yield Done(finish_reason)


class OpenAICompatProvider:
    def __init__(self, config: OpenAICompatConfig, name: str = KIND) -> None:
        """`name` should be the provider's own DB id for a configured instance,
        so usage/pricing rows (keyed by provider name) stay distinguishable
        between e.g. a local Ollama and a cloud endpoint that both use this
        same adapter class. Defaults to the generic kind for ad-hoc use."""
        self._config = config
        self.name = name

    def _headers(self) -> dict[str, str]:
        headers = dict(self._config.extra_headers)
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    async def list_models(self) -> list[ModelInfo]:
        return list(self._config.models)

    async def test_connection(self) -> None:
        url = f"{self._config.base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=self._headers())
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ProviderAuthError(str(exc)) from exc
            raise ProviderConnectionError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderConnectionError(str(exc)) from exc

    async def stream_chat(
        self,
        model: ModelInfo,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        payload = _build_request(model, system, messages, tools)
        try:
            async with (
                httpx.AsyncClient(timeout=120) as client,
                client.stream("POST", url, json=payload, headers=self._headers()) as response,
            ):
                if response.is_error:
                    # Read the body while the stream is still open — once this
                    # `async with` exits, the connection is torn down and a
                    # later read raises httpx.StreamClosed instead of
                    # reporting the original error.
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    yield Error(f"HTTP {response.status_code}: {body}")
                    return
                async for event in _parse_sse(response):
                    yield event
        except httpx.HTTPError as exc:
            yield Error(str(exc))


def load_from_db(conn: sqlite3.Connection, provider_id: str) -> OpenAICompatProvider | None:
    """Build one configured `OpenAICompatProvider` instance by its own DB id."""
    row = repositories.get_provider(conn, provider_id)
    if row is None or row.kind != KIND or not row.enabled:
        return None

    model_infos = tuple(
        ModelInfo(
            id=m.model_id,
            display_name=m.display_name,
            supports_tools=m.supports_tools,
            supports_images=m.supports_images,
            context_tokens=m.context_tokens,
        )
        for m in repositories.list_models(conn, provider_id)
    )

    config = row.config
    api_key = secrets.get_secret(f"provider:{provider_id}:api_key")
    extra_headers = config.get("extra_headers")

    return OpenAICompatProvider(
        OpenAICompatConfig(
            base_url=str(config.get("base_url", DEFAULT_BASE_URL)),
            api_key=api_key,
            display_name=row.display_name,
            extra_headers=dict(extra_headers) if isinstance(extra_headers, dict) else {},
            models=model_infos,
        ),
        name=provider_id,
    )


def load_all_from_db(conn: sqlite3.Connection) -> dict[str, OpenAICompatProvider]:
    """Every enabled custom provider the user has configured (Settings → Providers → Custom)."""
    providers: dict[str, OpenAICompatProvider] = {}
    for row in repositories.list_providers(conn):
        if row.kind != KIND:
            continue
        provider = load_from_db(conn, row.id)
        if provider is not None:
            providers[row.id] = provider
    return providers
