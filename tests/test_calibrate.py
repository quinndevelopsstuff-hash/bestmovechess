"""Calibration, tested with a second piece set (the Noto outlines the web page ships in
static/pieces.js, OFL) drawn at test time on a different board theme, so the built-in
python-chess templates don't already know it."""

import random
import re
from pathlib import Path

import chess
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import calibrate, config, render
from app.main import app
from app.recognize import recognize
from app.vision import profiles
from app.vision import screenshot as ss

pytestmark = pytest.mark.skipif(render.cairosvg is None, reason="needs Cairo to render boards")
PATHS = dict(re.findall(r"^\s+([kqrbnp]): \"([^\"]+)\"", (Path(__file__).parent.parent / "static" / "pieces.js").read_text(), re.M))
START = chess.STARTING_BOARD_FEN


def noto_board(placement: str, square: int = 90, highlight=(), flipped=False, labels=False) -> np.ndarray:
    """A board in a blue theme with Noto pieces, inside a dark phone screen. labels=True
    draws chess.com-style coordinates inside the edge squares."""
    rows = []
    board = chess.BaseBoard(placement)
    for sq in chess.SQUARES:
        f, r = chess.square_file(sq), chess.square_rank(sq)
        x, y = (7 - f, r) if flipped else (f, 7 - r)
        colour = "#cdd26a" if chess.square_name(sq) in highlight else ("#dee3e6" if (f + r) % 2 else "#8ca2ad")
        rows.append(f'<rect x="{x * square}" y="{y * square}" width="{square}" height="{square}" fill="{colour}"/>')
        piece = board.piece_at(sq)
        if piece:
            fill, stroke, width = ("#fbf8f1", "#2b2420", 3.5) if piece.color else ("#1c1a18", "#f3ead8", 1.5)
            d, at = PATHS[piece.symbol().lower()], f'transform="translate({x * square} {y * square}) scale({square / 100})"'
            # Outline first, then the fill on top (Cairo ignores paint-order).
            rows.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{width}" stroke-linejoin="round" {at}/>')
            rows.append(f'<path d="{d}" fill="{fill}" {at}/>')
    size = 8 * square
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">{"".join(rows)}</svg>'
    rendered = cv2.imdecode(np.frombuffer(render.cairosvg.svg2png(bytestring=svg.encode()), np.uint8), cv2.IMREAD_COLOR)
    if labels:
        scale, thick = square / 160, max(1, square // 45)
        for i in range(8):   # rank digits in the left column, file letters along the bottom row
            rank = str(i + 1 if flipped else 8 - i)
            colour = (98, 117, 128) if i % 2 == 0 else (230, 227, 222)
            cv2.putText(rendered, rank, (int(square * 0.06), int(i * square + square * 0.28)),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thick, cv2.LINE_AA)
            letter = "hgfedcba"[i] if flipped else "abcdefgh"[i]
            colour = (230, 227, 222) if i % 2 == 0 else (98, 117, 128)
            cv2.putText(rendered, letter, (int((i + 1) * square - square * 0.25), int(8 * square - square * 0.08)),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thick, cv2.LINE_AA)
    w = int(size / 0.95)
    screen = np.full((int(w * 2.1), w, 3), (40, 38, 36), np.uint8)
    top, left = int(screen.shape[0] * 0.28), (w - size) // 2
    screen[top: top + size, left: left + size] = rendered
    cv2.putText(screen, "You vs. Bot", (24, top - 40), cv2.FONT_HERSHEY_SIMPLEX, w / 700, (220, 220, 220), 2)
    return screen


def png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


def random_position(seed: int) -> tuple[str, list[str]]:
    rng = random.Random(seed)
    board = chess.Board()
    move = None
    for _ in range(rng.randint(6, 60)):
        moves = list(board.legal_moves)
        if not moves:
            break
        move = rng.choice(moves)
        board.push(move)
    return board.board_fen(), [chess.square_name(move.from_square), chess.square_name(move.to_square)] if move else []


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILES_DIR", str(tmp_path))
    return tmp_path



def test_calibration_learns_a_new_piece_set(profile_dir):
    positions = [random_position(seed) for seed in range(6)]
    shots = [png(noto_board(p, square=random.Random(i).randint(50, 120), highlight=h)) for i, (p, h) in enumerate(positions)]

    def score() -> int:
        right = 0
        for (placement, _), data in zip(positions, shots):
            rec, _ = recognize(data, "screenshot")
            right += sum(a == b for a, b in zip(ss.expand(rec.placement), ss.expand(placement)))
        return right

    before = score()
    result = calibrate.calibrate(png(noto_board(START, square=100)), "noto-blue")
    assert result["profile"] == "noto-blue" and result["profiles"] == ["noto-blue"]
    after = score()
    assert after == 64 * len(positions), f"calibrated: {after}/{64 * len(positions)} (built-in only: {before})"
    assert before < after


def test_profile_holds_only_small_templates(profile_dir):
    calibrate.calibrate(png(noto_board(START)), "noto")
    path = profile_dir / "noto.npz"
    assert path.stat().st_size < 64 * 1024
    with np.load(path, allow_pickle=False) as data:
        for key in data.files:
            assert data[key].size <= ss.T * ss.T, key   # no array bigger than one template
    assert [p.name for p in profiles.all_profiles()][:1] == ["noto"]


@pytest.mark.parametrize("make, code", [
    (lambda: noto_board("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR"), "wrong_position"),
    (lambda: np.full((1500, 700, 3), 40, np.uint8), "board_not_found"),
])
def test_rejects_bad_calibration_screenshots(profile_dir, make, code):
    with pytest.raises(calibrate.CalibrationError) as err:
        calibrate.calibrate(png(make()), "x")
    assert err.value.code == code
    assert not list(profile_dir.iterdir())


def test_calibrates_from_blacks_side(profile_dir):
    result = calibrate.calibrate(png(noto_board(START, flipped=True)), "noto-flipped")
    assert result["flipped"] is True
    placement, highlight = random_position(9)
    rec, _ = recognize(png(noto_board(placement, highlight=highlight)), "screenshot")
    assert rec.profile == "noto-flipped" and rec.placement == placement


def test_other_known_positions_can_calibrate(profile_dir):
    placement = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR"
    calibrate.calibrate(png(noto_board(placement)), "from-e4e5", placement)
    assert (profile_dir / "from-e4e5.npz").exists()


# --- API ---

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def post(client, name, image, **extra):
    return client.post("/calibrate", data={"name": name, **extra}, files={"image": ("s.png", image, "image/png")})


def test_calibrate_endpoint(client, profile_dir, monkeypatch):
    start = png(noto_board(START))
    r = post(client, "Noto", start)
    assert r.status_code == 200 and r.json()["profile"] == "noto"
    assert client.get("/profiles").json() == {"profiles": ["noto"], "builtin": True}
    r = post(client, "noto", start)
    assert r.status_code == 409 and r.json()["error"] == "profile_exists"
    assert post(client, "noto", start, replace="true").status_code == 200
    assert post(client, "../etc", start).json()["error"] == "invalid_request"
    assert post(client, "x", png(noto_board(START, flipped=True))).json()["flipped"] is True
    monkeypatch.setattr(config, "MAX_PROFILES", 2)
    r = post(client, "second", start)
    assert r.status_code == 422 and "MAX_PROFILES" in r.json()["detail"]
    monkeypatch.setattr(config, "CALIBRATION_ENABLED", False)
    assert post(client, "third", start).status_code == 403
    assert sorted(p.name for p in profile_dir.iterdir()) == ["noto.npz", "x.npz"]


def test_calibrate_upload_stays_in_memory(client, profile_dir, monkeypatch):
    import tempfile

    def boom(*a, **k):
        raise AssertionError("upload touched the disk")
    for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, boom)
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", boom)
    assert post(client, "mem", png(noto_board(START))).status_code == 200
    assert [p.name for p in profile_dir.iterdir()] == ["mem.npz"]   # the templates, nothing else
