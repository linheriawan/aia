"""Tests for date parsing (ARCHITECTURE.md §10.3)."""

from __future__ import annotations

from datetime import date

import pytest

from deskai.extraction.dates import DateParseError, parse_date


def test_parse_iso_date() -> None:
    result = parse_date("2026-10-23")
    assert result.value == date(2026, 10, 23)
    assert result.ambiguous is False


def test_parse_long_indonesian_month_name() -> None:
    result = parse_date("23 Oktober 2026")
    assert result.value == date(2026, 10, 23)
    assert result.ambiguous is False


def test_parse_long_english_month_name() -> None:
    result = parse_date("23 October 2026")
    assert result.value == date(2026, 10, 23)


def test_parse_short_abbreviated_month_two_digit_year() -> None:
    result = parse_date("23-Okt-26")
    assert result.value == date(2026, 10, 23)
    assert result.ambiguous is False


def test_parse_short_english_abbreviation() -> None:
    result = parse_date("23-Oct-2026")
    assert result.value == date(2026, 10, 23)


def test_numeric_date_unambiguous_when_first_part_over_twelve() -> None:
    # 23 can't be a month, so this must be dd/mm/yyyy regardless of convention.
    result = parse_date("23/10/2026", day_first=False)
    assert result.value == date(2026, 10, 23)
    assert result.ambiguous is False


def test_numeric_date_unambiguous_when_second_part_over_twelve() -> None:
    result = parse_date("10/23/2026", day_first=True)
    assert result.value == date(2026, 10, 23)
    assert result.ambiguous is False


def test_numeric_date_both_parts_valid_as_day_or_month_is_ambiguous_day_first() -> None:
    result = parse_date("03/04/2026", day_first=True)
    assert result.value == date(2026, 4, 3)
    assert result.ambiguous is True


def test_numeric_date_both_parts_valid_as_day_or_month_is_ambiguous_month_first() -> None:
    result = parse_date("03/04/2026", day_first=False)
    assert result.value == date(2026, 3, 4)
    assert result.ambiguous is True


def test_numeric_date_two_digit_year_expansion() -> None:
    assert parse_date("15/01/26", day_first=True).value == date(2026, 1, 15)
    assert parse_date("15/01/99", day_first=True).value == date(1999, 1, 15)


@pytest.mark.parametrize("raw", ["", "   ", "not a date", "32/13/2026"])
def test_parse_date_rejects_garbage(raw: str) -> None:
    with pytest.raises(DateParseError):
        parse_date(raw)


def test_parse_date_unknown_month_name_raises() -> None:
    with pytest.raises(DateParseError):
        parse_date("23 Zonktober 2026")
