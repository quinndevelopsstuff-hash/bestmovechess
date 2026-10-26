"""Move models: the `model` / `elo` parameters, the /models list, and the engine pool.

Maia needs lc0 and weights, which CI doesn't have. A small fake `lc0` that hands the UCI
conversation to Stockfish stands in for it, so the command line, the weights check and the
one-node search are all exercised for real.
"""

import os
import shutil
import stat

import pytest
from fastapi.testclient import TestClient

from app import config, models
from app.main import app

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
needs_engine = pytest.mark.skipif(shutil.which(config.STOCKFISH_PATH) is None, reason="Stockfish not installed")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_maia(tmp_path, monkeypatch):
    """A fake lc0 (Stockfish in disguise) plus a maia-1500 weights file."""
    stockfish = shutil.which(config.STOCKFISH_PATH)
    lc0 = tmp_path / "lc0"
    lc0.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/lc0-args"\nexec {stockfish}\n')
    lc0.chmod(lc0.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "maia-1500.pb.gz").write_bytes(b"weights")
    monkeypatch.setattr(config, "LC0_PATH", str(lc0))
    monkeypatch.setattr(config, "MAIA_DIR", str(tmp_path))
    return tmp_path


def test_models_list_marks_what_is_installed(client):
    body = client.get("/models").json()
    names = {m["name"]: m for m in body["models"]}
    assert body["default"] == "stockfish" and "maia-1100" in names and "maia-1900" in names
    assert names["stockfish"]["supports_elo"] and names["maia-1500"]["human_like"]
    assert names["maia-1500"]["installed"] is False and "lc0" in names["maia-1500"]["unavailable"]


def test_unknown_and_uninstalled_models(client):
    r = client.get("/move", params={"fen": START, "model": "alphazero"})
    assert r.status_code == 422 and r.json()["error"] == "unknown_model"
    r = client.get("/move", params={"fen": START, "model": "maia-1500"})
    assert r.status_code == 501 and r.json()["error"] == "model_unavailable" and "lc0" in r.json()["detail"]


@pytest.mark.parametrize("params", [{"elo": "900"}, {"elo": "9000"}, {"elo": "abc"}])
def test_bad_elo(client, params):
    r = client.get("/move", params={"fen": START, **params})
    assert r.status_code == 422 and "elo" in r.json()["detail"]


@needs_engine
def test_default_model_is_reported(client):
    body = client.get("/move", params={"fen": START, "depth": 6}).json()
    assert body["model"] == "stockfish" and "elo" not in body and body["best"]


@needs_engine
def test_stockfish_at_an_elo(client):
    body = client.get("/move", params={"fen": START, "depth": 6, "elo": "1500", "model": "Stockfish"}).json()
    assert body["model"] == "stockfish" and body["elo"] == 1500 and len(body["best"]) in (4, 5)


@needs_engine
def test_the_elo_setting_does_not_stick_to_later_requests(client):
    client.get("/move", params={"fen": START, "depth": 4, "elo": "1400"})
    # Full strength again: a mate in one must be found, which a 1400-limited engine may miss.
    mate = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
    assert client.get("/move", params={"fen": mate, "depth": 6}).json()["best"] == "f3f7"


@needs_engine
def test_maia_model_runs_lc0_with_its_weights_and_one_node(client, fake_maia):
    r = client.get("/move", params={"fen": START, "model": "maia-1500", "depth": 25})
    body = r.json()
    assert r.status_code == 200 and body["model"] == "maia-1500" and body["best"]
    assert f"--weights={fake_maia}/maia-1500.pb.gz" in (fake_maia / "lc0-args").read_text()
    assert body["depth"] in (None, 0, 1) or body["depth"] < 5   # one node, not a depth-25 search
    assert {m["name"]: m["installed"] for m in client.get("/models").json()["models"]}["maia-1500"]


@needs_engine
def test_elo_is_refused_for_models_with_their_own_level(client, fake_maia):
    r = client.get("/move", params={"fen": START, "model": "maia-1500", "elo": "1500"})
    assert r.status_code == 422 and "maia-1500" in r.json()["detail"]


@needs_engine
def test_custom_uci_engine(client, monkeypatch):
    sf = shutil.which(config.STOCKFISH_PATH)
    monkeypatch.setattr(config, "UCI_ENGINES", '{"sf-novice": {"path": "%s", "label": "Novice", '
                        '"options": {"Skill Level": 2}, "human_like": true}, "bad name": {"path": "x"}}' % sf)
    listed = {m["name"]: m for m in client.get("/models").json()["models"]}
    assert listed["sf-novice"]["installed"] and listed["sf-novice"]["human_like"] and "bad name" not in listed
    body = client.get("/move", params={"fen": START, "model": "sf-novice", "depth": 4}).json()
    assert body["model"] == "sf-novice" and body["best"]


@needs_engine
def test_idle_engines_are_stopped_beyond_the_limit(client, fake_maia, monkeypatch):
    for elo in (1100, 1700):
        (fake_maia / f"maia-{elo}.pb.gz").write_bytes(b"w")
    monkeypatch.setattr(config, "MAX_LIVE_ENGINES", 1)
    for name in ("maia-1100", "maia-1500", "maia-1700"):
        assert client.get("/move", params={"fen": START, "model": name}).status_code == 200
    live = [n for n in client.app.state.engine._procs if n != "stockfish"]
    assert live == ["maia-1700"]


def test_post_move_validates_model_before_reading_the_image(client):
    files = {"image": ("b.png", b"not even an image", "image/png")}
    r = client.post("/move", data={"turn": "w", "model": "nope"}, files=files)
    assert r.status_code == 422 and r.json()["error"] == "unknown_model"
    r = client.post("/move", data={"turn": "w", "elo": "5"}, files=files)
    assert r.status_code == 422 and "elo" in r.json()["detail"]


def test_bad_custom_engine_json_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "UCI_ENGINES", "{not json")
    assert set(models.all_models()) >= {"stockfish", "maia-1100"}
