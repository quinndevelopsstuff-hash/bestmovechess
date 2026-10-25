"""Make a recognised position legal with the least likely-to-be-wrong changes.

Each square has a probability for every reading ("." = empty, FEN letters for pieces);
the cost of a reading is -log(probability). Starting from the most likely board, the
fixes are:

1. pawns on rank 1 or 8: each becomes its cheapest non-pawn reading;
2. kings: an exhaustive search over every (white king, black king) square pair that
   isn't adjacent, counting the cost of placing both kings and of demoting every other
   square currently read as a king, so "exactly one king each" and "kings not adjacent"
   are solved together at minimum total cost;
3. more than 8 pawns of one colour: the cheapest extra pawns change to another piece.

Squares are in screen order with White at the bottom: index 0 is a8, 63 is h1.
"""

from __future__ import annotations

import math

import numpy as np

READINGS = ".PNBRQKpnbrqk"
BACK_RANKS = frozenset(range(8)) | frozenset(range(56, 64))
FLOOR = 1e-6


def square_name(i: int) -> str:
    return f"{'abcdefgh'[i % 8]}{8 - i // 8}"


def _adjacent() -> np.ndarray:
    r, c = np.divmod(np.arange(64), 8)
    return (np.abs(r[:, None] - r[None, :]) <= 1) & (np.abs(c[:, None] - c[None, :]) <= 1)


ADJACENT_OR_SAME = _adjacent()   # includes the diagonal (same square)


def repair(probs: list[dict[str, float]]) -> tuple[list[str], list[dict]]:
    """(board as 64 readings, changes as {square, from, to, reason})."""
    cost = [{p: -math.log(max(q.get(p, 0.0), FLOOR)) for p in READINGS} for q in probs]
    board = [min(c, key=c.get) for c in cost]
    changes: list[dict] = []

    def change(i: int, new: str, reason: str) -> None:
        changes.append({"square": square_name(i), "from": board[i], "to": new, "reason": reason})
        board[i] = new

    def cheapest(i: int, banned: str) -> str:
        return min((p for p in READINGS if p not in banned and p != board[i]), key=cost[i].get)

    def delta(i: int, new: str) -> float:
        return cost[i][new] - cost[i][board[i]]

    # 1. Pawns can't stand on the first or eighth rank.
    for i in sorted(BACK_RANKS):
        if board[i] in "Pp":
            change(i, cheapest(i, "Pp"), "pawn_on_back_rank")

    # 2. Exactly one king per side, not adjacent: minimum-cost pair.
    had = {"K": [i for i in range(64) if board[i] == "K"], "k": [i for i in range(64) if board[i] == "k"]}
    kings = had["K"] + had["k"]
    demote = np.zeros(64)
    demote_to = {}
    for i in kings:
        demote_to[i] = cheapest(i, "Kk" + ("Pp" if i in BACK_RANKS else ""))
        demote[i] = delta(i, demote_to[i])
    place_white = np.array([delta(i, "K") if board[i] != "K" else 0.0 for i in range(64)])
    place_black = np.array([delta(i, "k") if board[i] != "k" else 0.0 for i in range(64)])
    total = place_white[:, None] + place_black[None, :] + demote.sum() - demote[:, None] - demote[None, :]
    total[ADJACENT_OR_SAME] = np.inf
    w, b = map(int, np.unravel_index(int(np.argmin(total)), total.shape))
    adjacent_before = any(ADJACENT_OR_SAME[i, j] for i in had["K"] for j in had["k"])
    for i in kings:
        if i not in (w, b):
            colour = "K" if board[i] == "K" else "k"
            change(i, demote_to[i], "extra_king" if len(had[colour]) > 1 else "kings_adjacent")
    for i, king in ((w, "K"), (b, "k")):
        if board[i] != king:
            change(i, king, "kings_adjacent" if had[king] and adjacent_before else "missing_king")

    # 3. At most 8 pawns per side.
    for pawn in "Pp":
        while True:
            pawns = [i for i in range(64) if board[i] == pawn]
            if len(pawns) <= 8:
                break
            i = min(pawns, key=lambda j: delta(j, cheapest(j, "PpKk")))
            change(i, cheapest(i, "PpKk"), "too_many_pawns")
    return board, changes
