"""Typed events a provider streams, and the agent loop forwards to the UI (ARCHITECTURE.md §6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallComplete:
    """Emitted by a `Provider`: the model finished requesting this tool call."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolCallStarted:
    """Emitted by the agent loop (never by a `Provider`): about to run this tool."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolCallFinished:
    """Emitted by the agent loop: a tool call's result, for the tool-call card."""

    id: str
    name: str
    result_summary: str
    duration_ms: int
    is_error: bool


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Done:
    stop_reason: str


@dataclass(frozen=True)
class Error:
    message: str


StreamEvent = (
    TextDelta | ToolCallComplete | ToolCallStarted | ToolCallFinished | Usage | Done | Error
)
