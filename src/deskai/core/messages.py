"""Normalized message / content-part types (ARCHITECTURE.md §5.1).

Providers convert to and from their own wire formats; nothing outside
`providers/` knows a wire format (see CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    media_type: str


@dataclass(frozen=True)
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolResultPart:
    tool_call_id: str
    content: list[TextPart | ImagePart]
    is_error: bool = False


ContentPart = TextPart | ImagePart | ToolCallPart | ToolResultPart


@dataclass
class Message:
    role: Role
    parts: list[ContentPart] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    """What a provider needs to offer a tool to the model. See `tools/base.py` (Phase 3)."""

    name: str
    description: str
    input_schema: dict[str, object]
