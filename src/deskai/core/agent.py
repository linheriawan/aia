"""Agent loop (ARCHITECTURE.md §6): streams a provider, runs any tool calls it
makes, and loops until the model stops calling tools or the step limit is hit.

RiskLevel.WRITE+ tools ask for confirmation first (ARCHITECTURE.md §8.1),
showing the tool's own `preview()` (diff or structured summary) and letting the
user deny, allow once, or allow this tool for the rest of the session.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

import jsonschema

from deskai.core.errors import ProviderError
from deskai.core.events import (
    Done,
    Error,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
)
from deskai.core.messages import ContentPart, Message, Role, TextPart, ToolCallPart, ToolResultPart
from deskai.providers.base import ModelInfo, Provider
from deskai.tools.base import ConfirmDecision, RiskLevel, Tool, ToolContext, ToolResult
from deskai.tools.registry import ToolRegistry
from deskai.tools.sandbox import SandboxError

MAX_STEPS = 12
TOOL_TIMEOUT_SECONDS = 60

ConfirmFn = Callable[[str, dict[str, object], RiskLevel, str], ConfirmDecision]


def deny_everything(
    _name: str, _arguments: dict[str, object], _risk: RiskLevel, _preview: str
) -> ConfirmDecision:
    """The default `confirm` — fail-safe: no caller-supplied confirm means no writes happen."""
    return ConfirmDecision.DENY


async def run(
    provider: Provider,
    model: ModelInfo,
    system: str,
    history: list[Message],
    registry: ToolRegistry,
    ctx: ToolContext,
    on_message: Callable[[Message], None] = lambda message: None,
    on_usage: Callable[[Usage], None] = lambda usage: None,
    confirm: ConfirmFn = deny_everything,
    allowed_tools: set[str] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run one user turn to completion, mutating `history` in place as it goes.

    `history` already ends with the user's message when this is called.
    `on_message` fires for each assistant/tool message appended along the way,
    so the caller can persist it; `on_usage` fires for each `Usage` event.
    `allowed_tools` is mutated in place when the user picks "allow for this
    session", so the caller can keep it across calls to `run`.
    """
    if allowed_tools is None:
        allowed_tools = set()
    tool_specs = (
        registry.tool_specs(allow_vision=model.supports_images) if model.supports_tools else []
    )

    for _step in range(MAX_STEPS):
        text_buffer = ""
        tool_calls: list[ToolCallComplete] = []

        try:
            async for event in provider.stream_chat(model, system, history, tool_specs):
                if isinstance(event, TextDelta):
                    text_buffer += event.text
                    yield event
                elif isinstance(event, ToolCallComplete):
                    tool_calls.append(event)
                elif isinstance(event, Usage):
                    on_usage(event)
                    yield event
                elif isinstance(event, Error):
                    yield event
                    return
                elif isinstance(event, Done):
                    pass
        except ProviderError as exc:
            yield Error(str(exc))
            return

        assistant_parts: list[ContentPart] = []
        if text_buffer:
            assistant_parts.append(TextPart(text_buffer))
        assistant_parts.extend(ToolCallPart(c.id, c.name, c.arguments) for c in tool_calls)
        if assistant_parts:
            assistant_message = Message(Role.ASSISTANT, assistant_parts)
            history.append(assistant_message)
            on_message(assistant_message)

        if not tool_calls:
            yield Done("end_turn")
            return

        result_parts: list[ContentPart] = []
        for call in tool_calls:
            yield ToolCallStarted(call.id, call.name, call.arguments)
            tool = registry.get(call.name)

            if tool is not None:
                validation_error = _validate_arguments(tool, call.arguments)
                if validation_error is not None:
                    result_parts.append(ToolResultPart(call.id, [TextPart(validation_error)], True))
                    yield ToolCallFinished(call.id, call.name, validation_error, 0, True)
                    continue

                if tool.risk >= RiskLevel.WRITE and call.name not in allowed_tools:
                    preview_text = await _safe_preview(tool, call.arguments, ctx)
                    decision = confirm(call.name, call.arguments, tool.risk, preview_text)
                    if decision is ConfirmDecision.ALLOW_SESSION:
                        allowed_tools.add(call.name)
                    elif decision is ConfirmDecision.DENY:
                        result_parts.append(
                            ToolResultPart(
                                call.id, [TextPart("The user denied this action.")], True
                            )
                        )
                        yield ToolCallFinished(call.id, call.name, "denied by user", 0, True)
                        continue

            start_time = time.monotonic()
            result = await _execute_tool(registry, call, ctx)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result_parts.append(ToolResultPart(call.id, result.content, result.is_error))
            yield ToolCallFinished(
                call.id, call.name, _summarize(result), duration_ms, result.is_error
            )

        tool_message = Message(Role.TOOL, result_parts)
        history.append(tool_message)
        on_message(tool_message)

    yield Error(f"Stopped after {MAX_STEPS} steps without finishing — ask me to continue.")


def _validate_arguments(tool: Tool, arguments: dict[str, object]) -> str | None:
    """ARCHITECTURE.md §6: validate args against the tool's JSON Schema before running it.

    Local models in particular misfire on tool calls (§5.5); the error is fed
    back to the model as a normal tool result, so it can retry within the
    existing step budget — no separate retry mechanism needed.
    """
    try:
        jsonschema.validate(arguments, tool.input_schema)
    except jsonschema.ValidationError as exc:
        return f"Invalid arguments for {tool.name}: {exc.message}"
    except jsonschema.SchemaError:
        return None  # a malformed schema is our bug, not the model's — don't block the call
    return None


async def _safe_preview(tool: Tool, arguments: dict[str, object], ctx: ToolContext) -> str:
    try:
        return await tool.preview(arguments, ctx)
    except Exception as exc:  # the confirmation dialog must still show something
        return f"(preview unavailable: {exc})"


async def _execute_tool(
    registry: ToolRegistry, call: ToolCallComplete, ctx: ToolContext
) -> ToolResult:
    tool = registry.get(call.name)
    if tool is None:
        return ToolResult.text(f"Unknown tool: {call.name}", is_error=True)
    try:
        return await asyncio.wait_for(tool.run(call.arguments, ctx), timeout=TOOL_TIMEOUT_SECONDS)
    except SandboxError as exc:
        return ToolResult.text(str(exc), is_error=True)
    except TimeoutError:
        return ToolResult.text(
            f"{call.name} timed out after {TOOL_TIMEOUT_SECONDS}s", is_error=True
        )
    except Exception as exc:  # a tool bug must not crash the agent loop
        return ToolResult.text(f"{call.name} failed: {exc}", is_error=True)


def _summarize(result: ToolResult, limit: int = 200) -> str:
    text = "".join(part.text for part in result.content if isinstance(part, TextPart))
    if not text and result.content:
        text = "(image result)"
    return text if len(text) <= limit else text[:limit] + "…"
