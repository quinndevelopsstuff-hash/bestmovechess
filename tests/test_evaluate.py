from collections import Counter

import pytest

from app import evaluate

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


def test_expand_and_squares():
    cells = evaluate.expand(START + " w KQkq - 0 1")
    assert len(cells) == 64 and cells[0] == "r" and cells[63] == "R" and cells[20] == "."
    assert evaluate.SQUARES[0] == "a8" and evaluate.SQUARES[63] == "h1"


@pytest.mark.parametrize("bad", ["8/8/8", "9/8/8/8/8/8/8/8", "x7/8/8/8/8/8/8/8"])
def test_expand_rejects_bad_placements(bad):
    with pytest.raises(ValueError):
        evaluate.expand(bad)


def test_compare_and_confusion():
    got = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBN1"   # h1 rook missed
    result = evaluate.compare(START, got)
    wrong = [(sq, t, p) for sq, t, p in result if t != p]
    assert wrong == [("h1", "R", ".")]
    pairs = Counter((t, p) for _, t, p in result)
    table = evaluate.confusion_table(pairs)
    assert "      R" in table and "50%" in table   # 1 of 2 white rooks right
    assert "rook       3/4" in evaluate.by_piece_type(pairs)


def test_board_not_found_counts_every_square_wrong():
    result = evaluate.compare(START, None)
    assert all(p == evaluate.NOT_FOUND for _, _, p in result)
    assert "?" in evaluate.confusion_table(Counter((t, p) for _, t, p in result))


def test_load_fixtures(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "a.fen").write_text(START + "\n")
    (tmp_path / "no-sidecar.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignored")
    assert evaluate.load_fixtures(tmp_path) == [("a", tmp_path / "a.png", START)]
