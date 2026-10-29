"""MCP client (ARCHITECTURE.md §8.3): starts configured servers, lists their
tools, and calls them.

Two transports, both speaking the same JSON-RPC 2.0 envelope:
- **stdio**: a local command/args/env, spoken over the child process's own
  stdin/stdout (one newline-delimited JSON message per line).
- **remote HTTP**: a URL plus an optional auth header, spoken as a single
  JSON-RPC request per HTTP POST.

MCP servers run with the user's own OS permissions — unlike the sandboxed
file tools (`tools/sandbox.py`), nothing here restricts what a server can
do. That limitation is surfaced to the user in Settings → MCP, not hidden.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from deskai.storage import repositories, secrets

_PROTOCOL_VERSION = "2024-11-05"
_REQUEST_TIMEOUT = 30.0
_CLIENT_INFO = {"name": "deskai", "version": "0.1.0"}


class McpError(Exception):
    """A server couldn't be started or reached, or sent back something
    malformed. Callers (ChatSession, Settings' Test button) treat this as a
    normal, recoverable failure — never a reason to crash the chat turn."""


@dataclass(frozen=True)
class McpToolSpec:
    server_id: str
    name: str
    description: str
    input_schema: dict[str, object]

    @property
    def qualified_name(self) -> str:
        return f"mcp__{self.server_id}__{self.name}"


@dataclass(frozen=True)
class McpToolResult:
    text: str
    is_error: bool


@dataclass(frozen=True)
class McpServerConfig:
    id: str
    name: str
    enabled: bool = True
    command: str | None = None  # stdio transport
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None  # remote HTTP transport
    has_auth_header: bool = False
    read_only_tools: frozenset[str] = frozenset()

    @property
    def is_remote(self) -> bool:
        return bool(self.url)


def config_from_row(row: repositories.McpServerRow) -> McpServerConfig:
    config = row.config
    args_raw = config.get("args")
    env_raw = config.get("env")
    read_only_raw = config.get("read_only_tools")
    return McpServerConfig(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        command=str(config["command"]) if config.get("command") else None,
        args=[str(a) for a in args_raw] if isinstance(args_raw, list) else [],
        env={str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {},
        url=str(config["url"]) if config.get("url") else None,
        has_auth_header=bool(config.get("has_auth_header")),
        read_only_tools=frozenset(str(t) for t in read_only_raw)
        if isinstance(read_only_raw, list)
        else frozenset(),
    )


def config_to_dict(config: McpServerConfig) -> dict[str, object]:
    data: dict[str, object] = {"read_only_tools": sorted(config.read_only_tools)}
    if config.is_remote:
        data["url"] = config.url
        data["has_auth_header"] = config.has_auth_header
    else:
        data["command"] = config.command
        data["args"] = config.args
        data["env"] = config.env
    return data


class _Transport(Protocol):
    async def start(self) -> None: ...
    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...
    async def notify(self, method: str, params: dict[str, object]) -> None: ...
    def terminate(self) -> None: ...
    async def wait_closed(self) -> None: ...


class _StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str]) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = itertools.count(1)
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        merged_env = {**os.environ, **self._env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                env=merged_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise McpError(
                f"Command not found or not runnable: {self._command!r}. Make sure it's "
                "installed and on PATH (many MCP servers need Node or Python)."
            ) from exc

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        process = self._require_process()
        request_id = next(self._next_id)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        async with self._lock:
            await self._send(process, message)
            return await self._read_response(process, request_id)

    async def notify(self, method: str, params: dict[str, object]) -> None:
        process = self._require_process()
        message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
        async with self._lock:
            await self._send(process, message)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise McpError(f"server {self._command!r} is not running")
        return self._process

    async def _send(self, process: asyncio.subprocess.Process, message: dict[str, object]) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpError(f"server {self._command!r} is no longer accepting input") from exc

    async def _read_response(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, object]:
        async def _read() -> dict[str, object]:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise McpError(f"server {self._command!r} closed its output unexpectedly")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # servers sometimes log non-protocol lines to stdout
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise McpError(str(message["error"]))
                return _as_dict(message.get("result"))

        try:
            return await asyncio.wait_for(_read(), timeout=_REQUEST_TIMEOUT)
        except TimeoutError as exc:
            raise McpError(f"server {self._command!r} timed out") from exc

    def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    async def wait_closed(self) -> None:
        """Waits for the terminated process to actually exit, so its subprocess
        transport finishes its own cleanup before the event loop that created
        it goes away (otherwise it finalizes later, during GC, against an
        already-closed loop — harmless, but noisy)."""
        if self._process is not None:
            await self._process.wait()


class _HttpTransport:
    def __init__(self, url: str, auth_header: str | None) -> None:
        self._url = url
        self._headers = {"Authorization": auth_header} if auth_header else {}
        self._next_id = itertools.count(1)

    async def start(self) -> None:
        pass  # nothing to spawn; the first request proves (or disproves) connectivity

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = next(self._next_id)
        body = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if "error" in body:
            raise McpError(str(body["error"]))
        return _as_dict(body.get("result"))

    async def notify(self, method: str, params: dict[str, object]) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params})

    async def _post(self, message: dict[str, object]) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(self._url, json=message, headers=self._headers)
        except httpx.HTTPError as exc:
            raise McpError(f"could not reach {self._url}: {exc}") from exc
        if response.is_error:
            raise McpError(f"{self._url} returned HTTP {response.status_code}")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise McpError(f"{self._url} did not return valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise McpError(f"{self._url} did not return a JSON object")
        return parsed

    def terminate(self) -> None:
        pass  # no process to stop

    async def wait_closed(self) -> None:
        pass  # no process to wait on


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _build_transport(config: McpServerConfig, auth_header: str | None) -> _Transport:
    if config.is_remote:
        assert config.url is not None
        return _HttpTransport(config.url, auth_header)
    return _StdioTransport(config.command or "", config.args, config.env)


class McpServerConnection:
    """One running (or remote-but-reachable) server: handshake, its advertised
    tools, and the ability to call them."""

    def __init__(self, config: McpServerConfig, auth_header: str | None) -> None:
        self.config = config
        self.tools: list[McpToolSpec] = []
        self._transport = _build_transport(config, auth_header)
        self._started = False

    async def ensure_started(self) -> list[McpToolSpec]:
        if self._started:
            return self.tools
        await self._transport.start()
        await self._transport.request(
            "initialize",
            {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO},
        )
        await self._transport.notify("notifications/initialized", {})
        result = await self._transport.request("tools/list", {})
        self.tools = _parse_tool_specs(self.config.id, result.get("tools"))
        self._started = True
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> McpToolResult:
        result = await self._transport.request(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )
        texts = [
            str(item.get("text", ""))
            for item in _as_list(result.get("content"))
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return McpToolResult(
            text="\n".join(texts) if texts else "(no text content)",
            is_error=bool(result.get("isError", False)),
        )

    def stop(self) -> None:
        self._transport.terminate()

    async def aclose(self) -> None:
        """Like `stop()`, but also waits for a stdio server's process to
        actually exit — used where we're in an async context that can
        afford to (tests, Settings' "Test" probe)."""
        self._transport.terminate()
        await self._transport.wait_closed()


def _parse_tool_specs(server_id: str, raw_tools: object) -> list[McpToolSpec]:
    if not isinstance(raw_tools, list):
        return []
    specs = []
    for item in raw_tools:
        if not isinstance(item, dict) or "name" not in item:
            continue
        specs.append(
            McpToolSpec(
                server_id=server_id,
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                input_schema=dict(item.get("inputSchema") or {"type": "object", "properties": {}}),
            )
        )
    return specs


async def probe_server(config: McpServerConfig, auth_header: str | None) -> list[McpToolSpec]:
    """Starts a server just long enough to list its tools, then stops it —
    used by Settings → MCP's "Test" button, independent of any session's
    live `McpManager`."""
    connection = McpServerConnection(config, auth_header)
    try:
        return await connection.ensure_started()
    finally:
        await connection.aclose()


class McpManager:
    """Owns every started server for the running app (ARCHITECTURE.md §8.3):
    lazy startup, tool discovery, and calling into them — one instance lives
    in `AppController` and is shared by every `ChatSession`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._connections: dict[str, McpServerConnection] = {}
        self._last_errors: dict[str, str] = {}

    def list_configs(self) -> list[McpServerConfig]:
        return [config_from_row(row) for row in repositories.list_mcp_servers(self._conn)]

    async def ensure_enabled_servers_started(self) -> None:
        """Starts any enabled server not already running (ARCHITECTURE.md
        §8.3: "enabled servers start lazily the first time a session needs
        tools"). One server failing to start never blocks the others."""
        for config in self.list_configs():
            if not config.enabled or config.id in self._connections:
                continue
            auth_header = (
                secrets.get_secret(f"mcp:{config.id}:auth_header")
                if config.has_auth_header
                else None
            )
            connection = McpServerConnection(config, auth_header)
            try:
                await connection.ensure_started()
            except McpError as exc:
                self._last_errors[config.id] = str(exc)
                continue
            self._last_errors.pop(config.id, None)
            self._connections[config.id] = connection

    def connections(self) -> list[McpServerConnection]:
        return list(self._connections.values())

    def last_error(self, server_id: str) -> str | None:
        return self._last_errors.get(server_id)

    async def call_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, object]
    ) -> McpToolResult:
        connection = self._connections.get(server_id)
        if connection is None:
            raise McpError(f"MCP server not running: {server_id!r}")
        return await connection.call_tool(tool_name, arguments)

    def stop_all(self) -> None:
        for connection in self._connections.values():
            connection.stop()
        self._connections.clear()
