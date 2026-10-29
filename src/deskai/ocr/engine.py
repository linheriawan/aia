"""RapidOCR wrapper, run in a process pool (ARCHITECTURE.md §9, §13).

CPU-heavy OCR inference must never block the Qt main thread, so it runs in a
`ProcessPoolExecutor`; results are cached by (file hash, page, pipeline
version) in SQLite so re-reading the same scanned page is free.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import cv2
import numpy as np

from deskai.storage import repositories

LOW_CONFIDENCE_THRESHOLD = 0.6
PIPELINE_VERSION = "ocr-v1"

_engine_singleton: Any | None = None


@dataclasses.dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    # (x0, y0, x1, y1) bounding rect, for layout-based row grouping
    # (extraction/layout.py, ARCHITECTURE.md §10.3). Optional and defaulted
    # so it round-trips through older cache rows that predate this field.
    box: tuple[float, float, float, float] | None = None


def worker_count() -> int:
    return max(1, min(2, (os.cpu_count() or 2) - 1))


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _get_engine() -> Any:
    """Lazily created per worker process so the ONNX model loads once per process."""
    global _engine_singleton
    if _engine_singleton is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine_singleton = RapidOCR()
    return _engine_singleton


def _run_ocr_worker(
    image_bytes: bytes,
) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Runs inside a worker process: preprocess, then OCR."""
    from deskai.ocr.preprocess import preprocess

    array = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        return []
    processed = preprocess(array)
    engine = _get_engine()
    result, _elapse = engine(processed)
    if not result:
        return []
    lines = []
    for box, text, confidence in result:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        lines.append((str(text), float(confidence), (min(xs), min(ys), max(xs), max(ys))))
    return lines


def _ocr_line_from_json(item: dict[str, Any]) -> OcrLine:
    """`json.loads` hands back a list, not a tuple, for `box` — normalize it
    back so a cache hit returns the exact same shape as a fresh OCR run."""
    box = item.get("box")
    return OcrLine(
        text=item["text"],
        confidence=item["confidence"],
        box=tuple(box) if box is not None else None,
    )


def format_ocr_lines(lines: Sequence[OcrLine]) -> str:
    if not lines:
        return "(no text detected)"
    rendered = []
    for line in lines:
        marker = " [low-confidence]" if line.confidence < LOW_CONFIDENCE_THRESHOLD else ""
        rendered.append(f"{line.text}{marker}")
    return "\n".join(rendered)


class OcrEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._pool = ProcessPoolExecutor(max_workers=worker_count())

    async def run_cached(self, image_bytes: bytes, file_hash: str, page: int) -> list[OcrLine]:
        cached = repositories.get_ocr_cache(self._conn, file_hash, page, PIPELINE_VERSION)
        if cached is not None:
            return [_ocr_line_from_json(item) for item in json.loads(cached)]

        lines = await self._run(image_bytes)
        repositories.set_ocr_cache(
            self._conn,
            file_hash,
            page,
            PIPELINE_VERSION,
            json.dumps([dataclasses.asdict(line) for line in lines]),
        )
        return lines

    async def _run(self, image_bytes: bytes) -> list[OcrLine]:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(self._pool, _run_ocr_worker, image_bytes)
        return [OcrLine(text=text, confidence=confidence, box=box) for text, confidence, box in raw]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
