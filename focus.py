#!/usr/bin/env python3
"""Focus and aperture assistant: live sharpness score + 1:1 pixel view.

Manual lenses are hard to set by eye, especially over SSH: a downscaled
preview looks sharp even when it isn't. This shows a numeric sharpness
score (higher = sharper) with peak-hold, so you can turn the focus ring
slowly, watch the number rise and fall, and settle on the maximum.

  python3 focus.py             # browser view at http://<pi-address>:8000
  python3 focus.py --terminal  # just print the score (no browser needed)

Stop the papercam service first - both want the camera:

  sudo systemctl stop papercam && python3 focus.py

The score is measured on real capture pixels (never a resized image), on
the paper if one is detected, otherwise the centre of the frame. It is
normalised for brightness, so changing the aperture or the lighting does
not by itself move the number - only actual sharpness does.
"""

import argparse
import time

import cv2
import numpy as np

from config import CAPTURE_SIZE, FPS
from detection import DetectionError, find_paper_quad

PANEL = (640, 480)        # size of each of the two preview panels
ROI_MAX = (1000, 750)     # biggest native-res window the score is measured on
DETECT_INTERVAL = 2.0     # seconds between paper-detection passes
PEAK_DECAY = 12.0         # seconds for the peak-hold to fade back down


def sharpness(gray: np.ndarray) -> float:
    """Tenengrad (mean squared gradient), normalised by brightness squared.

    Gradients scale linearly with scene contrast, so dividing by the mean
    intensity squared makes the score comparable across exposures and
    aperture settings - it responds to focus, not to light level.
    """
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mean = max(float(gray.mean()), 1.0)
    return float((gx * gx + gy * gy).mean()) / (mean * mean) * 1000.0


def roi_box(centre, size, shape):
    """A native-resolution box of `size` centred on `centre`, clamped."""
    h, w = shape[:2]
    bw, bh = min(size[0], w), min(size[1], h)
    x = int(np.clip(centre[0] - bw // 2, 0, w - bw))
    y = int(np.clip(centre[1] - bh // 2, 0, h - bh))
    return x, y, bw, bh


def draw_panel(canvas, img, x, y, label):
    h, w = img.shape[:2]
    canvas[y:y + h, x:x + w] = img
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), (90, 90, 90), 1)
    cv2.putText(canvas, label, (x + 10, y + 26), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(canvas, label, (x + 10, y + 26), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 1, cv2.LINE_AA)


def render(frame, gray, score, peak, on_paper):
    """Compose: downscaled framing view | 1:1 pixel view | score readout."""
    canvas = np.full((720, 1280, 3), 30, np.uint8)
    view = cv2.resize(frame, PANEL, interpolation=cv2.INTER_AREA)
    draw_panel(canvas, view, 0, 0, "framing (downscaled)")

    # 1:1 crop from the middle of the measured region: this is the only
    # view that shows true focus - everything else is resampled.
    cx, cy = gray.shape[1] // 2, gray.shape[0] // 2
    x, y, w, h = roi_box((cx, cy), PANEL, frame.shape)
    draw_panel(canvas, frame[y:y + h, x:x + w].copy(), 640, 0,
               "1:1 pixels - judge focus here")

    bar_y = 560
    pct = min(score / max(peak, 1e-6), 1.0) if peak > 0 else 0.0
    cv2.rectangle(canvas, (40, bar_y), (1240, bar_y + 46), (60, 60, 60), -1)
    cv2.rectangle(canvas, (40, bar_y), (40 + int(1200 * pct), bar_y + 46),
                  (80, 200, 80), -1)
    # peak marker
    cv2.line(canvas, (1240, bar_y - 6), (1240, bar_y + 52), (80, 160, 255), 3)

    cv2.putText(canvas, f"{score:8.1f}", (40, 530), cv2.FONT_HERSHEY_SIMPLEX,
                1.6, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(canvas, f"best {peak:.1f}  ({pct * 100:.0f}% of best)",
                (400, 530), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 160, 255), 2,
                cv2.LINE_AA)
    cv2.putText(canvas,
                ("measuring on the detected paper" if on_paper
                 else "no paper detected - measuring frame centre"),
                (40, 660), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1,
                cv2.LINE_AA)
    cv2.putText(canvas, "turn the focus ring slowly; stop where the number peaks",
                (40, 695), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1,
                cv2.LINE_AA)
    return canvas


def measurements():
    """Yields (frame, gray_roi, score, peak, on_paper) forever."""
    from picamera2 import Picamera2

    picam2 = Picamera2()
    cfg = picam2.create_video_configuration(
        main={"size": CAPTURE_SIZE, "format": "RGB888"},
        controls={"FrameRate": FPS},
        buffer_count=3,
    )
    picam2.configure(cfg)
    picam2.start()
    time.sleep(1.5)

    centre = (CAPTURE_SIZE[0] // 2, CAPTURE_SIZE[1] // 2)
    on_paper = False
    last_detect = 0.0
    peak, peak_time = 0.0, time.monotonic()

    while True:
        frame = picam2.capture_array()
        now = time.monotonic()

        if now - last_detect > DETECT_INTERVAL:
            last_detect = now
            small = cv2.resize(frame, None, fx=0.25, fy=0.25,
                               interpolation=cv2.INTER_AREA)
            try:
                quad = find_paper_quad(small) / 0.25
                centre = tuple(quad.mean(axis=0).astype(int))
                on_paper = True
            except DetectionError:
                centre = (CAPTURE_SIZE[0] // 2, CAPTURE_SIZE[1] // 2)
                on_paper = False

        x, y, w, h = roi_box(centre, ROI_MAX, frame.shape)
        roi = frame[y:y + h, x:x + w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        score = sharpness(gray)

        if score > peak:
            peak, peak_time = score, now
        elif now - peak_time > PEAK_DECAY:  # let the peak follow big changes
            peak, peak_time = max(score, peak * 0.9), now

        yield frame, roi, score, peak, on_paper


def run_terminal():
    for _, _, score, peak, on_paper in measurements():
        where = "paper" if on_paper else "centre"
        bar = "#" * int(40 * min(score / max(peak, 1e-6), 1.0))
        print(f"\rsharpness {score:9.1f}  best {peak:9.1f}  [{bar:<40}] "
              f"({where})", end="", flush=True)


def run_http(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    gen = measurements()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for frame, roi, score, peak, on_paper in gen:
                    canvas = render(frame, roi, score, peak, on_paper)
                    ok, jpg = cv2.imencode(".jpg", canvas,
                                           [cv2.IMWRITE_JPEG_QUALITY, 92])
                    if not ok:
                        continue
                    jpg = jpg.tobytes()
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n"
                                     .encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    print(f"Focus assistant at http://<pi-address>:{port}")
    print("Turn the focus ring slowly and stop where the number peaks.")
    ThreadingHTTPServer(("", port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http", type=int, default=8000, metavar="PORT")
    ap.add_argument("--terminal", action="store_true",
                    help="print the score instead of serving a preview")
    args = ap.parse_args()
    if args.terminal:
        run_terminal()
    else:
        run_http(args.http)


if __name__ == "__main__":
    main()
