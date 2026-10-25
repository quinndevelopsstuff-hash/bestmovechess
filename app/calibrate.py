"""Calibrate screenshot recognition for a piece set: cut the 12 pieces out of a screenshot
of the starting position (White at the bottom) and save them as a named profile.

    python -m app.calibrate screenshot.png --name chesscom-neo
    python -m app.calibrate screenshot.png --name mine --fen <placement>   # any position with all 12 pieces
    python -m app.calibrate --list

Only the small piece templates are saved (in PROFILES_DIR, gitignored), never the
screenshot. The upload is decoded in memory and dropped as soon as the templates exist.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

from .recognize import decode
from .vision import profiles
from .vision.screenshot import (EMPTY_AREA, SQUARE_NAMES, START, _ncc, colour_split, expand, locate_board,
                                looks_like_screenshot, profile_from_squares, squares)

MIN_COPY_MATCH = 0.8   # every copy of a piece must match the average of its copies this well


class CalibrationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def verify(cells, placement: str) -> None:
    """Make sure the screenshot really shows `placement` before trusting it as templates."""
    expected = expand(placement)
    if len(expected) != 64 or any(p not in ".PNBRQKpnbrqk" for p in expected):
        raise CalibrationError("invalid_request", "fen: not a valid piece placement")
    missing = set("PNBRQKpnbrqk") - set(expected)
    if missing:
        raise CalibrationError("invalid_request", f"fen: the position must contain all 12 pieces (missing {''.join(sorted(missing))})")
    extra = [n for n, p, sq in zip(SQUARE_NAMES, expected, cells) if p == "." and sq.area >= EMPTY_AREA]
    absent = [n for n, p, sq in zip(SQUARE_NAMES, expected, cells) if p != "." and sq.area < EMPTY_AREA]
    if extra or absent:
        parts = ([f"pieces on {', '.join(extra[:6])}"] if extra else []) + ([f"no piece on {', '.join(absent[:6])}"] if absent else [])
        what = "the starting position" if placement == START else "the given position"
        raise CalibrationError("wrong_position", f"this doesn't look like {what}: {'; '.join(parts)}")
    occupied = [(p, sq) for p, sq in zip(expected, cells) if p != "."]
    split = colour_split([sq.lum for _, sq in occupied])
    if split is None:
        raise CalibrationError("wrong_position", "couldn't tell White's pieces from Black's")
    wrong = sum((sq.lum > split[0]) != p.isupper() for p, sq in occupied)
    if wrong == len(occupied):
        raise CalibrationError("flipped", "the board is shown from Black's side; use a screenshot with White at the bottom")
    if wrong:
        raise CalibrationError("wrong_position", f"{wrong} pieces have the wrong colour for this position")
    for piece in set(expected) - {"."}:
        copies = [(n, sq) for n, p, sq in zip(SQUARE_NAMES, expected, cells) if p == piece]
        if len(copies) > 1:
            mean = np.mean([sq.mask for _, sq in copies], axis=0).astype(np.float32)
            for n, sq in copies:
                if _ncc(sq.mask, mean) < MIN_COPY_MATCH:
                    raise CalibrationError("wrong_position", f"the piece on {n} doesn't match the other {piece!r} pieces")


def calibrate(data: bytes, name: str, placement: str = START, replace: bool = False) -> dict:
    """Build, check and save a profile from an encoded screenshot. Raises CalibrationError,
    profiles.ProfileError, FileExistsError or recognize.InvalidImage."""
    name = profiles.check_name(name)
    placement = placement.split()[0] if placement.strip() else START
    img = decode(data)
    try:
        grid = locate_board(img)
        if grid is None:
            raise CalibrationError("board_not_found", "couldn't find a flat 8x8 board in the screenshot")
        cells = squares(img, grid)
    finally:
        del img   # only the per-square silhouettes are kept from here on
    verify(cells, placement)
    profile = profile_from_squares(name, cells, placement)
    profiles.save(profile, replace=replace, square_px=grid.size)
    return {"profile": name, "square_px": round(grid.size, 1), "screenshot": looks_like_screenshot(grid),
            "profiles": profiles.saved_names()}


def default_name(path: Path) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", path.stem.lower()).strip("-_")[:40] or "profile"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("screenshot", nargs="?", type=Path, help="screenshot of the starting position, White at the bottom")
    ap.add_argument("--name", help="profile name (default: from the file name)")
    ap.add_argument("--fen", default=START, help="piece placement shown, if not the starting position")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing profile with this name")
    ap.add_argument("--list", action="store_true", help="list saved profiles")
    args = ap.parse_args(argv)
    if args.list:
        names = profiles.saved_names()
        print("\n".join(names) if names else f"no profiles in {profiles.path_for('x').parent}")
        return 0
    if not args.screenshot:
        ap.error("give a screenshot, or --list")
    try:
        data = args.screenshot.read_bytes()
        result = calibrate(data, args.name or default_name(args.screenshot), args.fen, args.replace)
    except FileExistsError as exc:
        print(f"error: profile {exc} already exists (use --replace)", file=sys.stderr)
        return 1
    except (CalibrationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        data = None
    print(f"saved profile {result['profile']!r} ({result['square_px']} px squares) to {profiles.path_for(result['profile'])}")
    if not result["screenshot"]:
        print("note: this didn't look like a clean screenshot; recognition may be unreliable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
