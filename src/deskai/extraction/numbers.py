"""Amount parsing for invoice/receipt fields (ARCHITECTURE.md §10.3).

Handles `1.250.000,00`, `1,250,000.00`, `Rp 1.250.000`, `1.250.000,-` and
`IDR 1,250,000`. The decimal separator is decided from the pattern of dots
and commas in the string; when that can't be done without guessing (e.g.
`1.250`, which is ambiguous between a thousands-grouped 1250 and a decimal
1.25-ish value), this raises `AmountParseError` instead of picking one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LEADING_NON_DIGIT_RE = re.compile(r"^[^\d]+")
_VALID_BODY_RE = re.compile(r"[0-9.,\s]+")


class AmountParseError(ValueError):
    """The amount's decimal separator is ambiguous, or it isn't a number at all."""


@dataclass(frozen=True)
class ParsedAmount:
    value: float
    currency_hint: str | None


def parse_amount(raw: str) -> ParsedAmount:
    text = raw.strip()
    if not text:
        raise AmountParseError("empty amount")

    currency_hint: str | None = None
    prefix_match = _LEADING_NON_DIGIT_RE.match(text)
    body = text
    if prefix_match:
        currency_hint = prefix_match.group().strip() or None
        body = text[prefix_match.end() :]

    body = body.strip()
    if body.endswith(",-"):
        body = body[:-2].strip()
    elif body.endswith("-"):
        body = body[:-1].strip()

    if not body or not _VALID_BODY_RE.fullmatch(body):
        raise AmountParseError(f"not a number: {raw!r}")

    body = body.replace(" ", "")
    dot_count = body.count(".")
    comma_count = body.count(",")

    if dot_count and comma_count:
        decimal_sep = "," if body.rfind(",") > body.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        without_thousands = body.replace(thousands_sep, "")
        integer_part, _, frac_part = without_thousands.rpartition(decimal_sep)
        value = float(f"{integer_part}.{frac_part}")
    elif dot_count > 1 or comma_count > 1:
        sep = "." if dot_count > 1 else ","
        groups = body.split(sep)
        if not groups[0] or any(len(g) != 3 for g in groups[1:]):
            raise AmountParseError(f"ambiguous amount: {raw!r}")
        value = float("".join(groups))
    elif dot_count == 1 or comma_count == 1:
        sep = "." if dot_count == 1 else ","
        integer_part, frac_part = body.split(sep)
        if not integer_part or not frac_part or len(frac_part) == 3 or len(frac_part) > 3:
            raise AmountParseError(f"ambiguous amount: {raw!r}")
        value = float(f"{integer_part}.{frac_part}")
    else:
        value = float(body)

    return ParsedAmount(value=value, currency_hint=currency_hint)
