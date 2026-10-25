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
