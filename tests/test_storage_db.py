import json
from pathlib import Path

import pytest

from deskai.storage import db, repositories, secrets


def test_connect_creates_db_file_and_settings_table(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "deskai.db"
    db.connect(path).close()
    conn = db.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(db._MIGRATIONS)
    finally:
        conn.close()


def test_settings_persist_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "deskai.db"

    conn = db.connect(path)
    repositories.set_setting(conn, "theme", "Dark")
    conn.close()

    conn = db.connect(path)
    try:
        assert repositories.get_setting(conn, "theme") == "Dark"
    finally:
        conn.close()


def test_get_setting_returns_default_when_missing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        assert repositories.get_setting(conn, "missing", "fallback") == "fallback"
        assert repositories.get_setting(conn, "missing") is None
    finally:
        conn.close()


def test_migrate_cloudflare_to_generic_custom_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider row saved under the old bespoke "cloudflare" kind (account_id +

    gateway_id) must come out the other side as a generic "openai_compat" row
    with the equivalent base_url + extra_headers, and its secret renamed from
    api_token to api_key — so an already-configured Cloudflare provider keeps
    working after the switch to the generic custom-provider system.
    """
    fake_secrets: dict[str, str] = {"provider:cf1:api_token": "old-token-value"}
    monkeypatch.setattr(secrets, "get_secret", lambda key: fake_secrets.get(key))
    monkeypatch.setattr(
        secrets, "set_secret", lambda key, value: fake_secrets.__setitem__(key, value)
    )
    monkeypatch.setattr(secrets, "delete_secret", lambda key: fake_secrets.pop(key, None))

    conn = db.connect(tmp_path / "deskai.db")
    try:
        conn.execute(
            "INSERT INTO providers (id, kind, display_name, config_json, enabled) "
            "VALUES ('cf1', 'cloudflare', 'Cloudflare Workers AI', ?, 1)",
            (json.dumps({"account_id": "acct123", "gateway_id": "gw1"}),),
        )
        conn.commit()

        db._006_migrate_cloudflare_to_generic_custom_provider(conn)

        row = repositories.get_provider(conn, "cf1")
        assert row is not None
        assert row.kind == "openai_compat"
        assert (
            row.config["base_url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/v1"
        )
        assert row.config["extra_headers"] == {"cf-aig-gateway-id": "gw1"}

        assert "provider:cf1:api_token" not in fake_secrets
        assert fake_secrets["provider:cf1:api_key"] == "old-token-value"
    finally:
        conn.close()


def test_migrate_cloudflare_without_gateway_id_omits_extra_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "get_secret", lambda key: None)
    monkeypatch.setattr(secrets, "set_secret", lambda key, value: None)
    monkeypatch.setattr(secrets, "delete_secret", lambda key: None)

    conn = db.connect(tmp_path / "deskai.db")
    try:
        conn.execute(
            "INSERT INTO providers (id, kind, display_name, config_json, enabled) "
            "VALUES ('cf1', 'cloudflare', 'Cloudflare Workers AI', ?, 1)",
            (json.dumps({"account_id": "acct123", "gateway_id": None}),),
        )
        conn.commit()

        db._006_migrate_cloudflare_to_generic_custom_provider(conn)

        row = repositories.get_provider(conn, "cf1")
        assert row is not None
        assert "extra_headers" not in row.config
    finally:
        conn.close()
