"""Tests for invoice validation (ARCHITECTURE.md §10.3): arithmetic, dates,
regex patterns, duplicates and low-confidence flags all surface as review notes
rather than raising, since review happens in the spreadsheet output."""

from __future__ import annotations

from deskai.extraction.invoice_schema import InvoiceFields, LineItem
from deskai.extraction.validate import validate_invoice


def _fields(**overrides: object) -> InvoiceFields:
    base: dict[str, object] = {
        "vendor_name": "Acme Corp",
        "document_number": "INV-001",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-31",
        "subtotal": "1000000",
        "discount": None,
        "tax": "110000",
        "total": "1110000",
        "line_items": [],
    }
    base.update(overrides)
    return InvoiceFields(**base)  # type: ignore[arg-type]


def test_clean_invoice_is_ok() -> None:
    result = validate_invoice(_fields())
    assert result.status == "OK"
    assert result.notes == []


def test_missing_required_field_flags_needs_review() -> None:
    result = validate_invoice(_fields(vendor_name=None))
    assert result.status == "Needs review"
    assert any("vendor name" in n for n in result.notes)


def test_total_formula_mismatch_is_flagged() -> None:
    result = validate_invoice(_fields(total="999999"))
    assert result.status == "Needs review"
    assert any("doesn't match total" in n for n in result.notes)


def test_total_formula_within_tolerance_is_ok() -> None:
    result = validate_invoice(_fields(total="1110000.00"))
    assert result.status == "OK"


def test_line_amount_mismatch_is_flagged() -> None:
    line = LineItem(description="Widget", qty="2", unit_price="500000", amount="999999")
    result = validate_invoice(_fields(line_items=[line]))
    assert any("doesn't match amount" in n for n in result.notes)


def test_line_total_vs_subtotal_mismatch_is_flagged() -> None:
    line = LineItem(description="Widget", qty="2", unit_price="500000", amount="1000000")
    result = validate_invoice(_fields(subtotal="2000000", line_items=[line]))
    assert any("doesn't match subtotal" in n for n in result.notes)


def test_due_date_before_invoice_date_is_flagged() -> None:
    result = validate_invoice(_fields(invoice_date="2026-10-31", due_date="2026-10-01"))
    assert any("before invoice date" in n for n in result.notes)


def test_implausible_year_is_flagged() -> None:
    result = validate_invoice(_fields(invoice_date="1899-01-01"))
    assert any("outside a plausible range" in n for n in result.notes)


def test_unparseable_amount_is_flagged_not_raised() -> None:
    result = validate_invoice(_fields(total="garbage"))
    assert result.status == "Needs review"
    assert any("Could not parse" in n for n in result.notes)


def test_ambiguous_date_is_flagged() -> None:
    result = validate_invoice(_fields(invoice_date="03/04/2026", due_date=None))
    assert any("ambiguous" in n for n in result.notes)


def test_npwp_pattern_mismatch_is_flagged() -> None:
    result = validate_invoice(
        _fields(vendor_npwp="not-an-npwp"), npwp_pattern=r"\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}"
    )
    assert any("NPWP" in n for n in result.notes)


def test_npwp_pattern_match_passes() -> None:
    result = validate_invoice(
        _fields(vendor_npwp="01.234.567.8-901.000"),
        npwp_pattern=r"\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}",
    )
    assert not any("NPWP" in n for n in result.notes)


def test_document_number_pattern_mismatch_is_flagged() -> None:
    result = validate_invoice(_fields(document_number="XYZ"), document_number_pattern=r"INV-\d+")
    assert any("Document number" in n for n in result.notes)


def test_duplicate_in_batch_is_flagged_on_second_occurrence() -> None:
    seen: set[tuple[str, str]] = set()
    first = validate_invoice(_fields(), seen_in_batch=seen)
    second = validate_invoice(_fields(), seen_in_batch=seen)
    assert not any("Duplicate" in n for n in first.notes)
    assert any("Duplicate" in n for n in second.notes)


def test_low_confidence_fields_are_flagged() -> None:
    result = validate_invoice(_fields(), low_confidence_fields=frozenset({"document text"}))
    assert any("low-confidence" in n for n in result.notes)


def test_no_line_items_skips_line_checks_but_still_ok() -> None:
    result = validate_invoice(_fields(line_items=[]))
    assert result.status == "OK"
