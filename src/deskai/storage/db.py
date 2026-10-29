"""SQLite connection and schema migrations.

Migrations are applied in order, tracked via `PRAGMA user_version`. Each phase
of the build adds its own tables here as it needs them (see ARCHITECTURE.md
§12) rather than creating the full schema up front.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

Migration = Callable[[sqlite3.Connection], None]

_MIGRATIONS: list[Migration] = []


def _migration(fn: Migration) -> Migration:
    _MIGRATIONS.append(fn)
    return fn


@_migration
def _001_settings(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")


@_migration
def _002_chat(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE providers (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            display_name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE models (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            supports_tools INTEGER NOT NULL DEFAULT 0,
            supports_images INTEGER NOT NULL DEFAULT 0,
            context_tokens INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            provider_id TEXT REFERENCES providers(id) ON DELETE SET NULL,
            model_row_id TEXT REFERENCES models(id) ON DELETE SET NULL,
            privacy_mode TEXT NOT NULL DEFAULT 'cloud',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            parts_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT REFERENCES sessions(id),
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            triggered_by TEXT NOT NULL,
            est_cost REAL,
            currency TEXT
        )
        """
    )


@_migration
def _003_session_folders(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE session_folders (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            read_only INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL,
            PRIMARY KEY (session_id, path)
        )
        """
    )


@_migration
def _004_ocr_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ocr_cache (
            file_hash TEXT NOT NULL,
            page INTEGER NOT NULL,
            params_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (file_hash, page, params_hash)
        )
        """
    )


@_migration
def _005_pricing(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE pricing (
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            input_per_mtok REAL NOT NULL,
            output_per_mtok REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT 'USD',
            PRIMARY KEY (provider, model_id)
        )
        """
    )


@_migration
def _006_migrate_cloudflare_to_generic_custom_provider(conn: sqlite3.Connection) -> None:
    """The bespoke "cloudflare" provider kind was replaced by the generic

    "openai_compat" one, configured with a plain base URL instead of an
    account ID (any provider that speaks the same API can now be added the
    same way). Any row saved under the old kind is converted in place —
    same id, same display name — so an already-configured Cloudflare
    provider keeps working without the user re-entering anything.
    """
    from deskai.storage import (
        secrets,
    )  # local import: avoid a storage/db -> keyring hard dependency

    rows = conn.execute(
        "SELECT id, config_json FROM providers WHERE kind = 'cloudflare'"
    ).fetchall()
    for provider_id, config_json in rows:
        old_config = json.loads(config_json)
        account_id = old_config.get("account_id", "")
        new_config: dict[str, object] = {
            "base_url": f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
            "is_local": False,
        }
        gateway_id = old_config.get("gateway_id")
        if gateway_id:
            new_config["extra_headers"] = {"cf-aig-gateway-id": gateway_id}
        conn.execute(
            "UPDATE providers SET kind = 'openai_compat', config_json = ? WHERE id = ?",
            (json.dumps(new_config), provider_id),
        )

        old_token = secrets.get_secret(f"provider:{provider_id}:api_token")
        if old_token:
            secrets.set_secret(f"provider:{provider_id}:api_key", old_token)
            secrets.delete_secret(f"provider:{provider_id}:api_token")


@_migration
def _007_mcp_servers(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL
        )
        """
    )


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the app database at `db_path`, creating parent dirs and applying migrations."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, apply in enumerate(_MIGRATIONS, start=1):
        if version <= current:
            continue
        with conn:
            apply(conn)
            conn.execute(f"PRAGMA user_version = {version}")
