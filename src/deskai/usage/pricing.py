"""Editable price table and cost estimation (ARCHITECTURE.md §11).

CLAUDE.md hard rule: never hardcode model prices or model IDs in code. Real
prices live in the `pricing` DB table (Settings → Pricing, see
`storage/repositories.py`); this module only ships a sample data file the
user can load as a starting point — it is never applied automatically.
"""

from __future__ import annotations

import json
import sqlite3
from importlib import resources

from deskai.storage import repositories


def estimate_cost(
    conn: sqlite3.Connection, provider: str, model_id: str, input_tokens: int, output_tokens: int
) -> tuple[float, str] | None:
    """`(estimated_cost, unit)`, or `None` if no price is on file for this model."""
    price = repositories.get_price(conn, provider, model_id)
    if price is None:
        return None
    cost = (input_tokens / 1_000_000) * price.input_per_mtok
    cost += (output_tokens / 1_000_000) * price.output_per_mtok
    return cost, price.unit


def load_sample_prices() -> list[repositories.PriceRow]:
    """Sample starting prices from the packaged data file — never auto-applied."""
    text = resources.files("deskai.usage").joinpath("sample_pricing.json").read_text()
    rows = json.loads(text)
    return [
        repositories.PriceRow(
            provider=str(row["provider"]),
            model_id=str(row["model_id"]),
            input_per_mtok=float(row["input_per_mtok"]),
            output_per_mtok=float(row["output_per_mtok"]),
            unit=str(row.get("unit", "USD")),
        )
        for row in rows
    ]
