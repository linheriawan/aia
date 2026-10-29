"""Tests for grouping positioned text into rows by y-coordinate (ARCHITECTURE.md §10.3)."""

from __future__ import annotations

from deskai.extraction.layout import PositionedLine, group_into_rows


def test_empty_input_returns_no_rows() -> None:
    assert group_into_rows([]) == []


def test_single_line_is_its_own_row() -> None:
    lines = [PositionedLine("Invoice #123", (10, 10, 100, 20))]
    assert group_into_rows(lines) == ["Invoice #123"]


def test_lines_on_the_same_y_merge_left_to_right() -> None:
    lines = [
        PositionedLine("100", (200, 50, 220, 60)),
        PositionedLine("Widget", (10, 50, 80, 60)),
        PositionedLine("2", (100, 51, 110, 61)),
    ]
    assert group_into_rows(lines) == ["Widget  2  100"]


def test_lines_on_different_y_become_separate_rows_in_order() -> None:
    lines = [
        PositionedLine("Row two", (10, 100, 80, 110)),
        PositionedLine("Row one", (10, 10, 80, 20)),
    ]
    assert group_into_rows(lines) == ["Row one", "Row two"]


def test_y_tolerance_controls_merge_sensitivity() -> None:
    lines = [
        PositionedLine("A", (10, 10, 20, 20)),
        PositionedLine("B", (30, 18, 40, 28)),
    ]
    assert group_into_rows(lines, y_tolerance=20) == ["A  B"]
    assert group_into_rows(lines, y_tolerance=1) == ["A", "B"]
