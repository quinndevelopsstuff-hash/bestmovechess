"""Photo -> piece placement with the ChessLens square classifier (ONNX Runtime).

Pipeline: board detection (X-corners + RANSAC, perspective homography) -> an upright
crop per square -> CNN -> orientation -> most likely legal placement.
Parts are adapted from ChessLens (https://github.com/zeljko1611/ChessLens), MIT License,
Copyright (c) 2026 zeljko1611. See app/vision/LICENSE.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .board import detect_board
from .squares import crop_squares, grid_points, orientation_scores, rotate_grid, square_brightness

# Class order of the model's output.
CLASSES = [".", "P", "N", "B", "R", "Q", "K", "p", "n", "b", "r", "q", "k"]
CLASS_INDEX = {c: i for i, c in enumerate(CLASSES)}
EMPTY = "."
# Input normalisation used when the model was trained.
MEAN = np.array([0.5, 0.5, 0.5], np.float32)
STD = np.array([0.25, 0.25, 0.25], np.float32)
UNCERTAIN_BELOW = 0.85  # correct boards almost always have every square above ~0.89
# Squares below this get the model's runner-up guesses, for the correction UI.
ALTERNATIVES_BELOW = 0.9
# The model's probabilities top out near 0.95 (it was trained with smoothed labels), and a
# product over 64 squares is meaningless. Board confidence instead combines the weakest
# square with how clearly one rotation of the board beat the others. Thresholds were
# picked on 104 held-out real photos: correct boards have a weakest square >= ~0.89 and
# an orientation margin >= ~2.4; most wrong boards fail one of the two.
SQUARE_FLOOR, SQUARE_GOOD = 0.5, 0.94
ORIENTATION_GOOD = 3.0


class BoardNotFound(Exception):
    pass


@dataclass
class Recognition:
    placement: str                  # FEN piece placement, a8 first
    confidence: float               # 0-1 heuristic confidence that the whole board is right
    squares: dict[str, float]       # per-square confidence in the chosen piece
    uncertain: list[str]            # squares below UNCERTAIN_BELOW
    repairs: list[dict]             # squares changed to make the position legal
    alternatives: dict[str, list]   # square -> [[piece, prob], ...] for less certain squares ("." = empty)
    corners: list[list[float]]      # board corners a8, h8, h1, a1 in image pixels
    source: str = "camera"          # which pipeline produced it: "camera" or "screenshot"
    profile: str | None = None      # screenshot path: the template profile that matched best
    flipped: bool = False           # screenshot path: board shown with Black at the bottom
    orientation: str | None = None  # screenshot path: how that was decided (labels/pieces/default/requested)


def square_name(r: int, c: int) -> str:
    return f"{'abcdefgh'[c]}{8 - r}"


class Recognizer:
    def __init__(self, model_path: str | Path, threads: int = 2) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        self._lock = threading.Lock()

    def _probs(self, crops: np.ndarray) -> np.ndarray:
        """(N, 64, 32, 3) uint8 BGR crops -> (N, 13) probabilities."""
        x = crops[..., ::-1].astype(np.float32) / 255.0
        x = np.ascontiguousarray(((x - MEAN) / STD).transpose(0, 3, 1, 2))
        with self._lock:
            logits = self._session.run(None, {self._input: x})[0].astype(np.float32)
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def recognize(self, image: np.ndarray) -> Recognition:
        """BGR image -> Recognition. Raises BoardNotFound."""
        det = detect_board(image)
        if det is None:
            raise BoardNotFound("couldn't find a chessboard in the photo")
        crops = crop_squares(image, det.corners)
        probs_grid = self._probs(crops.reshape(64, *crops.shape[2:])).reshape(8, 8, len(CLASSES))

        # Rotate the grid so a8 is top-left (h1 is light; White usually nearer the camera).
        scores = orientation_scores(probs_grid, square_brightness(image, det.corners))
        k = int(np.argmax(scores))
        margin = float(np.diff(np.sort(scores)[-2:])[0])
        probs = rotate_grid(probs_grid, k)
        board, repairs = repair(probs)

        squares = {
            square_name(r, c): round(float(probs[r, c, CLASS_INDEX[board[r][c]]]), 3)
            for r in range(8) for c in range(8)
        }
        # Image position of the board's a8, h8, h1, a1 corners after rotation.
        cell = rotate_grid(np.arange(64).reshape(8, 8), k)
        pts = grid_points(det.corners)
        corners = []
        for r, c in [(0, 0), (0, 7), (7, 7), (7, 0)]:
            gr, gc = divmod(int(cell[r, c]), 8)
            # Of that corner square's four grid points, the board corner is the one farthest from the centre.
            cand = [pts[gr + a, gc + b] for a in (0, 1) for b in (0, 1)]
            corner = max(cand, key=lambda p: np.linalg.norm(p - pts[4, 4]))
            corners.append([round(float(v), 1) for v in corner])

        alternatives = {}
        for name, conf in squares.items():
            if conf < ALTERNATIVES_BELOW:
                r, c = 8 - int(name[1]), ord(name[0]) - 97
                top = np.argsort(-probs[r, c])[:3]
                alternatives[name] = [[CLASSES[i], round(float(probs[r, c, i]), 3)] for i in top if probs[r, c, i] >= 0.02]

        return Recognition(
            placement=board_to_fen(board),
            confidence=board_confidence(min(squares.values()), margin),
            squares=squares,
            uncertain=[s for s, p in squares.items() if p < UNCERTAIN_BELOW],
            repairs=repairs,
            alternatives=alternatives,
            corners=corners,
        )


def board_confidence(weakest_square: float, orientation_margin: float) -> float:
    sq = (weakest_square - SQUARE_FLOOR) / (SQUARE_GOOD - SQUARE_FLOOR)
    orient = orientation_margin / ORIENTATION_GOOD
    return round(float(np.clip(sq, 0, 1) * np.clip(orient, 0, 1)), 3)


def board_to_fen(board: list[list[str]]) -> str:
    rows = []
    for row in board:
        out, run = "", 0
        for p in row:
            if p == EMPTY:
                run += 1
                continue
            if run:
                out += str(run)
                run = 0
            out += p
        rows.append(out + (str(run) if run else ""))
    return "/".join(rows)


def repair(probs: np.ndarray, max_changes: int = 6) -> tuple[list[list[str]], list[dict]]:
    """Most likely board from (8, 8, 13) probabilities that obeys the basic rules
    (one king each, no pawns on the back ranks, at most 8 pawns each).

    Greedy: start from the argmax and fix each broken rule with the cheapest change.
    """
    p = np.clip(probs.astype(np.float64), 1e-9, 1.0)
    logp = np.log(p)
    board = [[CLASSES[int(np.argmax(p[r, c]))] for c in range(8)] for r in range(8)]
    changes: list[dict] = []

    def find(piece):
        return [(r, c) for r in range(8) for c in range(8) if board[r][c] == piece]

    def replace(r, c, forbidden, why):
        for idx in np.argsort(-p[r, c]):
            cand = CLASSES[idx]
            if cand not in forbidden and cand != board[r][c]:
                changes.append({"square": square_name(r, c), "from": board[r][c], "to": cand, "reason": why})
                board[r][c] = cand
                return

    for _ in range(max_changes * 4):
        if len(changes) >= max_changes:
            break
        n = len(changes)
        for r in (0, 7):
            for c in range(8):
                if board[r][c] in ("P", "p"):
                    replace(r, c, {"P", "p"}, "pawn_on_back_rank")
        if len(changes) > n:
            continue
        for king in ("K", "k"):
            found = find(king)
            if len(found) > 1:
                found.sort(key=lambda rc: -logp[rc[0], rc[1], CLASS_INDEX[king]])
                for r, c in found[1:]:
                    replace(r, c, {king}, "extra_king")
            elif not found:
                cost = np.full((8, 8), np.inf)
                for r in range(8):
                    for c in range(8):
                        if board[r][c] not in ("K", "k"):
                            cost[r, c] = logp[r, c, CLASS_INDEX[board[r][c]]] - logp[r, c, CLASS_INDEX[king]]
                r, c = map(int, np.unravel_index(int(np.argmin(cost)), cost.shape))
                changes.append({"square": square_name(r, c), "from": board[r][c], "to": king, "reason": "missing_king"})
                board[r][c] = king
        if len(changes) > n:
            continue
        for pawn in ("P", "p"):
            found = find(pawn)
            if len(found) > 8:
                r, c = min(found, key=lambda rc: logp[rc[0], rc[1], CLASS_INDEX[pawn]])
                replace(r, c, {pawn}, "too_many_pawns")
        if len(changes) == n:
            break
    return board, changes
