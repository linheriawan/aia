"""OpenAI-compatible adapter tests, against a fake HTTP transport (no real network calls —
see CLAUDE.md "Provider adapters get tests against recorded responses")."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from deskai.core.errors import ProviderAuthError, ProviderConnectionError
from deskai.core.events import Done, Error, TextDelta, ToolCallComplete, Usage
from deskai.core.messages import ImagePart, Message, Role, TextPart, ToolCallPart, ToolResultPart
from deskai.providers.base import ModelInfo
from deskai.providers.openai_compat import (
    KIND,
    OpenAICompatConfig,
    OpenAICompatProvider,
    _message_to_openai,
    load_all_from_db,
    load_from_db,
)
from deskai.storage import db, repositories

MODEL = ModelInfo(
    id="llama3",
    display_name="Llama 3",
    supports_tools=True,
    supports_images=True,
    context_tokens=8192,
)


def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    async def _run() -> list[Any]:
        return [item async for item in ait]

    return asyncio.run(_run())


def _sse(*chunks: dict[str, Any]) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def test_message_to_openai_text() -> None:
    [entry] = _message_to_openai(Message(Role.USER, [TextPart("hello")]))
    assert entry == {"role": "user", "content": "hello"}


def test_message_to_openai_with_image() -> None:
    [entry] = _message_to_openai(
        Message(Role.USER, [TextPart("what is this"), ImagePart(b"\x89PNG", "image/png")])
    )
    assert entry["role"] == "user"
    assert entry["content"][0] == {"type": "text", "text": "what is this"}
    assert entry["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_message_to_openai_tool_call() -> None:
    [entry] = _message_to_openai(
        Message(Role.ASSISTANT, [ToolCallPart("call-1", "read_text", {"path": "a.txt"})])
    )
    assert entry["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_text", "arguments": '{"path": "a.txt"}'},
        }
    ]


def test_message_to_openai_tool_result_expands_to_one_message_per_part() -> None:
    message = Message(
        Role.TOOL,
        [
            ToolResultPart("call-1", [TextPart("result one")]),
            ToolResultPart("call-2", [TextPart("result two")]),
        ],
    )
    entries = _message_to_openai(message)
    assert entries == [
        {"role": "tool", "tool_call_id": "call-1", "content": "result one"},
        {"role": "tool", "tool_call_id": "call-2", "content": "result two"},
    ]


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patches `httpx.AsyncClient` so the real provider code runs against a
    `httpx.MockTransport`, instead of duplicating its request/error logic here."""
    real_async_client = httpx.AsyncClient

    def install(handler: Any) -> None:
        transport = httpx.MockTransport(handler)

        def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.setdefault("transport", transport)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("deskai.providers.openai_compat.httpx.AsyncClient", fake_client)

    return install


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(OpenAICompatConfig(base_url="http://localhost:11434/v1"))


def test_stream_chat_yields_text_and_usage(mock_transport: Any) -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo!"}, "finish_reason": "stop"}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    mock_transport(handler)
    history = [Message(Role.USER, [TextPart("hi")])]
    events = _collect(_provider().stream_chat(MODEL, "sys", history, []))

    assert events == [TextDelta("Hel"), TextDelta("lo!"), Usage(10, 2), Done("stop")]


def test_stream_chat_assembles_tool_call_across_chunks(mock_transport: Any) -> None:
    body = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call-1", "function": {"name": "read_text"}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]}}
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    mock_transport(handler)
    history = [Message(Role.USER, [TextPart("hi")])]
    events = _collect(_provider().stream_chat(MODEL, "sys", history, []))

    assert events == [
        ToolCallComplete("call-1", "read_text", {"path": "a.txt"}),
        Done("tool_calls"),
    ]


def test_stream_chat_http_error_becomes_error_event(mock_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server exploded")

    mock_transport(handler)
    history = [Message(Role.USER, [TextPart("hi")])]
    events = _collect(_provider().stream_chat(MODEL, "sys", history, []))

    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "500" in events[0].message
    assert "server exploded" in events[0].message


class _OneShotStream(httpx.AsyncByteStream):
    """A response body that can only be read while 'open' — like a real network
    stream, and unlike `httpx.MockTransport`'s default buffered content, which
    stays readable forever and so can't catch a late-read-after-close bug."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._closed:
            raise httpx.StreamClosed
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self._closed = True


def test_stream_chat_reads_error_body_before_the_stream_closes(mock_transport: Any) -> None:
    """Regression test: an earlier version read the error body in an `except`
    block positioned *after* the `async with client.stream(...)` had already
    exited and torn down the connection, which raised `httpx.StreamClosed`
    instead of the intended error — silently swallowing the real HTTP error
    and leaving the chat window showing nothing at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, stream=_OneShotStream([b"Forbidden: bad token"]))

    mock_transport(handler)
    history = [Message(Role.USER, [TextPart("hi")])]
    events = _collect(_provider().stream_chat(MODEL, "sys", history, []))

    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "403" in events[0].message
    assert "Forbidden: bad token" in events[0].message


def test_test_connection_success(mock_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    mock_transport(handler)
    asyncio.run(_provider().test_connection())  # must not raise


def test_test_connection_auth_error(mock_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    mock_transport(handler)
    with pytest.raises(ProviderAuthError):
        asyncio.run(_provider().test_connection())


def test_test_connection_connection_error(mock_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    mock_transport(handler)
    with pytest.raises(ProviderConnectionError):
        asyncio.run(_provider().test_connection())


def test_provider_name_defaults_to_the_generic_kind() -> None:
    provider = OpenAICompatProvider(OpenAICompatConfig())
    assert provider.name == KIND


def test_provider_name_can_be_set_to_its_own_db_id() -> None:
    """Each configured instance gets its own identity for usage/pricing, even
    though they all share this one adapter class."""
    provider = OpenAICompatProvider(OpenAICompatConfig(), name="my-ollama")
    assert provider.name == "my-ollama"


def test_load_from_db_returns_none_for_unknown_id(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    assert load_from_db(conn, "nope") is None


def test_load_from_db_builds_a_provider_with_its_own_name_and_extra_headers(
    tmp_path: Path,
) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    repositories.upsert_provider(
        conn,
        "my-cf",
        KIND,
        "My Cloudflare",
        {
            "base_url": "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1",
            "extra_headers": {"cf-aig-gateway-id": "gw1"},
        },
    )

    provider = load_from_db(conn, "my-cf")

    assert provider is not None
    assert provider.name == "my-cf"
    assert provider._config.base_url == "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1"
    assert provider._config.extra_headers == {"cf-aig-gateway-id": "gw1"}


def test_load_from_db_skips_disabled_providers(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    repositories.upsert_provider(conn, "p1", KIND, "P1", {}, enabled=False)
    assert load_from_db(conn, "p1") is None


def test_load_all_from_db_returns_every_enabled_custom_provider(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    repositories.upsert_provider(
        conn, "local", KIND, "Local", {"base_url": "http://localhost:11434/v1"}
    )
    repositories.upsert_provider(
        conn, "cf", KIND, "Cloudflare", {"base_url": "https://api.cloudflare.com/..."}
    )
    repositories.upsert_provider(conn, "disabled", KIND, "Disabled", {}, enabled=False)
    repositories.upsert_provider(conn, "bedrock", "bedrock", "Bedrock", {})  # a different kind

    providers = load_all_from_db(conn)

    assert set(providers) == {"local", "cf"}
    assert providers["local"].name == "local"
    assert providers["cf"].name == "cf"
