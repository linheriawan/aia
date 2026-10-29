"""Tests for building InvoiceFields from the extraction LLM's raw JSON reply."""

from __future__ import annotations

from deskai.extraction.invoice_schema import invoice_fields_from_dict


def test_full_round_trip() -> None:
    data: dict[str, object] = {
        "doc_type": "invoice",
        "vendor_name": "Acme Corp",
        "vendor_npwp": "01.234.567.8-901.000",
        "document_number": "INV-001",
        "invoice_date": "23/10/2026",
        "due_date": "23/11/2026",
        "currency": "IDR",
        "subtotal": "1.000.000",
        "discount": None,
        "tax": "110.000",
        "total": "1.110.000",
        "line_items": [
            {"description": "Widget", "qty": "2", "unit_price": "500.000", "amount": "1.000.000"}
        ],
    }
    fields = invoice_fields_from_dict(data)
    assert fields.vendor_name == "Acme Corp"
    assert fields.discount is None
    assert len(fields.line_items) == 1
    assert fields.line_items[0].description == "Widget"
    assert fields.line_items[0].qty == "2"


def test_missing_keys_default_to_none_and_empty_line_items() -> None:
    fields = invoice_fields_from_dict({})
    assert fields.vendor_name is None
    assert fields.line_items == []


def test_non_string_values_are_coerced_to_strings() -> None:
    data = {"subtotal": 1000000, "line_items": [{"qty": 2, "amount": 100}]}
    fields = invoice_fields_from_dict(data)
    assert fields.subtotal == "1000000"
    assert fields.line_items[0].qty == "2"
    assert fields.line_items[0].amount == "100"


def test_malformed_line_items_are_ignored_rather_than_crashing() -> None:
    fields = invoice_fields_from_dict({"line_items": ["not a dict", 42, None]})
    assert fields.line_items == []
