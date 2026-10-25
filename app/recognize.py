"""Photo -> board position. Works on in-memory bytes only; never writes the image anywhere."""

import os
import threading

from . import config

# Refuse to decode absurdly large images (decompression bombs). Must be set before cv2 loads.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(config.MAX_IMAGE_PIXELS))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from .vision import profiles, screenshot  # noqa: E402
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


def recognize(image: bytes, source: str = "auto") -> tuple[Recognition, tuple[int, int]]:
    """Return the recognition and the (width, height) its corner coordinates refer to.

    source: "screenshot" uses the template matcher for flat digital boards, "camera" the
    photo model, and "auto" picks the template matcher when the image contains a clean
    axis-aligned board."""
    if source == "camera":
        _get_recognizer()   # fail fast if the model is missing
    img = decode(image)
    try:
        size = (img.shape[1], img.shape[0])
        if source != "camera":
            grid = screenshot.locate_board(img)
            if source == "screenshot" or screenshot.looks_like_screenshot(grid):
                known = profiles.all_profiles()
                if grid is None and source == "screenshot":
                    raise BoardNotFound("couldn't find a flat 8x8 board in the screenshot")
                if known and grid is not None:
                    return screenshot.recognize(img, grid, known), size
                if source == "screenshot":
                    raise RecognitionUnavailable("no piece profiles available; calibrate one first")
        return _get_recognizer().recognize(img), size
    finally:
        del img
