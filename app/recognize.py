"""Photo -> board position. Works on in-memory bytes only; never writes the image anywhere."""

import os
import threading

from . import config

# Refuse to decode absurdly large images (decompression bombs). Must be set before cv2 loads.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(config.MAX_IMAGE_PIXELS))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from .vision import board_finder, screenshot  # noqa: E402
from .vision.recognizer import BoardNotFound, Recognition, Recognizer  # noqa: E402,F401

# Larger photos are shrunk first: no accuracy gain beyond this, just slower on a Pi.
WORK_MAX_SIDE = 2000


class RecognitionUnavailable(RuntimeError):
    pass


class InvalidImage(ValueError):
    pass


_recognizer: Recognizer | None = None
_load_lock = threading.Lock()


def _get_recognizer() -> Recognizer:
    global _recognizer
    with _load_lock:
        if _recognizer is None:
            if not os.path.isfile(config.MODEL_PATH):
                raise RecognitionUnavailable(
                    "recognition model not found; run `python scripts/download_model.py` "
                    "or set MODEL_PATH"
                )
            _recognizer = Recognizer(config.MODEL_PATH, threads=config.VISION_THREADS)
        return _recognizer


def decode(image: bytes) -> np.ndarray:
    """Encoded image bytes -> BGR array, entirely in memory."""
    try:
        img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        img = None
    if img is None:
        raise InvalidImage("couldn't decode the image; use JPEG, PNG or WebP")
    scale = WORK_MAX_SIDE / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return img


SOURCES = ("auto", "screenshot", "camera")


class UnknownProfile(ValueError):
    pass


def calibrate(image: bytes, name: str):
    """Make and save a piece profile from a start-position screenshot (in memory only)."""
    from .vision import calibration
    if not calibration.available():
        raise RecognitionUnavailable("calibration needs chessfen, which needs Python 3.13+")
    img = decode(image)
    try:
        profile = calibration.calibrate(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), name)
        calibration.save(profile, config.PROFILE_DIR)
        return profile
    finally:
        del img


def recognize(image: bytes, source: str = "auto", flip: bool | None = None,
              profile: str | None = None) -> tuple[Recognition, tuple[int, int]]:
    """Return the recognition and the (width, height) its corner coordinates refer to.

    source "auto": a flat digital board (screenshot) is read by chessfen; anything else,
    or a screenshot chessfen can't read, goes to the photo model. "screenshot" uses
    chessfen only, "camera" the photo model only. flip forces a screenshot's orientation
    (True = Black at the bottom); None lets chessfen guess. Camera photos have their own
    orientation detection. profile: calibration profile for screenshots (a saved name,
    "builtin" for stock chessfen, None = pick the best saved one automatically)."""
    if source == "camera":
        _get_recognizer()   # fail fast if the model is missing
    img = decode(image)
    try:
        size = (img.shape[1], img.shape[0])
        if source != "camera":
            grid = board_finder.locate_board(img)
            if source == "screenshot" or board_finder.looks_like_screenshot(grid):
                try:
                    return screenshot.recognize(img, flip, grid, profile), size
                except screenshot.UnknownProfile as exc:
                    raise UnknownProfile(str(exc)) from None
                except screenshot.ScreenshotFailed as exc:
                    if source == "screenshot":
                        if not screenshot.available():
                            raise RecognitionUnavailable(exc.reason) from None
                        raise BoardNotFound(f"couldn't read a board in the screenshot ({exc.reason})") from None
        return _get_recognizer().recognize(img), size
    finally:
        del img
