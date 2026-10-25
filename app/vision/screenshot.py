"""Screenshot recognition without ML: find a flat, axis-aligned 8x8 board and match pieces
against templates.

1. Locate: search for a grid whose same-coloured squares are uniform and alternate
   (checked on a downscaled copy), then snap it to the grid lines at full resolution.
2. Segment: per square, take the background colour from the square's border, mark pixels
   that differ, keep only blobs reaching the centre (drops coordinate labels and
   highlight edges), and fill holes to get a solid silhouette. Tiny silhouette = empty.
3. Classify: normalised cross-correlation of the silhouette (scaled to TxT) against each
   profile's piece templates gives the piece type; the fill brightness gives the colour.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

T = 64                    # every square is normalised to T x T pixels
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
PIECES = "PNBRQKpnbrqk"
TYPES = "pnbrqk"
EMPTY_AREA = 0.03         # silhouettes smaller than this fraction of the square are noise
FG_DIST = 25.0            # colour distance (8-bit Lab) from the background that counts as foreground
COARSE_SIDE = 160         # the grid search runs on a copy with this shorter side


@dataclass
class Grid:
    x0: float             # top-left corner of the board, full-resolution pixels
    y0: float
    size: float           # square side in pixels
    consistent: int       # squares whose corner colour fits the checker pattern (of 64)
    contrast: float       # colour distance between light and dark squares
    spread: float         # typical deviation of squares from their parity's colour
    sharpness: float      # grid-line edge strength relative to mid-square
    noise: float          # pixel noise inside the plain part of a square

    @property
    def corners(self) -> list[list[float]]:
        """Image corners: top-left, top-right, bottom-right, bottom-left."""
        x1, y1 = self.x0 + 8 * self.size, self.y0 + 8 * self.size
        return [[self.x0, self.y0], [x1, self.y0], [x1, y1], [self.x0, y1]]


def _lab(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)


def _parity() -> np.ndarray:
    return (np.add.outer(np.arange(8), np.arange(8)) % 2).astype(bool)


def _coarse_search(lab: np.ndarray) -> tuple[int, int, int] | None:
    """Best (square size, x0, y0) on the small image, scored by how many squares fit an
    alternating two-colour pattern. Samples each square near its top-right corner, which
    pieces and coordinate labels rarely cover."""
    H, W = lab.shape[:2]
    odd = _parity()
    best, best_key = None, (-1, 0.0)
    # The board must fill at least half of the shorter side (true for phone and desktop
    # screenshots of a game), which keeps the search small.
    for S in range(max(6, min(H, W) // 16), min(H, W) // 8 + 1):
        ny, nx = H - 8 * S + 1, W - 8 * S + 1
        if ny <= 0 or nx <= 0:
            continue
        # Candidate origins every `st` pixels. The corner samples only pin the grid to within
        # about half a square (they fit anywhere a square is plain); _refine snaps it exactly.
        st = max(1, S // 6)
        p = max(2, round(S * 0.16))
        blur = cv2.blur(lab, (p, p))
        dx, dy = round(0.8 * S), round(0.2 * S)
        rows = [dy + r * S for r in range(8)]
        cols = [dx + c * S for c in range(8)]
        # (64, h, w, 3): every square's corner sample for every candidate origin.
        cells = np.stack([blur[y: y + ny: st, x: x + nx: st] for y in rows for x in cols])
        flat_odd = odd.ravel()
        means = np.stack([cells[~flat_odd].mean(axis=0), cells[flat_odd].mean(axis=0)])
        contrast = np.sqrt(((means[0] - means[1]) ** 2).sum(axis=-1))
        tol = np.maximum(6.0, 0.25 * contrast)
        d2 = ((cells - means[flat_odd.astype(int)]) ** 2).sum(axis=-1)
        count = (d2 < tol[None] ** 2).sum(axis=0)
        count[contrast < 20] = 0
        key = count + contrast / 1000.0
        i, j = np.unravel_index(int(np.argmax(key)), key.shape)
        if (count[i, j], contrast[i, j]) > best_key:
            best_key, best = (int(count[i, j]), float(contrast[i, j])), (S, j * st, i * st)
    return best if best_key[0] >= 40 else None


def _refine(gray: np.ndarray, x0: float, y0: float, S: float, reach: float,
            size_reach: float) -> tuple[float, float, float]:
    """Snap the grid to its 9 grid lines in each direction (lines on or just past the image
    edge, as for a board spanning the full screen width, score what's there)."""
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    H, W = gray.shape
    ya, yb = int(max(0, y0 + 0.5 * S)), int(min(H, y0 + 7.5 * S))
    xa, xb = int(max(0, x0 + 0.5 * S)), int(min(W, x0 + 7.5 * S))
    col = gx[ya:yb].sum(axis=0)
    row = gy[:, xa:xb].sum(axis=1)
    k = np.arange(0, 9)
    best = (-1.0, x0, y0, S)
    for s in np.arange(S - size_reach, S + size_reach + 1e-6, 0.25):
        def line_energy(profile, start):
            starts = np.arange(start - reach, start + reach + 1e-6, 0.5)
            idx = np.rint(starts[:, None] + k[None, :] * s).astype(int)
            ok = (idx >= -2).all(axis=1) & (idx <= len(profile) + 1).all(axis=1)
            if not ok.any():
                return -1.0, start
            e = np.where(ok, profile[np.clip(idx, 0, len(profile) - 1)].sum(axis=1), -1.0)
            i = int(np.argmax(e))
            return float(e[i]), float(starts[i])
        ex, bx = line_energy(col, x0)
        ey, by = line_energy(row, y0)
        if ex + ey > best[0]:
            best = (ex + ey, bx, by, float(s))
    return best[1], best[2], best[3]


def _corner_samples(lab: np.ndarray, x0: float, y0: float, S: float) -> np.ndarray | None:
    """(8, 8, 3) median colour near each square's top-right corner, or None if off-image."""
    H, W = lab.shape[:2]
    v = np.zeros((8, 8, 3), np.float32)
    for r in range(8):
        for c in range(8):
            ya, yb = int(y0 + (r + 0.1) * S), int(y0 + (r + 0.3) * S)
            xa, xb = int(x0 + (c + 0.7) * S), int(x0 + (c + 0.9) * S)
            if ya < 0 or xa < 0 or yb > H or xb > W or yb <= ya or xb <= xa:
                return None
            v[r, c] = np.median(lab[ya:yb, xa:xb].reshape(-1, 3), axis=0)
    return v


def _fit(v: np.ndarray) -> tuple[int, float]:
    """(squares fitting the checker pattern, contrast) for corner samples."""
    odd = _parity()
    med = [np.median(v[odd == p], axis=0) for p in (False, True)]
    dist = np.linalg.norm(v - np.where(odd[..., None], med[1], med[0]), axis=2)
    contrast = float(np.linalg.norm(med[0] - med[1]))
    return int((dist < max(6.0, 0.25 * contrast)).sum()), contrast


def _best_shift(img: np.ndarray, x0: float, y0: float, S: float) -> tuple[float, float]:
    """The refined grid can still be a whole square off (one row or column outside the
    board); keep whichever of the 9 one-square shifts fits the checker pattern best."""
    lab = _lab(img)
    best, best_key = (x0, y0), (-1, 0.0)
    for dy in (0, -1, 1):
        for dx in (0, -1, 1):
            v = _corner_samples(lab, x0 + dx * S, y0 + dy * S, S)
            if v is not None:
                key = _fit(v)
                if key[0] > best_key[0]:
                    best, best_key = (x0 + dx * S, y0 + dy * S), key
    return best


def _measure(img: np.ndarray, x0: float, y0: float, S: float) -> tuple[int, float, float, float, float]:
    """Robust checker statistics, grid-line sharpness and noise for a full-resolution grid."""
    lab = _lab(img)
    v = _corner_samples(lab, x0, y0, S)
    if v is None:
        return 0, 0.0, 99.0, 0.0, 99.0
    consistent, contrast = _fit(v)
    odd = _parity()
    med = [np.median(v[odd == p], axis=0) for p in (False, True)]
    spread = float(np.median(np.linalg.norm(v - np.where(odd[..., None], med[1], med[0]), axis=2)))
    noise = [float(lab[int(y0 + (r + 0.1) * S):int(y0 + (r + 0.3) * S),
                       int(x0 + (c + 0.7) * S):int(x0 + (c + 0.9) * S), 0].std())
             for r in range(8) for c in range(8)]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    ya, yb = int(y0 + 0.5 * S), int(y0 + 7.5 * S)
    on = np.mean([gx[ya:yb, int(round(x0 + k * S))].mean() for k in range(1, 8)])
    off = np.mean([gx[ya:yb, int(round(x0 + (k + 0.5) * S))].mean() for k in range(0, 8)]) + 1e-3
    return consistent, contrast, spread, float(on / off), float(np.median(noise))


def locate_board(img: np.ndarray) -> Grid | None:
    """Find a flat axis-aligned board in a BGR image, or None."""
    h, w = img.shape[:2]
    f = min(1.0, COARSE_SIDE / min(h, w))
    small = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else img
    found = _coarse_search(_lab(small))
    if found is None:
        return None
    S, x0, y0 = (v / f for v in found)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # Grid lines repeat every square, so a search of +-0.55 squares can't lock onto the wrong one.
    x0, y0, S = _refine(gray, x0, y0, S, reach=0.55 * S + 2 / f, size_reach=1 / f + 0.5)
    x0, y0 = _best_shift(img, x0, y0, S)
    # Keep the board inside the image (it often spans the full width of a phone screen).
    if x0 < -2 or y0 < -2 or x0 + 8 * S > w + 2 or y0 + 8 * S > h + 2 or S < 8:
        return None
    x0, y0 = max(0.0, x0), max(0.0, y0)
    S = min(S, (w - x0) / 8, (h - y0) / 8)
    return Grid(x0, y0, S, *_measure(img, x0, y0, S))


def looks_like_screenshot(grid: Grid | None) -> bool:
    """A rendered board: nearly every square fits the pattern, squares of one colour are
    practically identical, and the grid lines are crisp. Spread is the main test: on 105
    real photos the flattest scored >= 4.6, screenshots score 0-1.3 (small, heavily
    compressed JPEGs included). Noise is a backstop: photos >= 1.2, large screenshots ~0,
    but small JPEG screenshots reach ~1.7 from compression ringing."""
    return (grid is not None and grid.consistent >= 54 and grid.spread <= 2.5 and grid.noise <= 2.0
            and grid.contrast >= 20 and grid.sharpness >= 3.0)


# --- per-square segmentation ---

@dataclass
class Square:
    mask: np.ndarray      # T x T float32 silhouette (0/1); all zeros when empty
    area: float           # silhouette area as a fraction of the square
    lum: float            # median brightness (0-255) inside the silhouette


def _square(lab: np.ndarray) -> Square:
    """Silhouette of whatever stands on a T x T square crop (8-bit Lab)."""
    band, corner = max(2, T // 16), T // 4
    ring = np.concatenate([
        lab[:band, corner:-corner].reshape(-1, 3), lab[-band:, corner:-corner].reshape(-1, 3),
        lab[corner:-corner, :band].reshape(-1, 3), lab[corner:-corner, -band:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    fg = (np.linalg.norm(lab - bg, axis=2) > FG_DIST).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # Keep blobs that reach the centre: pieces do, coordinate labels in corners don't.
    n, labels = cv2.connectedComponents(fg)
    centre = labels[int(T * 0.3):int(T * 0.7), int(T * 0.3):int(T * 0.7)]
    keep = np.isin(labels, [i for i in np.unique(centre) if i])
    if not keep.any():
        return Square(np.zeros((T, T), np.float32), 0.0, 0.0)
    # Fill holes: white pieces on light squares often only show their outline. Anything
    # not connected to the square's border is inside the piece.
    background = np.pad((~keep).astype(np.uint8), 1, constant_values=1)
    _, regions = cv2.connectedComponents(background, connectivity=4)
    sil = keep | (regions[1:-1, 1:-1] != regions[0, 0])
    inner = cv2.erode(sil.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    pick = inner if inner.sum() >= 20 else sil
    return Square(sil.astype(np.float32), float(sil.mean()), float(np.median(lab[..., 0][pick])))


def squares(img: np.ndarray, grid: Grid) -> list[Square]:
    """Segment all 64 squares, in screen order (top-left first)."""
    lab = _lab(img)
    S, inset = grid.size, grid.size * 0.03
    out = []
    for r in range(8):
        for c in range(8):
            ya, yb = int(round(grid.y0 + r * S + inset)), int(round(grid.y0 + (r + 1) * S - inset))
            xa, xb = int(round(grid.x0 + c * S + inset)), int(round(grid.x0 + (c + 1) * S - inset))
            crop = cv2.resize(lab[ya:yb, xa:xb], (T, T), interpolation=cv2.INTER_AREA)
            out.append(_square(crop))
    return out


# --- templates ---

@dataclass
class Profile:
    name: str
    masks: dict[str, np.ndarray]   # piece letter -> T x T float32 average silhouette
    lums: dict[str, float]         # piece letter -> fill brightness

    @property
    def white_lum(self) -> float:
        return float(np.median([self.lums[p] for p in "PNBRQK"]))

    @property
    def black_lum(self) -> float:
        return float(np.median([self.lums[p] for p in "pnbrqk"]))


def expand(placement: str) -> list[str]:
    out = []
    for row in placement.split("/"):
        for ch in row:
            out += ["."] * int(ch) if ch.isdigit() else [ch]
    return out


def build_profile(name: str, img: np.ndarray, grid: Grid, placement: str = START) -> Profile:
    """Templates from a screenshot showing a known position (white at the bottom):
    each piece's silhouettes are averaged over its copies."""
    cells = squares(img, grid)
    masks, lums = {}, {}
    for piece in PIECES:
        found = [sq for sq, p in zip(cells, expand(placement)) if p == piece and sq.area >= EMPTY_AREA]
        if not found:
            raise ValueError(f"no {piece!r} found on the calibration board")
        masks[piece] = np.mean([sq.mask for sq in found], axis=0).astype(np.float32)
        lums[piece] = float(np.median([sq.lum for sq in found]))
    return Profile(name, masks, lums)


def _ncc(mask: np.ndarray, template: np.ndarray, pad: int = 3) -> float:
    """Best normalised correlation with a few pixels of slack in any direction."""
    padded = cv2.copyMakeBorder(mask, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    return float(cv2.matchTemplate(padded, template, cv2.TM_CCOEFF_NORMED).max())


@dataclass
class Guess:
    piece: str                      # FEN letter or "."
    confidence: float
    alternatives: list[list]        # [[piece, score], ...] best first


def colour_split(lums: list[float]) -> tuple[float, float] | None:
    """(threshold, gap) separating light from dark pieces on this board (Otsu over the
    fill brightness of occupied squares), or None without two clearly distinct groups.
    Every legal position has both kings, so a real board always has both groups."""
    v = np.sort(np.asarray(lums, np.float64))
    best = None
    for i in range(1, len(v)):
        lo, hi = v[:i], v[i:]
        between = len(lo) * len(hi) * (hi.mean() - lo.mean()) ** 2
        if best is None or between > best[0]:
            best = (between, (lo.max() + hi.min()) / 2, hi.mean() - lo.mean())
    return (best[1], best[2]) if best and best[2] >= 40 else None


def classify(sq: Square, profiles: list[Profile], split: tuple[float, float] | None = None) -> Guess:
    if sq.area < EMPTY_AREA:
        return Guess(".", round(float(np.clip((0.04 - sq.area) / 0.03, 0, 1)), 3), [])
    # Piece type from the silhouette (both colours share a shape), best over all profiles.
    scores = {t: -1.0 for t in TYPES}
    best_profile, best_score = profiles[0], -1.0
    for prof in profiles:
        for t in TYPES:
            s = max(_ncc(sq.mask, prof.masks[t]), _ncc(sq.mask, prof.masks[t.upper()]))
            scores[t] = max(scores[t], s)
            if s > best_score:
                best_profile, best_score = prof, s
    ranked = sorted(scores, key=scores.get, reverse=True)
    s1, s2 = scores[ranked[0]], scores[ranked[1]]
    if split:
        # Colour relative to the other pieces on this board.
        threshold, _ = split
        white = sq.lum > threshold
        c_color = np.clip(abs(sq.lum - threshold) / 20, 0, 1)
    else:
        # Compare with this piece type's white and black versions in the profile
        # (a white queen with lots of black detail is darker than a white pawn).
        lw, lb = best_profile.lums[ranked[0].upper()], best_profile.lums[ranked[0]]
        d_white, d_black = abs(sq.lum - lw), abs(sq.lum - lb)
        white = d_white < d_black
        c_color = np.clip(2 * abs(d_white - d_black) / (abs(lw - lb) + 1e-6), 0, 1)
    # Similar silhouettes (pawn/bishop) correlate highly, so judge the lead relative to
    # how far the runner-up is from a perfect match.
    lead = (s1 - s2) / max(1e-6, 1 - s2)
    c_type = np.clip((s1 - 0.5) / 0.35, 0, 1) * np.clip(lead / 0.5, 0, 1)
    case = str.upper if white else str.lower
    alts = [[case(t), round(scores[t], 3)] for t in ranked[:3]]
    return Guess(case(ranked[0]), round(float(c_type * c_color), 3), alts)


SQUARE_NAMES = [f"{f}{r}" for r in range(8, 0, -1) for f in "abcdefgh"]   # screen order, white at the bottom
UNCERTAIN_BELOW = 0.6


def recognize(img: np.ndarray, grid: Grid, profiles: list[Profile]):
    """Recognition (same shape as the camera pipeline's) for a located screenshot board."""
    from .recognizer import Recognition, board_to_fen

    cells = squares(img, grid)
    split = colour_split([sq.lum for sq in cells if sq.area >= EMPTY_AREA])
    guesses = [classify(sq, profiles, split) for sq in cells]
    board = [[guesses[r * 8 + c].piece for c in range(8)] for r in range(8)]
    conf = {name: g.confidence for name, g in zip(SQUARE_NAMES, guesses)}
    return Recognition(
        placement=board_to_fen(board),
        confidence=round(min(conf.values()), 3),
        squares=conf,
        uncertain=[name for name, c in conf.items() if c < UNCERTAIN_BELOW],
        repairs=[],
        alternatives={name: g.alternatives for name, g in zip(SQUARE_NAMES, guesses) if g.confidence < 0.9 and g.alternatives},
        corners=[[round(x, 1), round(y, 1)] for x, y in grid.corners],
        source="screenshot",
    )


def builtin_profile() -> Profile | None:
    """Templates rendered from python-chess's own piece set (the one /img draws), so
    lichess-style boards work before any calibration. Built in memory; nothing is stored."""
    from .. import render

    if render.cairosvg is None:
        return None
    import chess
    import chess.svg

    size = 8 * 96
    svg = chess.svg.board(chess.Board(), size=size, coordinates=False)
    png = render.cairosvg.svg2png(bytestring=svg.encode())
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    grid = Grid(0.0, 0.0, img.shape[1] / 8, 64, 0.0, 0.0, 0.0, 0.0)
    return build_profile("builtin", img, grid)
