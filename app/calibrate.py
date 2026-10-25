"""Make a piece-shape profile from a screenshot of the start position.

    python -m app.calibrate start.png --name iphone-green
    python -m app.calibrate --list
    python -m app.calibrate --delete iphone-green

Use a clean screenshot of the starting position (no moves yet, no piece being dragged),
taken the same way as the ones you'll analyse (same device, theme and piece set). Only
12 tiny piece shapes are saved, to PROFILE_DIR; the screenshot is read in memory and
not copied anywhere.
"""

import argparse
import sys
from pathlib import Path

from . import config
from .vision import calibration


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image", nargs="?", type=Path, help="screenshot of the start position")
    ap.add_argument("--name", help="profile name, e.g. iphone-green (lowercase letters, digits, - _)")
    ap.add_argument("--list", action="store_true", help="list saved profiles")
    ap.add_argument("--delete", metavar="NAME", help="delete a saved profile")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(calibration.names(config.PROFILE_DIR)) or "(no profiles saved)")
        return 0
    if args.delete:
        if calibration.delete(args.delete, config.PROFILE_DIR):
            print(f"deleted {args.delete}")
            return 0
        print(f"no profile named {args.delete!r}", file=sys.stderr)
        return 1
    if not args.image or not args.name:
        ap.error("give an image and --name (or use --list / --delete)")

    from .recognize import InvalidImage, RecognitionUnavailable, calibrate
    try:
        profile = calibrate(args.image.read_bytes(), args.name)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (InvalidImage, RecognitionUnavailable, calibration.CalibrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"saved profile {profile.name!r} in {config.PROFILE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
