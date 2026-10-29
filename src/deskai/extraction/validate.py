"""Pure-Python validation for extracted invoice fields (ARCHITECTURE.md §10.3).

Every check here is deterministic arithmetic, regex, or date comparison —
never an LLM call — so the same extracted fields always produce the same
verdict. A failing or inapplicable check never raises; it's recorded as a
`CheckResult` and surfaces as a `Needs review` status with a human-readable
note, since review happens in the output spreadsheet, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from deskai.extraction.dates import DateParseError, parse_date
from deskai.extraction.invoice_schema import InvoiceFields
from deskai.extraction.numbers import AmountParseError, parse_amount

DEFAULT_AMOUNT_TOLERANCE = 0.01
_MIN_PLAUSIBLE_YEAR = 2000
_MAX_PLAUSIBLE_YEAR = 2100


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    note: str = ""


@dataclass(frozen=True)
class ValidationResult:
    status: str  # "OK" | "Needs review"
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def notes(self) -> list[str]:
        return [c.note for c in self.checks if not c.ok and c.note]


def validate_invoice(
    fields: InvoiceFields,
    *,
    npwp_pattern: str | None = None,
    document_number_pattern: str | None = None,
    day_first: bool = True,
    seen_in_batch: set[tuple[str, str]] | None = None,
    low_confidence_fields: frozenset[str] = frozenset(),
    tolerance: float = DEFAULT_AMOUNT_TOLERANCE,
) -> ValidationResult:
    checks: list[CheckResult] = []

    for field_name, label in (
        ("vendor_name", "vendor name"),
        ("document_number", "invoice/receipt number"),
        ("total", "total"),
    ):
        present = bool(getattr(fields, field_name))
        checks.append(
            CheckResult(
                f"required:{field_name}",
                present,
                "" if present else f"Missing required field: {label}",
            )
        )

    subtotal = _try_amount(fields.subtotal, "subtotal", checks)
    discount_parsed = _try_amount(fields.discount, "discount", checks) if fields.discount else 0.0
    discount = discount_parsed if discount_parsed is not None else 0.0
    tax = _try_amount(fields.tax, "tax", checks)
    total = _try_amount(fields.total, "total", checks)

    line_total = 0.0
    all_line_amounts_ok = True
    for index, item in enumerate(fields.line_items, start=1):
        qty = _try_amount(item.qty, f"line {index} qty", checks) if item.qty else None
        unit_price = (
            _try_amount(item.unit_price, f"line {index} unit price", checks)
            if item.unit_price
            else None
        )
        amount = _try_amount(item.amount, f"line {index} amount", checks) if item.amount else None
        if qty is not None and unit_price is not None and amount is not None:
            ok = abs(qty * unit_price - amount) <= tolerance
            all_line_amounts_ok = all_line_amounts_ok and ok
            checks.append(
                CheckResult(
                    f"line_amount:{index}",
                    ok,
                    ""
                    if ok
                    else (
                        f"Line {index}: qty x unit price ({qty * unit_price:.2f}) "
                        f"doesn't match amount ({amount:.2f})"
                    ),
                )
            )
        if amount is not None:
            line_total += amount

    if fields.line_items and subtotal is not None and all_line_amounts_ok:
        ok = abs(line_total - subtotal) <= tolerance
        checks.append(
            CheckResult(
                "line_total_matches_subtotal",
                ok,
                ""
                if ok
                else (
                    f"Sum of line amounts ({line_total:.2f}) doesn't match subtotal "
                    f"({subtotal:.2f})"
                ),
            )
        )

    if subtotal is not None and tax is not None and total is not None:
        expected_total = subtotal - discount + tax
        ok = abs(expected_total - total) <= tolerance
        checks.append(
            CheckResult(
                "total_formula",
                ok,
                ""
                if ok
                else (
                    f"Subtotal - discount + tax ({expected_total:.2f}) doesn't match "
                    f"total ({total:.2f})"
                ),
            )
        )

    invoice_date = _try_date(fields.invoice_date, "invoice date", checks, day_first)
    due_date = _try_date(fields.due_date, "due date", checks, day_first)
    if invoice_date is not None and due_date is not None:
        ok = due_date >= invoice_date
        checks.append(
            CheckResult(
                "due_date_after_invoice_date", ok, "" if ok else "Due date is before invoice date"
            )
        )
    for label, value in (("invoice date", invoice_date), ("due date", due_date)):
        if value is not None:
            ok = _MIN_PLAUSIBLE_YEAR <= value.year <= _MAX_PLAUSIBLE_YEAR
            checks.append(
                CheckResult(
                    f"plausible_range:{label}",
                    ok,
                    ""
                    if ok
                    else f"{label.capitalize()} {value.isoformat()} is outside a plausible range",
                )
            )

    if npwp_pattern and fields.vendor_npwp:
        ok = bool(re.fullmatch(npwp_pattern, fields.vendor_npwp))
        checks.append(
            CheckResult(
                "npwp_pattern",
                ok,
                ""
                if ok
                else f"Vendor NPWP {fields.vendor_npwp!r} doesn't match the expected pattern",
            )
        )

    if document_number_pattern and fields.document_number:
        ok = bool(re.fullmatch(document_number_pattern, fields.document_number))
        checks.append(
            CheckResult(
                "document_number_pattern",
                ok,
                ""
                if ok
                else (
                    f"Document number {fields.document_number!r} doesn't match the expected pattern"
                ),
            )
        )

    if seen_in_batch is not None and fields.vendor_name and fields.document_number:
        key = (fields.vendor_name.strip().lower(), fields.document_number.strip().lower())
        is_duplicate = key in seen_in_batch
        seen_in_batch.add(key)
        if is_duplicate:
            checks.append(
                CheckResult(
                    "duplicate",
                    False,
                    f"Duplicate: {fields.vendor_name} / {fields.document_number} "
                    "seen earlier in this batch",
                )
            )

    for low_conf_field in sorted(low_confidence_fields):
        checks.append(
            CheckResult(
                f"low_confidence:{low_conf_field}",
                False,
                f"{low_conf_field} came from low-confidence OCR text",
            )
        )

    status = "OK" if all(c.ok for c in checks) else "Needs review"
    return ValidationResult(status=status, checks=checks)


def _try_amount(raw: str | None, label: str, checks: list[CheckResult]) -> float | None:
    if not raw:
        return None
    try:
        return parse_amount(raw).value
    except AmountParseError as exc:
        checks.append(
            CheckResult(f"parse:{label}", False, f"Could not parse {label} {raw!r}: {exc}")
        )
        return None


def _try_date(
    raw: str | None, label: str, checks: list[CheckResult], day_first: bool
) -> date | None:
    if not raw:
        return None
    try:
        parsed = parse_date(raw, day_first=day_first)
    except DateParseError as exc:
        checks.append(
            CheckResult(f"parse:{label}", False, f"Could not parse {label} {raw!r}: {exc}")
        )
        return None
    if parsed.ambiguous:
        checks.append(
            CheckResult(
                f"ambiguous_date:{label}",
                False,
                f"{label.capitalize()} {raw!r} is ambiguous (day/month order unclear)",
            )
        )
    return parsed.value
