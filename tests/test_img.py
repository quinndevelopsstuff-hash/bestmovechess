import builtins
import io
import os
import shutil
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n"
ITALIAN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
needs_engine = pytest.mark.skipif(shutil.which(config.STOCKFISH_PATH) is None, reason="Stockfish not installed")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_files(monkeypatch):
    """Fail if the code under test opens any file from Python (fonts are opened by cairo in C)."""
    def boom(*a, **k):
        raise AssertionError(f"opened a file: {a[:1]}")
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(io, "open", boom)
    monkeypatch.setattr(os, "open", boom)


def test_png_with_move(client, no_files):
    r = client.get("/img", params={"fen": ITALIAN, "best": "f1b5", "eval": "0.45"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(PNG)


def test_svg_caption_and_orientation(client):
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    r = client.get("/img", params={"fen": fen, "best": "c7c5", "mate": "-3", "format": "svg"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "Best: c5" in r.text and "Mate in 3 for Black" in r.text and "Black to move" in r.text


def test_turn_overrides_side_to_move(client):
    r = client.get("/img", params={"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR", "turn": "b",
                                   "best": "e7e5", "format": "svg"})
    assert r.status_code == 200 and "Black to move" in r.text


def test_checkmate_without_move(client):
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    r = client.get("/img", params={"fen": fen, "format": "svg"})
    assert r.status_code == 200 and "Checkmate: Black wins." in r.text


def test_move_given_for_checkmate(client):
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    r = client.get("/img", params={"fen": fen, "best": "e2e4", "format": "svg"})
    assert r.status_code == 422 and "No legal moves" in r.text and "Checkmate: Black wins." in r.text


def test_zero_eval_has_no_sign(client):
    r = client.get("/img", params={"fen": ITALIAN, "best": "f1b5", "eval": "0", "format": "svg"})
    assert "Best: Bb5  ·  0.00" in r.text


@needs_engine
def test_img_out_on_game_over(client, fake_recognizer):
    fake_recognizer("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR")
    body = post(client, {"turn": "w", "img_out": "true"}).json()
    assert body["error"] == "game_over"
    q = parse_qs(urlparse(body["image_url"]).query)
    assert q["code"] == ["game_over"] and q["fen"][0].startswith("rnb1kbnr/")


@pytest.mark.parametrize("params, status, text", [
    ({"fen": "not a fen"}, 400, "Invalid FEN"),
    ({"fen": "8/8/8/8/8/8/8/K7 w - - 0 1"}, 422, "Missing king"),
    ({"fen": ITALIAN, "best": "e1e3"}, 422, "Illegal move"),
    ({"fen": ITALIAN, "best": "zz99"}, 422, "Illegal move"),
    ({"fen": ITALIAN, "size": "5000"}, 422, "Bad request"),
    ({"fen": ITALIAN, "eval": "nan"}, 422, "Bad request"),
    ({"fen": "x" * 500}, 422, "Bad request"),
])
def test_bad_input_gets_error_image(client, params, status, text):
    r = client.get("/img", params={**params, "format": "svg"})
    assert r.status_code == status and r.headers["content-type"].startswith("image/svg+xml")
    assert text in r.text


def test_svg_fallback_without_cairo(client, monkeypatch):
    monkeypatch.setattr("app.render.cairosvg", None)
    r = client.get("/img", params={"fen": ITALIAN, "best": "f1b5"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


def test_bad_input_png_by_default(client):
    r = client.get("/img", params={"fen": "nope"})
    assert r.status_code == 400 and r.content.startswith(PNG)


def test_error_image_highlights_position(client, no_files):
    r = client.get("/img/error", params={"code": "pawn_on_back_rank", "fen": "4k2P/8/8/8/8/8/8/4K3 w - - 0 1",
                                         "format": "svg"})
    assert r.status_code == 200 and "Pawn on back rank" in r.text
    assert "#e5484d" in r.text   # the offending square is filled


def test_error_image_never_draws_request_text(client):
    r = client.get("/img/error", params={"code": "<script>alert(1)</script>", "format": "svg"})
    assert r.status_code == 200 and "Something went wrong" in r.text and "script" not in r.text


# --- POST /move img_out ---

@pytest.fixture
def fake_recognizer(monkeypatch):
    from app.recognize import Recognition

    def use(placement):
        rec = Recognition(placement=placement, confidence=0.9, squares={}, uncertain=[], repairs=[],
                          alternatives={}, corners=[[0, 0], [8, 0], [8, 8], [0, 8]])
        monkeypatch.setattr("app.main.recognize", lambda image, source="auto", flip=None: (rec, (8, 8)))
    return use


def post(client, data, headers=None):
    return client.post("/move", data=data, files={"image": ("b.jpg", b"x", "image/jpeg")}, headers=headers or {})


@needs_engine
def test_img_out_false_is_unchanged(client, fake_recognizer):
    fake_recognizer("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR")
    assert "image_url" not in post(client, {"turn": "b", "depth": "4"}).json()
    assert "image_url" not in post(client, {"turn": "b", "depth": "4", "img_out": "false"}).json()


@needs_engine
def test_img_out_uses_forwarded_headers(client, fake_recognizer):
    fake_recognizer("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR")
    body = post(client, {"turn": "b", "depth": "4", "img_out": "true"},
                {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "pi.example.ts.net"}).json()
    url = urlparse(body["image_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "pi.example.ts.net", "/img")
    q = parse_qs(url.query)
    assert q["fen"] == [body["fen"]] and q["best"] == [body["best"]]
    r = client.get(f"/img?{url.query}")   # the link really renders
    assert r.status_code == 200 and r.content.startswith(PNG)


@needs_engine
def test_img_out_prefers_public_base_url(client, fake_recognizer, monkeypatch):
    fake_recognizer("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://chess.example.org/")
    body = post(client, {"turn": "b", "depth": "4", "img_out": "1"}, {"X-Forwarded-Host": "ignored"}).json()
    assert body["image_url"].startswith("https://chess.example.org/img?")


def test_img_out_on_error(client, fake_recognizer):
    fake_recognizer("8/8/8/8/8/8/8/K7")
    r = post(client, {"turn": "w", "img_out": "true"}, {"Host": "localhost:8000"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "illegal_position"
    url = urlparse(body["image_url"])
    assert url.path == "/img/error" and parse_qs(url.query)["code"] == ["missing_king"]


def test_img_out_on_error_without_board(client, monkeypatch):
    from app.recognize import BoardNotFound

    def no_board(image, source="auto", flip=None):
        raise BoardNotFound("couldn't find a chessboard in the photo")
    monkeypatch.setattr("app.main.recognize", no_board)
    body = post(client, {"turn": "w", "img_out": "true"}).json()
    assert body["error"] == "board_not_found"
    assert body["image_url"] == "http://testserver/img/error?code=board_not_found"


def test_img_out_bad_value(client):
    r = post(client, {"turn": "w", "img_out": "maybe"})
    assert r.status_code == 422 and "img_out" in r.json()["detail"]


def test_error_image_marks_uncertain_squares(client):
    params = {"code": "missing_king", "fen": "8/8/8/8/8/8/8/K7 w - - 0 1", "uncertain": "e8,d8", "format": "svg"}
    r = client.get("/img/error", params=params)
    assert r.status_code == 200 and "#f5a524" in r.text and "hard to read" in r.text
    r = client.get("/img/error", params={**params, "uncertain": "e9,<x>"})   # malformed: ignored
    assert r.status_code == 200 and "#f5a524" not in r.text


def test_img_out_error_url_carries_uncertain(client, monkeypatch):
    from app.recognize import Recognition
    rec = Recognition(placement="8/8/8/8/8/8/8/K7", confidence=0.2, squares={}, uncertain=["e8", "d8"],
                      repairs=[], alternatives={}, corners=[[0, 0], [8, 0], [8, 8], [0, 8]])
    monkeypatch.setattr("app.main.recognize", lambda image, source="auto", flip=None: (rec, (8, 8)))
    body = post(client, {"turn": "w", "img_out": "true"}).json()
    q = parse_qs(urlparse(body["image_url"]).query)
    assert q["code"] == ["missing_king"] and q["uncertain"] == ["e8,d8"]
