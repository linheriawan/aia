"""Tests for the extract_invoice tool (ARCHITECTURE.md §10.3): the pipeline
from a file on disk to a validated, compact JSON result, with a fake
provider standing in for the isolated LLM extraction call."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from deskai.core.events import Done, Error, StreamEvent, TextDelta, Usage
from deskai.core.messages import Message
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo
from deskai.storage import db, repositories
from deskai.tools.base import ToolContext
from deskai.tools.invoice_tools import ExtractInvoiceTool
from deskai.tools.sandbox import Sandbox, WorkspaceFolder

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = ModelInfo(
    id="m", display_name="M", supports_tools=False, supports_images=False, context_tokens=10
)

_CLEAN_FIELDS: dict[str, Any] = {
    "doc_type": "invoice",
    "vendor_name": "Acme Corp",
    "vendor_npwp": None,
    "document_number": "INV-001",
    "invoice_date": "2026-10-01",
    "due_date": "2026-10-31",
    "currency": "IDR",
    "subtotal": "1000000",
    "discount": None,
    "tax": "110000",
    "total": "1110000",
    "line_items": [
        {"description": "Widget", "qty": "2", "unit_price": "500000", "amount": "1000000"}
    ],
}


class FakeExtractionProvider:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.name = "fake"
        self._events = events
        self.calls = 0

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def test_connection(self) -> None:
        pass

    async def stream_chat(
        self, model: ModelInfo, system: str, messages: list[Message], tools: list[Any]
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        for event in self._events:
            yield event


def _json_events(data: dict[str, Any], *, usage: Usage | None = None) -> list[StreamEvent]:
    events: list[StreamEvent] = [TextDelta(json.dumps(data))]
    if usage is not None:
        events.append(usage)
    events.append(Done("end_turn"))
    return events


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("sample.pdf", "scanned.pdf"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    connection = db.connect(tmp_path / "deskai.db")
    repositories.create_session(connection, "s1", "New chat", None, None)
    yield connection
    connection.close()


def _ctx(
    workspace: Path,
    conn: Any,
    provider: Any,
    *,
    extraction_batch: set[tuple[str, str]] | None = None,
) -> ToolContext:
    sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=[])
    kwargs: dict[str, Any] = {}
    if extraction_batch is not None:
        kwargs["extraction_batch"] = extraction_batch
    return ToolContext(
        session_id="s1",
        workspace=sandbox,
        ocr=OcrEngine(conn),
        conn=conn,
        provider=provider,
        model=MODEL,
        **kwargs,
    )


def test_extract_invoice_from_text_layer_pdf(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS))
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))

    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["ocr_used"] is False
    assert payload["status"] == "OK"
    assert payload["fields"]["vendor_name"] == "Acme Corp"
    assert "line_items" not in payload["fields"]


def test_extract_invoice_include_lines_keeps_line_items(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS))
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "sample.pdf", "include_lines": True}, ctx))

    payload = json.loads(result.content[0].text)
    assert payload["fields"]["line_items"][0]["description"] == "Widget"


def test_extract_invoice_flags_mismatched_total_as_needs_review(workspace: Path, conn: Any) -> None:
    bad_fields = {**_CLEAN_FIELDS, "total": "999999"}
    provider = FakeExtractionProvider(_json_events(bad_fields))
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))

    payload = json.loads(result.content[0].text)
    assert payload["status"] == "Needs review"
    assert any("doesn't match total" in n for n in payload["notes"])


def test_extract_invoice_missing_file_is_an_error(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS))
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "nope.pdf"}, ctx))
    assert result.is_error


def test_extract_invoice_without_provider_is_an_error(workspace: Path, conn: Any) -> None:
    sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=[])
    ctx = ToolContext(session_id="s1", workspace=sandbox, ocr=OcrEngine(conn), conn=conn)

    result = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))
    assert result.is_error


def test_extract_invoice_propagates_extraction_failure(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider([Error("boom"), Done("end_turn")])
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))
    assert result.is_error
    assert "boom" in result.content[0].text


def test_extract_invoice_records_usage_with_extract_trigger(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS, usage=Usage(10, 20)))
    ctx = _ctx(workspace, conn, provider)

    _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))

    row = conn.execute("SELECT * FROM usage WHERE triggered_by = 'extract'").fetchone()
    assert row is not None
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20


def test_extract_invoice_flags_duplicate_across_calls_in_same_batch(
    workspace: Path, conn: Any
) -> None:
    batch: set[tuple[str, str]] = set()
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS))
    ctx = _ctx(workspace, conn, provider, extraction_batch=batch)

    first = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))
    second = _run(ExtractInvoiceTool().run({"path": "sample.pdf"}, ctx))

    first_payload = json.loads(first.content[0].text)
    second_payload = json.loads(second.content[0].text)
    assert not any("Duplicate" in n for n in first_payload["notes"])
    assert any("Duplicate" in n for n in second_payload["notes"])


def test_extract_invoice_on_scanned_pdf_sets_ocr_used(workspace: Path, conn: Any) -> None:
    provider = FakeExtractionProvider(_json_events(_CLEAN_FIELDS))
    ctx = _ctx(workspace, conn, provider)

    result = _run(ExtractInvoiceTool().run({"path": "scanned.pdf"}, ctx))

    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["ocr_used"] is True
