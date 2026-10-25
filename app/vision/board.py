# Vendored from ChessLens (https://github.com/zeljko1611/ChessLens, commit c4e27c1),
# MIT License, Copyright (c) 2026 zeljko1611. See app/vision/LICENSE.
# Local changes: SciPy cKDTree replaced with NumPy brute-force nearest neighbours.
"""Chessboard detection without ML.

Saddle points (X-corners) are fitted with RANSAC into a lattice and the 9x9 window that is the board is
chosen; quadrilateral contours are the fallback. Returns the playing-area corners clockwise from the top-left.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

WORK_SIZE = 1000  # detection runs on a downscaled image (longest side)
LATTICE_N = 400  # size of the rectified candidate used for the grid search
REFINE_CELL = 16  # pixels per square during refinement
RING_WEIGHT = 0.1  # how many nodes one grey level of ring_score is worth
PLAIN_WEIGHT = 1.0  # weight of the unsigned term in ring_score
# Minimum grid_score for a detection to count as a board. Measured: real boards score
# 107+ (RANSAC) and 142+ (contours); images without a board score at most 16 and 60.
MIN_GRID_SCORE = 80.0
WINDOW_SLACK = 3  # windows with up to this many fewer nodes than the best are compared by ring_score


@dataclass
class BoardDetection:
    corners: np.ndarray  # (4, 2) float32, TL, TR, BR, BL in the original image
    score: float

    def homography(self, cell: int = 1) -> np.ndarray:
        """Homography from the image to a grid where one square is `cell` pixels."""
        dst = np.array([[0, 0], [8 * cell, 0], [8 * cell, 8 * cell], [0, 8 * cell]], np.float32)
        return cv2.getPerspectiveTransform(self.corners.astype(np.float32), dst)


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Order four points clockwise: TL, TR, BR, BL."""
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    center = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    pts = pts[np.argsort(ang)]  # clockwise in image coordinates (y points down)
    start = int(np.argmin(pts.sum(axis=1)))
    return np.roll(pts, -start, axis=0)


def _quad_ok(q: np.ndarray, img_area: float) -> bool:
    if not cv2.isContourConvex(q.reshape(-1, 1, 2).astype(np.int32)):
        return False
    area = cv2.contourArea(q.astype(np.float32))
    if area < 0.04 * img_area:
        return False
    sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
    return min(sides) > 0.2 * max(sides)


def _approx_quad(contour: np.ndarray) -> np.ndarray | None:
    hull = cv2.convexHull(contour)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            return approx.reshape(4, 2).astype(np.float32)
    if hasattr(cv2, "approxPolyN"):  # OpenCV >= 4.11: exactly N vertices
        try:
            q = cv2.approxPolyN(hull, 4)
            return q.reshape(4, 2).astype(np.float32)
        except cv2.error:
            return None
    return None


def candidate_quads(gray: np.ndarray, max_candidates: int = 12) -> list[np.ndarray]:
    h, w = gray.shape
    img_area = float(h * w)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    masks = []
    med = float(np.median(blur))
    for lo, hi in ((0.5 * med, 1.2 * med), (20, 60), (40, 120)):
        e = cv2.Canny(blur, lo, hi)
        masks.append(cv2.dilate(e, np.ones((3, 3), np.uint8), iterations=2))
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 5)
    masks.append(255 - th)
    masks.append(th)

    quads: list[tuple[float, np.ndarray]] = []
    for m in masks:
        contours, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:15]
        for cnt in contours:
            if cv2.contourArea(cnt) < 0.04 * img_area:
                break
            q = _approx_quad(cnt)
            if q is not None and _quad_ok(order_corners(q), img_area):
                quads.append((cv2.contourArea(q), order_corners(q)))

    # Remove duplicates (nearly identical quadrilaterals from different masks).
    unique: list[np.ndarray] = []
    for _, q in sorted(quads, key=lambda t: -t[0]):
        if all(np.abs(q - u).max() > 0.02 * max(h, w) for u in unique):
            unique.append(q)
    return unique[:max_candidates]


def grid_score(gray: np.ndarray, corners: np.ndarray, cell: int = REFINE_CELL) -> float:
    """How well four corners describe an 8x8 checkerboard in the image.

    - A square's value is the median of its pixels, so a piece covering less
      than half of the square does not change it.
    - Neighbouring squares must differ in EVERY row and column; the outer rows
      and columns are checked separately, because a grid shifted by one square
      includes a row of frame or background with no contrast.
    - Edges must be stronger on the grid lines than in the middle of squares
      (this gives sub-square precision).
    """
    size = 8 * cell
    dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], np.float32)
    H = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    warped = cv2.warpPerspective(gray, H, (size, size), flags=cv2.INTER_AREA).astype(np.float32)
    m = 2
    cells = warped.reshape(8, cell, 8, cell)[:, m:-m, :, m:-m].transpose(0, 2, 1, 3).reshape(8, 8, -1)
    v = np.median(cells, axis=2)
    dh = np.abs(v[:, :-1] - v[:, 1:])  # 8 x 7
    dv = np.abs(v[:-1, :] - v[1:, :])  # 7 x 8
    overall = float(np.median(np.concatenate([dh.ravel(), dv.ravel()])))
    boundary = min(float(np.median(dh[0])), float(np.median(dh[7])), float(np.median(dv[:, 0])), float(np.median(dv[:, 7])))

    gx = np.abs(cv2.Sobel(warped, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(warped, cv2.CV_32F, 0, 1, ksize=3))
    idx = np.arange(1, 8) * cell
    on_lines = gx[:, idx].mean() + gy[idx, :].mean()
    mid = idx - cell // 2
    off_lines = gx[:, mid].mean() + gy[mid, :].mean() + 1e-3
    edge_ratio = min(float(on_lines / off_lines), 4.0)
    return (0.5 * overall + boundary) * edge_ratio**0.5


def find_lattice(gray: np.ndarray, quad: np.ndarray) -> tuple[np.ndarray, float]:
    """Find the 8x8 playing area inside a rectified quadrilateral."""
    N = LATTICE_N
    dst = np.array([[0, 0], [N, 0], [N, N], [0, N]], np.float32)
    H = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    warped = cv2.warpPerspective(gray, H, (N, N), flags=cv2.INTER_AREA).astype(np.float32)

    best = (-1.0, 0.0, 0.0, N / 8)
    for s in np.arange(N / 8 * 0.62, N / 8 * 1.001, 1.0):
        k = max(3, int(s * 0.5) | 1)
        box = cv2.blur(warped, (k, k))
        span = int(8 * s)
        if span > N:
            continue
        lim = N - span
        offs = np.arange(0, lim + 1, 2)
        # Values at the square centres for all offsets at once.
        acc = np.zeros((len(offs), len(offs)), np.float32)
        for r in range(8):
            for c in range(8):
                ys = np.clip((offs + (r + 0.5) * s).astype(int), 0, N - 1)
                xs = np.clip((offs + (c + 0.5) * s).astype(int), 0, N - 1)
                v = box[np.ix_(ys, xs)]
                acc += v if (r + c) % 2 == 0 else -v
        score = np.abs(acc) / 64.0
        i, j = np.unravel_index(int(np.argmax(score)), score.shape)
        if score[i, j] > best[0]:
            best = (float(score[i, j]), float(offs[j]), float(offs[i]), float(s))

    _, ox, oy, s = best
    inner = np.array([[ox, oy], [ox + 8 * s, oy], [ox + 8 * s, oy + 8 * s], [ox, oy + 8 * s]], np.float32)
    corners = cv2.perspectiveTransform(inner[None], np.linalg.inv(H))[0]
    return corners, grid_score(gray, corners)


def refine_corners(gray: np.ndarray, corners: np.ndarray, iters: int = 3) -> tuple[np.ndarray, float]:
    """Coordinate ascent: move each corner while the grid score improves."""
    corners = corners.astype(np.float32).copy()
    best = grid_score(gray, corners)
    scale = np.linalg.norm(corners[0] - corners[2]) / 8.0  # ~square size
    for step in (0.25 * scale, 0.1 * scale, 0.04 * scale):
        for _ in range(iters):
            improved = False
            for i in range(4):
                for d in ((step, 0), (-step, 0), (0, step), (0, -step)):
                    cand = corners.copy()
                    cand[i] += d
                    sc = grid_score(gray, cand)
                    if sc > best:
                        best, corners, improved = sc, cand, True
            if not improved:
                break
    return corners, best


def saddle_points(gray: np.ndarray, max_points: int = 400) -> np.ndarray:
    g = gray.astype(np.float32)
    pts, resp_all = [], []
    for sigma in (1.5, 2.5, 4.0):
        s = cv2.GaussianBlur(g, (0, 0), sigma)
        gxx = cv2.Sobel(s, cv2.CV_32F, 2, 0, ksize=3)
        gyy = cv2.Sobel(s, cv2.CV_32F, 0, 2, ksize=3)
        gxy = cv2.Sobel(s, cv2.CV_32F, 1, 1, ksize=3)
        resp = -(gxx * gyy - gxy * gxy) * sigma**4  # scale normalisation
        resp[resp < 0] = 0
        win = max(5, int(3 * sigma) | 1)
        local_max = resp == cv2.dilate(resp, np.ones((win, win), np.uint8))
        thr = np.percentile(resp, 99.5)
        ys, xs = np.nonzero(local_max & (resp > max(thr, 1e-6)))
        pts.append(np.stack([xs, ys], 1))
        resp_all.append(resp[ys, xs])
    if not pts:
        return np.zeros((0, 2), np.float32)
    P = np.concatenate(pts).astype(np.float32)
    R = np.concatenate(resp_all)
    order = np.argsort(-R)
    kept: list[np.ndarray] = []
    for i in order:  # merge nearby points found at different scales
        if all(np.abs(P[i] - k).max() > 4 for k in kept):
            kept.append(P[i])
        if len(kept) >= max_points:
            break
    return np.array(kept, np.float32)


def _node_matches(H: np.ndarray, pts: np.ndarray, tol: float, radius: float = 99.0) -> tuple[np.ndarray, np.ndarray]:
    """The closest point to every grid node (if it is close enough).
    Returns (node coordinates, image points)."""
    g = cv2.perspectiveTransform(pts[None], np.linalg.inv(H))[0]
    rounded = np.round(g)
    dev = np.abs(g - rounded).max(axis=1)
    ok = (dev < tol) & (np.abs(rounded - 0.5).max(axis=1) < radius)
    best: dict[tuple[int, int], int] = {}
    for i in np.nonzero(ok)[0]:
        key = (int(rounded[i, 0]), int(rounded[i, 1]))
        if key not in best or dev[i] < dev[best[key]]:
            best[key] = i
    idx = np.array(list(best.values()), int)
    if len(idx) == 0:
        return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
    return rounded[idx].astype(np.float32), pts[idx]


def _grow_lattice(H: np.ndarray, pts: np.ndarray) -> np.ndarray | None:
    """A single-square hypothesis is only accurate locally (perspective, noise).
    So it is grown step by step: take the nodes near that square, re-estimate
    the homography (with RANSAC, since pieces also produce saddle points),
    then widen the radius."""
    q = cv2.perspectiveTransform(np.array([[[0, 0], [1, 0], [0, 1]]], np.float32), H)[0]
    size0 = float(np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[0])) / 2
    for radius, tol in ((1.6, 0.25), (3.2, 0.2), (6.5, 0.15), (99, 0.12)):
        try:
            nodes, img_pts = _node_matches(H, pts, tol, radius)
        except np.linalg.LinAlgError:
            return None
        if len(nodes) < 6:
            return H if radius > 2 else None
        H_new, _ = cv2.findHomography(nodes, img_pts, cv2.RANSAC, 0.12 * size0)
        if H_new is None or abs(np.linalg.det(H_new)) < 1e-12:
            return H
        # Guard against a degenerate solution (a tiny grid that "fits" everything).
        if not 0.6 < _cell_size(H_new, img_pts) / size0 < 1.6:
            return None
        H = H_new
    return H


def _cell_size(H: np.ndarray, pts: np.ndarray) -> float:
    """Average square side length (in pixels) around the centroid of the points."""
    c = cv2.perspectiveTransform(pts.mean(axis=0, keepdims=True)[None], np.linalg.inv(H))[0, 0]
    q = cv2.perspectiveTransform(np.array([[c, c + [1, 0], c + [0, 1]]], np.float32), H)[0]
    return float(np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[0])) / 2


def _reduce_basis(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """The same lattice can be described by a skewed basis, e.g. (1,0) and (1,1).
    Lagrange-Gauss reduction picks the shortest vectors, which are the square
    sides, so the grid axes match the rows and columns of the board."""
    c = cv2.perspectiveTransform(pts.mean(axis=0, keepdims=True)[None], np.linalg.inv(H))[0, 0]
    base = np.array([[c, c + [1, 0], c + [0, 1]]], np.float32)
    img_pts = cv2.perspectiveTransform(base, H)[0]
    b1, b2 = img_pts[1] - img_pts[0], img_pts[2] - img_pts[0]
    T = np.eye(2)  # columns: the new basis vectors in old coordinates
    for _ in range(20):
        if b2 @ b2 < b1 @ b1:
            b1, b2 = b2, b1
            T = T[:, ::-1].copy()
        mu = round(float(b1 @ b2) / float(b1 @ b1))
        if mu == 0:
            break
        b2 = b2 - mu * b1
        T[:, 1] -= mu * T[:, 0]
    A = np.eye(3)
    A[:2, :2] = T
    return H @ A


def ring_score(gray: np.ndarray, H: np.ndarray, x0: int, y0: int, cell: int = 16) -> float:
    """Inside the board neighbouring squares differ; just outside it (frame,
    table) they do not. Comparing the outer ring of board squares with the ring
    just outside picks the right position when the border lines have no saddle
    points."""
    size = 10 * cell
    grid = np.array([[x0 - 1, y0 - 1], [x0 + 9, y0 - 1], [x0 + 9, y0 + 9], [x0 - 1, y0 + 9]], np.float32)
    src = cv2.perspectiveTransform(grid[None], H)[0]
    dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], np.float32)
    warped = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(src, dst), (size, size), flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)
    m = 2
    v = np.median(warped.astype(np.float32).reshape(10, cell, 10, cell)[:, m:-m, :, m:-m].transpose(0, 2, 1, 3).reshape(10, 10, -1), axis=2)

    # Expected checkerboard pattern (its sign) estimated from the board interior.
    # For each square, (v - mid) * pattern is positive when the square has the
    # colour the pattern predicts. The median along a row is robust to pieces,
    # and a uniform frame or table gives about 0 because the pattern alternates.
    parity = np.where(np.add.outer(np.arange(10), np.arange(10)) % 2 == 0, 1.0, -1.0)
    mid = float(np.median(v[2:8, 2:8]))
    sign = np.sign(np.mean((v[2:8, 2:8] - mid) * parity[2:8, 2:8])) or 1.0
    agree = (v - mid) * parity * sign

    # Each side separately: the inner row must follow the pattern and the row
    # just outside must not. A window shifted by one row breaks only one side,
    # which the median of the whole ring would miss but the per-side sum catches.
    sides = [
        (agree[1, 1:9], agree[0, 1:9]), (agree[8, 1:9], agree[9, 1:9]),
        (agree[1:9, 1], agree[1:9, 0]), (agree[1:9, 8], agree[1:9, 9]),
    ]
    signed = sum(np.median(inside) - abs(np.median(outside)) for inside, outside in sides)
    # Second term: unsigned neighbour contrast (handles boards without a frame).
    plain = sum(np.median(np.abs(np.diff(v[i]))) - np.median(np.abs(np.diff(v[o])))
                for i, o in ((np.s_[1, 1:9], np.s_[0, 1:9]), (np.s_[8, 1:9], np.s_[9, 1:9]), (np.s_[1:9, 1], np.s_[1:9, 0]), (np.s_[1:9, 8], np.s_[1:9, 9])))
    return float(signed + PLAIN_WEIGHT * plain)


def _place_window(gray: np.ndarray, H: np.ndarray, pts: np.ndarray) -> tuple[float, np.ndarray] | None:
    """For a lattice, find where exactly the 8x8 board is (which 9x9 line window).
    Returns (score = node count + ring correction, corners)."""
    nodes, img_pts = _node_matches(H, pts, 0.12)
    if len(nodes) < 8:
        return None
    # Inner corners are always saddle points, border corners only if the frame
    # differs from the squares. The 9x9 window with the most nodes wins; among
    # near ties the ring around the board decides.
    xs, ys = nodes[:, 0].astype(int), nodes[:, 1].astype(int)
    options = []
    for x0 in range(xs.min() - 8, xs.max() + 1):
        for y0 in range(ys.min() - 8, ys.max() + 1):
            inside = (xs >= x0) & (xs <= x0 + 8) & (ys >= y0) & (ys <= y0 + 8)
            if inside.sum() >= 8 and len(np.unique(xs[inside])) >= 4 and len(np.unique(ys[inside])) >= 4:
                options.append((int(inside.sum()), x0, y0, inside))
    if not options:
        return None
    top = max(o[0] for o in options)
    best: tuple[float, float, np.ndarray] | None = None
    for n, x0, y0, inside in options:
        if n < top - WINDOW_SLACK:
            continue
        Hw, _ = cv2.findHomography(nodes[inside], img_pts[inside], 0)
        if Hw is None:
            continue
        grid = np.array([[x0, y0], [x0 + 8, y0], [x0 + 8, y0 + 8], [x0, y0 + 8]], np.float32)
        corners = cv2.perspectiveTransform(grid[None], Hw)[0]
        if not _quad_ok(order_corners(corners), 0.0):
            continue
        key = n + RING_WEIGHT * ring_score(gray, Hw, x0, y0)
        if best is None or key > best[0]:
            best = (key, 0.0, corners)
    return None if best is None else (best[0], best[2])


def fit_grid(gray: np.ndarray, pts: np.ndarray, rng: np.random.Generator, iterations: int = 1000, keep: int = 4) -> np.ndarray | None:
    """RANSAC: a hypothesis is one square (four neighbouring X points) grown into
    the whole lattice. The few best hypotheses (by point count) are finally
    compared by the checkerboard pattern, which is the real judge, because small
    "lattices" through piece details can also collect many points."""
    if len(pts) < 9:
        return None
    # Nearest neighbours by brute force (at most a few hundred points, so no SciPy needed).
    dists = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
    nn = np.argsort(dists, axis=1)[:, : min(13, len(pts))]
    min_cell = max(gray.shape) / 40  # the board is not smaller than ~1/5 of the image
    unit = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    hyps: list[tuple[int, np.ndarray]] = []
    for _ in range(iterations):
        i = int(rng.integers(len(pts)))
        p = pts[i]
        a_idx, b_idx = rng.choice(nn[i][1:], 2, replace=False)
        a, b = pts[a_idx], pts[b_idx]
        va, vb = a - p, b - p
        la, lb = np.linalg.norm(va), np.linalg.norm(vb)
        if min(la, lb) < min_cell or max(la, lb) > 2.5 * min(la, lb):
            continue
        if abs(float(va @ vb) / (la * lb)) > 0.6:  # square sides are at least ~50° apart
            continue
        d_all = np.linalg.norm(pts - (a + b - p), axis=1)
        d_idx = int(np.argmin(d_all))
        dist = float(d_all[d_idx])
        if dist > 0.2 * min(la, lb):
            continue
        H = _grow_lattice(cv2.getPerspectiveTransform(unit, np.array([p, a, pts[d_idx], b], np.float32)), pts)
        if H is None or _cell_size(H, pts) < min_cell:
            continue
        H = _reduce_basis(H, pts)
        hyps.append((len(_node_matches(H, pts, 0.12)[0]), H))  # each node counts once

    hyps.sort(key=lambda t: -t[0])
    distinct: list[np.ndarray] = []
    for _, H in hyps:
        probe = cv2.perspectiveTransform(np.array([[[0, 0], [4, 4]]], np.float32), H)[0]
        if all(np.abs(probe - cv2.perspectiveTransform(np.array([[[0, 0], [4, 4]]], np.float32), D)[0]).max() > 3 for D in distinct):
            distinct.append(H)
        if len(distinct) >= keep:
            break

    best: tuple[float, np.ndarray] | None = None
    for H in distinct:
        placed = _place_window(gray, H, pts)
        if placed and (best is None or placed[0] > best[0]):
            best = placed
    return None if best is None else best[1]


def detect_board(image: np.ndarray, seed: int = 0) -> BoardDetection | None:
    """Main entry point: BGR image -> corners of the playing area, or None."""
    gray_full = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    scale = WORK_SIZE / max(gray_full.shape)
    scale = min(scale, 1.0)
    gray = cv2.resize(gray_full, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray_full
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    rng = np.random.default_rng(seed)  # deterministic result for the same image
    grid = fit_grid(gray, saddle_points(gray), rng)
    if grid is not None:
        corners = order_corners(grid)
        score = grid_score(gray, corners)
        if score >= MIN_GRID_SCORE:
            return BoardDetection(corners=corners / scale, score=score)

    # Fallback: quadrilateral contours + a grid search inside each.
    scored: list[tuple[float, np.ndarray]] = []
    for quad in candidate_quads(gray):
        corners = order_corners(find_lattice(gray, quad)[0])
        if _quad_ok(corners, 0.0):
            scored.append((grid_score(gray, corners), corners))
    if not scored:
        return None
    score, corners = max(scored, key=lambda t: t[0])
    corners, score = refine_corners(gray, corners)
    if score < MIN_GRID_SCORE:  # the fallback always finds some quadrilateral, even the image border
        return None
    return BoardDetection(corners=order_corners(corners) / scale, score=score)


def corners_from_user(points: list[list[float]]) -> BoardDetection:
    """Corners given by hand (e.g. dragged by the user in the UI)."""
    corners = order_corners(np.array(points, np.float32))
    return BoardDetection(corners=corners, score=float("nan"))
