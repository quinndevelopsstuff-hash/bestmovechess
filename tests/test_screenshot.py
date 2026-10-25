"""Screenshot path (chessfen), tested on synthetic phone screenshots rendered at test time
from python-chess's board SVGs, so no third-party artwork is needed or stored."""

import random

import chess
import chess.svg
import cv2
import numpy as np
import pytest

from app import render
from app.recognize import recognize
from app.vision import screenshot as ss

pytestmark = [pytest.mark.skipif(render.cairosvg is None, reason="needs Cairo to render boards"),
              pytest.mark.skipif(not ss.available(), reason="chessfen needs Python 3.13+")]


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


@pytest.mark.parametrize("seed, board_px, coordinates", [(1, 400, False), (2, 640, False), (3, 900, True)])
def test_reads_synthetic_screenshots(seed, board_px, coordinates):
    board, last = random_position(seed)
    rec, size = recognize(png(phone_screenshot(board, board_px, last, coordinates)), "screenshot")
    assert rec.source == "screenshot" and rec.orientation == "chessfen" and not rec.flipped
    assert rec.placement == board.board_fen()
    assert len(rec.squares) == 64 and all(0 <= c <= 1 for c in rec.squares.values())
    assert set(rec.uncertain) <= set(rec.squares)


def test_auto_prefers_chessfen_for_screenshots():
    board, last = random_position(4)
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "auto")
    assert rec.source == "screenshot" and rec.placement == board.board_fen()


def test_corners_point_at_the_board():
    board, last = random_position(5)
    img = phone_screenshot(board, 720, last)
    rec, _ = recognize(png(img), "screenshot")
    top, left = int(img.shape[0] * 0.3), (img.shape[1] - 720) // 2
    (ax, ay), _, (hx, hy), _ = rec.corners   # a8, h8, h1, a1
    assert abs(ax - left) <= 3 and abs(ay - top) <= 3 and abs(hx - (left + 720)) <= 3 and abs(hy - (top + 720)) <= 3


def test_flip_field_and_chessfen_guess():
    board, last = random_position(6, plies=20)
    data = png(phone_screenshot(board, 640, last, flipped=True))
    rec, _ = recognize(data, "screenshot")                 # chessfen guesses Black is at the bottom
    assert (rec.flipped, rec.orientation, rec.placement) == (True, "chessfen", board.board_fen())
    rec, _ = recognize(data, "screenshot", flip=True)      # forced
    assert (rec.flipped, rec.orientation, rec.placement) == (True, "requested", board.board_fen())
    assert rec.corners[0][0] > rec.corners[1][0]           # a8 is now the bottom-right corner
    rec, _ = recognize(data, "screenshot", flip=False)     # forced the wrong way: the board comes out rotated
    assert rec.placement == board.transform(chess.flip_vertical).transform(chess.flip_horizontal).board_fen()


def test_reads_the_cropped_board_first(monkeypatch):
    """chessfen gets a crop around the board found by its checker pattern; the whole
    screenshot is only the fallback. Corners are reported in full-image pixels."""
    board, last = random_position(7)
    img = phone_screenshot(board, 640, last)
    real = ss.chessfen.recognize
    seen = []

    def spy(arr, **kw):
        seen.append(arr.shape[:2])
        return real(arr, **kw)
    monkeypatch.setattr(ss.chessfen, "recognize", spy)
    rec, _ = recognize(png(img), "screenshot")
    assert len(seen) == 1 and seen[0][0] < img.shape[0] / 2   # one call, on the crop
    assert rec.placement == board.board_fen()
    assert abs(rec.corners[0][1] - int(img.shape[0] * 0.3)) <= 3


def test_whole_image_when_the_crop_fails(monkeypatch):
    board, last = random_position(11)
    img = phone_screenshot(board, 640, last)
    real = ss.chessfen.recognize

    def picky(arr, **kw):
        if arr.shape[:2] != img.shape[:2]:
            raise ss.chessfen.BoardNotFoundError("crop rejected")
        return real(arr, **kw)
    monkeypatch.setattr(ss.chessfen, "recognize", picky)
    rec, _ = recognize(png(img), "screenshot")
    assert rec.placement == board.board_fen()


def test_doubtful_squares_are_kept_not_rerouted(monkeypatch):
    """A screenshot chessfen is unsure about stays with chessfen; the doubtful squares
    come back as uncertain for the correction view (no fallback to the photo model)."""
    from app import recognize as rz
    board, last = random_position(8)
    real = ss.chessfen.recognize

    def unsure(arr, **kw):
        r = real(arr, **kw)
        from chessfen.classify import SquareVerdict
        verdicts = {sq: (SquareVerdict(v.piece, v.score, 0.0, v.color_margin) if v.piece else v) for sq, v in r.verdicts.items()}
        return type(r)(board=r.board, rect=r.rect, orientation=r.orientation, verdicts=verdicts)
    monkeypatch.setattr(ss.chessfen, "recognize", unsure)
    monkeypatch.setattr(rz, "_get_recognizer", lambda: pytest.fail("photo model must not be used"))
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "auto")
    assert rec.source == "screenshot" and rec.placement == board.board_fen()
    assert len(rec.uncertain) == len(board.piece_map()) and all(rec.squares[s] < 0.6 for s in rec.uncertain)


def test_photos_skip_chessfen(monkeypatch):
    from pathlib import Path
    from app import recognize as rz
    camera = rz.Recognition(placement="8/8/8/8/8/8/8/8", confidence=0.5, squares={}, uncertain=[], repairs=[],
                            alternatives={}, corners=[[0, 0], [1, 0], [1, 1], [0, 1]])
    monkeypatch.setattr(rz, "_get_recognizer", lambda: type("M", (), {"recognize": lambda self, img: camera})())
    monkeypatch.setattr(ss.chessfen, "recognize", lambda *a, **k: pytest.fail("chessfen must not run on photos"))
    photo = (Path(__file__).parent / "data" / "board.jpg").read_bytes()
    rec, _ = recognize(photo, "auto")
    assert rec is camera


def test_unreadable_screenshot_falls_back_to_the_photo_model(monkeypatch):
    from app import recognize as rz
    board, last = random_position(12)
    camera = rz.Recognition(placement="8/8/8/8/8/8/8/8", confidence=0.5, squares={}, uncertain=[], repairs=[],
                            alternatives={}, corners=[[0, 0], [1, 0], [1, 1], [0, 1]])
    monkeypatch.setattr(rz, "_get_recognizer", lambda: type("M", (), {"recognize": lambda self, img: camera})())

    def broken(*a, **k):
        raise ss.chessfen.BoardNotFoundError("no")
    monkeypatch.setattr(ss.chessfen, "recognize", broken)
    data = png(phone_screenshot(board, 640, last))
    assert recognize(data, "auto")[0] is camera
    with pytest.raises(rz.BoardNotFound):
        recognize(data, "screenshot")


def test_screenshot_stays_in_memory(monkeypatch):
    import builtins, io, os, tempfile
    board, last = random_position(9)
    data = png(phone_screenshot(board, 640, last))
    recognize(data, "screenshot")   # warm up (chessfen renders its templates once, in memory)

    def boom(*a, **k):
        raise AssertionError(f"touched the disk: {a[:1]}")
    for mod, names in ((builtins, ("open",)), (io, ("open",)), (os, ("open",)),
                       (tempfile, ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"))):
        for name in names:
            monkeypatch.setattr(mod, name, boom)
    rec, _ = recognize(data, "screenshot")
    assert rec.placement == board.board_fen()


def test_square_confidence_matches_chessfen_doubt():
    from chessfen.classify import SquareVerdict
    knight = chess.Piece(chess.KNIGHT, chess.WHITE)
    sure = SquareVerdict(piece=knight, score=0.9, margin=0.2, color_margin=0.4)
    shaky = SquareVerdict(piece=knight, score=0.9, margin=0.01, color_margin=0.4)
    assert sure.confident and ss.square_confidence(sure) >= 0.6
    assert not shaky.confident and ss.square_confidence(shaky) < 0.6
    assert ss.square_confidence(SquareVerdict(piece=None, score=0.0, margin=1.0, color_margin=1.0)) == 1.0


def _red_badge(img, cx, cy, r):
    out = img.copy()
    cv2.circle(out, (cx, cy), r, (45, 65, 250), -1)                       # chess.com's red check badge (BGR)
    cv2.rectangle(out, (cx - r // 2, cy - 2), (cx + r // 2, cy + 2), (255, 255, 255), -1)   # white icon inside
    return out


def test_check_badge_is_painted_over_and_the_board_still_reads():
    board, last = random_position(21)
    img = phone_screenshot(board, 640, last)
    left, top = (img.shape[1] - 640) // 2, int(img.shape[0] * 0.3)
    sq = 640 // 8
    king = board.king(chess.BLACK)
    cx = left + (chess.square_file(king) + 1) * sq - 4          # top-right corner of the king's square
    cy = top + (7 - chess.square_rank(king)) * sq + 4
    badged = _red_badge(img, cx, cy, 22)
    cleaned = ss.remove_check_badges(np.ascontiguousarray(badged[:, :, ::-1]))
    assert abs(int(cleaned[cy, cx - 12, 0]) - 250) > 60 or cleaned[cy, cx - 12, 1] > 100   # no longer solid red
    rec, _ = recognize(png(badged), "screenshot")
    assert rec.placement == board.board_fen()


def test_no_red_means_no_copy():
    rgb = np.ascontiguousarray(phone_screenshot(chess.Board(), 640)[:, :, ::-1])
    assert ss.remove_check_badges(rgb) is rgb


def test_a_red_ring_is_not_mistaken_for_a_badge():
    img = phone_screenshot(chess.Board(), 640)
    cv2.circle(img, (80, 80), 40, (45, 65, 250), 4)   # outline only
    rgb = np.ascontiguousarray(img[:, :, ::-1])
    assert ss.remove_check_badges(rgb) is rgb
