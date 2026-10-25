"""The checker-pattern board locator used to pre-crop screenshots (no chessfen needed)."""

import numpy as np
import pytest

from app import render
from app.vision.board_finder import locate_board
from tests.test_screenshot import phone_screenshot, random_position

pytestmark = pytest.mark.skipif(render.cairosvg is None, reason="needs Cairo to render boards")


def test_board_finder_locates_board():
    board, last = random_position(10)
    img = phone_screenshot(board, 720, last)
    grid = locate_board(img)
    top, left = int(img.shape[0] * 0.3), (img.shape[1] - 720) // 2
    assert abs(grid.x0 - left) <= 1.5 and abs(grid.y0 - top) <= 1.5 and abs(grid.size - 90) <= 0.5


def test_board_finder_ignores_blank_image():
    assert locate_board(np.full((1600, 740, 3), 40, np.uint8)) is None


def test_flat_screenshot_vs_camera_photo():
    import cv2
    from pathlib import Path
    from app.vision.board_finder import looks_like_screenshot
    board, last = random_position(11)
    assert looks_like_screenshot(locate_board(phone_screenshot(board, 640, last)))
    photo = cv2.imread(str(Path(__file__).parent / "data" / "board.jpg"))
    assert not looks_like_screenshot(locate_board(photo))
