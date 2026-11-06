import numpy as np

from deskai.ocr import preprocess


def test_upscale_if_small_enlarges_below_target() -> None:
    small = np.zeros((100, 200), dtype=np.uint8)
    result = preprocess.upscale_if_small(small, target_dim=1000)
    assert max(result.shape[:2]) >= 1000


def test_upscale_if_small_leaves_large_images_alone() -> None:
    large = np.zeros((1200, 800), dtype=np.uint8)
    result = preprocess.upscale_if_small(large, target_dim=1000)
    assert result.shape == large.shape


def test_enhance_contrast_preserves_shape() -> None:
    gray = np.random.default_rng(0).integers(0, 255, (100, 100), dtype=np.uint8)
    result = preprocess.enhance_contrast(gray)
    assert result.shape == gray.shape
    assert result.dtype == gray.dtype


def test_deskew_blank_image_is_a_no_op() -> None:
    blank = np.full((100, 100), 255, dtype=np.uint8)
    result = preprocess.deskew(blank)
    assert np.array_equal(result, blank)


def test_preprocess_returns_three_channel_bgr() -> None:
    gray = np.zeros((50, 50), dtype=np.uint8)
    result = preprocess.preprocess(gray)
    assert result.ndim == 3
    assert result.shape[2] == 3


def _text_lines(width: int = 800, height: int = 400) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(32)
    for i in range(4):
        draw.text((40, 60 + i * 60), "The quick brown fox jumps over", fill=0, font=font)
    return np.array(image)


def _rotate(gray: np.ndarray, degrees: float) -> np.ndarray:
    import cv2

    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width // 2, height // 2), degrees, 1.0)
    return cv2.warpAffine(gray, matrix, (width, height), borderValue=255)


def test_deskew_leaves_level_text_alone() -> None:
    # Regression: minAreaRect reports level text as 90° on some OpenCV
    # releases, which used to rotate the whole page sideways.
    level = _text_lines()
    assert np.array_equal(preprocess.deskew(level), level)


def test_deskew_levels_tilted_text_in_both_directions() -> None:
    for tilt in (5.0, -7.0, 20.0):
        corrected = preprocess.deskew(_rotate(_text_lines(), tilt))
        residual = preprocess.skew_angle(corrected)
        assert residual is not None
        assert abs(residual) < 1.0
