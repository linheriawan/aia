"""Tests for the Phase 3 read-only tools, run against tests/fixtures/*."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from deskai.ocr.engine import OcrEngine
from deskai.storage import db
from deskai.tools.base import ToolContext
from deskai.tools.docx_tools import ReadDocxTool
from deskai.tools.fs_tools import FileInfoTool, ListDirTool, SearchFilesTool
from deskai.tools.pdf_tools import ReadPdfTool
from deskai.tools.pptx_tools import ReadPptxTool
from deskai.tools.sandbox import Sandbox, SandboxError, WorkspaceFolder
from deskai.tools.text_tools import ReadTextTool
from deskai.tools.xlsx_tools import ReadXlsxTool

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("sample.docx", "sample.xlsx", "sample.pptx", "sample.pdf", "scanned.pdf"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    (tmp_path / "notes.txt").write_text("line one\nline two\nthe secret word is pineapple\n")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path, tmp_path: Path) -> ToolContext:
    sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=[])
    conn = db.connect(tmp_path / "test.db")
    return ToolContext(session_id="s1", workspace=sandbox, ocr=OcrEngine(conn))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_list_dir(ctx: ToolContext) -> None:
    result = _run(ListDirTool().run({"path": "."}, ctx))
    text = result.content[0].text
    assert "sample.docx" in text
    assert "notes.txt" in text
    assert not result.is_error


def test_list_dir_rejects_path_outside_workspace(ctx: ToolContext, tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    with pytest.raises(SandboxError):
        asyncio.run(ListDirTool().run({"path": str(outside)}, ctx))


def test_list_dir_pattern_filters_files(ctx: ToolContext) -> None:
    result = _run(ListDirTool().run({"path": ".", "pattern": "*.xlsx"}, ctx))
    text = result.content[0].text
    assert "sample.xlsx" in text
    assert "sample.docx" not in text


def test_file_info_reports_docx_paragraphs(ctx: ToolContext) -> None:
    result = _run(FileInfoTool().run({"path": "sample.docx"}, ctx))
    assert "Paragraphs" in result.content[0].text


def test_file_info_reports_xlsx_sheets(ctx: ToolContext) -> None:
    result = _run(FileInfoTool().run({"path": "sample.xlsx"}, ctx))
    assert "Data" in result.content[0].text


def test_file_info_reports_pptx_slides(ctx: ToolContext) -> None:
    result = _run(FileInfoTool().run({"path": "sample.pptx"}, ctx))
    assert "Slides: 2" in result.content[0].text


def test_file_info_reports_pdf_pages(ctx: ToolContext) -> None:
    result = _run(FileInfoTool().run({"path": "sample.pdf"}, ctx))
    assert "Pages: 1" in result.content[0].text


def test_file_info_missing_file_is_error(ctx: ToolContext) -> None:
    result = _run(FileInfoTool().run({"path": "nope.txt"}, ctx))
    assert result.is_error


def test_search_files_by_filename(ctx: ToolContext) -> None:
    result = _run(SearchFilesTool().run({"query": "sample"}, ctx))
    text = result.content[0].text
    assert "sample.docx" in text
    assert "sample.xlsx" in text


def test_search_files_by_content(ctx: ToolContext) -> None:
    result = _run(SearchFilesTool().run({"query": "pineapple"}, ctx))
    text = result.content[0].text
    assert "notes.txt" in text
    assert "pineapple" in text


def test_search_files_requires_query(ctx: ToolContext) -> None:
    result = _run(SearchFilesTool().run({"query": ""}, ctx))
    assert result.is_error


def test_read_text_whole_file(ctx: ToolContext) -> None:
    result = _run(ReadTextTool().run({"path": "notes.txt"}, ctx))
    text = result.content[0].text
    assert "line one" in text
    assert "pineapple" in text


def test_read_text_line_range(ctx: ToolContext) -> None:
    result = _run(ReadTextTool().run({"path": "notes.txt", "start_line": 2, "end_line": 2}, ctx))
    text = result.content[0].text
    assert "line two" in text
    assert "pineapple" not in text


def test_read_text_start_past_end_is_error(ctx: ToolContext) -> None:
    result = _run(ReadTextTool().run({"path": "notes.txt", "start_line": 99}, ctx))
    assert result.is_error


def test_read_docx_includes_paragraphs_and_table(ctx: ToolContext) -> None:
    result = _run(ReadDocxTool().run({"path": "sample.docx"}, ctx))
    text = result.content[0].text
    assert "Sample Report" in text
    assert "normal paragraph" in text
    assert "Widget" in text
    assert "| Name | Amount |" in text


def test_read_docx_paragraph_range(ctx: ToolContext) -> None:
    result = _run(ReadDocxTool().run({"path": "sample.docx", "paragraph_range": [0, 0]}, ctx))
    text = result.content[0].text
    assert "Sample Report" in text
    assert "normal paragraph" not in text


def test_read_xlsx_lists_sheets_when_none_given(ctx: ToolContext) -> None:
    result = _run(ReadXlsxTool().run({"path": "sample.xlsx"}, ctx))
    assert "Data" in result.content[0].text


def test_read_xlsx_reads_sheet_values(ctx: ToolContext) -> None:
    result = _run(ReadXlsxTool().run({"path": "sample.xlsx", "sheet": "Data"}, ctx))
    text = result.content[0].text
    assert "Widget" in text
    assert "100" in text


def test_read_xlsx_reads_a_range(ctx: ToolContext) -> None:
    result = _run(
        ReadXlsxTool().run({"path": "sample.xlsx", "sheet": "Data", "range": "A1:B1"}, ctx)
    )
    text = result.content[0].text
    assert "Name" in text
    assert "Widget" not in text


def test_read_xlsx_unknown_sheet_is_error(ctx: ToolContext) -> None:
    result = _run(ReadXlsxTool().run({"path": "sample.xlsx", "sheet": "Nope"}, ctx))
    assert result.is_error


def test_read_pptx_includes_slide_text_and_notes(ctx: ToolContext) -> None:
    result = _run(ReadPptxTool().run({"path": "sample.pptx"}, ctx))
    text = result.content[0].text
    assert "Slide One" in text
    assert "Slide Two" in text
    assert "Speaker note for slide two" in text


def test_read_pptx_slide_range(ctx: ToolContext) -> None:
    result = _run(ReadPptxTool().run({"path": "sample.pptx", "slide_range": [0, 0]}, ctx))
    text = result.content[0].text
    assert "Slide One" in text
    assert "Slide Two" not in text


def test_read_pdf_extracts_text_layer(ctx: ToolContext) -> None:
    result = _run(ReadPdfTool().run({"path": "sample.pdf"}, ctx))
    text = result.content[0].text
    assert "real text layer" in text


def test_read_pdf_auto_ocrs_a_scanned_page(ctx: ToolContext) -> None:
    result = _run(ReadPdfTool().run({"path": "scanned.pdf"}, ctx))
    text = result.content[0].text
    assert "Scanned" in text or "Invoice" in text
