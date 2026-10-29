"""extract_invoice tool (ARCHITECTURE.md §10.3): a deterministic pipeline
(get text -> group into rows -> one isolated LLM call -> parse -> validate)
exposed as a single tool call, so a skill processing a folder of invoices
never has to dump a whole document into the main chat history (ARCHITECTURE.md
§10.2 step 2) — only this compact JSON result enters it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pymupdf

from deskai.core.events import Usage
from deskai.extraction.invoice_schema import FIELD_SCHEMA, invoice_fields_from_dict
from deskai.extraction.layout import PositionedLine, group_into_rows
from deskai.extraction.llm_extract import ExtractionError, extract_fields
from deskai.extraction.validate import validate_invoice
from deskai.ocr.engine import LOW_CONFIDENCE_THRESHOLD, OcrLine, hash_bytes
from deskai.storage import repositories
from deskai.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from deskai.usage import tracker

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_SCANNED_TEXT_THRESHOLD = 20
_RENDER_DPI = 250

_NPWP_PATTERN_SETTING = "extraction.npwp_pattern"
_DOCUMENT_NUMBER_PATTERN_SETTING = "extraction.document_number_pattern"
_DAY_FIRST_SETTING = "extraction.day_first_dates"


def _extract_pdf_words(path: Path) -> tuple[list[PositionedLine], list[tuple[int, bytes]]]:
    """Sync pymupdf pass: positioned words for text-layer pages, plus
    (page_number, image_bytes) for pages that look scanned, to OCR separately
    (same heuristic as `tools/pdf_tools.py`)."""
    words: list[PositionedLine] = []
    scanned_pages: list[tuple[int, bytes]] = []
    matrix = pymupdf.Matrix(_RENDER_DPI / 72, _RENDER_DPI / 72)
    with pymupdf.open(str(path)) as document:
        for page in document:
            text = page.get_text().strip()
            non_whitespace = len(text.replace(" ", "").replace("\n", ""))
            if non_whitespace < _SCANNED_TEXT_THRESHOLD and page.get_images():
                scanned_pages.append((page.number, page.get_pixmap(matrix=matrix).tobytes("png")))
                continue
            for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
                words.append(PositionedLine(word, (x0, y0, x1, y1)))
    return words, scanned_pages


def _lines_from_ocr(ocr_lines: list[OcrLine]) -> list[PositionedLine]:
    return [
        PositionedLine(line.text, line.box, line.confidence)
        for line in ocr_lines
        if line.box is not None
    ]


class ExtractInvoiceTool(Tool):
    name = "extract_invoice"
    description = (
        "Extract invoice/receipt fields (vendor, dates, amounts, line items) from one "
        "PDF or image file as structured JSON, with arithmetic and date validation "
        "already applied. Call this once per document instead of reading it into the "
        "chat — the result is a small JSON summary, not the document text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "include_lines": {
                "type": "boolean",
                "description": "Include parsed line items in the result (default false).",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.provider is None or ctx.model is None:
            return ToolResult.text("No provider/model available for extraction", is_error=True)

        resolved = ctx.workspace.resolve(str(args.get("path", "")))
        if not resolved.exists():
            return ToolResult.text(f"Not found: {resolved}", is_error=True)

        try:
            positioned_lines, ocr_used, min_confidence = await self._gather_lines(resolved, ctx)
        except Exception as exc:  # the file may be corrupt, encrypted, or unsupported
            return ToolResult.text(f"Could not read {resolved}: {exc}", is_error=True)

        rows = group_into_rows(positioned_lines)

        try:
            fields_dict = await extract_fields(
                ctx.provider,
                ctx.model,
                rows,
                FIELD_SCHEMA,
                on_usage=lambda usage: self._record_usage(ctx, usage),
            )
        except ExtractionError as exc:
            return ToolResult.text(f"Extraction failed for {resolved}: {exc}", is_error=True)

        fields = invoice_fields_from_dict(fields_dict)

        # Which header field came from which OCR line isn't tracked once the
        # LLM has copied values out, so a low confidence anywhere in the
        # document's OCR text flags the whole document rather than guessing.
        low_confidence_fields: frozenset[str] = (
            frozenset({"document text"})
            if min_confidence is not None and min_confidence < LOW_CONFIDENCE_THRESHOLD
            else frozenset()
        )

        conn = ctx.conn
        npwp_pattern = repositories.get_setting(conn, _NPWP_PATTERN_SETTING, "") if conn else ""
        document_number_pattern = (
            repositories.get_setting(conn, _DOCUMENT_NUMBER_PATTERN_SETTING, "") if conn else ""
        )
        day_first_setting = (
            repositories.get_setting(conn, _DAY_FIRST_SETTING, "true") if conn else "true"
        )

        result = validate_invoice(
            fields,
            npwp_pattern=npwp_pattern or None,
            document_number_pattern=document_number_pattern or None,
            day_first=day_first_setting == "true",
            seen_in_batch=ctx.extraction_batch,
            low_confidence_fields=low_confidence_fields,
        )

        include_lines = bool(args.get("include_lines", False))
        fields_dict_out = asdict(fields)
        if not include_lines:
            fields_dict_out.pop("line_items", None)

        response: dict[str, object] = {
            "source_file": resolved.name,
            "fields": fields_dict_out,
            "checks": [asdict(c) for c in result.checks],
            "status": result.status,
            "notes": result.notes,
            "ocr_used": ocr_used,
            "min_confidence": min_confidence,
        }
        return ToolResult.text(json.dumps(response, indent=2))

    async def _gather_lines(
        self, path: Path, ctx: ToolContext
    ) -> tuple[list[PositionedLine], bool, float | None]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            words, scanned_pages = await asyncio.to_thread(_extract_pdf_words, path)
            if not scanned_pages:
                return words, False, None
            file_hash = hash_bytes(path.read_bytes())
            confidences: list[float] = []
            for page_number, image_bytes in scanned_pages:
                ocr_lines = await ctx.ocr.run_cached(image_bytes, file_hash, page_number)
                words.extend(_lines_from_ocr(ocr_lines))
                confidences.extend(line.confidence for line in ocr_lines)
            return words, True, (min(confidences) if confidences else None)

        if suffix in _IMAGE_SUFFIXES:
            image_bytes = path.read_bytes()
            file_hash = hash_bytes(image_bytes)
            ocr_lines = await ctx.ocr.run_cached(image_bytes, file_hash, 0)
            words = _lines_from_ocr(ocr_lines)
            confidences = [line.confidence for line in ocr_lines]
            return words, True, (min(confidences) if confidences else None)

        raise ValueError(f"unsupported file type: {suffix}")

    def _record_usage(self, ctx: ToolContext, usage: Usage) -> None:
        if ctx.conn is None or ctx.provider is None or ctx.model is None:
            return
        tracker.record(
            ctx.conn,
            session_id=ctx.session_id,
            provider=ctx.provider.name,
            model_id=ctx.model.id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            triggered_by="extract",
        )
