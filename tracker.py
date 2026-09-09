"""Continuous paper tracking for webcam.py --auto.

A background thread periodically runs paper detection on a downscaled copy
of the latest frame. The homography only changes when the paper has clearly
moved AND settled in its new position ("snap", not "chase"):

- detection failed (hand in the way, paper mid-air, glare) -> hold current H
- no detection at all for AUTO_LOST_TIMEOUT seconds -> the paper is gone:
  revert to the raw view, and re-lock when a page reappears
- quad matches the currently applied position -> nothing to do
- quad in a new position -> becomes a candidate; adopted only after it stays
  put for AUTO_STABLE_DETECTIONS consecutive detections

The output aspect ratio is recomputed from the quad at each adoption, so any
paper shape works (A4, root-2 anything, an open notebook spread) and never
"breathes" between adoptions.
"""

import threading
import time

import cv2
import numpy as np

from config import (
    OUTPUT_SIZE,
    AUTO_DETECT_INTERVAL,
    AUTO_DETECT_SCALE,
    AUTO_STABLE_DETECTIONS,
    AUTO_MOVE_TOLERANCE,
    AUTO_LOST_TIMEOUT,
    focal_px,
    principal_point,
)
from detection import (
    DetectionError,
    find_paper_quad,
    find_aruco_quad,
    homography_for,
    quad_aspect,
    refine_quad,
    true_aspect,
)

# Sanity limits for a "plausible paper" quad, as fractions of the frame area
# and width/height aspect. Rejects the detector latching onto a keyboard key
# (too small) or the whole desk edge (too big / too elongated).
MIN_AREA_FRAC = 0.03
MAX_AREA_FRAC = 0.95
MIN_ASPECT = 0.25
MAX_ASPECT = 4.0


def _quads_match(a: np.ndarray, b: np.ndarray, tol: float) -> bool:
    return float(np.max(np.linalg.norm(a - b, axis=1))) < tol


def _implausible_reason(quad: np.ndarray, frame_shape):
    """None if the quad could be a page, else why it was rejected."""
    area = cv2.contourArea(quad.astype(np.float32))
    frac = area / (frame_shape[0] * frame_shape[1])
    aspect = quad_aspect(quad)
    if frac < MIN_AREA_FRAC:
        return (f"quad too small ({frac * 100:.1f}% of frame, need "
                f"{MIN_AREA_FRAC * 100:.0f}%) - move the camera closer")
    if frac > MAX_AREA_FRAC:
        return f"quad too large ({frac * 100:.1f}% of frame)"
    if not MIN_ASPECT <= aspect <= MAX_ASPECT:
        return f"aspect {aspect:.2f} outside {MIN_ASPECT}-{MAX_ASPECT}"
    return None


class AutoTracker:
    """Homography provider that follows the paper as it's repositioned."""

    def __init__(self, use_aruco: bool = False, rotate: int = 0, initial_H=None):
        self.H = initial_H  # read by the capture loop
        self.use_aruco = use_aruco
        self.rotate = rotate
        self._applied_quad = None
        self._candidate = None
        self._stable = 0
        self._last_seen = time.monotonic()
        self._last_note = 0.0
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        thread = threading.Thread(target=self._detect_loop, daemon=True)
        thread.start()

    def maybe_reload(self) -> None:  # same interface as Calibration
        pass

    def _note(self, msg: str, every: float = 5.0) -> None:
        """Rate-limited explanation of what detection is doing, so a page
        that never locks says why instead of failing silently."""
        now = time.monotonic()
        if now - self._last_note >= every:
            self._last_note = now
            print(f"detect: {msg}")

    def submit_frame(self, frame: np.ndarray) -> None:
        """Called by the capture loop; hands the detector its input."""
        with self._frame_lock:
            self._latest_frame = frame

    def _detect_loop(self) -> None:
        while True:
            time.sleep(AUTO_DETECT_INTERVAL)
            with self._frame_lock:
                frame = self._latest_frame
                self._latest_frame = None
            if frame is None:
                # No input to judge (e.g. --on-demand idle): the lost timer
                # measures time spent looking and failing, not time not
                # looking, so keep it fresh and hold the current view.
                self._last_seen = time.monotonic()
                continue
            small = cv2.resize(
                frame, None, fx=AUTO_DETECT_SCALE, fy=AUTO_DETECT_SCALE,
                interpolation=cv2.INTER_AREA,
            )
            try:
                quad = (find_aruco_quad if self.use_aruco else find_paper_quad)(small)
            except DetectionError as exc:
                self._note(str(exc))
                self._check_lost()
                continue
            reason = _implausible_reason(quad, small.shape)
            if reason is not None:
                self._note(reason)
                self._check_lost()
                continue
            self._last_seen = time.monotonic()
            quad = quad / AUTO_DETECT_SCALE
            if not self.use_aruco:
                # Sub-pixel refinement against the full-res frame: removes
                # the coarse detector's outward bias (a border around the
                # paper) and its corner error (a slightly crooked crop).
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                quad = refine_quad(gray, quad)
            self._offer(quad)

    def _check_lost(self) -> None:
        """No paper found this tick: after a sustained absence, drop the
        held homography so the raw view shows. Brief occlusions (a hand,
        glare) don't reach the timeout and keep the current view."""
        if self.H is None:
            return
        if time.monotonic() - self._last_seen > AUTO_LOST_TIMEOUT:
            self.H = None
            self._applied_quad = None
            self._candidate, self._stable = None, 0
            print("Paper lost — showing raw view.")

    def _offer(self, quad: np.ndarray) -> None:
        if self._applied_quad is not None and _quads_match(
            quad, self._applied_quad, AUTO_MOVE_TOLERANCE
        ):
            # Paper hasn't moved (beyond jitter); drop any pending candidate.
            self._candidate, self._stable = None, 0
            return
        if self._candidate is not None and _quads_match(
            quad, self._candidate, AUTO_MOVE_TOLERANCE
        ):
            self._stable += 1
            self._candidate = quad
            if self._stable >= AUTO_STABLE_DETECTIONS:
                self._adopt(quad)
        else:
            self._candidate, self._stable = quad, 1
            self._note("paper seen, waiting for it to settle")

    def _adopt(self, quad: np.ndarray) -> None:
        self.H = homography_for(
            quad, OUTPUT_SIZE, rotate=self.rotate,
            focal_px=focal_px(), principal_point=principal_point(),
        )
        self._applied_quad = quad
        self._candidate, self._stable = None, 0
        aspect = true_aspect(quad, focal_px(), principal_point())
        print(f"Paper locked at new position (aspect {aspect:.2f}).")
