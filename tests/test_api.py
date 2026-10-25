import shutil

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
needs_engine = pytest.mark.skipif(
    shutil.which(config.STOCKFISH_PATH) is None, reason="Stockfish not installed"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@needs_engine
def test_start_position(client):
    r = client.get("/move", params={"fen": START, "depth": 8})
    assert r.status_code == 200
    body = r.json()
    assert len(body["best"]) in (4, 5)
    assert isinstance(body["eval"], float)
    assert body["fen"] == START


@needs_engine
def test_mate_in_one(client):
    # Scholar's-mate setup: Qxf7#
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
    body = client.get("/move", params={"fen": fen, "depth": 6}).json()
    assert body["best"] == "f3f7"
    assert body["mate"] == 1 and body["eval"] is None


def test_invalid_fen(client):
    r = client.get("/move", params={"fen": "not a fen"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_fen"


def test_illegal_position(client):
    r = client.get("/move", params={"fen": "8/8/8/8/8/8/8/8 w - - 0 1"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "illegal_position"
    assert "no king" in body["detail"]


def test_game_over(client):
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"  # fool's mate
    r = client.get("/move", params={"fen": fen})
    assert r.status_code == 422
    assert r.json()["error"] == "game_over"


def test_depth_out_of_range(client):
    r = client.get("/move", params={"fen": START, "depth": 999})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"


# --- POST /move (photo upload) ---

IMAGE = b"\xff\xd8\xff\xe0fake-jpeg-bytes-SECRET"


@pytest.fixture
def fake_recognizer(monkeypatch):
    from app.recognize import Recognition

    def use(placement, confidence=0.9):
        rec = Recognition(placement=placement, confidence=confidence, squares={}, uncertain=[],
                          repairs=[], alternatives={}, corners=[[0, 0], [8, 0], [8, 8], [0, 8]])
        monkeypatch.setattr("app.main.recognize", lambda image: (rec, (8, 8)))
    return use


@pytest.fixture
def no_disk(monkeypatch):
    """Fail the test if anything tries to create a temporary file."""
    import tempfile

    def boom(*a, **k):
        raise AssertionError("upload touched the disk")
    for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, boom)
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", boom)


def post(client, data=None, image=IMAGE):
    # Without an image, send some other file so the request is still multipart.
    files = {"image": ("board.jpg", image, "image/jpeg")} if image is not None else {"other": ("x", b"x")}
    return client.post("/move", data={"turn": "w"} if data is None else data, files=files)


def test_post_without_model(client, no_disk, monkeypatch):
    monkeypatch.setattr("app.config.MODEL_PATH", "/nonexistent/model.onnx")
    monkeypatch.setattr("app.recognize._recognizer", None)
    r = post(client)
    assert r.status_code == 501
    assert r.json()["error"] == "recognition_unavailable"


@needs_engine
def test_post_success(client, fake_recognizer, no_disk, caplog):
    fake_recognizer("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR", 0.87)
    big = IMAGE + b"x" * (3 * 1024 * 1024)  # bigger than Starlette's 1 MB spool threshold
    with caplog.at_level("DEBUG"):
        r = post(client, {"turn": "b", "castling": "KQkq", "depth": "6"}, image=big)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fen"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    assert body["confidence"] == 0.87
    assert body["best"]
    assert "SECRET" not in caplog.text


def test_post_illegal_position_returns_detection(client, fake_recognizer):
    fake_recognizer("8/8/8/8/8/8/8/8", 0.4)
    r = post(client)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "illegal_position"
    assert body["detected"] == "8/8/8/8/8/8/8/8" and body["confidence"] == 0.4
    assert body["fen"] == "8/8/8/8/8/8/8/8 w - - 0 1"


def test_post_impossible_castling(client, fake_recognizer):
    fake_recognizer("4k3/8/8/8/8/8/8/4K3")
    r = post(client, {"turn": "w", "castling": "K"})
    assert r.status_code == 422
    assert "castling" in r.json()["detail"]


def test_post_en_passant(client, fake_recognizer):
    fake_recognizer("4k3/8/8/3pP3/8/8/8/4K3")
    r = post(client, {"turn": "w", "ep": "d", "depth": "4"})
    if shutil.which(config.STOCKFISH_PATH):
        assert r.json()["fen"] == "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"


@pytest.mark.parametrize("data", [{}, {"turn": "x"}, {"turn": "w", "castling": "QK"},
                                  {"turn": "w", "ep": "z"}, {"turn": "w", "depth": "99"}])
def test_post_bad_fields(client, data):
    r = post(client, data)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"


def test_post_missing_image(client):
    r = post(client, image=None)
    assert r.status_code == 422
    assert "image" in r.json()["detail"]


def test_post_too_large(client, no_disk):
    r = post(client, image=b"x" * (config.MAX_UPLOAD_BYTES + 1))
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"


def test_post_not_multipart(client):
    r = client.post("/move", json={"turn": "w"})
    assert r.status_code == 415


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Best Move" in r.text


@needs_engine
@pytest.mark.skipif(not __import__("os").path.isfile(config.MODEL_PATH), reason="model not downloaded")
def test_post_real_photo(client, no_disk):
    from pathlib import Path

    data = Path(__file__).parent / "data"
    r = post(client, {"turn": "w", "depth": "6"}, image=(data / "board.jpg").read_bytes())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected"] == (data / "board.fen").read_text().strip()
    assert body["best"] and body["confidence"] >= 0.8


def test_post_garbage_image(client, no_disk):
    import os
    if not os.path.isfile(config.MODEL_PATH):
        pytest.skip("model not downloaded")
    r = post(client)
    assert r.status_code == 415
    assert r.json()["error"] == "invalid_image"


def test_post_no_board_in_photo(client, no_disk):
    import os

    import cv2
    import numpy as np
    if not os.path.isfile(config.MODEL_PATH):
        pytest.skip("model not downloaded")
    blank = cv2.imencode(".png", np.full((300, 400, 3), 128, np.uint8))[1].tobytes()
    r = post(client, image=blank)
    assert r.status_code == 422
    assert r.json()["error"] == "board_not_found"
