"""Tests for the MCP client (ARCHITECTURE.md §8.3): the real stdio transport
against a tiny real fixture server (tests/fixtures/fake_mcp_server.py, a
local subprocess — not a live external call), the HTTP transport against a
local test server, and McpManager's lazy-start/registration lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from deskai.mcp_client.manager import (
    McpError,
    McpManager,
    McpServerConfig,
    config_from_row,
    config_to_dict,
    probe_server,
)
from deskai.storage import db, repositories

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _stdio_config(server_id: str = "fake") -> McpServerConfig:
    return McpServerConfig(id=server_id, name="Fake", command=sys.executable, args=[FAKE_SERVER])


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "deskai.db")
    yield connection
    connection.close()


# --- stdio transport, via the real fixture subprocess ------------------------


def test_probe_server_lists_tools_over_stdio() -> None:
    tools = _run(probe_server(_stdio_config(), auth_header=None))
    assert [t.name for t in tools] == ["echo"]
    assert tools[0].qualified_name == "mcp__fake__echo"
    assert tools[0].input_schema["properties"] == {"text": {"type": "string"}}


def test_stdio_call_tool_echoes_back() -> None:
    async def _go() -> Any:
        from deskai.mcp_client.manager import McpServerConnection

        connection = McpServerConnection(_stdio_config(), auth_header=None)
        try:
            await connection.ensure_started()
            return await connection.call_tool("echo", {"text": "hello"})
        finally:
            await connection.aclose()

    result = _run(_go())
    assert result.text == "echo: hello"
    assert result.is_error is False


def test_stdio_call_tool_unknown_name_is_an_error() -> None:
    async def _go() -> Any:
        from deskai.mcp_client.manager import McpServerConnection

        connection = McpServerConnection(_stdio_config(), auth_header=None)
        try:
            await connection.ensure_started()
            return await connection.call_tool("boom", {})
        finally:
            await connection.aclose()

    result = _run(_go())
    assert result.is_error is True
    assert "unknown tool" in result.text


def test_stdio_command_not_found_raises_mcp_error() -> None:
    config = McpServerConfig(id="missing", name="Missing", command="not-a-real-command-xyz")
    with pytest.raises(McpError, match="not-a-real-command-xyz"):
        _run(probe_server(config, auth_header=None))


# --- HTTP transport, via a local test server --------------------------------


class _JsonRpcHandler(BaseHTTPRequestHandler):
    seen_auth_header: str | None = None

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _JsonRpcHandler.seen_auth_header = self.headers.get("Authorization")

        method = body.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping", "description": "Pings.", "inputSchema": {}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
        else:
            result = {}

        response = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # silence test output
        pass


@pytest.fixture
def http_server() -> Any:
    server = HTTPServer(("127.0.0.1", 0), _JsonRpcHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def test_probe_server_over_http(http_server: HTTPServer) -> None:
    url = f"http://127.0.0.1:{http_server.server_port}/"
    config = McpServerConfig(id="remote", name="Remote", url=url)

    tools = _run(probe_server(config, auth_header="Bearer secret-token"))

    assert [t.name for t in tools] == ["ping"]
    assert _JsonRpcHandler.seen_auth_header == "Bearer secret-token"


def test_http_call_tool(http_server: HTTPServer) -> None:
    from deskai.mcp_client.manager import McpServerConnection

    url = f"http://127.0.0.1:{http_server.server_port}/"
    config = McpServerConfig(id="remote", name="Remote", url=url)

    async def _go() -> Any:
        connection = McpServerConnection(config, auth_header=None)
        await connection.ensure_started()
        return await connection.call_tool("ping", {})

    result = _run(_go())
    assert result.text == "pong"


def test_http_unreachable_url_raises_mcp_error() -> None:
    config = McpServerConfig(id="remote", name="Remote", url="http://127.0.0.1:1")
    with pytest.raises(McpError):
        _run(probe_server(config, auth_header=None))


# --- config (de)serialization -------------------------------------------------


def test_config_round_trips_through_dict_for_stdio() -> None:
    config = McpServerConfig(
        id="fake",
        name="Fake",
        command="npx",
        args=["-y", "server"],
        env={"KEY": "value"},
        read_only_tools=frozenset({"echo"}),
    )
    row = repositories.McpServerRow(
        id=config.id, name=config.name, enabled=True, config=config_to_dict(config)
    )
    rebuilt = config_from_row(row)
    assert rebuilt.command == "npx"
    assert rebuilt.args == ["-y", "server"]
    assert rebuilt.env == {"KEY": "value"}
    assert rebuilt.read_only_tools == frozenset({"echo"})
    assert rebuilt.is_remote is False


def test_config_round_trips_through_dict_for_remote() -> None:
    config = McpServerConfig(
        id="remote", name="Remote", url="https://example.test/mcp", has_auth_header=True
    )
    row = repositories.McpServerRow(
        id=config.id, name=config.name, enabled=True, config=config_to_dict(config)
    )
    rebuilt = config_from_row(row)
    assert rebuilt.url == "https://example.test/mcp"
    assert rebuilt.has_auth_header is True
    assert rebuilt.is_remote is True


# --- McpManager ----------------------------------------------------------------


def test_manager_starts_enabled_servers_and_registers_tools(conn: Any) -> None:
    repositories.upsert_mcp_server(conn, "fake", "Fake", config_to_dict(_stdio_config()))
    manager = McpManager(conn)

    _run(manager.ensure_enabled_servers_started())

    assert len(manager.connections()) == 1
    assert manager.connections()[0].tools[0].name == "echo"
    manager.stop_all()


def test_manager_skips_disabled_servers(conn: Any) -> None:
    repositories.upsert_mcp_server(
        conn, "fake", "Fake", config_to_dict(_stdio_config()), enabled=False
    )
    manager = McpManager(conn)

    _run(manager.ensure_enabled_servers_started())

    assert manager.connections() == []


def test_manager_records_last_error_for_a_broken_server(conn: Any) -> None:
    broken = McpServerConfig(id="broken", name="Broken", command="not-a-real-command-xyz")
    repositories.upsert_mcp_server(conn, "broken", "Broken", config_to_dict(broken))
    manager = McpManager(conn)

    _run(manager.ensure_enabled_servers_started())

    assert manager.connections() == []
    assert manager.last_error("broken") is not None


def test_manager_call_tool_routes_to_the_right_server(conn: Any) -> None:
    repositories.upsert_mcp_server(conn, "fake", "Fake", config_to_dict(_stdio_config()))
    manager = McpManager(conn)

    async def _go() -> Any:
        # A real app has one long-lived event loop (qasync), so start and
        # call share it too — unlike `probe_server`, a started connection's
        # subprocess streams are bound to the loop that started them.
        await manager.ensure_enabled_servers_started()
        return await manager.call_tool("fake", "echo", {"text": "hi"})

    result = _run(_go())
    assert result.text == "echo: hi"
    manager.stop_all()


def test_manager_call_tool_on_unstarted_server_raises(conn: Any) -> None:
    manager = McpManager(conn)
    with pytest.raises(McpError):
        _run(manager.call_tool("nope", "echo", {}))
