"""Scanned-page preprocessing before OCR (ARCHITECTURE.md §9): grayscale,
deskew, contrast, upscale if small. Pure numpy/OpenCV, no I/O."""

from __future__ import annotations

import cv2
import numpy as np

_MIN_DESKEW_PIXELS = 20
_MIN_DESKEW_ANGLE_DEGREES = 0.5
_UPSCALE_TARGET_DIM = 1000


def preprocess(image: np.ndarray) -> np.ndarray:
    """Returns a 3-channel BGR image ready for OCR."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = deskew(gray)
    gray = enhance_contrast(gray)
    gray = upscale_if_small(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def deskew(gray: np.ndarray) -> np.ndarray:
    inverted = cv2.bitwise_not(gray)
    threshold = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(threshold > 0))
    if coords.shape[0] < _MIN_DESKEW_PIXELS:
        return gray  # not enough signal to estimate a reliable angle

    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < _MIN_DESKEW_ANGLE_DEGREES:
        return gray

    height, width = gray.shape[:2]
    center = (width // 2, height // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def enhance_contrast(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    result: np.ndarray = clahe.apply(gray)
    return result


def upscale_if_small(gray: np.ndarray, target_dim: int = _UPSCALE_TARGET_DIM) -> np.ndarray:
    height, width = gray.shape[:2]
    if max(height, width) >= target_dim:
        return gray
    scale = target_dim / max(height, width)
    result: np.ndarray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return result
