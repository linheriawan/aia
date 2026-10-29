"""Date parsing for invoice/receipt fields (ARCHITECTURE.md §10.3).

Handles `dd/mm/yyyy` (the default for Indonesian documents, configurable via
`day_first`), `yyyy-mm-dd`, `23 Oktober 2026`, `23-Okt-26`, and English month
names. A fully-numeric date with both parts <= 12 (e.g. `03/04/2026`) is
genuinely ambiguous between dd/mm and mm/dd with no hint either way, so it's
resolved by `day_first` but flagged via `ParsedDate.ambiguous` rather than
silently trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# English and Indonesian month names and their common abbreviations.
_MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "agu": 8,
    "oktober": 10,
    "okt": 10,
    "desember": 12,
    "des": 12,
}

_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_LONG_NAME_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})")
_SHORT_NAME_RE = re.compile(r"(\d{1,2})[-/\s]([A-Za-z]+)\.?[-/\s](\d{2,4})")
_NUMERIC_RE = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})")


class DateParseError(ValueError):
    """The string isn't a recognized date format, or has an invalid day/month/year."""


@dataclass(frozen=True)
class ParsedDate:
    value: date
    ambiguous: bool


def parse_date(raw: str, *, day_first: bool = True) -> ParsedDate:
    text = raw.strip()
    if not text:
        raise DateParseError("empty date")

    iso_match = _ISO_RE.fullmatch(text)
    if iso_match:
        year, month, day = (int(g) for g in iso_match.groups())
        return ParsedDate(_build_date(year, month, day, raw), ambiguous=False)

    long_match = _LONG_NAME_RE.fullmatch(text)
    if long_match:
        day_str, month_str, year_str = long_match.groups()
        month = _month_from_name(month_str, raw)
        return ParsedDate(_build_date(int(year_str), month, int(day_str), raw), ambiguous=False)

    short_match = _SHORT_NAME_RE.fullmatch(text)
    if short_match:
        day_str, month_str, year_str = short_match.groups()
        month = _month_from_name(month_str, raw)
        year = _expand_two_digit_year(int(year_str))
        return ParsedDate(_build_date(year, month, int(day_str), raw), ambiguous=False)

    numeric_match = _NUMERIC_RE.fullmatch(text)
    if numeric_match:
        first_str, second_str, year_str = numeric_match.groups()
        first, second = int(first_str), int(second_str)
        year = _expand_two_digit_year(int(year_str))
        ambiguous = False
        if first > 12 and second <= 12:
            day, month = first, second
        elif second > 12 and first <= 12:
            day, month = second, first
        elif first <= 12 and second <= 12:
            day, month = (first, second) if day_first else (second, first)
            ambiguous = True
        else:
            raise DateParseError(f"not a valid date: {raw!r}")
        return ParsedDate(_build_date(year, month, day, raw), ambiguous=ambiguous)

    raise DateParseError(f"unrecognized date format: {raw!r}")


def _month_from_name(name: str, raw: str) -> int:
    month = _MONTH_NAMES.get(name.lower())
    if month is None:
        raise DateParseError(f"unknown month {name!r} in {raw!r}")
    return month


def _expand_two_digit_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year <= 69 else 1900 + year


def _build_date(year: int, month: int, day: int, raw: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateParseError(f"not a valid date: {raw!r} ({exc})") from exc
