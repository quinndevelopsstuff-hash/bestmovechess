import chess
import pytest

from app.vision.repair import READINGS, repair, square_name

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


def confident(placement: str, p: float = 0.97) -> list[dict]:
    """Probabilities that strongly favour `placement`."""
    cells = []
    for row in placement.split("/"):
        for ch in row:
            cells += ["."] * int(ch) if ch.isdigit() else [ch]
    return [{r: (p if r == c else (1 - p) / 12) for r in READINGS} for c in cells]


def idx(name: str) -> int:
    return (8 - int(name[1])) * 8 + "abcdefgh".index(name[0])


def set_probs(probs, square, **readings):
    q = {r: 0.001 for r in READINGS}
    q.update({("." if k == "empty" else k): v for k, v in readings.items()})
    probs[idx(square)] = q


def placement(board: list[str]) -> str:
    b = chess.BaseBoard.empty()
    for i, p in enumerate(board):
        if p != ".":
            b.set_piece_at(chess.parse_square(square_name(i)), chess.Piece.from_symbol(p))
    return b.board_fen()


def test_legal_board_is_untouched():
    board, changes = repair(confident(START))
    assert placement(board) == START and changes == []


def test_extra_king_becomes_its_next_reading():
    probs = confident(START)
    set_probs(probs, "d1", K=0.6, Q=0.35)        # the queen misread as a second king
    board, changes = repair(probs)
    assert placement(board) == START
    assert changes == [{"square": "d1", "from": "K", "to": "Q", "reason": "extra_king"}]


def test_missing_king_goes_to_most_likely_square():
    probs = confident(START)
    set_probs(probs, "e8", q=0.55, k=0.4)        # black king misread as a queen
    board, changes = repair(probs)
    assert placement(board) == START
    assert changes == [{"square": "e8", "from": "q", "to": "k", "reason": "missing_king"}]


def test_adjacent_kings_move_the_cheaper_one():
    probs = confident("8/8/8/4k3/4K3/8/8/7r")
    set_probs(probs, "e5", k=0.5, q=0.45)        # unsure black king next to a sure white king
    set_probs(probs, "h8", empty=0.6, k=0.35)    # ...and a faint black king elsewhere
    board, changes = repair(probs)
    assert placement(board) == "7k/8/8/4q3/4K3/8/8/7r"
    assert {c["reason"] for c in changes} == {"kings_adjacent"}


def test_pawn_on_back_rank():
    probs = confident("P3k3/8/8/8/8/8/8/4K3")
    set_probs(probs, "a8", P=0.6, R=0.3)
    board, changes = repair(probs)
    assert placement(board) == "R3k3/8/8/8/8/8/8/4K3"
    assert changes[0]["reason"] == "pawn_on_back_rank"


def test_too_many_pawns_drops_the_least_likely():
    probs = confident("4k3/8/8/8/3P4/8/PPPPPPPP/4K3")
    set_probs(probs, "d4", P=0.5, B=0.45)        # 9th pawn, and the least sure one
    board, changes = repair(probs)
    assert placement(board) == "4k3/8/8/8/3B4/8/PPPPPPPP/4K3"
    assert changes == [{"square": "d4", "from": "P", "to": "B", "reason": "too_many_pawns"}]


def test_no_kings_at_all():
    probs = confident("8/8/8/8/8/8/8/8")
    set_probs(probs, "g1", empty=0.7, K=0.3)
    set_probs(probs, "g8", empty=0.7, k=0.3)
    board, _ = repair(probs)
    assert placement(board) == "6k1/8/8/8/8/8/8/6K1"


@pytest.mark.parametrize("seed", range(5))
def test_result_is_always_legal_enough(seed):
    import random
    rng = random.Random(seed)
    probs = [{r: rng.random() for r in READINGS} for _ in range(64)]
    board, _ = repair(probs)
    b = chess.Board(placement(board) + " w - - 0 1")
    assert len(b.pieces(chess.KING, chess.WHITE)) == 1 and len(b.pieces(chess.KING, chess.BLACK)) == 1
    assert chess.square_distance(b.king(chess.WHITE), b.king(chess.BLACK)) > 1
    assert not b.pieces(chess.PAWN, chess.WHITE) & chess.BB_BACKRANKS and not b.pieces(chess.PAWN, chess.BLACK) & chess.BB_BACKRANKS
    assert len(b.pieces(chess.PAWN, chess.WHITE)) <= 8 and len(b.pieces(chess.PAWN, chess.BLACK)) <= 8
