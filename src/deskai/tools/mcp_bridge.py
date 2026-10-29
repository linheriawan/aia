"""Exposes MCP server tools through the registry (ARCHITECTURE.md §8.3).

Namespaced `mcp__<server>__<tool>` so they can never collide with a built-in
tool name. Default risk is WRITE (confirmation required) since an MCP
server's own tools are opaque to this app; the user can mark individual
tools as READ in Settings → MCP once they've seen what a server offers.
"""

from __future__ import annotations

from deskai.mcp_client.manager import McpError, McpManager, McpToolSpec
from deskai.tools.base import RiskLevel, Tool, ToolContext, ToolResult


class McpTool(Tool):
    requires_vision = False

    def __init__(self, manager: McpManager, spec: McpToolSpec, *, risk: RiskLevel) -> None:
        self._manager = manager
        self._spec = spec
        self.name = spec.qualified_name
        self.description = spec.description
        self.input_schema = spec.input_schema
        self.risk = risk

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        try:
            result = await self._manager.call_tool(self._spec.server_id, self._spec.name, args)
        except McpError as exc:
            return ToolResult.text(str(exc), is_error=True)
        return ToolResult.text(result.text, is_error=result.is_error)


def build_mcp_tools(manager: McpManager) -> list[McpTool]:
    """One `McpTool` per tool a currently-started server advertised."""
    tools: list[McpTool] = []
    for connection in manager.connections():
        read_only = connection.config.read_only_tools
        for spec in connection.tools:
            risk = RiskLevel.READ if spec.name in read_only else RiskLevel.WRITE
            tools.append(McpTool(manager, spec, risk=risk))
    return tools
