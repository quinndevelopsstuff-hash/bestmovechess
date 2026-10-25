# Vendored from ChessLens (https://github.com/zeljko1611/ChessLens, commit c4e27c1),
# MIT License, Copyright (c) 2026 zeljko1611. See app/vision/LICENSE.
# Local changes: CLASSES imported from recognizer.py.
"""Square crops and board orientation.

Crops are cut from the photo itself, not from a rectified image, so pieces stay upright at any camera angle.
corners[0..3] are the grid corners (0, 0), (8, 0), (8, 8) and (0, 8).
"""

from __future__ import annotations

import cv2
import numpy as np

CROP_W, CROP_H = 32, 64
HEIGHT_FACTOR = 2.0  # crop height relative to the square width in the image


def grid_homography(corners: np.ndarray) -> np.ndarray:
    """Homography from grid coordinates (0..8) to image pixels."""
    src = np.array([[0, 0], [8, 0], [8, 8], [0, 8]], np.float32)
    return cv2.getPerspectiveTransform(src, np.asarray(corners, np.float32))


def grid_points(corners: np.ndarray) -> np.ndarray:
    """The 81 grid line intersections in the image, shape (9, 9, 2), indexed [row][column]."""
    g = np.array([[[c, r] for c in range(9)] for r in range(9)], np.float32).reshape(1, -1, 2)
    return cv2.perspectiveTransform(g, grid_homography(corners))[0].reshape(9, 9, 2)


def square_boxes(corners: np.ndarray) -> np.ndarray:
    """Crop rectangle (x0, y0, x1, y1) for every square, shape (8, 8, 4)."""
    pts = grid_points(corners)
    boxes = np.zeros((8, 8, 4), np.float32)
    for r in range(8):
        for c in range(8):
            quad = np.array([pts[r, c], pts[r, c + 1], pts[r + 1, c + 1], pts[r + 1, c]])
            x0, x1 = quad[:, 0].min(), quad[:, 0].max()
            bottom = quad[:, 1].max()
            w = x1 - x0
            pad = 0.1 * w
            height = HEIGHT_FACTOR * (w + 2 * pad)
            boxes[r, c] = (x0 - pad, bottom - height, x1 + pad, bottom + 0.05 * w)
    return boxes


def crop_squares(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Crops of all squares, shape (8, 8, CROP_H, CROP_W, 3), uint8 BGR."""
    boxes = square_boxes(corners)
    h, w = image.shape[:2]
    pad = int(max(h, w) * 0.5)  # crops may extend past the image (a piece on the edge)
    padded = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    out = np.zeros((8, 8, CROP_H, CROP_W, 3), np.uint8)
    for r in range(8):
        for c in range(8):
            x0, y0, x1, y1 = boxes[r, c]
            # Affine warp: the crop maps exactly to CROP_W x CROP_H with
            # sub-pixel precision (no rounding of the edges).
            src = np.array([[x0 + pad, y0 + pad], [x1 + pad, y0 + pad], [x0 + pad, y1 + pad]], np.float32)
            dst = np.array([[0, 0], [CROP_W, 0], [0, CROP_H]], np.float32)
            M = cv2.getAffineTransform(src, dst)
            out[r, c] = cv2.warpAffine(padded, M, (CROP_W, CROP_H), flags=cv2.INTER_AREA if (x1 - x0) > CROP_W else cv2.INTER_LINEAR)
    return out


def square_brightness(image: np.ndarray, corners: np.ndarray, cell: int = 24) -> np.ndarray:
    """Median brightness of every square (seen from above), shape (8, 8)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    size = 8 * cell
    dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], np.float32)
    H = cv2.getPerspectiveTransform(np.asarray(corners, np.float32), dst)
    warped = cv2.warpPerspective(gray, H, (size, size), flags=cv2.INTER_AREA)
    m = cell // 6
    cells = warped.reshape(8, cell, 8, cell)[:, m:-m, :, m:-m].transpose(0, 2, 1, 3).reshape(8, 8, -1)
    return np.median(cells, axis=2).astype(np.float32)


# Orientation: which grid rotation puts a8 in the top-left corner.
# Rules used:
#  1. h1 (and a8) are light squares -> decides between rotations {0, 2} and {1, 3}.
#  2. White is usually on the "bottom" side (ranks 1-2) and Black on the top ->
#     decides between k and k+2. This is a heuristic; the user can flip the
#     board in the UI with one click.


def rotate_grid(arr: np.ndarray, k: int) -> np.ndarray:
    """Rotate the first two axes of an array (grid -> board)."""
    return np.rot90(arr, k, axes=(0, 1)).copy()


def orientation_scores(probs: np.ndarray, brightness: np.ndarray) -> np.ndarray:
    """A score for each of the 4 rotations (higher = more likely)."""
    from .recognizer import CLASSES

    p_empty = probs[..., CLASSES.index(".")]
    white = probs[..., [CLASSES.index(c) for c in "PNBRQK"]].sum(-1)
    black = probs[..., [CLASSES.index(c) for c in "pnbrqk"]].sum(-1)
    pawn_w, pawn_b = probs[..., CLASSES.index("P")], probs[..., CLASSES.index("p")]

    scores = np.zeros(4)
    rows = np.arange(8, dtype=np.float32)[:, None] * np.ones((1, 8), np.float32)  # 0 = rank 8
    for k in range(4):
        b = rotate_grid(brightness, k)
        e = rotate_grid(p_empty, k)
        light = (np.add.outer(np.arange(8), np.arange(8)) % 2) == 0  # a8 and h1 are light
        # Prefer empty squares (pieces distort a square's brightness).
        wts = 0.2 + e
        diff = np.average(b[light], weights=wts[light]) - np.average(b[~light], weights=wts[~light])
        color_term = np.tanh(diff / 10.0) * 3.0

        w, bl = rotate_grid(white, k), rotate_grid(black, k)
        pw, pb = rotate_grid(pawn_w, k), rotate_grid(pawn_b, k)
        mean_w = (w * rows).sum() / max(w.sum(), 1e-6)
        mean_b = (bl * rows).sum() / max(bl.sum(), 1e-6)
        side_term = np.tanh((mean_w - mean_b) / 2.0) * 2.0
        # Pawns cannot stand on ranks 1 and 8, a strong signal when present.
        pawn_term = -2.0 * (pw[[0, 7]].sum() + pb[[0, 7]].sum())
        scores[k] = color_term + side_term + pawn_term
    return scores


def best_orientation(probs: np.ndarray, brightness: np.ndarray) -> int:
    return int(np.argmax(orientation_scores(probs, brightness)))
