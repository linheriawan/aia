from pathlib import Path

import pytest

from deskai.core.messages import ImagePart, Message, Role, TextPart, ToolCallPart, ToolResultPart
from deskai.storage import db, repositories


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "deskai.db")
    yield connection
    connection.close()


def test_provider_round_trip(conn) -> None:
    repositories.upsert_provider(
        conn, "bedrock", "bedrock", "Bedrock", {"region": "us-east-1", "auth_mode": "keys"}
    )
    row = repositories.get_provider(conn, "bedrock")
    assert row is not None
    assert row.kind == "bedrock"
    assert row.config["region"] == "us-east-1"
    assert row.enabled is True

    repositories.upsert_provider(
        conn, "bedrock", "bedrock", "Bedrock", {"region": "eu-west-1"}, enabled=False
    )
    updated = repositories.get_provider(conn, "bedrock")
    assert updated is not None
    assert updated.config["region"] == "eu-west-1"
    assert updated.enabled is False


def test_replace_models_is_idempotent(conn) -> None:
    repositories.upsert_provider(conn, "bedrock", "bedrock", "Bedrock", {})
    models = [
        repositories.ModelRow("m1", "bedrock", "claude-x", "Claude X", True, True, 200_000),
        repositories.ModelRow("m2", "bedrock", "claude-y", "Claude Y", False, False, 100_000),
    ]
    repositories.replace_models(conn, "bedrock", models)
    assert {m.model_id for m in repositories.list_models(conn, "bedrock")} == {
        "claude-x",
        "claude-y",
    }

    repositories.replace_models(conn, "bedrock", models[:1])
    remaining = repositories.list_models(conn, "bedrock")
    assert len(remaining) == 1
    assert remaining[0].model_id == "claude-x"


def test_delete_provider_cascades_to_its_models_and_clears_session_selection(conn) -> None:
    repositories.upsert_provider(conn, "p1", "openai_compat", "P1", {})
    repositories.replace_models(
        conn, "p1", [repositories.ModelRow("m1", "p1", "model-x", "Model X", False, False, 4096)]
    )
    repositories.create_session(conn, "s1", "New chat", "p1", "m1")

    repositories.delete_provider(conn, "p1")

    assert repositories.get_provider(conn, "p1") is None
    assert repositories.list_models(conn, "p1") == []
    [session] = repositories.list_sessions(conn)
    assert session.provider_id is None
    assert session.model_row_id is None


def test_session_and_messages_persist(conn) -> None:
    repositories.upsert_provider(conn, "bedrock", "bedrock", "Bedrock", {})
    repositories.replace_models(
        conn,
        "bedrock",
        [repositories.ModelRow("m1", "bedrock", "claude-x", "Claude X", True, True, 200_000)],
    )

    repositories.create_session(conn, "s1", "New chat", "bedrock", "m1")
    sessions = repositories.list_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0].id == "s1"
    assert sessions[0].model_row_id == "m1"

    repositories.append_message(conn, "s1", Message(Role.USER, [TextPart("hello")]))
    repositories.append_message(conn, "s1", Message(Role.ASSISTANT, [TextPart("hi there")]))

    history = repositories.list_messages(conn, "s1")
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].parts == [TextPart("hello")]
    assert history[1].parts == [TextPart("hi there")]


def test_set_session_title_renames_without_reordering_the_list(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)
    repositories.create_session(conn, "s2", "Second chat", None, None)

    repositories.set_session_title(conn, "s1", "Renamed chat")

    sessions = repositories.list_sessions(conn)
    assert {s.id: s.title for s in sessions} == {"s1": "Renamed chat", "s2": "Second chat"}
    # list_sessions orders by updated_at DESC — a rename must not bump the row.
    assert [s.id for s in sessions] == ["s2", "s1"]


def test_message_parts_round_trip_through_json(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)

    message = Message(
        role=Role.ASSISTANT,
        parts=[
            TextPart("here is an image"),
            ImagePart(b"\x89PNG...", "image/png"),
            ToolCallPart("call-1", "read_text", {"path": "a.txt"}),
            ToolResultPart("call-1", [TextPart("contents")], is_error=False),
        ],
    )
    repositories.append_message(conn, "s1", message)

    [loaded] = repositories.list_messages(conn, "s1")
    assert loaded.role is Role.ASSISTANT
    assert loaded.parts == message.parts


def test_deleting_a_model_clears_session_reference(conn) -> None:
    repositories.upsert_provider(conn, "bedrock", "bedrock", "Bedrock", {})
    repositories.replace_models(
        conn,
        "bedrock",
        [repositories.ModelRow("m1", "bedrock", "claude-x", "Claude X", True, True, 200_000)],
    )
    repositories.create_session(conn, "s1", "New chat", "bedrock", "m1")

    # Editing the model list in Settings replaces all rows for the provider;
    # existing sessions must not be left dangling (ON DELETE SET NULL).
    repositories.replace_models(conn, "bedrock", [])

    [session] = repositories.list_sessions(conn)
    assert session.model_row_id is None


def test_record_usage(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)
    repositories.record_usage(
        conn,
        session_id="s1",
        provider="bedrock",
        model_id="claude-x",
        input_tokens=10,
        output_tokens=20,
        triggered_by="chat",
    )
    row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["session_id"] == "s1"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["triggered_by"] == "chat"


def test_session_folders_crud(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)

    repositories.add_session_folder(conn, "s1", "/tmp/workspace", read_only=False)
    repositories.add_session_folder(conn, "s1", "/tmp/readonly", read_only=True)

    folders = repositories.list_session_folders(conn, "s1")
    assert {f.path for f in folders} == {"/tmp/workspace", "/tmp/readonly"}
    assert next(f for f in folders if f.path == "/tmp/readonly").read_only is True

    repositories.set_session_folder_read_only(conn, "s1", "/tmp/workspace", True)
    folders = repositories.list_session_folders(conn, "s1")
    assert next(f for f in folders if f.path == "/tmp/workspace").read_only is True

    repositories.remove_session_folder(conn, "s1", "/tmp/readonly")
    folders = repositories.list_session_folders(conn, "s1")
    assert [f.path for f in folders] == ["/tmp/workspace"]


def test_adding_the_same_folder_twice_updates_rather_than_duplicates(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)
    repositories.add_session_folder(conn, "s1", "/tmp/workspace", read_only=False)
    repositories.add_session_folder(conn, "s1", "/tmp/workspace", read_only=True)

    folders = repositories.list_session_folders(conn, "s1")
    assert len(folders) == 1
    assert folders[0].read_only is True


def test_list_usage_filters_by_range(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)
    repositories.record_usage(
        conn,
        session_id="s1",
        provider="bedrock",
        model_id="claude-x",
        input_tokens=10,
        output_tokens=5,
        triggered_by="chat",
    )

    all_rows = repositories.list_usage(
        conn, "0000-01-01T00:00:00+00:00", "9999-01-01T00:00:00+00:00"
    )
    assert len(all_rows) == 1
    assert all_rows[0].provider == "bedrock"

    none_rows = repositories.list_usage(
        conn, "9999-01-01T00:00:00+00:00", "9999-12-31T00:00:00+00:00"
    )
    assert none_rows == []


def test_mcp_server_round_trip(conn) -> None:
    repositories.upsert_mcp_server(
        conn, "fs", "Filesystem", {"command": "npx", "args": ["-y", "mcp-server-fs"]}
    )
    row = repositories.get_mcp_server(conn, "fs")
    assert row is not None
    assert row.name == "Filesystem"
    assert row.enabled is True
    assert row.config["command"] == "npx"

    repositories.upsert_mcp_server(
        conn, "fs", "Filesystem", {"command": "npx", "args": ["-y", "other"]}, enabled=False
    )
    updated = repositories.get_mcp_server(conn, "fs")
    assert updated is not None
    assert updated.enabled is False
    assert updated.config["args"] == ["-y", "other"]


def test_list_mcp_servers_orders_by_name(conn) -> None:
    repositories.upsert_mcp_server(conn, "b", "Bravo", {})
    repositories.upsert_mcp_server(conn, "a", "Alpha", {})
    assert [row.id for row in repositories.list_mcp_servers(conn)] == ["a", "b"]


def test_delete_mcp_server(conn) -> None:
    repositories.upsert_mcp_server(conn, "fs", "Filesystem", {})
    repositories.delete_mcp_server(conn, "fs")
    assert repositories.get_mcp_server(conn, "fs") is None
