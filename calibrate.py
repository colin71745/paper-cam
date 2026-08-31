#!/usr/bin/env python3
"""Find the paper in the camera's view and save a homography for webcam.py.

Use this for a fixed setup (or as the seed view for --auto mode). Run it
whenever the camera or paper position changes; webcam.py notices the updated
calibration.json and reloads it live. If you run webcam.py --auto instead,
the paper is re-detected continuously and this script is optional.

Modes:
  default          Detect the paper as the largest quadrilateral.
  --aruco          Detect a printed sheet with 4 ArUco markers (robust when
                   contour detection struggles with lighting or clutter).
  --make-markers   Generate markers.png to print for --aruco mode. Print at
                   100% scale on the paper size you'll use.

Other options:
  --paper auto|a4|letter   Output aspect ratio (default: auto, estimated
                           from the detected quad).
  --portrait               With a4/letter: portrait instead of landscape.
  --rotate 0|90|180|270    Rotate the output (use 180 if the image is
                           upside down for the camera's mounting).

Debug output: calibration_capture.jpg (what the camera saw, with the
detected quad drawn) and calibration_preview.jpg (the warped result).
Check these over SSH/scp if the result looks wrong.
"""

import argparse
import json
import sys
import time

import cv2
import numpy as np

from config import (
    CAPTURE_SIZE,
    OUTPUT_SIZE,
    CALIBRATION_FILE,
    focal_px,
    principal_point,
)
from detection import (
    DetectionError,
    aruco_dictionary,
    dest_rect,
    find_aruco_quad,
    find_paper_quad,
    refine_quad,
)


def capture_frame() -> np.ndarray:
    from picamera2 import Picamera2

    picam2 = Picamera2()
    cfg = picam2.create_still_configuration(
        main={"size": CAPTURE_SIZE, "format": "RGB888"}  # BGR byte order, OpenCV-ready
    )
    picam2.configure(cfg)
    picam2.start()
    time.sleep(2)  # let AE/AWB settle
    frame = picam2.capture_array()
    picam2.stop()
    picam2.close()
    return frame


def make_markers(path: str = "markers.png") -> None:
    """A printable sheet with one marker in each corner."""
    dictionary = aruco_dictionary()
    page_w, page_h = 2200, 1700  # landscape @ 200dpi
    margin, size = 60, 300
    page = np.full((page_h, page_w), 255, np.uint8)

    def draw(marker_id: int, px: int) -> np.ndarray:
        try:  # OpenCV >= 4.7
            return cv2.aruco.generateImageMarker(dictionary, marker_id, px)
        except AttributeError:
            return cv2.aruco.drawMarker(dictionary, marker_id, px)

    spots = {
        0: (margin, margin),
        1: (page_w - margin - size, margin),
        2: (page_w - margin - size, page_h - margin - size),
        3: (margin, page_h - margin - size),
    }
    for mid, (x, y) in spots.items():
        page[y : y + size, x : x + size] = draw(mid, size)
    cv2.imwrite(path, page)
    print(f"Wrote {path} — print at 100% scale (landscape) and lay it where "
          "your paper will go.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aruco", action="store_true")
    ap.add_argument("--make-markers", action="store_true")
    ap.add_argument("--paper", default="auto", choices=["auto", "a4", "letter"])
    ap.add_argument("--portrait", action="store_true")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    args = ap.parse_args()

    if args.make_markers:
        make_markers()
        return

    frame = capture_frame()
    try:
        if args.aruco:
            src = find_aruco_quad(frame)
        else:
            src = find_paper_quad(frame)
            src = refine_quad(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), src)
    except DetectionError as e:
        cv2.imwrite("calibration_capture.jpg", frame)
        sys.exit(
            f"Detection failed: {e}. Check calibration_capture.jpg for what "
            "the camera saw; improve lighting/contrast"
            + ("" if args.aruco else ", or try --aruco") + "."
        )
    dst = dest_rect(
        src, OUTPUT_SIZE, paper=args.paper, portrait=args.portrait,
        rotate=args.rotate, focal_px=focal_px(),
        principal_point=principal_point(),
    )
    H = cv2.getPerspectiveTransform(src, dst)

    with open(CALIBRATION_FILE, "w") as f:
        json.dump(
            {
                "homography": H.tolist(),
                "capture_size": list(CAPTURE_SIZE),
                "output_size": list(OUTPUT_SIZE),
            },
            f,
            indent=2,
        )

    debug = frame.copy()
    cv2.polylines(debug, [src.astype(int)], True, (0, 0, 255), 8)
    cv2.imwrite("calibration_capture.jpg", debug)
    cv2.imwrite(
        "calibration_preview.jpg", cv2.warpPerspective(frame, H, OUTPUT_SIZE)
    )
    print(f"Saved {CALIBRATION_FILE}.")
    print("Check calibration_preview.jpg — that's what the webcam will show.")


if __name__ == "__main__":
    main()
