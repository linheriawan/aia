from pathlib import Path

import pytest

from deskai.storage import db, repositories
from deskai.usage import pricing, tracker


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "deskai.db")
    yield connection
    connection.close()


def test_estimate_cost_returns_none_without_a_price_row(conn) -> None:
    assert pricing.estimate_cost(conn, "bedrock", "unknown-model", 1000, 1000) is None


def test_estimate_cost_computes_from_per_mtok_prices(conn) -> None:
    repositories.set_price(
        conn, repositories.PriceRow("bedrock", "claude-x", input_per_mtok=3.0, output_per_mtok=15.0)
    )
    cost, unit = pricing.estimate_cost(conn, "bedrock", "claude-x", 1_000_000, 500_000)
    assert cost == pytest.approx(3.0 + 7.5)
    assert unit == "USD"


def test_load_sample_prices_returns_rows_without_touching_the_db() -> None:
    rows = pricing.load_sample_prices()
    assert rows
    assert all(isinstance(r, repositories.PriceRow) for r in rows)
    assert {r.provider for r in rows} >= {"bedrock", "cloudflare"}


def test_tracker_record_fills_est_cost_when_a_price_exists(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)
    repositories.set_price(
        conn, repositories.PriceRow("bedrock", "claude-x", input_per_mtok=3.0, output_per_mtok=15.0)
    )

    tracker.record(
        conn,
        session_id="s1",
        provider="bedrock",
        model_id="claude-x",
        input_tokens=1_000_000,
        output_tokens=0,
        triggered_by="chat",
    )

    row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["est_cost"] == pytest.approx(3.0)
    assert row["currency"] == "USD"


def test_tracker_record_leaves_est_cost_null_without_a_price(conn) -> None:
    repositories.create_session(conn, "s1", "New chat", None, None)

    tracker.record(
        conn,
        session_id="s1",
        provider="bedrock",
        model_id="unpriced-model",
        input_tokens=100,
        output_tokens=100,
        triggered_by="chat",
    )

    row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["est_cost"] is None
    assert row["currency"] is None


def test_price_round_trip(conn) -> None:
    repositories.set_price(
        conn, repositories.PriceRow("cloudflare", "@cf/meta/llama-3", 0.0, 0.0, unit="Neurons")
    )
    row = repositories.get_price(conn, "cloudflare", "@cf/meta/llama-3")
    assert row is not None
    assert row.unit == "Neurons"

    repositories.delete_price(conn, "cloudflare", "@cf/meta/llama-3")
    assert repositories.get_price(conn, "cloudflare", "@cf/meta/llama-3") is None


def test_list_prices_is_sorted(conn) -> None:
    repositories.set_price(conn, repositories.PriceRow("openai_compat", "z-model", 0.0, 0.0))
    repositories.set_price(conn, repositories.PriceRow("bedrock", "a-model", 1.0, 1.0))
    rows = repositories.list_prices(conn)
    assert [r.provider for r in rows] == ["bedrock", "openai_compat"]
