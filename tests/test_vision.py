from pathlib import Path

import cv2
import numpy as np
import pytest

from app import config
from app.recognize import InvalidImage, decode, recognize
from app.vision.board import detect_board

DATA = Path(__file__).parent / "data"
needs_model = pytest.mark.skipif(not Path(config.MODEL_PATH).is_file(), reason="model not downloaded")


def synthetic_board(corners):
    """An empty checkerboard warped into `corners` (TL, TR, BR, BL) on a plain background."""
    size = 400
    flat = np.zeros((size, size, 3), np.uint8)
    for r in range(8):
        for c in range(8):
            colour = (225, 235, 240) if (r + c) % 2 == 0 else (60, 120, 70)
            cv2.rectangle(flat, (c * 50, r * 50), (c * 50 + 49, r * 50 + 49), colour, -1)
    framed = cv2.copyMakeBorder(flat, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=(200, 210, 215))
    src = np.float32([[20, 20], [420, 20], [420, 420], [20, 420]])
    H = cv2.getPerspectiveTransform(src, np.float32(corners))
    return cv2.warpPerspective(framed, H, (900, 700), borderValue=(90, 110, 140))


def test_detects_board_under_perspective():
    corners = np.float32([[260, 120], [640, 140], [760, 600], [150, 580]])
    det = detect_board(synthetic_board(corners))
    assert det is not None
    square = np.linalg.norm(corners[0] - corners[1]) / 8
    assert np.abs(det.corners - corners).max() < 0.25 * square


def test_no_board_in_blank_image():
    assert detect_board(np.full((600, 800, 3), 128, np.uint8)) is None


def test_decode_rejects_garbage():
    with pytest.raises(InvalidImage):
        decode(b"definitely not an image")


@needs_model
def test_recognizes_real_photo():
    rec, size = recognize((DATA / "board.jpg").read_bytes())
    assert rec.placement == (DATA / "board.fen").read_text().strip()
    assert rec.confidence >= 0.8
    assert len(rec.squares) == 64 and len(rec.corners) == 4
    assert size == (900, 600)
