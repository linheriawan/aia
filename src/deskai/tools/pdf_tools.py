"""PDF read tool (ARCHITECTURE.md §8.2, §9): text layer per page, auto-OCR for scanned pages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from deskai.ocr.engine import format_ocr_lines, hash_bytes
from deskai.tools.base import RiskLevel, Tool, ToolContext, ToolResult, as_int, truncate

_SCANNED_TEXT_THRESHOLD = 20
_RENDER_DPI = 250


@dataclass(frozen=True)
class _PageInfo:
    number: int
    text: str | None
    image_bytes: bytes | None


def _extract_pages(path: Path, page_range: object) -> tuple[list[_PageInfo], str]:
    """Open the PDF once, synchronously: text pages are extracted directly, and
    pages that look scanned are rendered to an image for the caller to OCR."""
    file_hash = hash_bytes(path.read_bytes())
    pages: list[_PageInfo] = []
    with pymupdf.open(str(path)) as document:
        total = document.page_count
        start, end = 0, total - 1
        if isinstance(page_range, list) and len(page_range) == 2:
            start = max(0, as_int(page_range[0], 0))
            end = min(total - 1, as_int(page_range[1], total - 1))

        matrix = pymupdf.Matrix(_RENDER_DPI / 72, _RENDER_DPI / 72)
        for number in range(start, end + 1):
            page = document[number]
            text = page.get_text().strip()
            non_whitespace = len(text.replace(" ", "").replace("\n", ""))
            if non_whitespace < _SCANNED_TEXT_THRESHOLD and page.get_images():
                image_bytes = page.get_pixmap(matrix=matrix).tobytes("png")
                pages.append(_PageInfo(number, None, image_bytes))
            else:
                pages.append(_PageInfo(number, text, None))
    return pages, file_hash


class ReadPdfTool(Tool):
    name = "read_pdf"
    description = (
        "Read a PDF's text layer per page. Pages with no extractable text are OCR'd "
        "automatically (flagged low-confidence where OCR isn't sure)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "page_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "[start, end] page indices, 0-based inclusive.",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")))
        if not resolved.exists():
            return ToolResult.text(f"Not found: {resolved}", is_error=True)

        page_range = args.get("page_range")
        try:
            pages, file_hash = await asyncio.to_thread(_extract_pages, resolved, page_range)
        except Exception as exc:  # the file may be corrupt, encrypted, or not a PDF
            return ToolResult.text(f"Could not open {resolved}: {exc}", is_error=True)

        lines: list[str] = []
        for page_info in pages:
            lines.append(f"\n--- Page {page_info.number} ---")
            if page_info.image_bytes is not None:
                ocr_lines = await ctx.ocr.run_cached(
                    page_info.image_bytes, file_hash, page_info.number
                )
                lines.append(format_ocr_lines(ocr_lines))
            else:
                lines.append(page_info.text or "(no text on this page)")

        return ToolResult.text(truncate("\n".join(lines)))
