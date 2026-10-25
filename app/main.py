"""bestmovechess: one FastAPI app serving the web page and the move API."""

import logging
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import chess
from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import calibrate as calibration
from . import config, render
from .engine import Engine, EngineUnavailable
from .recognize import SOURCES, BoardNotFound, InvalidImage, RecognitionUnavailable, recognize
from .upload import UploadError, read_form
from .vision import profiles

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
    over = {**extra, "fen": board.fen()} if extra else None
    if board.is_checkmate():
        raise APIError(422, "game_over", "no legal moves: checkmate", over)
    if board.is_stalemate():
        raise APIError(422, "game_over", "no legal moves: stalemate", over)
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


# The body is parsed by hand (to keep uploads in memory), so describe it for /docs explicitly.
_POST_MOVE_FORM = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["image", "turn"],
            "properties": {
                "image": {"type": "string", "format": "binary", "description": "Photo of the board (JPEG/PNG/WebP)"},
                "turn": {"type": "string", "enum": ["w", "b"], "description": "Side to move"},
                "castling": {"type": "string", "default": "-", "description": "Subset of KQkq, or -"},
                "ep": {"type": "string", "description": "En passant file a-h, if any"},
                "depth": {"type": "integer", "minimum": 1, "maximum": config.MAX_DEPTH},
                "source": {"type": "string", "enum": list(SOURCES), "default": "auto",
                           "description": "screenshot (flat digital board), camera (photo), or auto-detect"},
                "img_out": {"type": "boolean", "default": False,
                            "description": "Also return image_url, a rendered diagram of the result"},
            },
        }}},
    }
}


@app.post("/move", openapi_extra=_POST_MOVE_FORM)
async def post_move(request: Request):
    """Multipart form: image (file), turn=w|b, optional castling (e.g. "KQkq" or "-"),
    optional ep (en passant file a-h), optional depth."""
    fields, files = await read_form(request, config.MAX_UPLOAD_BYTES)
    image = files.pop("image", None)
    files.clear()
    img_out = fields.get("img_out", "").strip().lower()
    if img_out not in ("", "true", "false", "1", "0", "yes", "no", "on", "off"):
        raise APIError(422, "invalid_request", "img_out: must be true or false")
    img_out = img_out in ("true", "1", "yes", "on")
    try:
        result = await analyse_photo(request, fields, image)
    except APIError as exc:
        if img_out:
            exc.extra["image_url"] = error_image_url(request, exc)
        raise
    finally:
        del image
    if img_out:
        result["image_url"] = board_image_url(request, result)
    return result


async def analyse_photo(request: Request, fields: dict[str, str], image: bytes | None) -> dict:
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
    source = fields.get("source", "").strip().lower() or "auto"
    if source not in SOURCES:
        raise APIError(422, "invalid_request", "source: must be auto, screenshot or camera")
    try:
        depth = int(fields.get("depth") or config.DEFAULT_DEPTH)
    except ValueError:
        depth = 0
    if not 1 <= depth <= config.MAX_DEPTH:
        raise APIError(422, "invalid_request", f"depth: must be between 1 and {config.MAX_DEPTH}")

    try:
        rec, (width, height) = await run_in_threadpool(recognize, image, source)
    except RecognitionUnavailable as exc:
        raise APIError(501, "recognition_unavailable", str(exc)) from None
    except InvalidImage as exc:
        raise APIError(415, "invalid_image", str(exc)) from None
    except BoardNotFound as exc:
        raise APIError(422, "board_not_found", f"{exc}; fill the frame with the board and avoid glare") from None

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
        "source": rec.source,
        "profile": rec.profile,
    }
    return await best_move(request, parse_fen(fen, {**extra, "fen": fen}), depth, extra)


# --- screenshot calibration ---

_CALIBRATE_FORM = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["image", "name"],
            "properties": {
                "image": {"type": "string", "format": "binary",
                          "description": "Screenshot of the starting position, White at the bottom"},
                "name": {"type": "string", "description": "Profile name: lowercase letters, digits, - and _"},
                "replace": {"type": "boolean", "default": False, "description": "Overwrite a profile with this name"},
                "fen": {"type": "string", "description": "Piece placement shown, if not the starting position"},
            },
        }}},
    }
}


@app.post("/calibrate", openapi_extra=_CALIBRATE_FORM)
async def calibrate(request: Request):
    """Save the pieces of a starting-position screenshot as a template profile."""
    if not config.CALIBRATION_ENABLED:
        raise APIError(403, "calibration_disabled", "calibration is turned off on this server (CALIBRATION_ENABLED)")
    fields, files = await read_form(request, config.MAX_UPLOAD_BYTES)
    image = files.pop("image", None)
    files.clear()
    try:
        if not image:
            raise APIError(422, "invalid_request", "image: field required")
        name = fields.get("name", "")
        replace = fields.get("replace", "").strip().lower() in ("true", "1", "yes", "on")
        try:
            return await run_in_threadpool(calibration.calibrate, image, name, fields.get("fen", ""), replace)
        except FileExistsError:
            raise APIError(409, "profile_exists", f"a profile named {name.strip().lower()!r} already exists; send replace=true to overwrite it") from None
        except calibration.CalibrationError as exc:
            raise APIError(422, exc.code, exc.detail) from None
        except profiles.ProfileError as exc:
            raise APIError(422, "invalid_request", str(exc)) from None
        except InvalidImage as exc:
            raise APIError(415, "invalid_image", str(exc)) from None
    finally:
        del image


@app.get("/profiles")
async def list_profiles():
    """Template profiles screenshot recognition matches against."""
    return {"profiles": profiles.saved_names(), "builtin": any(p.name == profiles.BUILTIN for p in profiles.all_profiles())}


# --- diagrams ---

_HOST_RE = re.compile(r"^([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:\d{1,5})?$")


def public_base_url(request: Request) -> str:
    """Absolute base URL for links: PUBLIC_BASE_URL, else what the proxy (e.g. tailscale serve) forwarded."""
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    for host in (request.headers.get("x-forwarded-host", "").split(",")[0].strip(), request.headers.get("host", "")):
        if _HOST_RE.match(host):
            return f"{proto}://{host}"
    return f"{proto}://{request.url.netloc}"


def board_image_url(request: Request, result: dict) -> str:
    params = {"fen": result["fen"], "best": result["best"], "turn": result["fen"].split()[1]}
    if result.get("mate") is not None:
        params["mate"] = result["mate"]
    elif result.get("eval") is not None:
        params["eval"] = result["eval"]
    return f"{public_base_url(request)}/img?{urlencode(params)}"


def error_image_url(request: Request, exc: APIError) -> str:
    params = {"code": exc.error}
    fen = exc.extra.get("fen")
    if fen:
        params["fen"] = fen
        if exc.error == "illegal_position":
            # Name the specific problem (e.g. missing_king) when we can tell what it is.
            try:
                found = render.diagnose(chess.Board(fen))
            except ValueError:
                found = None
            if found:
                params["code"] = found[0]
    return f"{public_base_url(request)}/img/error?{urlencode(params)}"


class ImageError(Exception):
    def __init__(self, status: int, code: str, board: chess.Board | None = None, move: chess.Move | None = None):
        self.status, self.code, self.board, self.move = status, code, board, move


def _size(value: str) -> int:
    if not value:
        return render.DEFAULT_SIZE
    if not value.isdigit() or not render.MIN_SIZE <= int(value) <= render.MAX_SIZE:
        raise ImageError(422, "invalid_request")
    return int(value)


async def _image_response(svg: str, fmt: str, status: int = 200) -> Response:
    headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    png = await run_in_threadpool(render.to_png, svg) if fmt == "png" else None
    if png is not None:
        return Response(png, status_code=status, media_type="image/png", headers=headers)
    headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return Response(svg, status_code=status, media_type="image/svg+xml", headers=headers)


_MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


# Generous upper bounds; anything longer is rejected before parsing.
_MAX_LEN = {"fen": 100, "best": 5, "turn": 1, "eval": 12, "mate": 4}


@app.get("/img")
async def board_image(fen: str = "", best: str = "", turn: str = "", format: str = "png",
                      eval: str = "", mate: str = "", size: str = ""):
    """Diagram of a position with the best move drawn. Bad input gets an error image."""
    fmt = format if format in ("png", "svg") else "png"
    px = render.DEFAULT_SIZE
    try:
        values = {"fen": fen, "best": best, "turn": turn, "eval": eval, "mate": mate}
        if format not in ("png", "svg") or any(len(values[k]) > n for k, n in _MAX_LEN.items()):
            raise ImageError(422, "invalid_request")
        px = _size(size)
        try:
            board = chess.Board(fen.strip())
        except ValueError:
            raise ImageError(400, "invalid_fen") from None
        if turn:
            if turn not in ("w", "b"):
                raise ImageError(422, "invalid_request")
            board.turn = turn == "w"
        found = render.diagnose(board)
        if found and found[0] != "game_over":
            raise ImageError(422, found[0], board)
        move = None
        if best and found:   # game over: there's no move to draw, so say why
            raise ImageError(422, "game_over", board)
        if best:
            move = chess.Move.from_uci(best) if _MOVE_RE.match(best) else None
            if move is None or move not in board.legal_moves:
                raise ImageError(422, "illegal_move", board, move)
        try:
            eval_ = float(eval) if eval else None
            mate_ = int(mate) if mate else None
        except ValueError:
            raise ImageError(422, "invalid_request") from None
        if eval_ is not None and not (math.isfinite(eval_) and abs(eval_) <= 1000):
            raise ImageError(422, "invalid_request")
        if mate_ is not None and not 0 < abs(mate_) <= 999:
            raise ImageError(422, "invalid_request")
        svg = render.board_image(board, move, eval_, mate_, px)
    except ImageError as err:
        return await _image_response(render.error_image(err.code, err.board, px, err.move), fmt, err.status)
    return await _image_response(svg, fmt)


@app.get("/img/error")
async def error_image(code: str = "error", fen: str = "", format: str = "png", size: str = ""):
    """An image explaining an error code; draws the position too when it's a position problem."""
    fmt = format if format in ("png", "svg") else "png"
    try:
        px = _size(size)
    except ImageError:
        px = render.DEFAULT_SIZE
    board = None
    if fen and len(fen) <= _MAX_LEN["fen"]:
        try:
            board = chess.Board(fen.strip())
        except ValueError:
            board = None   # can't draw it; the message alone still explains the error
    return await _image_response(render.error_image(code, board, px), fmt)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
