"""OCR engine tests against the real RapidOCR runtime (it works on this machine —
see pyproject.toml's onnxruntime pin) and the SQLite cache."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from deskai.ocr import engine
from deskai.ocr.engine import OcrEngine
from deskai.storage import db, repositories


def _text_image_bytes(text: str = "Hello OCR Test") -> bytes:
    image = Image.new("RGB", (400, 100), color="white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 30), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "deskai.db")
    yield connection
    connection.close()


def test_hash_bytes_is_stable() -> None:
    data = b"same content"
    assert engine.hash_bytes(data) == engine.hash_bytes(data)
    assert engine.hash_bytes(data) != engine.hash_bytes(b"different content")


def test_run_ocr_worker_detects_real_text() -> None:
    lines = engine._run_ocr_worker(_text_image_bytes("Hello OCR Test"))
    assert lines
    text = " ".join(t for t, _confidence, _box in lines)
    assert "Hello" in text or "OCR" in text
    assert all(len(box) == 4 for _t, _c, box in lines)


def test_run_ocr_worker_on_blank_image_returns_nothing() -> None:
    blank = np.full((50, 200, 3), 255, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", blank)
    assert ok
    lines = engine._run_ocr_worker(bytes(buffer))
    assert lines == []


def test_format_ocr_lines_flags_low_confidence() -> None:
    lines = [engine.OcrLine("clear text", 0.95), engine.OcrLine("blurry text", 0.3)]
    rendered = engine.format_ocr_lines(lines)
    assert "clear text" in rendered
    assert "blurry text [low-confidence]" in rendered


def test_format_ocr_lines_empty() -> None:
    assert engine.format_ocr_lines([]) == "(no text detected)"


def test_run_cached_hits_cache_on_second_call(conn) -> None:
    ocr = OcrEngine(conn)
    image_bytes = _text_image_bytes("Cached line")
    file_hash = engine.hash_bytes(image_bytes)

    first = asyncio.run(ocr.run_cached(image_bytes, file_hash, page=0))
    assert first

    cached_row = repositories.get_ocr_cache(conn, file_hash, 0, engine.PIPELINE_VERSION)
    assert cached_row is not None

    # Even feeding garbage bytes in, the cache (keyed by file_hash) must win —
    # this proves the second call doesn't re-run inference.
    second = asyncio.run(ocr.run_cached(b"not a real image", file_hash, page=0))
    assert second == first

    ocr.shutdown()
