"""Records a usage row, estimating cost from the pricing table (ARCHITECTURE.md §11)."""

from __future__ import annotations

import sqlite3

from deskai.storage import repositories
from deskai.usage import pricing


def record(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    triggered_by: str,
) -> None:
    estimate = pricing.estimate_cost(conn, provider, model_id, input_tokens, output_tokens)
    est_cost, currency = estimate if estimate is not None else (None, None)
    repositories.record_usage(
        conn,
        session_id=session_id,
        provider=provider,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        triggered_by=triggered_by,
        est_cost=est_cost,
        currency=currency,
    )
