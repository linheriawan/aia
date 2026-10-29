"""ChatSession tests: persistence, usage recording, and the local-only privacy
enforcement (ARCHITECTURE.md §7: "force the session to a local provider, or
block cloud sending with a clear message"). There's no separate privacy_mode
flag — it's derived fresh each send from the session's actual attached
folders against Settings → Privacy's Local-only prefixes, so it can't drift."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from deskai.core.events import Done, Error, StreamEvent, TextDelta, ToolCallComplete, Usage
from deskai.core.messages import Message, Role, TextPart, ToolResultPart
from deskai.core.session import ChatSession
from deskai.mcp_client.manager import McpManager
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo
from deskai.skills.loader import Skill
from deskai.storage import db, repositories
from deskai.tools.registry import ToolRegistry
from deskai.tools.sandbox import Sandbox, WorkspaceFolder

MODEL = ModelInfo(
    id="m", display_name="M", supports_tools=False, supports_images=False, context_tokens=10
)


class FakeProvider:
    def __init__(self, name: str, events: list[StreamEvent]) -> None:
        self.name = name
        self._events = events
        self.called = False
        self.last_system: str | None = None

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def test_connection(self) -> None:
        pass

    async def stream_chat(
        self, model: ModelInfo, system: str, messages: list[Message], tools: list[Any]
    ) -> AsyncIterator[StreamEvent]:
        self.called = True
        self.last_system = system
        for event in self._events:
            yield event


def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    async def _run() -> list[Any]:
        return [item async for item in ait]

    return asyncio.run(_run())


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "deskai.db")
    repositories.create_session(connection, "s1", "New chat", None, None)
    yield connection
    connection.close()


def _session(
    conn: Any,
    provider: Any,
    local_only_folder: Path | None = None,
    skills_manager: Any = None,
    mcp_manager: Any = None,
) -> ChatSession:
    folders = [WorkspaceFolder(local_only_folder)] if local_only_folder is not None else []
    return ChatSession(
        id="s1",
        title="New chat",
        provider=provider,
        provider_id=provider.name if provider is not None else "",
        model=MODEL,
        model_row_id="m1",
        conn=conn,
        sandbox=Sandbox(folders, deny_list=[]),
        registry=ToolRegistry(),
        ocr=OcrEngine(conn),
        skills_manager=skills_manager,
        mcp_manager=mcp_manager,
    )


def test_send_message_raises_without_a_provider(conn: Any) -> None:
    session = _session(conn, None)
    with pytest.raises(RuntimeError):
        _collect(session.send_message("hi"))


def test_send_message_persists_user_and_assistant_messages(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("hello back"), Done("end_turn")])
    session = _session(conn, provider)

    _collect(session.send_message("hi there"))

    history = repositories.list_messages(conn, "s1")
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].parts == [TextPart("hi there")]
    assert history[1].parts == [TextPart("hello back")]
    assert session.history == history


def test_send_message_records_usage(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("hi"), Usage(10, 20), Done("end_turn")])
    session = _session(conn, provider)

    _collect(session.send_message("hi"))

    row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["provider"] == "bedrock"
    assert row["model_id"] == "m"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20


def test_cloud_session_with_no_local_only_folder_allows_a_cloud_provider(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("cloud reply"), Done("end_turn")])
    session = _session(conn, provider)

    events = _collect(session.send_message("hi"))

    assert provider.called is True
    assert TextDelta("cloud reply") in events


def test_local_only_folder_blocks_a_cloud_provider(conn: Any, tmp_path: Path) -> None:
    secret_dir = tmp_path / "secret-project"
    secret_dir.mkdir()
    repositories.set_setting_list(conn, "privacy.local_only_folder_prefixes", [str(secret_dir)])

    provider = FakeProvider("bedrock", [TextDelta("should not run"), Done("end_turn")])
    session = _session(conn, provider, local_only_folder=secret_dir)

    events = _collect(session.send_message("hi"))

    assert provider.called is False
    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "local provider" in events[0].message

    # Blocking must not persist a half-finished turn.
    assert repositories.list_messages(conn, "s1") == []


def test_local_only_folder_allows_a_provider_marked_local(conn: Any, tmp_path: Path) -> None:
    secret_dir = tmp_path / "secret-project"
    secret_dir.mkdir()
    repositories.set_setting_list(conn, "privacy.local_only_folder_prefixes", [str(secret_dir)])
    repositories.upsert_provider(
        conn,
        "ollama",
        "openai_compat",
        "Ollama",
        {"base_url": "http://localhost:11434/v1", "is_local": True},
    )

    provider = FakeProvider("ollama", [TextDelta("local reply"), Done("end_turn")])
    session = _session(conn, provider, local_only_folder=secret_dir)

    events = _collect(session.send_message("hi"))

    assert provider.called is True
    assert TextDelta("local reply") in events


def test_local_only_folder_blocks_an_openai_compat_kind_provider_not_marked_local(
    conn: Any, tmp_path: Path
) -> None:
    """A provider using the generic OpenAI-compatible adapter (e.g. a custom
    Cloudflare endpoint) must not be treated as local just because of its
    kind — only an explicit `is_local` flag in its own config counts."""
    secret_dir = tmp_path / "secret-project"
    secret_dir.mkdir()
    repositories.set_setting_list(conn, "privacy.local_only_folder_prefixes", [str(secret_dir)])
    repositories.upsert_provider(
        conn,
        "my-cloudflare",
        "openai_compat",
        "My Cloudflare",
        {"base_url": "https://api.cloudflare.com/..."},
    )

    provider = FakeProvider("my-cloudflare", [TextDelta("should not run"), Done("end_turn")])
    session = _session(conn, provider, local_only_folder=secret_dir)

    events = _collect(session.send_message("hi"))

    assert provider.called is False
    assert len(events) == 1
    assert isinstance(events[0], Error)


def test_requires_local_provider_is_false_with_no_matching_folder(
    conn: Any, tmp_path: Path
) -> None:
    repositories.set_setting_list(
        conn, "privacy.local_only_folder_prefixes", [str(tmp_path / "other-dir")]
    )
    session = _session(conn, FakeProvider("bedrock", []), local_only_folder=tmp_path / "unrelated")
    assert session.requires_local_provider() is False


class FakeSkillsManager:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = skills

    def enabled_skills(self) -> list[Skill]:
        return self._skills

    def get(self, name: str) -> Skill | None:
        return next((s for s in self._skills if s.name == name), None)


def _skill(name: str = "invoice-list", body: str = "Do the invoice thing.") -> Skill:
    return Skill(
        name=name,
        description="A test skill.",
        version=1,
        tools=[],
        body=body,
        source_path=Path("SKILL.md"),
    )


def test_skill_index_is_added_to_the_system_prompt_when_skills_are_enabled(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("ok"), Done("end_turn")])
    session = _session(conn, provider, skills_manager=FakeSkillsManager([_skill()]))

    _collect(session.send_message("hi"))

    assert provider.last_system is not None
    assert "invoice-list" in provider.last_system
    assert "A test skill." in provider.last_system


def test_no_skill_section_when_no_skills_enabled(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("ok"), Done("end_turn")])
    session = _session(conn, provider, skills_manager=FakeSkillsManager([]))

    _collect(session.send_message("hi"))

    assert provider.last_system is not None
    assert "Available skills" not in provider.last_system


def test_slash_skill_name_injects_the_skill_body_into_the_system_prompt(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("ok"), Done("end_turn")])
    skill = _skill(body="Always sort by due date.")
    session = _session(conn, provider, skills_manager=FakeSkillsManager([skill]))

    _collect(session.send_message("/invoice-list use the Downloads folder"))

    assert provider.last_system is not None
    assert "Always sort by due date." in provider.last_system
    # The user's own message is untouched in history — only the system prompt changes.
    assert session.history[0].parts == [TextPart("/invoice-list use the Downloads folder")]


def test_unknown_slash_command_does_not_inject_anything(conn: Any) -> None:
    provider = FakeProvider("bedrock", [TextDelta("ok"), Done("end_turn")])
    session = _session(conn, provider, skills_manager=FakeSkillsManager([_skill()]))

    _collect(session.send_message("/not-a-real-skill do something"))

    assert provider.last_system is not None
    assert "explicitly invoked" not in provider.last_system


class _TurnsProvider:
    """Like FakeProvider, but yields a different canned turn on each call —
    needed to drive the agent loop through a tool call and then a final
    reply, rather than repeating the same events forever."""

    def __init__(self, name: str, turns: list[list[StreamEvent]]) -> None:
        self.name = name
        self._turns = list(turns)

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def test_connection(self) -> None:
        pass

    async def stream_chat(
        self, model: ModelInfo, system: str, messages: list[Message], tools: list[Any]
    ) -> AsyncIterator[StreamEvent]:
        for event in self._turns.pop(0):
            yield event


def test_mcp_tools_are_registered_and_callable_without_a_workspace_folder(conn: Any) -> None:
    """ARCHITECTURE.md §8.3: MCP tools aren't sandboxed, so — unlike the file/
    OCR/extraction tools — they must stay available even with no workspace
    folder attached to the session. Exercised against the same real stdio
    fixture server test_mcp_client.py uses, not a mock."""
    fake_server = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    repositories.upsert_mcp_server(
        conn,
        "fake",
        "Fake",
        {
            "command": sys.executable,
            "args": [str(fake_server)],
            "read_only_tools": ["echo"],
        },
    )
    mcp_manager = McpManager(conn)

    provider = _TurnsProvider(
        "bedrock",
        [
            [ToolCallComplete("call-1", "mcp__fake__echo", {"text": "hi"})],
            [TextDelta("done"), Done("end_turn")],
        ],
    )
    session = _session(conn, provider, mcp_manager=mcp_manager)
    assert not session.sandbox.has_folders()

    _collect(session.send_message("use the echo tool"))

    assert "mcp__fake__echo" in {t.name for t in session.registry.all_tools()}
    tool_messages = [m for m in session.history if m.role == Role.TOOL]
    assert len(tool_messages) == 1
    result_part = tool_messages[0].parts[0]
    assert isinstance(result_part, ToolResultPart)
    assert result_part.content[0].text == "echo: hi"
    assert result_part.is_error is False

    mcp_manager.stop_all()
