"""Tests for amount parsing (ARCHITECTURE.md §10.3: "thorough tests required")."""

from __future__ import annotations

import pytest

from deskai.extraction.numbers import AmountParseError, parse_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.250.000,00", 1250000.00),
        ("1,250,000.00", 1250000.00),
        ("1.250.000", 1250000.0),
        ("IDR 1,250,000", 1250000.0),
        ("1.250.000,-", 1250000.0),
        ("100", 100.0),
        ("1,50", 1.50),
        ("1.5", 1.5),
        ("0,99", 0.99),
    ],
)
def test_parse_amount_unambiguous_formats(raw: str, expected: float) -> None:
    assert parse_amount(raw).value == expected


def test_parse_amount_extracts_currency_prefix() -> None:
    result = parse_amount("Rp 1.250.000")
    assert result.value == 1250000.0
    assert result.currency_hint == "Rp"


def test_parse_amount_extracts_idr_prefix() -> None:
    result = parse_amount("IDR 1,250,000")
    assert result.currency_hint == "IDR"


def test_parse_amount_no_prefix_has_no_currency_hint() -> None:
    assert parse_amount("1.250.000,00").currency_hint is None


@pytest.mark.parametrize("raw", ["1.250", "1,250"])
def test_parse_amount_single_separator_three_digits_is_ambiguous(raw: str) -> None:
    with pytest.raises(AmountParseError):
        parse_amount(raw)


@pytest.mark.parametrize("raw", ["", "   ", "not a number", "abc.def"])
def test_parse_amount_rejects_garbage(raw: str) -> None:
    with pytest.raises(AmountParseError):
        parse_amount(raw)


def test_parse_amount_malformed_grouping_is_ambiguous() -> None:
    with pytest.raises(AmountParseError):
        parse_amount("1.25.000")  # second group isn't 3 digits
