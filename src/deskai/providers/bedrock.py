"""AWS Bedrock Converse adapter (ARCHITECTURE.md §5.3).

boto3 is synchronous, so every call runs in the default executor and is
bridged back to an async generator via a queue filled from the worker thread.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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

if TYPE_CHECKING:
    from collections.abc import Sequence

PROVIDER_ID = "bedrock"


@dataclass(frozen=True)
class BedrockConfig:
    region: str
    auth_mode: str  # "keys" | "profile"
    display_name: str = "Bedrock"
    profile_name: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    models: Sequence[ModelInfo] = ()


def _media_format(media_type: str) -> str:
    return media_type.split("/")[-1]


def _part_to_bedrock(part: TextPart | ImagePart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"text": part.text}
    return {"image": {"format": _media_format(part.media_type), "source": {"bytes": part.data}}}


def _message_to_bedrock(message: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart | ImagePart):
            content.append(_part_to_bedrock(part))
        elif isinstance(part, ToolCallPart):
            content.append(
                {"toolUse": {"toolUseId": part.id, "name": part.name, "input": part.arguments}}
            )
        elif isinstance(part, ToolResultPart):
            content.append(
                {
                    "toolResult": {
                        "toolUseId": part.tool_call_id,
                        "content": [_part_to_bedrock(p) for p in part.content],
                        "status": "error" if part.is_error else "success",
                    }
                }
            )
    role = "assistant" if message.role is Role.ASSISTANT else "user"
    return {"role": role, "content": content}


def _tool_config(tools: list[ToolSpec]) -> dict[str, Any] | None:
    if not tools:
        return None
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {"json": tool.input_schema},
                }
            }
            for tool in tools
        ]
    }


class BedrockProvider:
    name = "bedrock"

    def __init__(self, config: BedrockConfig) -> None:
        self._config = config

    def _client(self, service_name: str = "bedrock-runtime") -> Any:
        cfg = self._config
        if cfg.auth_mode == "profile":
            if not cfg.profile_name:
                raise ProviderAuthError("Bedrock profile auth requires a profile name")
            session = boto3.Session(profile_name=cfg.profile_name)
            return session.client(service_name, region_name=cfg.region)
        if cfg.auth_mode == "keys":
            if not cfg.access_key_id or not cfg.secret_access_key:
                raise ProviderAuthError("Bedrock key auth requires an access key and secret")
            return boto3.client(
                service_name,
                region_name=cfg.region,
                aws_access_key_id=cfg.access_key_id,
                aws_secret_access_key=cfg.secret_access_key,
            )
        raise ProviderAuthError(f"Unknown Bedrock auth mode: {cfg.auth_mode!r}")

    async def list_models(self) -> list[ModelInfo]:
        return list(self._config.models)

    async def test_connection(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._test_connection_sync)
        except ProviderAuthError:
            raise
        except (BotoCoreError, ClientError) as exc:
            raise ProviderConnectionError(str(exc)) from exc

    def _test_connection_sync(self) -> None:
        client = self._client("bedrock")
        client.list_foundation_models()

    def _build_request(
        self,
        model: ModelInfo,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> dict[str, Any]:
        # Role.SYSTEM entries are chat-log-only annotations (e.g. "workspace
        # folder changed"); Bedrock's Converse API only accepts user/assistant.
        request: dict[str, Any] = {
            "modelId": model.id,
            "messages": [_message_to_bedrock(m) for m in messages if m.role is not Role.SYSTEM],
        }
        if system:
            request["system"] = [{"text": system}]
        if model.supports_tools:
            tool_config = _tool_config(tools)
            if tool_config:
                request["toolConfig"] = tool_config
        return request

    async def stream_chat(
        self,
        model: ModelInfo,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

        def worker() -> None:
            try:
                client = self._client()
                request = self._build_request(model, system, messages, tools)
                response = client.converse_stream(**request)
                input_tokens = 0
                output_tokens = 0
                stop_reason = "end_turn"
                tool_blocks: dict[int, dict[str, str]] = {}
                for event in response["stream"]:
                    if "contentBlockStart" in event:
                        start_event = event["contentBlockStart"]
                        index = start_event.get("contentBlockIndex", 0)
                        tool_use = start_event.get("start", {}).get("toolUse")
                        if tool_use:
                            tool_blocks[index] = {
                                "id": tool_use.get("toolUseId", ""),
                                "name": tool_use.get("name", ""),
                                "input_json": "",
                            }
                    elif "contentBlockDelta" in event:
                        delta_event = event["contentBlockDelta"]
                        index = delta_event.get("contentBlockIndex", 0)
                        delta = delta_event.get("delta", {})
                        text = delta.get("text")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, TextDelta(text))
                        tool_use_delta = delta.get("toolUse")
                        if tool_use_delta and index in tool_blocks:
                            tool_blocks[index]["input_json"] += tool_use_delta.get("input", "")
                    elif "contentBlockStop" in event:
                        index = event["contentBlockStop"].get("contentBlockIndex", 0)
                        block = tool_blocks.pop(index, None)
                        if block is not None:
                            try:
                                arguments = (
                                    json.loads(block["input_json"]) if block["input_json"] else {}
                                )
                            except json.JSONDecodeError:
                                arguments = {}
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ToolCallComplete(block["id"], block["name"], arguments),
                            )
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason", "end_turn")
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage")
                        if usage:
                            input_tokens = usage.get("inputTokens", 0)
                            output_tokens = usage.get("outputTokens", 0)
                if input_tokens or output_tokens:
                    loop.call_soon_threadsafe(queue.put_nowait, Usage(input_tokens, output_tokens))
                loop.call_soon_threadsafe(queue.put_nowait, Done(stop_reason))
            except ProviderAuthError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, Error(str(exc)))
            except (BotoCoreError, ClientError) as exc:
                loop.call_soon_threadsafe(queue.put_nowait, Error(str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, worker)

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item


def load_from_db(conn: sqlite3.Connection) -> BedrockProvider | None:
    """Build a `BedrockProvider` from the saved settings row + keyring secrets, if configured."""
    row = repositories.get_provider(conn, PROVIDER_ID)
    if row is None or not row.enabled:
        return None

    model_infos = tuple(
        ModelInfo(
            id=m.model_id,
            display_name=m.display_name,
            supports_tools=m.supports_tools,
            supports_images=m.supports_images,
            context_tokens=m.context_tokens,
        )
        for m in repositories.list_models(conn, PROVIDER_ID)
    )

    config = row.config
    auth_mode = str(config.get("auth_mode", "keys"))
    access_key_id = secrets.get_secret(f"provider:{PROVIDER_ID}:access_key_id")
    secret_access_key = secrets.get_secret(f"provider:{PROVIDER_ID}:secret_access_key")

    return BedrockProvider(
        BedrockConfig(
            region=str(config.get("region", "us-east-1")),
            auth_mode=auth_mode,
            display_name=row.display_name,
            profile_name=(str(config["profile_name"]) if config.get("profile_name") else None),
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            models=model_infos,
        )
    )
