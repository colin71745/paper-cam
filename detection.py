"""Paper detection and homography fitting, shared by calibrate.py (one-shot)
and webcam.py --auto (continuous tracking)."""

import cv2
import numpy as np

ARUCO_DICT_ID = cv2.aruco.DICT_4X4_50
MARKER_IDS = [0, 1, 2, 3]  # TL, TR, BR, BL of the printed sheet

PAPER_ASPECTS = {"a4": 297 / 210, "letter": 11 / 8.5}  # long side / short side


class DetectionError(Exception):
    """Raised with a human-readable reason when no usable quad was found."""


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as TL, TR, BR, BL."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def find_paper_quad(frame: np.ndarray, min_area_frac: float = 0.05) -> np.ndarray:
    """Largest 4-sided contour in the frame, as ordered corners."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = min_area_frac * frame.shape[0] * frame.shape[1]
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(c) < min_area:
            break
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return order_corners(approx)
    raise DetectionError("no paper-sized quadrilateral found")


def _edge_points(gray: np.ndarray, p0, p1, n_samples=16, search=24):
    """Sub-pixel points on the strongest brightness transition along the
    p0->p1 edge, found by scanning perpendicular profiles."""
    p0 = np.asarray(p0, dtype=np.float64)
    p1 = np.asarray(p1, dtype=np.float64)
    edge = p1 - p0
    length = float(np.linalg.norm(edge))
    if length < 20:
        return []
    normal = np.array([-edge[1], edge[0]]) / length
    offsets = np.arange(-search, search + 1, dtype=np.float64)
    smooth = np.array([1, 4, 6, 4, 1], dtype=np.float64) / 16
    h, w = gray.shape
    points = []
    for t in np.linspace(0.12, 0.88, n_samples):
        base = p0 + t * edge
        coords = base[None, :] + offsets[:, None] * normal[None, :]
        x, y = coords[:, 0], coords[:, 1]
        if x.min() < 1 or y.min() < 1 or x.max() >= w - 2 or y.max() >= h - 2:
            continue
        x0 = np.floor(x).astype(int)
        y0 = np.floor(y).astype(int)
        fx, fy = x - x0, y - y0
        prof = (gray[y0, x0] * (1 - fx) * (1 - fy)
                + gray[y0, x0 + 1] * fx * (1 - fy)
                + gray[y0 + 1, x0] * (1 - fx) * fy
                + gray[y0 + 1, x0 + 1] * fx * fy)
        prof = np.convolve(prof, smooth, mode="same")
        d = prof[2:] - prof[:-2]  # centered derivative at offset index i+1
        mag = np.abs(d)
        i = int(np.argmax(mag))
        if mag[i] < 6:  # too weak to be a real edge here (e.g. occluded)
            continue
        # Parabolic sub-pixel interpolation around the peak.
        delta = 0.0
        if 0 < i < len(mag) - 1:
            denom = mag[i - 1] - 2 * mag[i] + mag[i + 1]
            if abs(denom) > 1e-9:
                delta = float(np.clip(0.5 * (mag[i - 1] - mag[i + 1]) / denom,
                                      -1, 1))
        points.append(base + (offsets[i + 1] + delta) * normal)
    return points


def _fit_line(points):
    """Total-least-squares line fit with one outlier-rejection pass.
    Returns (centroid, unit direction)."""
    pts = np.asarray(points, dtype=np.float64)
    for _ in range(2):
        c = pts.mean(axis=0)
        _, _, vt = np.linalg.svd(pts - c)
        v = vt[0]
        if len(pts) <= 6:
            break
        rel = pts - c
        residuals = np.abs(rel[:, 0] * v[1] - rel[:, 1] * v[0])
        keep = residuals < max(2.0, 2.5 * residuals.mean())
        if keep.all():
            break
        pts = pts[keep]
        if len(pts) < 6:
            return None
    return c, v


def _intersect(line_a, line_b):
    (ca, va), (cb, vb) = line_a, line_b
    denom = va[0] * vb[1] - va[1] * vb[0]
    if abs(denom) < 1e-9:
        return None
    t = ((cb[0] - ca[0]) * vb[1] - (cb[1] - ca[1]) * vb[0]) / denom
    return ca + t * va


def refine_quad(gray: np.ndarray, quad: np.ndarray,
                max_shift: float = 30.0) -> np.ndarray:
    """Refine a coarse quad to sub-pixel accuracy against the full-res image.

    Coarse contour detection (downscaled, Canny+dilate) lands a few pixels
    outside the true paper edge; this scans across each edge for the actual
    brightness transition, fits a line per edge, and intersects them. Any
    edge or corner that can't be refined confidently keeps its coarse value.
    """
    lines = []
    for i in range(4):
        pts = _edge_points(gray, quad[i], quad[(i + 1) % 4])
        line = _fit_line(pts) if len(pts) >= 6 else None
        if line is None:  # fall back to the coarse edge
            p0 = np.asarray(quad[i], dtype=np.float64)
            edge = np.asarray(quad[(i + 1) % 4], dtype=np.float64) - p0
            line = (p0, edge / max(np.linalg.norm(edge), 1e-9))
        lines.append(line)
    refined = quad.astype(np.float32).copy()
    for i in range(4):  # corner i joins edge i-1 and edge i
        p = _intersect(lines[(i + 3) % 4], lines[i])
        if p is not None and np.linalg.norm(p - quad[i]) <= max_shift:
            refined[i] = p
    return refined


def aruco_dictionary():
    return cv2.aruco.getPredefinedDictionary(ARUCO_DICT_ID)


def find_aruco_quad(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    dictionary = aruco_dictionary()
    try:  # OpenCV >= 4.7
        detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
    except AttributeError:  # older API (Bookworm ships 4.6)
        corners, ids, _ = cv2.aruco.detectMarkers(gray, dictionary)
    if ids is None:
        raise DetectionError("no ArUco markers found")
    found = {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}
    missing = [m for m in MARKER_IDS if m not in found]
    if missing:
        raise DetectionError(f"missing ArUco markers {missing}")
    # Marker i sits at sheet corner i, and its own corner i (ArUco corner
    # order is TL,TR,BR,BL) is the one nearest the sheet corner.
    return np.array([found[i][i] for i in MARKER_IDS], dtype=np.float32)


def quad_aspect(quad: np.ndarray) -> float:
    """Approximate width/height of the quad from its image edge lengths.

    Ignores foreshortening, so it's only accurate when the camera is roughly
    overhead. Used as the fallback when no focal length is configured, and
    for cheap plausibility checks.
    """
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    left = np.linalg.norm(quad[3] - quad[0])
    right = np.linalg.norm(quad[2] - quad[1])
    return ((top + bottom) / 2) / ((left + right) / 2)


def rectangle_aspect(
    quad: np.ndarray, focal_px: float, principal_point: tuple
) -> float:
    """Exact width/height of the real-world rectangle behind the quad.

    Closed form from Zhang & He, "Whiteboard Scanning and Image Enhancement"
    (2004). The constraint that makes it solvable is that opposite edges of
    the rectangle are equal: how unequal they appear in the image encodes the
    plane's 3D orientation, and with the camera focal length + principal
    point known, the true aspect follows for any tilt direction.

    Raises ValueError on numerically degenerate quads (caller falls back to
    quad_aspect).
    """
    cx, cy = principal_point
    q = quad.astype(np.float64)

    def m(i: int) -> np.ndarray:  # homogeneous coords, principal point at origin
        return np.array([q[i, 0] - cx, q[i, 1] - cy, 1.0])

    # Zhang's labeling: m1=TL, m2=TR, m3=BL, m4=BR (ours is TL,TR,BR,BL).
    m1, m2, m3, m4 = m(0), m(1), m(3), m(2)
    c14 = np.cross(m1, m4)
    d2 = np.dot(np.cross(m2, m4), m3)
    d3 = np.dot(np.cross(m3, m4), m2)
    if abs(d2) < 1e-9 or abs(d3) < 1e-9:
        raise ValueError("degenerate quad")
    n2 = (np.dot(c14, m3) / d2) * m2 - m1  # direction of the rectangle's width
    n3 = (np.dot(c14, m2) / d3) * m3 - m1  # direction of the rectangle's height
    f2 = float(focal_px) ** 2
    num = (n2[0] ** 2 + n2[1] ** 2) / f2 + n2[2] ** 2
    den = (n3[0] ** 2 + n3[1] ** 2) / f2 + n3[2] ** 2
    if num <= 0 or den <= 0 or not np.isfinite(num / den):
        raise ValueError("degenerate quad")
    return float(np.sqrt(num / den))


def true_aspect(
    quad: np.ndarray, focal_px=None, principal_point=None
) -> float:
    """Best available width/height estimate for the quad."""
    if focal_px is not None and principal_point is not None:
        try:
            aspect = rectangle_aspect(quad, focal_px, principal_point)
            if np.isfinite(aspect) and aspect > 0:
                return aspect
        except ValueError:
            pass
    return quad_aspect(quad)


def dest_rect(
    src: np.ndarray,
    output_size: tuple,
    paper: str = "auto",
    portrait: bool = False,
    rotate: int = 0,
    focal_px=None,
    principal_point=None,
) -> np.ndarray:
    """A rectangle with the paper's aspect ratio, fitted inside output_size."""
    if paper == "auto":
        aspect = true_aspect(src, focal_px, principal_point)
    else:
        long_short = PAPER_ASPECTS[paper]
        aspect = 1 / long_short if portrait else long_short
    if rotate in (90, 270):
        aspect = 1 / aspect

    out_w, out_h = output_size
    if out_w / out_h > aspect:
        h, w = out_h, out_h * aspect
    else:
        w, h = out_w, out_w / aspect
    x0, y0 = (out_w - w) / 2, (out_h - h) / 2
    dst = np.array(
        [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]], dtype=np.float32
    )
    # Rotating the output = shifting which dest corner each src corner maps to.
    return np.roll(dst, -rotate // 90, axis=0)


def homography_for(quad: np.ndarray, output_size: tuple, **kwargs) -> np.ndarray:
    return cv2.getPerspectiveTransform(quad, dest_rect(quad, output_size, **kwargs))
