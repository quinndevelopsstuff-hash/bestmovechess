"""Screenshot recognition with chessfen (https://github.com/sunfmin/chessfen, GPL-3.0):
pure pixel math, no ML model.

chessfen supplies only the piece placement, each square's verdict and its orientation
guess; the final FEN (side to move, castling, en passant) is built by the caller from the
user's own choices. Images are handed over as in-memory arrays; nothing is written.

chessfen needs Python 3.13+. On older Pythons it isn't installed and `available()` is
False, so screenshots go to the photo model instead.
"""

from __future__ import annotations

import chess
import cv2
import numpy as np

from .board_finder import Grid
from .recognizer import Recognition

try:
    import chessfen
    from chessfen.classify import LOW_COLOR_MARGIN, LOW_MARGIN, LOW_SCORE
except ImportError:   # Python < 3.13, or not installed
    chessfen = None

# Margin around the pre-cropped board, as a fraction of a square, so chessfen still sees
# the board's edge.
CROP_MARGIN = 0.15


def available() -> bool:
    return chessfen is not None


class ScreenshotFailed(Exception):
    """chessfen isn't available, found no board, or raised an error."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def square_confidence(verdict) -> float:
    """0-1 confidence for one chessfen square verdict, scaled so that 0.6 is exactly
    chessfen's own line between confident and doubtful (each of shape match, lead over
    the runner-up and colour margin must clear its threshold)."""
    if verdict.piece is None:
        return 1.0
    parts = (verdict.score / LOW_SCORE, verdict.margin / LOW_MARGIN, verdict.color_margin / LOW_COLOR_MARGIN)
    return round(float(min(1.0, 0.6 * min(parts))), 3)


def _to_recognition(r, offset: tuple[int, int], flip: bool | None) -> Recognition:
    """Our Recognition from a chessfen one. `offset` is where the array chessfen saw sits
    in the full image, so corners come out in full-image pixels."""
    board = chess.BaseBoard.empty()
    for square, verdict in r.verdicts.items():
        if verdict.piece is not None:
            board.set_piece_at(square, verdict.piece)
    squares = {chess.square_name(sq): square_confidence(v) for sq, v in r.verdicts.items()}
    uncertain = [chess.square_name(sq) for sq in r.shaky]
    dx, dy = offset
    x0, y0, size = r.rect.left + dx, r.rect.top + dy, r.rect.size
    corners = [[x0, y0], [x0 + size, y0], [x0 + size, y0 + size], [x0, y0 + size]]   # screen TL, TR, BR, BL
    flipped = r.orientation == chessfen.Orientation.BLACK
    if flipped:   # Black at the bottom: a8 is the bottom-right corner
        corners = corners[2:] + corners[:2]
    return Recognition(
        placement=board.board_fen(),
        confidence=min(squares.values()),
        squares=squares,
        uncertain=uncertain,
        repairs=[],
        alternatives={},
        corners=[[float(x), float(y)] for x, y in corners],
        source="screenshot",
        flipped=flipped,
        orientation="requested" if flip is not None else "chessfen",
    )


def recognize(img: np.ndarray, flip: bool | None = None, grid: Grid | None = None) -> Recognition:
    """Read a screenshot (BGR array). flip: True = Black at the bottom, False = White,
    None = let chessfen guess. If `grid` (the board found by its checker pattern) is
    given, chessfen reads a crop around it first, which is faster and keeps phone UI
    out of the way; the whole image is the fallback. Doubtful squares are reported in
    `uncertain`, never hidden. Raises ScreenshotFailed."""
    if chessfen is None:
        raise ScreenshotFailed("screenshot recognition needs chessfen, which needs Python 3.13+")
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    orientation = (chessfen.Orientation.AUTO if flip is None
                   else chessfen.Orientation.BLACK if flip else chessfen.Orientation.WHITE)

    def attempt(arr: np.ndarray, offset: tuple[int, int]) -> Recognition:
        # Castling.NONE: we fill castling in ourselves from the user's choices.
        result = chessfen.recognize(np.ascontiguousarray(arr), orientation=orientation, castling=chessfen.Castling.NONE)
        return _to_recognition(result, offset, flip)

    reasons = []
    if grid is not None:
        m = grid.size * CROP_MARGIN
        h, w = img.shape[:2]
        x0, y0 = max(0, int(grid.x0 - m)), max(0, int(grid.y0 - m))
        x1, y1 = min(w, int(grid.x0 + 8 * grid.size + m)), min(h, int(grid.y0 + 8 * grid.size + m))
        try:
            return attempt(rgb[y0:y1, x0:x1], (x0, y0))
        except Exception as exc:   # BoardNotFoundError, or anything chessfen trips over
            reasons.append(f"cropped board: {exc}")
    try:
        return attempt(rgb, (0, 0))
    except Exception as exc:
        reasons.append(f"whole image: {exc}")
    raise ScreenshotFailed("; ".join(reasons))
