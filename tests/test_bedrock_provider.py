"""Bedrock adapter tests: wire-format conversion and streaming against recorded/fake responses.

No live AWS calls — see CLAUDE.md "Provider adapters get tests against recorded responses".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from botocore.exceptions import ClientError

from deskai.core.errors import ProviderAuthError, ProviderConnectionError
from deskai.core.events import Done, Error, TextDelta, ToolCallComplete, Usage
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
from deskai.providers.bedrock import (
    BedrockConfig,
    BedrockProvider,
    _message_to_bedrock,
    _tool_config,
)

MODEL = ModelInfo(
    id="anthropic.claude-x",
    display_name="Claude X",
    supports_tools=True,
    supports_images=True,
    context_tokens=200_000,
)


def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    async def _run() -> list[Any]:
        return [item async for item in ait]

    return asyncio.run(_run())


class FakeRuntimeClient:
    def __init__(self, stream: list[dict[str, Any]]) -> None:
        self._stream = stream
        self.last_request: dict[str, Any] | None = None

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.last_request = kwargs
        return {"stream": self._stream}


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        raise self._error

    def list_foundation_models(self) -> None:
        raise self._error


def test_message_to_bedrock_converts_all_part_types() -> None:
    message = Message(
        role=Role.ASSISTANT,
        parts=[
            TextPart("hello"),
            ImagePart(b"\x89PNG", "image/png"),
            ToolCallPart("call-1", "read_text", {"path": "a.txt"}),
            ToolResultPart("call-1", [TextPart("contents")], is_error=True),
        ],
    )
    converted = _message_to_bedrock(message)

    assert converted["role"] == "assistant"
    content = converted["content"]
    assert content[0] == {"text": "hello"}
    assert content[1] == {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}}
    assert content[2] == {
        "toolUse": {"toolUseId": "call-1", "name": "read_text", "input": {"path": "a.txt"}}
    }
    assert content[3] == {
        "toolResult": {
            "toolUseId": "call-1",
            "content": [{"text": "contents"}],
            "status": "error",
        }
    }


def test_tool_config_empty_returns_none() -> None:
    assert _tool_config([]) is None


def test_tool_config_builds_tool_spec_block() -> None:
    spec = ToolSpec(name="read_text", description="Read a file", input_schema={"type": "object"})
    config = _tool_config([spec])
    assert config == {
        "tools": [
            {
                "toolSpec": {
                    "name": "read_text",
                    "description": "Read a file",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }


def test_stream_chat_yields_text_usage_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_events = [
        {"contentBlockDelta": {"delta": {"text": "Hel"}}},
        {"contentBlockDelta": {"delta": {"text": "lo!"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 12, "outputTokens": 3}}},
    ]
    fake_client = FakeRuntimeClient(recorded_events)

    provider = BedrockProvider(BedrockConfig(region="us-east-1", auth_mode="keys"))
    monkeypatch.setattr(provider, "_client", lambda service_name="bedrock-runtime": fake_client)

    history = [Message(Role.USER, [TextPart("hi")])]
    events = _collect(provider.stream_chat(MODEL, "system prompt", history, []))

    assert events == [
        TextDelta("Hel"),
        TextDelta("lo!"),
        Usage(12, 3),
        Done("end_turn"),
    ]
    assert fake_client.last_request is not None
    assert fake_client.last_request["modelId"] == MODEL.id
    assert fake_client.last_request["system"] == [{"text": "system prompt"}]


def test_stream_chat_yields_tool_call_assembled_across_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock streams a tool call's JSON input as fragments across multiple
    contentBlockDelta events, keyed by contentBlockIndex; they must be
    accumulated and only turned into one ToolCallComplete at contentBlockStop."""
    recorded_events = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "call-1", "name": "read_text"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": '{"path": '}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": '"a.txt"}'}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    fake_client = FakeRuntimeClient(recorded_events)
    provider = BedrockProvider(BedrockConfig(region="us-east-1", auth_mode="keys"))
    monkeypatch.setattr(provider, "_client", lambda service_name="bedrock-runtime": fake_client)

    events = _collect(provider.stream_chat(MODEL, "", [Message(Role.USER, [TextPart("hi")])], []))

    assert events == [
        ToolCallComplete("call-1", "read_text", {"path": "a.txt"}),
        Done("tool_use"),
    ]


def test_stream_chat_surfaces_client_error_as_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ConverseStream"
    )
    fake_client = FailingClient(error)

    provider = BedrockProvider(BedrockConfig(region="us-east-1", auth_mode="keys"))
    monkeypatch.setattr(provider, "_client", lambda service_name="bedrock-runtime": fake_client)

    events = _collect(provider.stream_chat(MODEL, "", [Message(Role.USER, [TextPart("hi")])], []))

    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "slow down" in events[0].message


def test_client_raises_auth_error_without_credentials() -> None:
    provider = BedrockProvider(BedrockConfig(region="us-east-1", auth_mode="keys"))
    with pytest.raises(ProviderAuthError):
        provider._client()


def test_stream_chat_without_credentials_yields_error_event_not_an_exception() -> None:
    """A provider configured but missing credentials must surface an Error event

    from the worker thread, not raise out of the async generator (chat_window.py
    relies on this to show an inline error instead of crashing the send task)."""
    provider = BedrockProvider(BedrockConfig(region="us-east-1", auth_mode="keys"))

    events = _collect(provider.stream_chat(MODEL, "", [Message(Role.USER, [TextPart("hi")])], []))

    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "access key" in events[0].message


def test_test_connection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class OkClient:
        def list_foundation_models(self) -> None:
            calls.append("called")

    config = BedrockConfig(
        region="us-east-1", auth_mode="keys", access_key_id="a", secret_access_key="b"
    )
    provider = BedrockProvider(config)
    monkeypatch.setattr(provider, "_client", lambda service_name="bedrock-runtime": OkClient())

    asyncio.run(provider.test_connection())
    assert calls == ["called"]


def test_test_connection_wraps_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListFoundationModels"
    )
    config = BedrockConfig(
        region="us-east-1", auth_mode="keys", access_key_id="a", secret_access_key="b"
    )
    provider = BedrockProvider(config)
    monkeypatch.setattr(
        provider, "_client", lambda service_name="bedrock-runtime": FailingClient(error)
    )

    with pytest.raises(ProviderConnectionError):
        asyncio.run(provider.test_connection())
