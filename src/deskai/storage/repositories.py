"""Typed access to the SQLite tables (ARCHITECTURE.md §12).

Secrets never go through here — see `storage/secrets.py`. `config_json` /
`parts_json` columns hold only non-secret data.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from deskai.core.messages import (
    ContentPart,
    ImagePart,
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

# --- settings ---------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    value = json.loads(row["value_json"])
    return value if isinstance(value, str) else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )
    conn.commit()


def get_setting_list(conn: sqlite3.Connection, key: str) -> list[str]:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return []
    value = json.loads(row["value_json"])
    return [str(v) for v in value] if isinstance(value, list) else []


def set_setting_list(conn: sqlite3.Connection, key: str, values: list[str]) -> None:
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(values)),
    )
    conn.commit()


def get_setting_json(conn: sqlite3.Connection, key: str, default: object = None) -> object:
    """For settings shaped as something other than a string or list of strings
    (e.g. skills.py's `{skill_name: hash}` map) — `get_setting`/`get_setting_list`
    cover the common cases above with a narrower, more convenient return type."""
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value_json"])


def set_setting_json(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )
    conn.commit()


# --- providers / models -------------------------------------------------------


@dataclass(frozen=True)
class ProviderRow:
    id: str
    kind: str
    display_name: str
    config: dict[str, object]
    enabled: bool


@dataclass(frozen=True)
class ModelRow:
    id: str
    provider_id: str
    model_id: str
    display_name: str
    supports_tools: bool
    supports_images: bool
    context_tokens: int


def upsert_provider(
    conn: sqlite3.Connection,
    provider_id: str,
    kind: str,
    display_name: str,
    config: dict[str, object],
    enabled: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO providers (id, kind, display_name, config_json, enabled) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "kind = excluded.kind, display_name = excluded.display_name, "
        "config_json = excluded.config_json, enabled = excluded.enabled",
        (provider_id, kind, display_name, json.dumps(config), int(enabled)),
    )
    conn.commit()


def get_provider(conn: sqlite3.Connection, provider_id: str) -> ProviderRow | None:
    row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
    return _provider_from_row(row) if row is not None else None


def list_providers(conn: sqlite3.Connection) -> list[ProviderRow]:
    rows = conn.execute("SELECT * FROM providers ORDER BY display_name").fetchall()
    return [_provider_from_row(row) for row in rows]


def delete_provider(conn: sqlite3.Connection, provider_id: str) -> None:
    """Deletes the provider and its models (ON DELETE CASCADE); any session
    referencing it just loses that selection rather than breaking (ON DELETE SET NULL)."""
    conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    conn.commit()


def _provider_from_row(row: sqlite3.Row) -> ProviderRow:
    return ProviderRow(
        id=row["id"],
        kind=row["kind"],
        display_name=row["display_name"],
        config=json.loads(row["config_json"]),
        enabled=bool(row["enabled"]),
    )


def replace_models(conn: sqlite3.Connection, provider_id: str, models: list[ModelRow]) -> None:
    with conn:
        conn.execute("DELETE FROM models WHERE provider_id = ?", (provider_id,))
        conn.executemany(
            "INSERT INTO models "
            "(id, provider_id, model_id, display_name, supports_tools, "
            "supports_images, context_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    m.id,
                    m.provider_id,
                    m.model_id,
                    m.display_name,
                    int(m.supports_tools),
                    int(m.supports_images),
                    m.context_tokens,
                )
                for m in models
            ],
        )


def list_models(conn: sqlite3.Connection, provider_id: str) -> list[ModelRow]:
    rows = conn.execute(
        "SELECT * FROM models WHERE provider_id = ? ORDER BY display_name", (provider_id,)
    ).fetchall()
    return [_model_from_row(row) for row in rows]


def list_all_models(conn: sqlite3.Connection) -> list[ModelRow]:
    rows = conn.execute("SELECT * FROM models ORDER BY display_name").fetchall()
    return [_model_from_row(row) for row in rows]


def get_model(conn: sqlite3.Connection, model_row_id: str) -> ModelRow | None:
    row = conn.execute("SELECT * FROM models WHERE id = ?", (model_row_id,)).fetchone()
    return _model_from_row(row) if row is not None else None


def _model_from_row(row: sqlite3.Row) -> ModelRow:
    return ModelRow(
        id=row["id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        display_name=row["display_name"],
        supports_tools=bool(row["supports_tools"]),
        supports_images=bool(row["supports_images"]),
        context_tokens=row["context_tokens"],
    )


# --- MCP servers (ARCHITECTURE.md §8.3) --------------------------------------


@dataclass(frozen=True)
class McpServerRow:
    id: str
    name: str
    enabled: bool
    config: dict[str, object]


def upsert_mcp_server(
    conn: sqlite3.Connection,
    server_id: str,
    name: str,
    config: dict[str, object],
    enabled: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO mcp_servers (id, name, enabled, config_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name = excluded.name, enabled = excluded.enabled, config_json = excluded.config_json",
        (server_id, name, int(enabled), json.dumps(config)),
    )
    conn.commit()


def get_mcp_server(conn: sqlite3.Connection, server_id: str) -> McpServerRow | None:
    row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
    return _mcp_server_from_row(row) if row is not None else None


def list_mcp_servers(conn: sqlite3.Connection) -> list[McpServerRow]:
    rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
    return [_mcp_server_from_row(row) for row in rows]


def delete_mcp_server(conn: sqlite3.Connection, server_id: str) -> None:
    conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
    conn.commit()


def _mcp_server_from_row(row: sqlite3.Row) -> McpServerRow:
    return McpServerRow(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        config=json.loads(row["config_json"]),
    )


# --- sessions / messages -------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    id: str
    title: str
    provider_id: str | None
    model_row_id: str | None
    privacy_mode: str
    created_at: str
    updated_at: str


def create_session(
    conn: sqlite3.Connection,
    session_id: str,
    title: str,
    provider_id: str | None,
    model_row_id: str | None,
    privacy_mode: str = "cloud",
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO sessions "
        "(id, title, provider_id, model_row_id, privacy_mode, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, title, provider_id, model_row_id, privacy_mode, now, now),
    )
    conn.commit()


def set_session_model(
    conn: sqlite3.Connection, session_id: str, provider_id: str | None, model_row_id: str | None
) -> None:
    """`model_row_id=None` for "this provider has no models configured yet" — an
    empty string would violate the FK to models(id), which must be NULL or a real row."""
    conn.execute(
        "UPDATE sessions SET provider_id = ?, model_row_id = ?, updated_at = ? WHERE id = ?",
        (provider_id, model_row_id or None, datetime.now(UTC).isoformat(), session_id),
    )
    conn.commit()


def set_session_title(conn: sqlite3.Connection, session_id: str, title: str) -> None:
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()


def list_sessions(conn: sqlite3.Connection) -> list[SessionRow]:
    rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
    return [_session_from_row(row) for row in rows]


@dataclass(frozen=True)
class SessionFolderRow:
    session_id: str
    path: str
    read_only: bool
    added_at: str


def add_session_folder(
    conn: sqlite3.Connection, session_id: str, path: str, read_only: bool = False
) -> None:
    conn.execute(
        "INSERT INTO session_folders (session_id, path, read_only, added_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session_id, path) DO UPDATE SET read_only = excluded.read_only",
        (session_id, path, int(read_only), datetime.now(UTC).isoformat()),
    )
    conn.commit()


def remove_session_folder(conn: sqlite3.Connection, session_id: str, path: str) -> None:
    conn.execute(
        "DELETE FROM session_folders WHERE session_id = ? AND path = ?", (session_id, path)
    )
    conn.commit()


def set_session_folder_read_only(
    conn: sqlite3.Connection, session_id: str, path: str, read_only: bool
) -> None:
    conn.execute(
        "UPDATE session_folders SET read_only = ? WHERE session_id = ? AND path = ?",
        (int(read_only), session_id, path),
    )
    conn.commit()


def list_session_folders(conn: sqlite3.Connection, session_id: str) -> list[SessionFolderRow]:
    rows = conn.execute(
        "SELECT * FROM session_folders WHERE session_id = ? ORDER BY added_at", (session_id,)
    ).fetchall()
    return [
        SessionFolderRow(
            session_id=row["session_id"],
            path=row["path"],
            read_only=bool(row["read_only"]),
            added_at=row["added_at"],
        )
        for row in rows
    ]


def _session_from_row(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id=row["id"],
        title=row["title"],
        provider_id=row["provider_id"],
        model_row_id=row["model_row_id"],
        privacy_mode=row["privacy_mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _part_to_dict(part: ContentPart) -> dict[str, object]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImagePart):
        return {
            "type": "image",
            "data": base64.b64encode(part.data).decode("ascii"),
            "media_type": part.media_type,
        }
    if isinstance(part, ToolCallPart):
        return {"type": "tool_call", "id": part.id, "name": part.name, "arguments": part.arguments}
    if isinstance(part, ToolResultPart):
        return {
            "type": "tool_result",
            "tool_call_id": part.tool_call_id,
            "content": [_part_to_dict(p) for p in part.content],
            "is_error": part.is_error,
        }
    raise TypeError(f"Unknown content part: {part!r}")


def _part_from_dict(data: dict[str, object]) -> ContentPart:
    kind = data["type"]
    if kind == "text":
        text = data["text"]
        assert isinstance(text, str)
        return TextPart(text)
    if kind == "image":
        raw = data["data"]
        media_type = data["media_type"]
        assert isinstance(raw, str) and isinstance(media_type, str)
        return ImagePart(base64.b64decode(raw), media_type)
    if kind == "tool_call":
        call_id, name, arguments = data["id"], data["name"], data["arguments"]
        assert isinstance(call_id, str) and isinstance(name, str) and isinstance(arguments, dict)
        return ToolCallPart(call_id, name, arguments)
    if kind == "tool_result":
        tool_call_id = data["tool_call_id"]
        content = data["content"]
        is_error = data.get("is_error", False)
        assert isinstance(tool_call_id, str) and isinstance(content, list)
        assert isinstance(is_error, bool)
        sub_parts = [_part_from_dict(c) for c in content]
        text_or_image_parts = [p for p in sub_parts if isinstance(p, TextPart | ImagePart)]
        return ToolResultPart(tool_call_id, text_or_image_parts, is_error)
    raise ValueError(f"Unknown content part type: {kind!r}")


def append_message(conn: sqlite3.Connection, session_id: str, message: Message) -> str:
    message_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    parts_json = json.dumps([_part_to_dict(p) for p in message.parts])
    with conn:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, parts_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, session_id, message.role.value, parts_json, now),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    return message_id


def list_messages(conn: sqlite3.Connection, session_id: str) -> list[Message]:
    rows = conn.execute(
        "SELECT role, parts_json FROM messages WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return [
        Message(
            role=Role(row["role"]),
            parts=[_part_from_dict(p) for p in json.loads(row["parts_json"])],
        )
        for row in rows
    ]


# --- usage -------------------------------------------------------------------


def record_usage(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    triggered_by: str,
    est_cost: float | None = None,
    currency: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO usage "
        "(ts, session_id, provider, model_id, input_tokens, output_tokens, "
        "triggered_by, est_cost, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(),
            session_id,
            provider,
            model_id,
            input_tokens,
            output_tokens,
            triggered_by,
            est_cost,
            currency,
        ),
    )
    conn.commit()


@dataclass(frozen=True)
class UsageRow:
    ts: str
    session_id: str | None
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int
    triggered_by: str
    est_cost: float | None
    currency: str | None


def list_usage(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> list[UsageRow]:
    rows = conn.execute(
        "SELECT * FROM usage WHERE ts >= ? AND ts <= ? ORDER BY ts", (start_iso, end_iso)
    ).fetchall()
    return [
        UsageRow(
            ts=row["ts"],
            session_id=row["session_id"],
            provider=row["provider"],
            model_id=row["model_id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            triggered_by=row["triggered_by"],
            est_cost=row["est_cost"],
            currency=row["currency"],
        )
        for row in rows
    ]


# --- OCR cache -----------------------------------------------------------


def get_ocr_cache(
    conn: sqlite3.Connection, file_hash: str, page: int, params_hash: str
) -> str | None:
    row = conn.execute(
        "SELECT result_json FROM ocr_cache WHERE file_hash = ? AND page = ? AND params_hash = ?",
        (file_hash, page, params_hash),
    ).fetchone()
    return row["result_json"] if row is not None else None


def set_ocr_cache(
    conn: sqlite3.Connection, file_hash: str, page: int, params_hash: str, result_json: str
) -> None:
    conn.execute(
        "INSERT INTO ocr_cache (file_hash, page, params_hash, result_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(file_hash, page, params_hash) "
        "DO UPDATE SET result_json = excluded.result_json",
        (file_hash, page, params_hash, result_json),
    )
    conn.commit()


# --- pricing (ARCHITECTURE.md §11; CLAUDE.md: never hardcode model prices) ---


@dataclass(frozen=True)
class PriceRow:
    provider: str
    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    unit: str = "USD"


def set_price(conn: sqlite3.Connection, price: PriceRow) -> None:
    conn.execute(
        "INSERT INTO pricing (provider, model_id, input_per_mtok, output_per_mtok, unit) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, model_id) DO UPDATE SET "
        "input_per_mtok = excluded.input_per_mtok, "
        "output_per_mtok = excluded.output_per_mtok, "
        "unit = excluded.unit",
        (price.provider, price.model_id, price.input_per_mtok, price.output_per_mtok, price.unit),
    )
    conn.commit()


def delete_price(conn: sqlite3.Connection, provider: str, model_id: str) -> None:
    conn.execute("DELETE FROM pricing WHERE provider = ? AND model_id = ?", (provider, model_id))
    conn.commit()


def get_price(conn: sqlite3.Connection, provider: str, model_id: str) -> PriceRow | None:
    row = conn.execute(
        "SELECT * FROM pricing WHERE provider = ? AND model_id = ?", (provider, model_id)
    ).fetchone()
    return _price_from_row(row) if row is not None else None


def list_prices(conn: sqlite3.Connection) -> list[PriceRow]:
    rows = conn.execute("SELECT * FROM pricing ORDER BY provider, model_id").fetchall()
    return [_price_from_row(row) for row in rows]


def _price_from_row(row: sqlite3.Row) -> PriceRow:
    return PriceRow(
        provider=row["provider"],
        model_id=row["model_id"],
        input_per_mtok=row["input_per_mtok"],
        output_per_mtok=row["output_per_mtok"],
        unit=row["unit"],
    )
