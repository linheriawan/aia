"""Provider protocol (ARCHITECTURE.md §5.2). All model providers implement this."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from deskai.core.events import StreamEvent
from deskai.core.messages import Message, ToolSpec


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    supports_tools: bool
    supports_images: bool
    context_tokens: int


class Provider(Protocol):
    name: str

    async def list_models(self) -> list[ModelInfo]: ...

    async def test_connection(self) -> None:
        """Raise a `core.errors.ProviderError` on failure."""
        ...

    def stream_chat(
        self,
        model: ModelInfo,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]: ...
