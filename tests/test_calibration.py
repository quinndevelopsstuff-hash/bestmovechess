"""Calibration profiles: piece shapes cut from a start-position screenshot. Tested on
synthetic screenshots rendered from python-chess's SVGs, so no third-party artwork is needed."""

import chess
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, render
from app import recognize as rz
from app.main import app
from app.recognize import recognize
from app.vision import calibration as cal
from app.vision import screenshot as ss

from test_screenshot import phone_screenshot, png, random_position

pytestmark = [pytest.mark.skipif(render.cairosvg is None, reason="needs Cairo to render boards"),
              pytest.mark.skipif(not ss.available(), reason="chessfen needs Python 3.13+")]


def rgb(img):
    return np.ascontiguousarray(img[:, :, ::-1])


def start_rgb(flipped=False):
    return rgb(phone_screenshot(chess.Board(), 640, flipped=flipped))


def test_calibrate_reads_back_the_start_position_and_round_trips(tmp_path):
    profile = cal.calibrate(start_rgb(), "mine")
    assert len(profile.shapes) == 12 and all(m.any() for m in profile.shapes.values())
    cal.save(profile, tmp_path)
    loaded = cal.load("mine", tmp_path)
    assert all(np.array_equal(loaded.shapes[k], profile.shapes[k]) for k in profile.shapes)
    assert cal.names(tmp_path) == ["mine"]
    assert cal.delete("mine", tmp_path) and not cal.delete("mine", tmp_path)
    with pytest.raises(KeyError):
        cal.load("mine", tmp_path)


def test_calibrate_from_black_side_screenshot():
    # White at the top: the picture is turned round first, so the profile still comes out right.
    profile = cal.calibrate(start_rgb(flipped=True), "mine")
    with cal.using(profile):
        board, _ = random_position(3)
        rec, _ = recognize(png(phone_screenshot(board, 640)), "screenshot", profile="builtin")
    assert rec.placement == board.board_fen()


def test_calibrate_rejects_a_mid_game_screenshot():
    board, _ = random_position(2)
    with pytest.raises(cal.CalibrationError, match="start position"):
        cal.calibrate(rgb(phone_screenshot(board, 640)), "mine")


def test_calibrate_rejects_something_that_is_not_a_board():
    with pytest.raises(cal.CalibrationError):
        cal.calibrate(np.full((400, 300, 3), 128, np.uint8), "mine")


@pytest.mark.parametrize("name", ["", "Bad Name", "../x", "builtin", "a" * 41, "-x"])
def test_bad_profile_names(name):
    assert not cal.valid_name(name)
    with pytest.raises(cal.CalibrationError):
        cal.calibrate(start_rgb(), name)


def test_active_profile_really_changes_what_is_matched():
    """Swap the white knight and bishop shapes: the start position must then read with
    knights and bishops exchanged. Proves the patch point is live and used per context."""
    p = cal.calibrate(start_rgb(), "swap")
    s = dict(p.shapes)
    s[(chess.WHITE, chess.KNIGHT)], s[(chess.WHITE, chess.BISHOP)] = s[(chess.WHITE, chess.BISHOP)], s[(chess.WHITE, chess.KNIGHT)]
    swapped = cal.Profile("swap", s)
    img = rgb(phone_screenshot(chess.Board(), 640))
    import chessfen
    with cal.using(swapped):
        r = chessfen.recognize(img, orientation=chessfen.Orientation.WHITE)
    assert r.board.board_fen() == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RBNQKNBR"
    r = chessfen.recognize(img, orientation=chessfen.Orientation.WHITE)   # outside the block: stock again
    assert r.board.board_fen() == chess.STARTING_BOARD_FEN


def test_several_profiles_read_the_board_once(monkeypatch):
    cal.save(cal.calibrate(start_rgb(), "one"), config.PROFILE_DIR)
    cal.save(cal.calibrate(start_rgb(), "two"), config.PROFILE_DIR)
    calls = []
    orig = cal._STOCK["square"]
    monkeypatch.setitem(cal._STOCK, "square", lambda cell: calls.append(1) or orig(cell))
    board, last = random_position(9)
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "screenshot")
    assert rec.placement == board.board_fen() and rec.profile in ("one", "two", "builtin")
    assert 0 < len(calls) <= 64   # at most one read per distinct square, not one per candidate profile


def test_auto_picks_a_saved_profile_and_named_profiles_are_honoured():
    cal.save(cal.calibrate(start_rgb(), "mine"), config.PROFILE_DIR)
    board, last = random_position(12)
    data = png(phone_screenshot(board, 640, last))
    rec, _ = recognize(data, "screenshot")
    assert rec.profile in ("mine", "builtin") and rec.placement == board.board_fen()
    assert recognize(data, "screenshot", profile="mine")[0].profile == "mine"
    assert recognize(data, "screenshot", profile="builtin")[0].profile == "builtin"
    with pytest.raises(rz.UnknownProfile):
        recognize(data, "screenshot", profile="nope")


def test_unreadable_profile_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "broken.npz").write_bytes(b"not an npz")
    board, last = random_position(13)
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "screenshot")
    assert rec.profile == "builtin"


def test_calibration_does_not_keep_the_screenshot(monkeypatch):
    import builtins
    import tempfile
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, mode="r", *a, **k: (opened.append((str(f), mode)) or real_open(f, mode, *a, **k)))
    for name in ("TemporaryFile", "NamedTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, lambda *a, **k: pytest.fail("touched the disk"))
    rz.calibrate(png(phone_screenshot(chess.Board(), 640)), "mine")
    written = [p for p, m in opened if any(c in m for c in "wax+")]
    assert not written, written   # only the .npz via Path.write_bytes (not builtins.open)
    files = list(__import__("pathlib").Path(config.PROFILE_DIR).iterdir())
    assert [f.name for f in files] == ["mine.npz"] and files[0].stat().st_size < 20_000


# --- HTTP ---

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_profile_endpoints(client):
    assert client.get("/profiles").json()["profiles"] == []
    r = client.post("/profiles", data={"name": "iphone"}, files={"image": ("s.png", png(phone_screenshot(chess.Board(), 640)), "image/png")})
    assert r.status_code == 200 and r.json()["profiles"] == ["iphone"]
    assert client.get("/profiles").json()["profiles"] == ["iphone"]
    assert client.delete("/profiles/iphone").json()["profiles"] == []
    assert client.delete("/profiles/iphone").status_code == 404


def test_profile_endpoint_errors(client):
    good = png(phone_screenshot(chess.Board(), 640))
    files = {"image": ("s.png", good, "image/png")}
    assert client.post("/profiles", data={"name": "Bad Name"}, files=files).status_code == 422
    assert client.post("/profiles", data={"name": "ok"}, files={"other": ("x", b"x")}).status_code == 422
    board, _ = random_position(2)
    r = client.post("/profiles", data={"name": "ok"}, files={"image": ("s.png", png(phone_screenshot(board, 640)), "image/png")})
    assert r.status_code == 422 and r.json()["error"] == "calibration_failed"
    r = client.post("/profiles", data={"name": "ok"}, files={"image": ("s.png", b"garbage", "image/png")})
    assert r.status_code == 415
    assert client.delete("/profiles/Bad..Name").status_code in (404, 422)


def test_profile_limit(client, monkeypatch):
    monkeypatch.setattr("app.main.MAX_PROFILES", 1)
    files = lambda: {"image": ("s.png", png(phone_screenshot(chess.Board(), 640)), "image/png")}   # noqa: E731
    assert client.post("/profiles", data={"name": "a"}, files=files()).status_code == 200
    assert client.post("/profiles", data={"name": "a"}, files=files()).status_code == 200   # replacing is fine
    r = client.post("/profiles", data={"name": "b"}, files=files())
    assert r.status_code == 409 and r.json()["error"] == "too_many_profiles"


def test_post_move_reports_and_accepts_profile(client, monkeypatch):
    async def no_engine(request, board, depth, extra=None, *rest):   # these tests don't need Stockfish
        return {**extra, "fen": board.fen()}
    monkeypatch.setattr("app.main.best_move", no_engine)
    client.post("/profiles", data={"name": "mine"}, files={"image": ("s.png", png(phone_screenshot(chess.Board(), 640)), "image/png")})
    board, last = random_position(14)
    files = {"image": ("b.png", png(phone_screenshot(board, 640, last)), "image/png")}
    r = client.post("/move", data={"turn": "w", "source": "screenshot", "profile": "mine", "depth": "4"}, files=files)
    assert r.json()["profile"] == "mine" and r.json()["detected"] == board.board_fen()
    r = client.post("/move", data={"turn": "w", "source": "screenshot", "profile": "ghost"}, files=files)
    assert r.status_code == 422 and r.json()["error"] == "unknown_profile"
    r = client.post("/move", data={"turn": "w", "source": "screenshot", "profile": "../x"}, files=files)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_uncalibrated_misread_carries_a_hint(client):
    board, last = random_position(14)
    files = {"image": ("b.png", png(phone_screenshot(board, 640, last)), "image/png")}
    r = client.post("/move", data={"turn": "w", "source": "screenshot", "profile": "builtin"}, files=files)
    body = r.json()
    if body.get("uncertain"):   # stock shapes on python-chess art are usually right; only assert when doubtful
        assert "Calibrate" in body["hint"]
    else:
        assert "hint" not in body


def test_bundled_profile_is_listed_and_loadable(tmp_path, monkeypatch):
    import shutil
    bundled = tmp_path / "b"
    bundled.mkdir()
    cal.save(cal.calibrate(start_rgb(), "shipped"), bundled)
    monkeypatch.setattr(cal, "BUNDLED_DIR", bundled)
    assert cal.names(config.PROFILE_DIR) == ["shipped"]
    assert cal.load("shipped", config.PROFILE_DIR).name == "shipped"
    assert not cal.delete("shipped", config.PROFILE_DIR) and (bundled / "shipped.npz").exists()
    board, last = random_position(15)
    rec, _ = recognize(png(phone_screenshot(board, 640, last)), "screenshot", profile="shipped")
    assert rec.profile == "shipped" and rec.placement == board.board_fen()
    shutil.rmtree(bundled)
