"""Tests for the Usage window's pure helpers — range math and cost formatting —
split out so they don't need a Qt application to run."""

from __future__ import annotations

from datetime import UTC, date, datetime

from deskai.storage.repositories import UsageRow
from deskai.ui.usage_window import _format_cost_totals, _range_bounds


def _row(est_cost: float | None, currency: str | None) -> UsageRow:
    return UsageRow(
        ts="2026-01-01T00:00:00+00:00",
        session_id="s1",
        provider="bedrock",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        triggered_by="chat",
        est_cost=est_cost,
        currency=currency,
    )


def test_range_bounds_today_starts_at_midnight() -> None:
    start_iso, end_iso = _range_bounds("Today", date(2026, 1, 1), date(2026, 1, 1))
    start = datetime.fromisoformat(start_iso)
    assert start.hour == 0 and start.minute == 0
    assert start.date() == datetime.now(UTC).date()


def test_range_bounds_7_days_spans_a_week() -> None:
    start_iso, end_iso = _range_bounds("7 days", date(2026, 1, 1), date(2026, 1, 1))
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    assert (end - start).days == 7


def test_range_bounds_custom_uses_given_dates() -> None:
    start_iso, end_iso = _range_bounds("Custom", date(2026, 1, 1), date(2026, 1, 31))
    assert start_iso.startswith("2026-01-01")
    assert end_iso.startswith("2026-01-31")


def test_format_cost_totals_sums_same_unit() -> None:
    rows = [_row(1.0, "USD"), _row(2.5, "USD")]
    assert _format_cost_totals(rows) == "3.5000 USD"


def test_format_cost_totals_keeps_different_units_separate() -> None:
    rows = [_row(1.0, "USD"), _row(100.0, "Neurons")]
    result = _format_cost_totals(rows)
    assert "1.0000 USD" in result
    assert "100.0000 Neurons" in result


def test_format_cost_totals_flags_unpriced_rows() -> None:
    rows = [_row(1.0, "USD"), _row(None, None)]
    result = _format_cost_totals(rows)
    assert "1.0000 USD" in result
    assert "some models have no price on file" in result


def test_format_cost_totals_empty_rows() -> None:
    assert _format_cost_totals([]) == "no price on file for any usage in this range"
