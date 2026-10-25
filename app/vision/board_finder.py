"""Find an axis-aligned 8x8 board inside a larger image by its checker pattern.

Used as a pre-crop before screenshot recognition: a phone screenshot has avatars, a move
list and buttons around the board. The search runs on a downscaled copy (squares of one
colour must be uniform and alternate), then snaps the grid to the grid lines at full
resolution and checks it isn't one square off.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

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
    """A flat digital board: nearly every square fits the checker pattern, squares of one
    colour are practically identical, and the grid lines are crisp. Spread is the main
    test: on 104 real camera photos the flattest scored >= 4.6, while screenshots score
    0-1.3 (small, heavily compressed JPEGs included). Noise is a backstop: photos >= 1.2,
    large screenshots ~0, small JPEG screenshots up to ~1.7."""
    return (grid is not None and grid.consistent >= 54 and grid.spread <= 2.5 and grid.noise <= 2.0
            and grid.contrast >= 20 and grid.sharpness >= 3.0)
