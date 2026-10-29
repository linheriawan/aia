"""Invoice/receipt field schema (ARCHITECTURE.md §10.3).

Every value here is a raw string exactly as it appeared in the document — the
extraction LLM call is instructed to copy, never convert — so `numbers.py`
and `dates.py` are the only things that ever turn them into numbers or dates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FIELD_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": ["string", "null"], "description": "'invoice' or 'receipt'"},
        "vendor_name": {"type": ["string", "null"]},
        "vendor_npwp": {"type": ["string", "null"]},
        "document_number": {"type": ["string", "null"], "description": "invoice/receipt number"},
        "invoice_date": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "subtotal": {"type": ["string", "null"]},
        "discount": {"type": ["string", "null"]},
        "tax": {"type": ["string", "null"]},
        "total": {"type": ["string", "null"]},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": ["string", "null"]},
                    "qty": {"type": ["string", "null"]},
                    "unit_price": {"type": ["string", "null"]},
                    "amount": {"type": ["string", "null"]},
                },
            },
        },
    },
    "required": [
        "doc_type",
        "vendor_name",
        "vendor_npwp",
        "document_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "discount",
        "tax",
        "total",
        "line_items",
    ],
}


@dataclass(frozen=True)
class LineItem:
    description: str | None = None
    qty: str | None = None
    unit_price: str | None = None
    amount: str | None = None


@dataclass(frozen=True)
class InvoiceFields:
    doc_type: str | None = None
    vendor_name: str | None = None
    vendor_npwp: str | None = None
    document_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    subtotal: str | None = None
    discount: str | None = None
    tax: str | None = None
    total: str | None = None
    line_items: list[LineItem] = field(default_factory=list)


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def invoice_fields_from_dict(data: dict[str, object]) -> InvoiceFields:
    """Builds `InvoiceFields` from the extraction LLM's raw (and not fully
    trusted) JSON reply, coercing every value to `str | None` defensively —
    a model that returns a number instead of a string shouldn't crash this."""
    line_items: list[LineItem] = []
    raw_items = data.get("line_items")
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                line_items.append(
                    LineItem(
                        description=_as_str(raw_item.get("description")),
                        qty=_as_str(raw_item.get("qty")),
                        unit_price=_as_str(raw_item.get("unit_price")),
                        amount=_as_str(raw_item.get("amount")),
                    )
                )

    return InvoiceFields(
        doc_type=_as_str(data.get("doc_type")),
        vendor_name=_as_str(data.get("vendor_name")),
        vendor_npwp=_as_str(data.get("vendor_npwp")),
        document_number=_as_str(data.get("document_number")),
        invoice_date=_as_str(data.get("invoice_date")),
        due_date=_as_str(data.get("due_date")),
        currency=_as_str(data.get("currency")),
        subtotal=_as_str(data.get("subtotal")),
        discount=_as_str(data.get("discount")),
        tax=_as_str(data.get("tax")),
        total=_as_str(data.get("total")),
        line_items=line_items,
    )
