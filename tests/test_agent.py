"""Tests for the agent loop (ARCHITECTURE.md §6): tool-call execution, the step
limit, and provider-error handling — against a fake provider and fake tools,
no real model or filesystem access needed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from deskai.core import agent
from deskai.core.errors import ProviderConnectionError
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
from deskai.core.messages import Message, Role, TextPart, ToolCallPart, ToolResultPart
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo
from deskai.storage import db
from deskai.tools.base import ConfirmDecision, RiskLevel, Tool, ToolContext, ToolResult
from deskai.tools.registry import ToolRegistry
from deskai.tools.sandbox import Sandbox, SandboxError

MODEL = ModelInfo(
    id="fake-model",
    display_name="Fake",
    supports_tools=True,
    supports_images=False,
    context_tokens=1000,
)


class FakeProvider:
    name = "fake"

    def __init__(self, turns: list[list[StreamEvent] | Exception]) -> None:
        self._turns = list(turns)
        self.requests: list[list[Message]] = []
        self.tool_specs_seen: list[list[Any]] = []

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def test_connection(self) -> None:
        pass

    async def stream_chat(
        self, model: ModelInfo, system: str, messages: list[Message], tools: list[Any]
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        self.tool_specs_seen.append(list(tools))
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for event in turn:
            yield event


class EchoTool(Tool):
    name = "echo"
    description = "Echoes text back."
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        return ToolResult.text(f"echoed: {args['text']}")


class StrictTool(Tool):
    name = "strict"
    description = "Requires an integer 'count' argument."
    input_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    risk = RiskLevel.READ

    def __init__(self) -> None:
        self.run_count = 0

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult.text(f"got {args['count']}")


class SandboxViolationTool(Tool):
    name = "bad_path"
    description = "Always rejects."
    input_schema = {"type": "object", "properties": {}}
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        raise SandboxError("outside the workspace")


class SlowTool(Tool):
    name = "slow"
    description = "Never finishes in time."
    input_schema = {"type": "object", "properties": {}}
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult.text("should not get here")


class WriteTool(Tool):
    name = "write_thing"
    description = "A WRITE-risk tool that records how many times it actually ran."
    input_schema = {"type": "object", "properties": {}}
    risk = RiskLevel.WRITE

    def __init__(self) -> None:
        self.run_count = 0

    async def preview(self, args: dict[str, object], ctx: ToolContext) -> str:
        return "would write something"

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult.text("wrote it")


def _ctx() -> ToolContext:
    conn = db.connect(":memory:")
    return ToolContext(session_id="s1", workspace=Sandbox([], deny_list=[]), ocr=OcrEngine(conn))


def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    async def _run() -> list[Any]:
        return [item async for item in ait]

    return asyncio.run(_run())


def test_plain_reply_with_no_tool_calls() -> None:
    provider = FakeProvider([[TextDelta("hi"), Usage(1, 2), Done("end_turn")]])
    history = [Message(Role.USER, [TextPart("hello")])]
    messages: list[Message] = []
    usages: list[Usage] = []

    events = _collect(
        agent.run(
            provider,
            MODEL,
            "sys",
            history,
            ToolRegistry(),
            _ctx(),
            on_message=messages.append,
            on_usage=usages.append,
        )
    )

    assert events == [TextDelta("hi"), Usage(1, 2), Done("end_turn")]
    assert usages == [Usage(1, 2)]
    assert len(messages) == 1
    assert messages[0].role is Role.ASSISTANT
    assert messages[0].parts == [TextPart("hi")]
    assert history[-1] is messages[0]


def test_tool_call_then_final_answer() -> None:
    provider = FakeProvider(
        [
            [ToolCallComplete("call-1", "echo", {"text": "hi"})],
            [TextDelta("done"), Done("end_turn")],
        ]
    )
    history = [Message(Role.USER, [TextPart("echo hi")])]
    registry = ToolRegistry([EchoTool()])
    messages: list[Message] = []

    events = _collect(
        agent.run(provider, MODEL, "sys", history, registry, _ctx(), on_message=messages.append)
    )

    assert events[0] == ToolCallStarted("call-1", "echo", {"text": "hi"})
    finished = events[1]
    assert isinstance(finished, ToolCallFinished)
    assert finished.id == "call-1"
    assert not finished.is_error
    assert "echoed: hi" in finished.result_summary
    assert events[2:] == [TextDelta("done"), Done("end_turn")]

    assert len(messages) == 3
    assert messages[0].parts == [ToolCallPart("call-1", "echo", {"text": "hi"})]
    assert isinstance(messages[1].parts[0], ToolResultPart)
    assert messages[1].parts[0].tool_call_id == "call-1"
    assert messages[2].parts == [TextPart("done")]
    assert len(provider.requests) == 2  # tool result was fed back for a second turn


def test_sandbox_error_in_tool_becomes_error_result_not_a_crash() -> None:
    provider = FakeProvider(
        [
            [ToolCallComplete("call-1", "bad_path", {})],
            [Done("end_turn")],
        ]
    )
    history = [Message(Role.USER, [TextPart("read /etc/passwd")])]
    registry = ToolRegistry([SandboxViolationTool()])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert "outside the workspace" in finished.result_summary


def test_unknown_tool_name_becomes_error_result() -> None:
    provider = FakeProvider([[ToolCallComplete("call-1", "nonexistent", {})], [Done("end_turn")]])
    history = [Message(Role.USER, [TextPart("hi")])]

    events = _collect(agent.run(provider, MODEL, "sys", history, ToolRegistry(), _ctx()))

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert "Unknown tool" in finished.result_summary


def test_tool_timeout_becomes_error_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "TOOL_TIMEOUT_SECONDS", 0.05)
    provider = FakeProvider([[ToolCallComplete("call-1", "slow", {})], [Done("end_turn")]])
    history = [Message(Role.USER, [TextPart("hi")])]
    registry = ToolRegistry([SlowTool()])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert "timed out" in finished.result_summary


def test_step_limit_stops_with_an_error_event() -> None:
    # The provider calls a tool on every single turn, so the loop never gets
    # a turn with zero tool calls — it must give up after MAX_STEPS.
    provider = FakeProvider(
        [[ToolCallComplete(f"call-{i}", "echo", {"text": "x"})] for i in range(agent.MAX_STEPS)]
    )
    history = [Message(Role.USER, [TextPart("loop forever")])]
    registry = ToolRegistry([EchoTool()])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    assert isinstance(events[-1], Error)
    assert "Stopped after" in events[-1].message
    assert not any(isinstance(e, Done) for e in events)


def test_provider_exception_becomes_error_event_not_a_crash() -> None:
    provider = FakeProvider([ProviderConnectionError("network down")])
    history = [Message(Role.USER, [TextPart("hi")])]

    events = _collect(agent.run(provider, MODEL, "sys", history, ToolRegistry(), _ctx()))

    assert events == [Error("network down")]


def test_provider_error_event_stops_the_loop() -> None:
    provider = FakeProvider([[Error("bad request")]])
    history = [Message(Role.USER, [TextPart("hi")])]

    events = _collect(agent.run(provider, MODEL, "sys", history, ToolRegistry(), _ctx()))

    assert events == [Error("bad request")]


def test_no_tools_offered_when_model_does_not_support_tools() -> None:
    no_tools_model = ModelInfo(
        id="m", display_name="m", supports_tools=False, supports_images=False, context_tokens=10
    )
    provider = FakeProvider([[TextDelta("hi"), Done("end_turn")]])
    registry = ToolRegistry([EchoTool()])
    history = [Message(Role.USER, [TextPart("hi")])]

    _collect(agent.run(provider, no_tools_model, "sys", history, registry, _ctx()))

    assert provider.tool_specs_seen == [[]]


def test_tools_offered_when_model_supports_tools() -> None:
    provider = FakeProvider([[TextDelta("hi"), Done("end_turn")]])
    registry = ToolRegistry([EchoTool()])
    history = [Message(Role.USER, [TextPart("hi")])]

    _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    [specs] = provider.tool_specs_seen
    assert [s.name for s in specs] == ["echo"]


def test_vision_only_tool_hidden_from_non_vision_model() -> None:
    class ViewTool(Tool):
        name = "view_image"
        description = "vision only"
        input_schema = {"type": "object", "properties": {}}
        risk = RiskLevel.READ
        requires_vision = True

        async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
            return ToolResult.text("image")

    no_vision_model = ModelInfo(
        id="m", display_name="m", supports_tools=True, supports_images=False, context_tokens=10
    )
    provider = FakeProvider([[TextDelta("hi"), Done("end_turn")]])
    registry = ToolRegistry([EchoTool(), ViewTool()])
    history = [Message(Role.USER, [TextPart("hi")])]

    _collect(agent.run(provider, no_vision_model, "sys", history, registry, _ctx()))

    [specs] = provider.tool_specs_seen
    assert sorted(s.name for s in specs) == ["echo"]


def test_write_tool_denied_by_default_never_runs() -> None:
    write_tool = WriteTool()
    provider = FakeProvider([[ToolCallComplete("call-1", "write_thing", {})], [Done("end_turn")]])
    history = [Message(Role.USER, [TextPart("write it")])]
    registry = ToolRegistry([write_tool])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    assert write_tool.run_count == 0
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert "denied" in finished.result_summary


def test_write_tool_allow_once_runs_but_asks_again_next_time() -> None:
    write_tool = WriteTool()
    confirm_calls: list[str] = []

    def confirm(name: str, arguments: dict[str, object], risk, preview: str) -> ConfirmDecision:
        confirm_calls.append(preview)
        return ConfirmDecision.ALLOW_ONCE

    allowed: set[str] = set()

    async def one_call() -> None:
        provider = FakeProvider(
            [[ToolCallComplete("call-1", "write_thing", {})], [Done("end_turn")]]
        )
        history = [Message(Role.USER, [TextPart("write it")])]
        registry = ToolRegistry([write_tool])
        async for _event in agent.run(
            provider,
            MODEL,
            "sys",
            history,
            registry,
            _ctx(),
            confirm=confirm,
            allowed_tools=allowed,
        ):
            pass

    asyncio.run(one_call())
    asyncio.run(one_call())

    assert write_tool.run_count == 2
    assert confirm_calls == ["would write something", "would write something"]
    assert allowed == set()  # allow-once never adds to the session allow-list


def test_write_tool_allow_session_skips_confirmation_on_next_call() -> None:
    write_tool = WriteTool()
    confirm_calls: list[str] = []

    def confirm(name: str, arguments: dict[str, object], risk, preview: str) -> ConfirmDecision:
        confirm_calls.append(preview)
        return ConfirmDecision.ALLOW_SESSION

    allowed: set[str] = set()

    async def one_call() -> None:
        provider = FakeProvider(
            [[ToolCallComplete("call-1", "write_thing", {})], [Done("end_turn")]]
        )
        history = [Message(Role.USER, [TextPart("write it")])]
        registry = ToolRegistry([write_tool])
        async for _event in agent.run(
            provider,
            MODEL,
            "sys",
            history,
            registry,
            _ctx(),
            confirm=confirm,
            allowed_tools=allowed,
        ):
            pass

    asyncio.run(one_call())
    asyncio.run(one_call())

    assert write_tool.run_count == 2
    assert confirm_calls == ["would write something"]  # only asked once
    assert allowed == {"write_thing"}


def test_read_tool_never_asks_for_confirmation() -> None:
    confirm_calls: list[str] = []

    def confirm(name: str, arguments: dict[str, object], risk, preview: str) -> ConfirmDecision:
        confirm_calls.append(name)
        return ConfirmDecision.DENY

    provider = FakeProvider(
        [[ToolCallComplete("call-1", "echo", {"text": "hi"})], [Done("end_turn")]]
    )
    history = [Message(Role.USER, [TextPart("echo hi")])]
    registry = ToolRegistry([EchoTool()])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx(), confirm=confirm))

    assert confirm_calls == []
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert not finished.is_error


def test_invalid_arguments_are_rejected_before_the_tool_runs() -> None:
    strict_tool = StrictTool()
    provider = FakeProvider(
        [
            [ToolCallComplete("call-1", "strict", {"count": "not an int"})],
            [Done("end_turn")],
        ]
    )
    history = [Message(Role.USER, [TextPart("hi")])]
    registry = ToolRegistry([strict_tool])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    assert strict_tool.run_count == 0
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert "Invalid arguments" in finished.result_summary


def test_missing_required_argument_is_rejected() -> None:
    strict_tool = StrictTool()
    provider = FakeProvider([[ToolCallComplete("call-1", "strict", {})], [Done("end_turn")]])
    history = [Message(Role.USER, [TextPart("hi")])]
    registry = ToolRegistry([strict_tool])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    assert strict_tool.run_count == 0
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error


def test_valid_arguments_pass_validation_and_run() -> None:
    strict_tool = StrictTool()
    provider = FakeProvider(
        [[ToolCallComplete("call-1", "strict", {"count": 5})], [Done("end_turn")]]
    )
    history = [Message(Role.USER, [TextPart("hi")])]
    registry = ToolRegistry([strict_tool])

    events = _collect(agent.run(provider, MODEL, "sys", history, registry, _ctx()))

    assert strict_tool.run_count == 1
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert not finished.is_error
