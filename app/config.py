"""Settings, read from environment variables. Nothing machine-specific lives in the repo."""

import os
import shutil


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


STOCKFISH_PATH = (
    os.environ.get("STOCKFISH_PATH")
    or shutil.which("stockfish")
    or "/usr/games/stockfish"
)
ENGINE_THREADS = _int("ENGINE_THREADS", 2)
ENGINE_HASH_MB = _int("ENGINE_HASH_MB", 64)
DEFAULT_DEPTH = _int("DEFAULT_DEPTH", 15)
MAX_DEPTH = _int("MAX_DEPTH", 25)
# Hard wall-clock cap per search so a deep request can't tie up a small board forever.
ENGINE_MAX_SECONDS = _float("ENGINE_MAX_SECONDS", 10.0)
MODEL_PATH = os.environ.get(
    "MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "square_cnn.onnx"),
)
VISION_THREADS = _int("VISION_THREADS", 2)
MAX_IMAGE_PIXELS = _int("MAX_IMAGE_PIXELS", 50_000_000)
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_MB", 10) * 1024 * 1024
# Base for absolute links such as image_url, e.g. https://chess.example.ts.net. When unset,
# links are built from the request's X-Forwarded-Proto/X-Forwarded-Host (or Host) headers.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# Where piece-shape calibration profiles (tiny .npz files, never screenshots) are kept.
PROFILE_DIR = os.environ.get(
    "PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles"),
)
# Move models (see app/models.py). lc0 + Maia weights enable the maia-* models.
LC0_PATH = os.environ.get("LC0_PATH", "").strip()
LC0_BACKEND = os.environ.get("LC0_BACKEND", "").strip()   # e.g. blas / eigen; empty = lc0's default
MAIA_DIR = os.environ.get(
    "MAIA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "maia"),
)
# Extra UCI engines as JSON, e.g. {"rodent": {"path": "/opt/rodent/rodent", "label": "Rodent IV"}}
UCI_ENGINES = os.environ.get("UCI_ENGINES", "")
# Non-default engines kept running at once; the least recently used is stopped beyond this.
MAX_LIVE_ENGINES = _int("MAX_LIVE_ENGINES", 2)
