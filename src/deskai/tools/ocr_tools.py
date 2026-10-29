"""OCR and vision tools (ARCHITECTURE.md §8.2, §9): ocr_image, ocr_pdf, view_image."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pymupdf

from deskai.core.messages import ImagePart
from deskai.ocr.engine import format_ocr_lines, hash_bytes
from deskai.tools.base import RiskLevel, Tool, ToolContext, ToolResult, as_int, truncate

_RENDER_DPI = 250


class OcrImageTool(Tool):
    name = "ocr_image"
    description = (
        "OCR an image file, returning text lines with a low-confidence marker on uncertain ones."
    )
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")))
        if not resolved.exists():
            return ToolResult.text(f"Not found: {resolved}", is_error=True)
        image_bytes = await asyncio.to_thread(resolved.read_bytes)
        file_hash = hash_bytes(image_bytes)
        lines = await ctx.ocr.run_cached(image_bytes, file_hash, page=0)
        return ToolResult.text(truncate(format_ocr_lines(lines)))


class OcrPdfTool(Tool):
    name = "ocr_pdf"
    description = (
        "Force OCR on PDF pages (even ones with a thin text layer), returning text lines "
        "with a low-confidence marker on uncertain ones."
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
            page_images, file_hash = await asyncio.to_thread(_render_pages, resolved, page_range)
        except Exception as exc:  # the file may be corrupt, encrypted, or not a PDF
            return ToolResult.text(f"Could not open {resolved}: {exc}", is_error=True)

        lines: list[str] = []
        for number, image_bytes in page_images:
            lines.append(f"\n--- Page {number} ---")
            ocr_lines = await ctx.ocr.run_cached(image_bytes, file_hash, number)
            lines.append(format_ocr_lines(ocr_lines))
        return ToolResult.text(truncate("\n".join(lines)))


def _render_pages(path: Path, page_range: object) -> tuple[list[tuple[int, bytes]], str]:
    file_hash = hash_bytes(path.read_bytes())
    with pymupdf.open(str(path)) as document:
        total = document.page_count
        start, end = 0, total - 1
        if isinstance(page_range, list) and len(page_range) == 2:
            start = max(0, as_int(page_range[0], 0))
            end = min(total - 1, as_int(page_range[1], total - 1))
        matrix = pymupdf.Matrix(_RENDER_DPI / 72, _RENDER_DPI / 72)
        pages = [
            (number, document[number].get_pixmap(matrix=matrix).tobytes("png"))
            for number in range(start, end + 1)
        ]
    return pages, file_hash


class ViewImageTool(Tool):
    name = "view_image"
    description = (
        "Return an image (a file, or one page of a PDF) for a vision-capable model to look at."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "page": {"type": "integer", "minimum": 0, "description": "PDF page, 0-based."},
        },
        "required": ["path"],
    }
    risk = RiskLevel.READ
    requires_vision = True

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")))
        if not resolved.exists():
            return ToolResult.text(f"Not found: {resolved}", is_error=True)

        page = args.get("page")
        if resolved.suffix.lower() == ".pdf":
            return await asyncio.to_thread(self._render_pdf_page, resolved, as_int(page, 0))
        return await asyncio.to_thread(self._read_image, resolved)

    def _read_image(self, path: Path) -> ToolResult:
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
        }.get(path.suffix.lower())
        if media_type is None:
            return ToolResult.text(f"Unsupported image type: {path.suffix}", is_error=True)
        return ToolResult([ImagePart(path.read_bytes(), media_type)])

    def _render_pdf_page(self, path: Path, page: int) -> ToolResult:
        try:
            with pymupdf.open(str(path)) as document:
                if not 0 <= page < document.page_count:
                    return ToolResult.text(
                        f"Page {page} out of range (0-{document.page_count - 1})", is_error=True
                    )
                matrix = pymupdf.Matrix(_RENDER_DPI / 72, _RENDER_DPI / 72)
                png_bytes = document[page].get_pixmap(matrix=matrix).tobytes("png")
        except Exception as exc:  # the file may be corrupt, encrypted, or not a PDF
            return ToolResult.text(f"Could not open {path}: {exc}", is_error=True)
        return ToolResult([ImagePart(png_bytes, "image/png")])
