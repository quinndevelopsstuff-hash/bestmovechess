"""Screenshot path, tested on synthetic phone screenshots rendered at test time from
python-chess's board SVGs (so no third-party artwork is needed or stored)."""

import random
from pathlib import Path

import chess
import chess.svg
import cv2
import numpy as np
import pytest

from app import render
from app.recognize import decode, recognize
from app.vision import screenshot as ss

pytestmark = pytest.mark.skipif(render.cairosvg is None, reason="needs Cairo to render boards")


def random_position(seed: int, plies: int = 30) -> tuple[chess.Board, chess.Move | None]:
    rng = random.Random(seed)
    board, last = chess.Board(), None
    for _ in range(plies):
        moves = list(board.legal_moves)
        if not moves:
            break
        last = rng.choice(moves)
        board.push(last)
    return board, last


def phone_screenshot(board: chess.Board, board_px: int, lastmove=None, coordinates=False, flipped=False) -> np.ndarray:
    """The board inside a dark 'app' screen with buttons and text above and below it."""
    svg = chess.svg.board(board, size=board_px, lastmove=lastmove, coordinates=coordinates,
                          orientation=chess.BLACK if flipped else chess.WHITE)
    rendered = cv2.imdecode(np.frombuffer(render.cairosvg.svg2png(bytestring=svg.encode()), np.uint8), cv2.IMREAD_COLOR)
    w = int(rendered.shape[1] / 0.96)
    h = int(w * 2.17)
    screen = np.full((h, w, 3), (31, 35, 36), np.uint8)
    top = int(h * 0.3)
    left = (w - rendered.shape[1]) // 2
    screen[top: top + rendered.shape[0], left: left + rendered.shape[1]] = rendered
    cv2.putText(screen, "Opponent (1500)", (20, top - 30), cv2.FONT_HERSHEY_SIMPLEX, w / 600, (230, 230, 230), 2)
    for i in range(4):   # bottom toolbar buttons
        x = 20 + i * (w - 40) // 4
        cv2.rectangle(screen, (x + 8, h - 160), (x + (w - 40) // 4 - 8, h - 60), (60, 60, 60), -1)
    cv2.circle(screen, (w // 2, int(h * 0.12)), int(w * 0.06), (90, 140, 200), -1)   # avatar
    return screen


def png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


@pytest.mark.parametrize("seed, board_px, coordinates", [(1, 360, False), (2, 640, False), (3, 1000, True)])
def test_reads_synthetic_screenshots(seed, board_px, coordinates):
    board, last = random_position(seed)
    rec, _ = recognize(png(phone_screenshot(board, board_px, last, coordinates)), "auto")
    assert rec.source == "screenshot"
    assert rec.placement == board.board_fen()
    assert rec.confidence > 0.5 and not rec.uncertain


def test_locates_board_and_corners():
    board, last = random_position(4)
    img = phone_screenshot(board, 720, last)
    grid = ss.locate_board(img)
    assert ss.looks_like_screenshot(grid)
    top, left = int(img.shape[0] * 0.3), (img.shape[1] - 720) // 2
    assert abs(grid.x0 - left) <= 1.5 and abs(grid.y0 - top) <= 1.5 and abs(grid.size - 90) <= 0.5


def test_jpeg_screenshot_still_detected():
    board, last = random_position(5)
    jpeg = cv2.imencode(".jpg", phone_screenshot(board, 600, last), [cv2.IMWRITE_JPEG_QUALITY, 60])[1].tobytes()
    rec, _ = recognize(jpeg, "auto")
    assert rec.source == "screenshot" and rec.placement == board.board_fen()


def test_camera_photo_is_not_a_screenshot():
    photo = (Path(__file__).parent / "data" / "board.jpg").read_bytes()
    assert not ss.looks_like_screenshot(ss.locate_board(decode(photo)))


def test_empty_and_labelled_squares():
    # Coordinate labels and highlighted squares must not be read as pieces.
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    rec, _ = recognize(png(phone_screenshot(board, 600, chess.Move.from_uci("e2e1"), coordinates=True)), "screenshot")
    assert rec.placement == "4k3/8/8/8/8/8/8/4K3"


def test_no_board_in_blank_screenshot():
    assert ss.locate_board(np.full((1600, 740, 3), 40, np.uint8)) is None


def test_repair_restores_a_misread_king(monkeypatch):
    """Make the classifier prefer 'queen' on the black king's square; repair should put the
    king back (the board would otherwise have no black king) and flag the square."""
    board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    real = ss.classify

    def unsure_king(sq, profiles, split=None):
        g = real(sq, profiles, split)
        if g.piece == "k":
            g.piece, g.confidence = "q", 0.3
            g.probs = {**g.probs, "q": 0.55, "k": 0.4}
        return g
    monkeypatch.setattr(ss, "classify", unsure_king)
    rec, _ = recognize(png(phone_screenshot(board, 600)), "screenshot")
    assert rec.placement == "4k3/8/8/8/8/8/8/R3K3"
    assert rec.repairs == [{"square": "e8", "from": "q", "to": "k", "reason": "missing_king"}]
    assert "e8" in rec.uncertain


# --- orientation ---

@pytest.mark.parametrize("seed", [21, 22, 23])
def test_detects_board_shown_from_blacks_side(seed):
    board, last = random_position(seed, plies=20)
    rec, _ = recognize(png(phone_screenshot(board, 640, last, flipped=True)), "screenshot")
    assert rec.flipped and rec.orientation == "pieces"
    assert rec.placement == board.board_fen()
    # a8 is now the screen's bottom-right corner
    assert rec.corners[0][0] > rec.corners[1][0] and rec.corners[0][1] > rec.corners[2][1]


def test_normal_board_is_not_flipped():
    board, last = random_position(24, plies=20)
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "screenshot")
    assert not rec.flipped and rec.orientation == "pieces" and rec.placement == board.board_fen()


def test_labels_decide_when_pieces_cannot(monkeypatch):
    from tests.test_calibrate import noto_board
    from app import calibrate, config
    import tempfile
    monkeypatch.setattr(config, "PROFILES_DIR", tempfile.mkdtemp())
    calibrate.calibrate(png(noto_board(chess.STARTING_BOARD_FEN)), "noto")
    endgame = "8/8/4k3/8/8/3KR3/8/8"   # no pawns: the pieces alone can't tell
    for flipped in (False, True):
        rec, _ = recognize(png(noto_board(endgame, square=110, flipped=flipped, labels=True)), "screenshot")
        assert (rec.flipped, rec.orientation, rec.placement) == (flipped, "labels", endgame)


def test_no_evidence_keeps_white_at_the_bottom():
    board = chess.Board("8/8/8/2k1K3/8/8/8/8 w - - 0 1")   # kings side by side: no clue either way
    rec, _ = recognize(png(phone_screenshot(board, 600)), "screenshot")
    assert (rec.flipped, rec.orientation) == (False, "default")


def test_flip_can_be_forced():
    board, last = random_position(25, plies=20)
    data = png(phone_screenshot(board, 600, last, flipped=True))
    rec, _ = recognize(data, "screenshot", flip=True)
    assert rec.placement == board.board_fen() and rec.orientation == "requested"
    rec, _ = recognize(data, "screenshot", flip=False)
    assert rec.placement == board.transform(chess.flip_vertical).transform(chess.flip_horizontal).board_fen()


def test_piece_orientation_rules():
    start = ss.expand(chess.STARTING_BOARD_FEN)
    assert ss.piece_orientation(start) is False and ss.piece_orientation(start[::-1]) is True
    assert ss.piece_orientation(ss.expand("8/8/8/2k1K3/8/8/8/8")) is None
    assert ss.piece_orientation(ss.expand("8/8/4k3/8/8/3K4/8/8")) is False   # White's king clearly lower
