"""Piece-template profiles for screenshot recognition.

The built-in profile is rendered in memory from python-chess's piece set. Calibrated
profiles are small .npz files in PROFILES_DIR holding only the 12 piece silhouettes
(T x T, uint8) and their fill brightness, never a screenshot. They're reloaded whenever
that folder changes.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

from .. import config
from .screenshot import PIECES, T, Profile, builtin_profile

log = logging.getLogger("bestmovechess")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
BUILTIN = "builtin"
FORMAT_VERSION = 1

_lock = threading.Lock()
_builtin: list[Profile] | None = None
_saved: tuple[float, list[Profile]] = (-1.0, [])


class ProfileError(ValueError):
    pass


def check_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME_RE.match(name) or name == BUILTIN:
        raise ProfileError("name: use 1-40 lowercase letters, digits, '-' or '_' (and not 'builtin')")
    return name


def path_for(name: str) -> Path:
    return Path(config.PROFILES_DIR) / f"{check_name(name)}.npz"


def save(profile: Profile, replace: bool = False, square_px: float = 0.0) -> Path:
    """Write a profile atomically. Only the small templates are stored."""
    path = path_for(profile.name)
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError(profile.name)
    if not path.exists() and len(saved_names()) >= config.MAX_PROFILES:
        raise ProfileError(f"there are already {config.MAX_PROFILES} profiles (MAX_PROFILES); delete one first")
    arrays = {"version": np.int32(FORMAT_VERSION), "size": np.int32(T), "square_px": np.float32(square_px),
              "created": np.int64(time.time())}
    for p in PIECES:
        arrays[f"mask_{p}"] = np.clip(np.rint(profile.masks[p] * 255), 0, 255).astype(np.uint8)
        arrays[f"lum_{p}"] = np.float32(profile.lums[p])
    tmp = folder / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            np.savez_compressed(f, **arrays)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def load(path: Path) -> Profile:
    with np.load(path, allow_pickle=False) as data:
        if int(data["size"]) != T:
            raise ValueError(f"templates are {int(data['size'])} px, expected {T}")
        masks = {p: data[f"mask_{p}"].astype(np.float32) / 255.0 for p in PIECES}
        lums = {p: float(data[f"lum_{p}"]) for p in PIECES}
    return Profile(path.stem, masks, lums)


def saved_names() -> list[str]:
    folder = Path(config.PROFILES_DIR)
    return sorted(p.stem for p in folder.glob("*.npz")) if folder.is_dir() else []


def _load_saved() -> list[Profile]:
    global _saved
    folder = Path(config.PROFILES_DIR)
    try:
        stamp = folder.stat().st_mtime_ns
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
