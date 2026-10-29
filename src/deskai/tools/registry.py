"""Merges built-in tools (and, from Phase 7, MCP tools) into one lookup (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

from deskai.core.messages import ToolSpec
from deskai.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def tool_specs(self, *, allow_vision: bool = True) -> list[ToolSpec]:
        """`allow_vision=False` drops tools that only make sense for a vision-capable
        model (view_image) — that model gets OCR text instead (ARCHITECTURE.md §5.2)."""
        return [
            ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self._tools.values()
            if allow_vision or not t.requires_vision
        ]


def build_default_registry() -> ToolRegistry:
    """The v1 tool set: reads (Phase 3), writes, and OCR/vision (Phase 4)."""
    from deskai.tools.docx_tools import CreateDocxTool, ReadDocxTool, UpdateDocxTool
    from deskai.tools.fs_tools import FileInfoTool, ListDirTool, SearchFilesTool
    from deskai.tools.invoice_tools import ExtractInvoiceTool
    from deskai.tools.ocr_tools import OcrImageTool, OcrPdfTool, ViewImageTool
    from deskai.tools.pdf_tools import ReadPdfTool
    from deskai.tools.pptx_tools import ReadPptxTool, UpdatePptxTool
    from deskai.tools.text_tools import EditTextTool, ReadTextTool, WriteTextTool
    from deskai.tools.xlsx_tools import ReadXlsxTool, WriteXlsxTool

    return ToolRegistry(
        [
            ListDirTool(),
            FileInfoTool(),
            SearchFilesTool(),
            ReadTextTool(),
            WriteTextTool(),
            EditTextTool(),
            ReadDocxTool(),
            CreateDocxTool(),
            UpdateDocxTool(),
            ReadXlsxTool(),
            WriteXlsxTool(),
            ReadPptxTool(),
            UpdatePptxTool(),
            ReadPdfTool(),
            OcrImageTool(),
            OcrPdfTool(),
            ViewImageTool(),
            ExtractInvoiceTool(),
        ]
    )
