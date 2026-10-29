"""Groups positioned text into rows by y-coordinate (ARCHITECTURE.md §10.3 step 2).

Both PDF text-layer words (pymupdf's `page.get_text("words")`) and OCR lines
(RapidOCR, via `ocr/engine.py`) report a bounding box in the same
`(x0, y0, x1, y1)` shape, so this works on either source without caring which
one produced it — the invoice pipeline mixes both within one document when
some pages have a text layer and others are scanned.
"""

from __future__ import annotations

from dataclasses import dataclass

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PositionedLine:
    text: str
    box: Box
    confidence: float = 1.0


def group_into_rows(lines: list[PositionedLine], *, y_tolerance: float = 6.0) -> list[str]:
    """Sorts top-to-bottom, left-to-right, merging lines whose vertical
    centers fall within `y_tolerance` points of each other into one row."""
    if not lines:
        return []

    def y_center(line: PositionedLine) -> float:
        return (line.box[1] + line.box[3]) / 2

    ordered = sorted(lines, key=y_center)
    rows: list[list[PositionedLine]] = [[ordered[0]]]
    for line in ordered[1:]:
        current_row = rows[-1]
        row_y = sum(y_center(item) for item in current_row) / len(current_row)
        if abs(y_center(line) - row_y) <= y_tolerance:
            current_row.append(line)
        else:
            rows.append([line])

    return [
        "  ".join(item.text for item in sorted(row, key=lambda item: item.box[0])) for row in rows
    ]
