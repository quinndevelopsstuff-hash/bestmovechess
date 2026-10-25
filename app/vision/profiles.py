"""Piece-template profiles for screenshot recognition.

The built-in profile is rendered in memory from python-chess's piece set. Calibrated
profiles (small template arrays, never screenshots) are .npz files in PROFILES_DIR;
they're reloaded whenever that folder changes.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np

from .. import config
from .screenshot import PIECES, Profile, builtin_profile

log = logging.getLogger("bestmovechess")
_lock = threading.Lock()
_builtin: list[Profile] | None = None
_saved: tuple[float, list[Profile]] = (-1.0, [])


def load(path: Path) -> Profile:
    with np.load(path, allow_pickle=False) as data:
        masks = {p: data[f"mask_{p}"].astype(np.float32) for p in PIECES}
        lums = {p: float(data[f"lum_{p}"]) for p in PIECES}
    return Profile(path.stem, masks, lums)


def _load_saved() -> list[Profile]:
    global _saved
    folder = Path(config.PROFILES_DIR)
    try:
        stamp = folder.stat().st_mtime
    except OSError:
        return []
    if stamp != _saved[0]:
        found = []
        for path in sorted(folder.glob("*.npz")):
            try:
                found.append(load(path))
            except (OSError, KeyError, ValueError) as exc:
                log.warning("skipping profile %s: %s", path.name, exc)
        _saved = (stamp, found)
    return _saved[1]


def all_profiles() -> list[Profile]:
    """Calibrated profiles plus the built-in one (when Cairo is available)."""
    global _builtin
    with _lock:
        if _builtin is None:
            made = builtin_profile()
            _builtin = [made] if made else []
        return _load_saved() + _builtin
