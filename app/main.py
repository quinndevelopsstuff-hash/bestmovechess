"""bestmovechess: one FastAPI app serving the web page and the move API."""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import re

import chess
from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .engine import Engine, EngineUnavailable
from .recognize import BoardNotFound, InvalidImage, RecognitionUnavailable, recognize
from .upload import UploadError, read_form

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bestmovechess")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class APIError(Exception):
    def __init__(self, status: int, error: str, detail: str, extra: dict | None = None) -> None:
        self.status, self.error, self.detail, self.extra = status, error, detail, extra or {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = Engine()
    try:
        await engine.start()
    except EngineUnavailable as exc:
        # Keep serving (health check and errors still work); retry on first request.
        log.error("%s", exc)
    app.state.engine = engine
    yield
    await engine.stop()


app = FastAPI(title="bestmovechess", lifespan=lifespan)


# --- logging: method, path, status, timing only. Never query strings or bodies. ---

@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        ms = (time.perf_counter() - start) * 1000
        log.info("%s %s %d %.0fms", request.method, request.url.path, status, ms)


# --- errors: always JSON {"error": code, "detail": message} ---

@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse({"error": exc.error, "detail": exc.detail, **exc.extra}, status_code=exc.status)


@app.exception_handler(UploadError)
async def upload_error_handler(request: Request, exc: UploadError):
    return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("query", "body"))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return JSONResponse({"error": "invalid_request", "detail": "; ".join(parts)}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(exc.status_code, "http_error")
    return JSONResponse({"error": code, "detail": str(exc.detail)}, status_code=exc.status_code)


# --- chess helpers ---

_STATUS_MESSAGES = [
    (chess.STATUS_NO_WHITE_KING, "White has no king"),
    (chess.STATUS_NO_BLACK_KING, "Black has no king"),
    (chess.STATUS_TOO_MANY_KINGS, "too many kings"),
    (chess.STATUS_TOO_MANY_WHITE_PIECES, "White has more than 16 pieces"),
    (chess.STATUS_TOO_MANY_BLACK_PIECES, "Black has more than 16 pieces"),
    (chess.STATUS_TOO_MANY_WHITE_PAWNS, "White has more than 8 pawns"),
    (chess.STATUS_TOO_MANY_BLACK_PAWNS, "Black has more than 8 pawns"),
    (chess.STATUS_PAWNS_ON_BACKRANK, "pawns on the first or eighth rank"),
    (chess.STATUS_OPPOSITE_CHECK, "the side not to move is in check"),
    (chess.STATUS_BAD_CASTLING_RIGHTS, "castling rights don't match king/rook placement"),
    (chess.STATUS_INVALID_EP_SQUARE, "invalid en passant square"),
    (chess.STATUS_TOO_MANY_CHECKERS, "impossible check (too many checkers)"),
    (chess.STATUS_IMPOSSIBLE_CHECK, "impossible check"),
    (chess.STATUS_RACE_CHECK, "impossible check"),
]


def parse_fen(fen: str, extra: dict | None = None) -> chess.Board:
    fen = fen.strip()
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise APIError(400, "invalid_fen", f"could not parse FEN: {exc}", extra) from None
    status = board.status()
    if status != chess.STATUS_VALID:
        problems = list(dict.fromkeys(msg for flag, msg in _STATUS_MESSAGES if status & flag))
        raise APIError(422, "illegal_position", "; ".join(problems) or "illegal position", extra)
    return board


async def best_move(request: Request, board: chess.Board, depth: int, extra: dict | None = None) -> dict:
    if board.is_checkmate():
        raise APIError(422, "game_over", "no legal moves: checkmate", extra)
    if board.is_stalemate():
        raise APIError(422, "game_over", "no legal moves: stalemate", extra)
    try:
        info = await request.app.state.engine.analyse(board, depth)
    except EngineUnavailable as exc:
        log.error("%s", exc)
        raise APIError(503, "engine_unavailable", "chess engine is not available") from None
    pv = info.get("pv") or []
    if not pv:
        raise APIError(503, "engine_unavailable", "engine returned no move")
    score = info["score"].white()
    return {
        "best": pv[0].uci(),
        "san": board.san(pv[0]),
        # Pawns from White's point of view; null when a forced mate is found (see "mate").
        "eval": None if score.is_mate() else round(score.score() / 100, 2),
        "mate": score.mate(),
        "depth": info.get("depth"),
        "fen": board.fen(),
        **(extra or {}),
    }


# --- routes ---

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/move")
async def get_move(
    request: Request,
    fen: str = Query(..., description="Position in FEN"),
    depth: int = Query(config.DEFAULT_DEPTH, ge=1, le=config.MAX_DEPTH),
):
    return await best_move(request, parse_fen(fen), depth)


_CASTLING_RE = re.compile(r"^(-|K?Q?k?q?)$")


@app.post("/move")
async def post_move(request: Request):
    """Multipart form: image (file), turn=w|b, optional castling (e.g. "KQkq" or "-"),
    optional ep (en passant file a-h), optional depth."""
    fields, files = await read_form(request, config.MAX_UPLOAD_BYTES)
    image = files.pop("image", None)
    files.clear()
    if not image:
        raise APIError(422, "invalid_request", "image: field required")

    turn = fields.get("turn", "").strip().lower()
    if turn not in ("w", "b"):
        raise APIError(422, "invalid_request", "turn: must be 'w' or 'b'")
    castling = fields.get("castling", "").strip() or "-"
    if not _CASTLING_RE.match(castling):
        raise APIError(422, "invalid_request", "castling: use a subset of 'KQkq' in that order, or '-'")
    ep = fields.get("ep", "").strip().lower()
    if ep and (len(ep) != 1 or ep not in "abcdefgh"):
        raise APIError(422, "invalid_request", "ep: must be a file letter a-h")
    ep_square = f"{ep}{'6' if turn == 'w' else '3'}" if ep else "-"
    try:
        depth = int(fields.get("depth") or config.DEFAULT_DEPTH)
    except ValueError:
        depth = 0
    if not 1 <= depth <= config.MAX_DEPTH:
        raise APIError(422, "invalid_request", f"depth: must be between 1 and {config.MAX_DEPTH}")

    try:
        rec, (width, height) = await run_in_threadpool(recognize, image)
    except RecognitionUnavailable as exc:
        raise APIError(501, "recognition_unavailable", str(exc)) from None
    except InvalidImage as exc:
        raise APIError(415, "invalid_image", str(exc)) from None
    except BoardNotFound as exc:
        raise APIError(422, "board_not_found", f"{exc}; fill the frame with the board and avoid glare") from None
    finally:
        del image

    fen = f"{rec.placement} {turn} {castling} {ep_square} 0 1"
    extra = {
        "detected": rec.placement,
        "confidence": rec.confidence,
        "squares": rec.squares,
        "uncertain": rec.uncertain,
        "repairs": rec.repairs,
        "alternatives": rec.alternatives,
        "corners": rec.corners,
        "image_size": [width, height],
    }
    return await best_move(request, parse_fen(fen, {**extra, "fen": fen}), depth, extra)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
