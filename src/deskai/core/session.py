"""ChatSession: history, provider/model, workspace, and persistence (ARCHITECTURE.md §4, §6, §7)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date

from deskai.core import agent
from deskai.core.events import Error, StreamEvent, Usage
from deskai.core.messages import Message, Role, TextPart
from deskai.mcp_client.manager import McpManager
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo, Provider
from deskai.skills.loader import Skill, skill_index
from deskai.skills.manager import SkillsManager
from deskai.storage import repositories
from deskai.tools.base import Tool, ToolContext
from deskai.tools.mcp_bridge import build_mcp_tools
from deskai.tools.registry import ToolRegistry
from deskai.tools.sandbox import Sandbox, matches_local_only_rule
from deskai.usage import tracker

DEFAULT_SYSTEM_PROMPT = "You are deskai, a helpful desktop assistant."
_SYSTEM_PROMPT_SETTING_KEY = "chat.system_prompt"

# Tools that never touch `ctx.workspace`, so unlike the file/OCR/extraction
# tools they stay available even with no folder attached to the session —
# MCP servers were never sandboxed in the first place (ARCHITECTURE.md §8.3).
_SANDBOX_INDEPENDENT_TOOL_NAMES = {"load_skill"}


def _tools_for_turn(registry: ToolRegistry, sandbox: Sandbox) -> ToolRegistry:
    if sandbox.has_folders():
        return registry

    def _is_sandbox_independent(tool: Tool) -> bool:
        return tool.name.startswith("mcp__") or tool.name in _SANDBOX_INDEPENDENT_TOOL_NAMES

    return ToolRegistry([t for t in registry.all_tools() if _is_sandbox_independent(t)])


def build_system_prompt(
    sandbox: Sandbox, base_prompt: str = DEFAULT_SYSTEM_PROMPT, skills: list[Skill] | None = None
) -> str:
    """`base_prompt` + workspace folders + today's date + the skill index
    (ARCHITECTURE.md §6, §10.1).

    `base_prompt` is user-editable (Settings → General); `DEFAULT_SYSTEM_PROMPT`
    is only the fallback when nothing has been saved yet. The skill index is
    name + description only, so the prompt stays small no matter how many
    skills exist — the model calls `load_skill(name)` for the full body.
    """
    if sandbox.has_folders():
        folders = "\n".join(
            f"- {folder.path} ({'read-only' if folder.read_only else 'read-write'})"
            for folder in sandbox.folders
        )
    else:
        folders = (
            "(none attached — file tools are unavailable; ask the user to attach a "
            "workspace folder before trying to read any files)"
        )
    skills_section = (
        f"\n\nAvailable skills (call load_skill(name) for the full instructions "
        f"when a request matches one):\n{skill_index(skills)}"
        if skills
        else ""
    )
    return (
        f"{base_prompt}\n\n"
        f"Today's date: {date.today().isoformat()}\n\n"
        f"Workspace folders:\n{folders}"
        f"{skills_section}"
    )


def _is_local_provider(conn: sqlite3.Connection, provider_id: str) -> bool:
    """Whether `provider_id` is explicitly marked local/offline in its own config

    (Settings → Providers) — never inferred from its kind, since an
    OpenAI-compatible-shaped endpoint can just as easily be a cloud service.
    """
    row = repositories.get_provider(conn, provider_id)
    return bool(row is not None and row.config.get("is_local"))


@dataclass
class ChatSession:
    id: str
    title: str
    provider: Provider | None
    provider_id: str
    model: ModelInfo
    model_row_id: str
    conn: sqlite3.Connection
    sandbox: Sandbox
    registry: ToolRegistry
    ocr: OcrEngine
    history: list[Message] = field(default_factory=list)
    allowed_write_tools: set[str] = field(default_factory=set)
    skills_manager: SkillsManager | None = None
    mcp_manager: McpManager | None = None

    def requires_local_provider(self) -> bool:
        """True if any attached folder falls under a Local-only rule (ARCHITECTURE.md §7) —
        there's no separate manual toggle; this is derived fresh from the session's
        actual folders each time, so it can never drift out of sync with them."""
        return any(
            matches_local_only_rule(self.conn, folder.path) for folder in self.sandbox.folders
        )

    async def send_message(
        self, text: str, confirm: agent.ConfirmFn | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Append `text` as a user message, stream the reply, and persist every turn.

        `confirm` decides WRITE+ tool calls (ARCHITECTURE.md §8.1); the UI
        supplies a real one backed by a dialog. With none, every write is
        denied — safe by default rather than silently allowed.

        Raises `RuntimeError` if no provider is configured for this session.
        """
        if self.provider is None:
            raise RuntimeError("No provider configured for this session")
        provider = self.provider

        if self.requires_local_provider() and not _is_local_provider(self.conn, self.provider_id):
            yield Error(
                "A workspace folder attached to this session requires a local provider "
                "(Settings → Privacy → Local-only folder rules). Pick a provider marked "
                "local, or detach that folder."
            )
            return

        forced_skill: Skill | None = None
        stripped = text.strip()
        if stripped.startswith("/") and self.skills_manager is not None:
            skill_name = stripped[1:].split(maxsplit=1)[0] if stripped[1:].split() else ""
            forced_skill = self.skills_manager.get(skill_name)

        user_message = Message(role=Role.USER, parts=[TextPart(text)])
        self.history.append(user_message)
        repositories.append_message(self.conn, self.id, user_message)

        ctx = ToolContext(
            session_id=self.id,
            workspace=self.sandbox,
            ocr=self.ocr,
            conn=self.conn,
            provider=provider,
            model=self.model,
        )
        if self.mcp_manager is not None:
            await self.mcp_manager.ensure_enabled_servers_started()
            for mcp_tool in build_mcp_tools(self.mcp_manager):
                self.registry.register(mcp_tool)
        tools = _tools_for_turn(self.registry, self.sandbox)
        base_prompt = (
            repositories.get_setting(self.conn, _SYSTEM_PROMPT_SETTING_KEY, DEFAULT_SYSTEM_PROMPT)
            or DEFAULT_SYSTEM_PROMPT
        )
        skills = self.skills_manager.enabled_skills() if self.skills_manager is not None else []
        system_prompt = build_system_prompt(self.sandbox, base_prompt, skills)
        if forced_skill is not None:
            system_prompt = (
                f"{system_prompt}\n\n---\nThe user explicitly invoked the "
                f"'{forced_skill.name}' skill for this message. Follow these "
                f"instructions:\n\n{forced_skill.body}"
            )

        def on_message(message: Message) -> None:
            repositories.append_message(self.conn, self.id, message)

        def on_usage(usage: Usage) -> None:
            tracker.record(
                self.conn,
                session_id=self.id,
                provider=provider.name,
                model_id=self.model.id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                triggered_by="chat",
            )

        async for event in agent.run(
            provider,
            self.model,
            system_prompt,
            self.history,
            tools,
            ctx,
            on_message=on_message,
            on_usage=on_usage,
            confirm=confirm if confirm is not None else agent.deny_everything,
            allowed_tools=self.allowed_write_tools,
        ):
            yield event
