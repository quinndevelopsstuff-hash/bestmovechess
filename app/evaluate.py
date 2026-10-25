"""Measure board recognition on local fixtures: per-square accuracy and a confusion table.

    python -m app.evaluate                    # all fixtures in tests/fixtures/
    python -m app.evaluate path/to/dir -v     # another folder; -v lists every wrong square

A fixture is an image (`.png`, `.jpg`, `.jpeg` or `.webp`) plus a sidecar with the same
name and a `.fen` extension holding the expected position (only the piece placement,
the first FEN field, is used). Fixtures stay local: tests/fixtures/ is gitignored.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

CLASSES = ".PNBRQKpnbrqk"          # "." = empty square
NOT_FOUND = "?"                    # prediction when no board was found at all
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
SQUARES = [f"{f}{r}" for r in range(8, 0, -1) for f in "abcdefgh"]   # a8 ... h1, FEN order


def expand(placement: str) -> list[str]:
    """FEN piece placement -> 64 characters, a8 first, "." for empty squares."""
    rows = placement.split()[0].split("/")
    out = []
    for row in rows:
        cells = []
        for ch in row:
            if ch.isdigit():
                cells += ["."] * int(ch)
            elif ch in CLASSES[1:]:
                cells.append(ch)
            else:
                raise ValueError(f"bad character {ch!r} in placement {placement!r}")
        if len(cells) != 8:
            raise ValueError(f"rank {row!r} doesn't have 8 squares")
        out += cells
    if len(rows) != 8:
        raise ValueError(f"placement {placement!r} doesn't have 8 ranks")
    return out


def load_fixtures(folder: Path) -> list[tuple[str, Path, str]]:
    """(name, image path, expected placement) for every image with a .fen sidecar."""
    fixtures = []
    for image in sorted(folder.iterdir()):
        if image.suffix.lower() not in IMAGE_EXTS:
            continue
        sidecar = image.with_suffix(".fen")
        if not sidecar.is_file():
            print(f"skipping {image.name}: no {sidecar.name}", file=sys.stderr)
            continue
        expected = sidecar.read_text().strip()
        expand(expected)   # fail early on a malformed sidecar
        fixtures.append((image.stem, image, expected))
    return fixtures


def compare(expected: str, predicted: str | None) -> list[tuple[str, str, str]]:
    """(square, expected piece, predicted piece) for all 64 squares."""
    truth = expand(expected)
    guess = expand(predicted) if predicted else [NOT_FOUND] * 64
    return list(zip(SQUARES, truth, guess))


def confusion_table(pairs: Counter) -> str:
    """Rows = expected, columns = predicted; counts of squares."""
    cols = list(CLASSES) + ([NOT_FOUND] if any(p == NOT_FOUND for _, p in pairs) else [])
    lines = ["exp\\got " + " ".join(f"{c:>3}" for c in cols) + "   acc"]
    for t in CLASSES:
        row = [pairs[(t, p)] for p in cols]
        total = sum(row)
        if not total:
            continue
        acc = pairs[(t, t)] / total
        lines.append(f"{t:>7} " + " ".join(f"{n:>3}" if n else "  ." for n in row) + f"  {acc:4.0%}")
    return "\n".join(lines)


def by_piece_type(pairs: Counter) -> str:
    """Accuracy per piece type, colours combined (a white knight read as a black knight counts as wrong)."""
    names = {".": "empty", "p": "pawn", "n": "knight", "b": "bishop", "r": "rook", "q": "queen", "k": "king"}
    lines = []
    for key, name in names.items():
        total = sum(n for (t, _), n in pairs.items() if t.lower() == key)
        right = sum(n for (t, p), n in pairs.items() if t.lower() == key and p == t)
        if total:
            lines.append(f"  {name:<7} {right:>4}/{total:<4} {right / total:6.1%}")
    return "\n".join(lines)


def recognizer(source: str):
    """The recognition pipeline exactly as POST /move runs it with this `source`.
    Returns (placement or None if no board, pipeline actually used)."""
    from .recognize import BoardNotFound, RecognitionUnavailable, recognize

    def run(data: bytes) -> tuple[str | None, str]:
        try:
            rec, _ = recognize(data, source)
        except BoardNotFound:
            return None, source
        except RecognitionUnavailable as exc:
            sys.exit(f"error: {exc}")
        return rec.placement, rec.source
    return run


SOURCES = ("auto", "screenshot", "camera")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", nargs="?", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--source", choices=SOURCES, default="auto", help="which pipeline to run (as the API's source field)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every misread square")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"no fixtures folder at {args.folder}", file=sys.stderr)
        return 1
    fixtures = load_fixtures(args.folder)
    if not fixtures:
        print(f"no fixtures in {args.folder} (need <name>.png/.jpg plus <name>.fen)", file=sys.stderr)
        return 1

    run = recognizer(args.source)
    pairs: Counter = Counter()
    right_total = boards_right = 0
    print(f"recognizer: {args.source}   fixtures: {len(fixtures)}\n")
    for name, path, expected in fixtures:
        predicted, used = run(path.read_bytes())
        result = compare(expected, predicted)
        right = sum(t == p for _, t, p in result)
        right_total += right
        boards_right += right == 64
        pairs.update((t, p) for _, t, p in result)
        pieces = [(t, p) for _, t, p in result if t != "."]
        piece_right = sum(t == p for t, p in pieces)
        status = "board not found" if predicted is None else f"{used}: {predicted}"
        print(f"{name}: {right}/64 squares ({right / 64:.1%}), pieces {piece_right}/{len(pieces)}  [{status}]")
        if args.verbose:
            wrong = [f"{sq} {t}->{p}" for sq, t, p in result if t != p]
            if wrong:
                print("   wrong: " + ", ".join(wrong))

    n = len(fixtures)
    print(f"\nsquares correct: {right_total}/{64 * n} ({right_total / (64 * n):.1%})"
          f"   boards fully correct: {boards_right}/{n}")
    print("\naccuracy by piece type:\n" + by_piece_type(pairs))
    print("\nconfusion (rows = expected, columns = predicted):\n" + confusion_table(pairs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
