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
