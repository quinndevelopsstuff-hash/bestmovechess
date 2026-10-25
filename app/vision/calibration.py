"""Piece-shape profiles: chessfen's piece templates, re-cut from your own screenshots.

chessfen matches every square against silhouettes of python-chess's piece set. Chess.com
(and every other site/theme) draws its pieces a little differently, so the stock match is
often wrong or doubtful. A *profile* replaces those 12 silhouettes with ones cut from one
screenshot of the start position in your own theme and device; the result is far more
accurate on that theme.

A profile is just 12 small black-and-white 64x64 masks. They are saved as a tiny .npz file
in PROFILE_DIR. The screenshot used to make one is processed in memory and never stored.

Everything here is a thin layer over chessfen's public pieces plus two patch points
(`chessfen.classify.piece_shapes`, and `find_board`/`read_square` in `chessfen.recognize`),
both of which behave exactly like stock chessfen unless a profile / shared-reading context
is active. The context is held in ContextVars, so concurrent requests don't affect each other.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import importlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

import chess
import numpy as np

try:
    import chessfen
    import chessfen.classify as _classify
    from chessfen.geometry import find_board
    from chessfen.imaging import normalize_shape
    from chessfen.squares import read_square
    from chessfen.templates import PIECE_TYPES, SHAPE_SIZE

    # The submodule: `chessfen.recognize` as an attribute is the function of the same name.
    _recognize_module = importlib.import_module("chessfen.recognize")
except ImportError:   # Python < 3.13, or not installed
    chessfen = None

BUILTIN = "builtin"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class CalibrationError(Exception):
    """The screenshot couldn't be used to make a profile."""


@dataclass(frozen=True)
class Profile:
    name: str
    shapes: dict   # (chess.Color, piece type) -> bool mask, SHAPE_SIZE x SHAPE_SIZE


def available() -> bool:
    return chessfen is not None


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and name != BUILTIN


# --- Making a profile --------------------------------------------------------------------

def calibrate(rgb: np.ndarray, name: str) -> Profile:
    """Cut a profile out of a screenshot (RGB array) of the *start position* with White at
    the bottom or top. The result is checked: the same screenshot must read back as exactly
    the start position through the new profile, otherwise it wasn't a start position (or
    wasn't a clean screenshot) and CalibrationError is raised."""
    if chessfen is None:
        raise CalibrationError("calibration needs chessfen, which needs Python 3.13+")
    if not valid_name(name):
        raise CalibrationError("profile name must be 1-40 characters: lowercase letters, digits, - or _")
    rgb = np.ascontiguousarray(rgb)
    try:
        # White at the top (board shown from Black's side)? Turn the picture round; a
        # half-turn keeps the board square and puts White at the bottom.
        if chessfen.recognize(rgb, castling=chessfen.Castling.NONE).orientation == chessfen.Orientation.BLACK:
            rgb = np.ascontiguousarray(rgb[::-1, ::-1])
        rect = find_board(rgb)
    except Exception as exc:
        raise CalibrationError(f"couldn't find a chess board in the screenshot ({exc})") from None

    start = chess.Board()
    acc: dict[tuple[bool, int], list[np.ndarray]] = {}
    for row in range(8):
        for col in range(8):
            piece = start.piece_at(chess.square(col, 7 - row))
            if piece is None:
                continue
            ink = read_square(rect.crop(rgb, row, col)).ink
            acc.setdefault((piece.color, piece.piece_type), []).append(normalize_shape(ink, SHAPE_SIZE).astype(float))
    if len(acc) != 12:
        raise CalibrationError("couldn't read all 12 piece types from the screenshot")
    # Average the copies (8 pawns, 2 rooks...) and keep what most of them agree on.
    profile = Profile(name, {key: np.mean(masks, axis=0) > 0.5 for key, masks in acc.items()})
    if any(not mask.any() for mask in profile.shapes.values()):
        raise CalibrationError("a piece came out blank; is this a clear screenshot of the start position?")

    with using(profile):
        check = chessfen.recognize(rgb, orientation=chessfen.Orientation.WHITE, castling=chessfen.Castling.NONE)
    if check.board.board_fen() != start.board_fen() or check.shaky:
        raise CalibrationError("that doesn't look like a clean screenshot of the start position "
                               "(White at the bottom or top, no moves made, no piece being dragged)")
    return profile


# --- Saving and loading ------------------------------------------------------------------

def _key(color: bool, piece_type: int) -> str:
    return f"{'w' if color else 'b'}{piece_type}"


def _path(directory: str | os.PathLike, name: str) -> Path:
    if not valid_name(name):
        raise ValueError(f"invalid profile name: {name!r}")
    return Path(directory) / f"{name}.npz"


def save(profile: Profile, directory: str | os.PathLike) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    np.savez_compressed(buf, **{_key(*k): m for k, m in profile.shapes.items()})
    path = _path(directory, profile.name)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)   # atomic, so a reader never sees half a file
    return path


def load(name: str, directory: str | os.PathLike) -> Profile:
    path = _path(directory, name)
    try:
        with np.load(path) as data:
            shapes = {(color, pt): np.asarray(data[_key(color, pt)], dtype=bool)
                      for color in (chess.WHITE, chess.BLACK) for pt in PIECE_TYPES}
    except FileNotFoundError:
        raise KeyError(name) from None
    except Exception as exc:   # corrupt or wrong-shaped file
        raise ValueError(f"profile {name!r} is unreadable: {exc}") from None
    if any(m.shape != (SHAPE_SIZE, SHAPE_SIZE) for m in shapes.values()):
        raise ValueError(f"profile {name!r} was made for a different chessfen version")
    return Profile(name, shapes)


def names(directory: str | os.PathLike) -> list[str]:
    try:
        return sorted(p.stem for p in Path(directory).glob("*.npz") if valid_name(p.stem))
    except OSError:
        return []


def load_all(directory: str | os.PathLike) -> list[Profile]:
    out = []
    for name in names(directory):
        try:
            out.append(load(name, directory))
        except (ValueError, KeyError):
            continue   # skip a bad file rather than break every screenshot
    return out


def delete(name: str, directory: str | os.PathLike) -> bool:
    try:
        _path(directory, name).unlink()
        return True
    except FileNotFoundError:
        return False


# --- Using a profile ---------------------------------------------------------------------

_active: contextvars.ContextVar[Profile | None] = contextvars.ContextVar("bmc_profile", default=None)
_reads: contextvars.ContextVar[dict | None] = contextvars.ContextVar("bmc_reads", default=None)


@contextlib.contextmanager
def using(profile: Profile | None):
    """Inside this block chessfen matches against `profile`'s shapes (None = stock)."""
    token = _active.set(profile)
    try:
        yield
    finally:
        _active.reset(token)


@contextlib.contextmanager
def shared_reading():
    """Inside this block, finding the board and reading its squares (85% of chessfen's time)
    is done once per distinct image and reused when several profiles are tried."""
    token = _reads.set({})
    try:
        yield
    finally:
        _reads.reset(token)


def _digest(arr: np.ndarray) -> bytes:
    arr = np.ascontiguousarray(arr)
    h = hashlib.blake2b(digest_size=16)
    h.update(str(arr.shape).encode())
    h.update(arr.data)
    return h.digest()


_STOCK: dict = {}   # the original chessfen functions the memo wrappers call


def _install() -> None:
    stock_shapes = _classify.piece_shapes
    _STOCK["board"], _STOCK["square"] = _recognize_module.find_board, _recognize_module.read_square

    def piece_shapes(color):
        profile = _active.get()
        if profile is None:
            return stock_shapes(color)
        return tuple((pt, profile.shapes[(color, pt)]) for pt in PIECE_TYPES)

    def memo(tag):
        def wrapper(arr):
            cache = _reads.get()
            if cache is None:
                return _STOCK[tag](arr)
            key = (tag, _digest(arr))
            if key in cache:
                hit = cache[key]
                if isinstance(hit, Exception):
                    raise hit
                return hit
            try:
                cache[key] = value = _STOCK[tag](arr)
            except Exception as exc:
                cache[key] = exc
                raise
            return value
        return wrapper

    _classify.piece_shapes = piece_shapes
    _recognize_module.find_board = memo("board")
    _recognize_module.read_square = memo("square")


if chessfen is not None and not getattr(_classify, "_bmc_installed", False):
    _install()
    _classify._bmc_installed = True
