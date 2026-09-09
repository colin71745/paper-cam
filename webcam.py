#!/usr/bin/env python3
"""Capture → perspective-correct → MJPEG → v4l2loopback (or HTTP preview).

Default mode writes MJPEG frames to the v4l2loopback device that uvc-gadget
streams to the host, making the Pi appear as a normal USB webcam.

  python3 webcam.py                # production: write to /dev/video10
  python3 webcam.py --auto         # track the paper continuously: move it,
                                   # and the view snaps to it once it settles
  python3 webcam.py --auto --aruco # track a sheet with printed ArUco markers
  python3 webcam.py --http 8000    # setup/debug: MJPEG stream in a browser
                                   # at http://<pi-address>:8000
  python3 webcam.py --paper-ae     # recommended: slow software auto-exposure
                                   # metered on the paper itself — adapts to
                                   # changing room light over seconds, barely
                                   # reacts to a hand passing through
  python3 webcam.py --lock         # freeze exposure/white balance after 2s
                                   # (only for perfectly constant lighting)

Without --auto, the homography comes from calibration.json (written by
calibrate.py). The file's mtime is checked continuously, so re-running
calibrate.py takes effect within a second. With --auto, calibration.json
(if present) only seeds the initial view; tracking takes over from there.
Until a homography exists, the raw (uncorrected, scaled) view is streamed.
"""

import argparse
import json
import os
import threading
import time

import cv2
import numpy as np

from config import (
    CAPTURE_SIZE,
    OUTPUT_SIZE,
    FPS,
    JPEG_QUALITY,
    CALIBRATION_FILE,
    LOOPBACK_DEVICE,
    STREAM_FLAG,
)


class Calibration:
    """Loads the homography and hot-reloads it when calibrate.py rewrites it."""

    def __init__(self):
        self.H = None
        self.mtime = None
        self.last_check = 0.0
        self.reload()

    def reload(self) -> None:
        try:
            mtime = os.path.getmtime(CALIBRATION_FILE)
        except OSError:
            self.H, self.mtime = None, None
            return
        if mtime == self.mtime:
            return
        with open(CALIBRATION_FILE) as f:
            data = json.load(f)
        if tuple(data["capture_size"]) != CAPTURE_SIZE:
            print("calibration.json was made at a different capture size; "
                  "re-run calibrate.py. Streaming uncorrected view.")
            self.H, self.mtime = None, mtime
            return
        self.H = np.array(data["homography"], dtype=np.float64)
        self.mtime = mtime
        print("Loaded calibration.")

    def maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self.last_check > 1.0:
            self.last_check = now
            self.reload()


def create_camera():
    """Configure the camera without starting it; frames() controls capture."""
    from picamera2 import Picamera2

    picam2 = Picamera2()
    cfg = picam2.create_video_configuration(
        main={"size": CAPTURE_SIZE, "format": "RGB888"},  # BGR byte order
        controls={"FrameRate": FPS},
        buffer_count=3,
    )
    picam2.configure(cfg)
    return picam2


def apply_lock(picam2) -> None:
    time.sleep(2)  # let AE/AWB converge, then freeze them
    md = picam2.capture_metadata()
    picam2.set_controls(
        {
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": md["ExposureTime"],
            "AnalogueGain": md["AnalogueGain"],
            "ColourGains": md["ColourGains"],
        }
    )
    print("Exposure and white balance locked.")


def make_provider(args):
    """The homography source: static file watcher, or continuous tracker."""
    if not args.auto:
        return Calibration()
    from tracker import AutoTracker

    initial = Calibration()  # seed from calibration.json when available
    return AutoTracker(
        use_aruco=args.aruco, rotate=args.rotate, initial_H=initial.H
    )


class PaperExposure:
    """Slow software auto-exposure metered on the (warped) paper view.

    Camera-side AE meters the raw scene and reacts in fractions of a second
    to anything entering the frame. For a document camera we instead hold
    the paper at a constant brightness: adapt over seconds as room light
    changes (sun in and out of clouds), with a deadband and a slew limit so
    a hand passing through causes at most a slight, slow drift that recovers
    just as smoothly. White balance stays on camera auto — white paper is
    the scene AWB algorithms love.
    """

    TARGET = 200.0     # 75th-percentile gray we hold the paper at
    DEADBAND = 0.05    # ignore errors under 5% (no hunting)
    MAX_STEP = 1.03    # 3% per update: individually imperceptible steps
    INTERVAL = 0.25    # seconds between updates (~12%/s total slew)
    MIN_GAIN, MAX_GAIN = 1.0, 16.0
    MID_GAIN = 4.0     # exposure is chosen to park gain here at startup

    def __init__(self, picam2, fps, seed=None):
        self.picam2 = picam2
        self.max_exposure = int(0.9 * 1_000_000 / fps)  # stay under frame time
        if seed is not None:
            # Resuming from idle: reuse the exposure we had settled on rather
            # than waiting for AE to converge again (and visibly flashing).
            self.exposure, self.gain = seed
            picam2.set_controls(
                {"AeEnable": False, "ExposureTime": self.exposure,
                 "AnalogueGain": self.gain}
            )
            self.last = time.monotonic()
            return
        time.sleep(2)  # let camera AE converge once, then take over from it
        md = picam2.capture_metadata()
        # Fix the exposure time so that gain lands mid-range, then adjust
        # ONLY gain at runtime: the sensor applies gain atomically on one
        # frame, whereas changing exposure+gain together lands on different
        # frames and shows as a brightness blink. Gain 1..16 around the
        # midpoint gives +/-2 EV of silent adjustment range; exposure is
        # touched again only if gain runs out of room.
        total = md["ExposureTime"] * md["AnalogueGain"]
        self.exposure = int(min(self.max_exposure, max(200, total / self.MID_GAIN)))
        self.gain = min(self.MAX_GAIN, max(self.MIN_GAIN, total / self.exposure))
        picam2.set_controls(
            {"AeEnable": False, "ExposureTime": self.exposure,
             "AnalogueGain": self.gain}
        )
        self.last = time.monotonic()
        print("Paper-metered auto-exposure active (gain-only adjustments).")

    def state(self):
        """Current (exposure, gain), to seed the next session after idling."""
        return self.exposure, self.gain

    def update(self, out: np.ndarray) -> None:
        now = time.monotonic()
        if now - self.last < self.INTERVAL:
            return
        self.last = now
        # Central half of the output is paper by construction (the dest
        # rect is centered); subsample for cheapness. 75th percentile reads
        # the paper surface, largely ignoring printed text.
        h, w = out.shape[:2]
        crop = out[h // 4:3 * h // 4:4, w // 4:3 * w // 4:4]
        measured = float(np.percentile(crop.mean(axis=2), 75))
        ratio = self.TARGET / max(measured, 5.0)
        if abs(ratio - 1.0) < self.DEADBAND:
            return
        ratio = min(max(ratio, 1.0 / self.MAX_STEP), self.MAX_STEP)
        wanted = self.gain * ratio
        gain = min(self.MAX_GAIN, max(self.MIN_GAIN, wanted))
        if gain != self.gain:
            # Normal path: silent, atomic gain-only adjustment.
            self.gain = gain
            self.picam2.set_controls({"AnalogueGain": self.gain})
            return
        # Gain is pinned at a limit and we still need more range: shift the
        # remaining correction into exposure time. Rare (very bright sun or
        # very dark room), and may show a one-frame blink.
        exposure = int(min(self.max_exposure,
                           max(100, self.exposure * wanted / gain)))
        if exposure != self.exposure:
            self.exposure = exposure
            self.picam2.set_controls({"ExposureTime": self.exposure})


def frames(args):
    """Yields perspective-corrected JPEG frames forever.

    With --on-demand, capture only runs while the USB host is actually
    streaming (uvc-gadget maintains the flag file). Between calls the sensor
    is stopped: no capture, no processing, near-zero CPU.
    """
    provider = make_provider(args)
    gate = args.on_demand and args.stream_flag
    running = False
    paper_ae, ae_seed = None, None
    if gate:
        print(f"On-demand: idle until the host streams (flag {gate}).")
    # While idle we stop the camera but must KEEP FEEDING the loopback:
    # v4l2loopback only presents a usable capture device while a producer is
    # writing, and uvc-gadget opens (and re-opens) /dev/video10 at its own
    # startup. Starve it and it fails with "unable to enumerate formats",
    # crash-loops, and the USB gadget never stays activated - the host then
    # sees a device that malfunctions rather than a webcam.
    idle_jpeg = cv2.imencode(
        ".jpg", np.zeros((OUTPUT_SIZE[1], OUTPUT_SIZE[0], 3), np.uint8),
        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
    )[1].tobytes()

    # Bringing up libcamera takes ~20s on a Zero 2 W. Do it on a background
    # thread and write blanks meanwhile: uvc-gadget opens /dev/video10 at its
    # own startup and exits ("unable to enumerate formats") if no producer is
    # writing, so a silent startup window makes it crash-loop and the USB
    # gadget flap.
    init = {}

    def _open_camera():
        try:
            init["cam"] = create_camera()
        except BaseException as exc:            # surfaced in the main loop
            init["error"] = exc

    threading.Thread(target=_open_camera, daemon=True).start()
    while "cam" not in init:
        if "error" in init:
            raise init["error"]
        yield idle_jpeg
        time.sleep(0.2)
    picam2 = init["cam"]
    # --rotate is baked into the homography for the corrected view; the raw
    # fallback view has to be rotated explicitly. (90/270 rotate within the
    # same output frame, so the aspect ratio is squashed - only 180 is a
    # true like-for-like view, which is the case that matters here.)
    raw_rotation = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }.get(args.rotate)
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    n, t0 = 0, time.monotonic()
    while True:
        if gate and not os.path.exists(gate):
            if running:
                if paper_ae is not None:
                    ae_seed = paper_ae.state()  # resume where we left off
                    paper_ae = None
                picam2.stop()
                running = False
                print("Host stopped streaming — camera idle.")
            yield idle_jpeg      # keeps the loopback (and uvc-gadget) alive
            time.sleep(0.2)
            continue
        if not running:
            picam2.start()
            running = True
            if args.lock:
                apply_lock(picam2)
            if args.paper_ae:
                paper_ae = PaperExposure(picam2, FPS, seed=ae_seed)
            if gate:
                print("Host started streaming — camera live.")
            n, t0 = 0, time.monotonic()
        frame = picam2.capture_array()
        if args.auto:
            provider.submit_frame(frame)
        provider.maybe_reload()
        H = provider.H
        if H is not None:
            out = cv2.warpPerspective(frame, H, OUTPUT_SIZE, flags=cv2.INTER_LINEAR)
        else:
            out = cv2.resize(frame, OUTPUT_SIZE, interpolation=cv2.INTER_AREA)
            if raw_rotation is not None:
                out = cv2.rotate(out, raw_rotation)
        if paper_ae is not None:
            paper_ae.update(out)
        ok, jpg = cv2.imencode(".jpg", out, encode_params)
        if ok:
            yield jpg.tobytes()
        n += 1
        if n % 100 == 0:
            now = time.monotonic()
            print(f"{100 / (now - t0):.1f} fps")
            t0 = now


def run_loopback(args) -> None:
    from v4l2_output import MJPEGOutput

    out = MJPEGOutput(LOOPBACK_DEVICE, *OUTPUT_SIZE)
    print(f"Writing MJPEG to {LOOPBACK_DEVICE}")
    for jpg in frames(args):
        out.write(jpg)


def run_http(port: int, args) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    gen = frames(args)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.end_headers()
            try:
                for jpg in gen:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    print(f"Preview at http://<pi-address>:{port} (one viewer at a time)")
    ThreadingHTTPServer(("", port), Handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http", type=int, metavar="PORT",
                    help="serve an HTTP MJPEG preview instead of the loopback")
    ap.add_argument("--paper-ae", action="store_true",
                    help="slow software auto-exposure metered on the paper "
                         "(recommended); mutually exclusive with --lock")
    ap.add_argument("--lock", action="store_true",
                    help="freeze exposure/white balance after startup")
    ap.add_argument("--on-demand", action="store_true",
                    help="only capture while the USB host is streaming; stop "
                         "the camera in between (needs uvc-gadget built with "
                         "setup/uvc-gadget-stream-flag.patch)")
    ap.add_argument("--stream-flag", default=STREAM_FLAG, metavar="PATH",
                    help=f"flag file uvc-gadget writes while streaming "
                         f"(default {STREAM_FLAG})")
    ap.add_argument("--auto", action="store_true",
                    help="continuously track the paper instead of using a "
                         "fixed calibration")
    ap.add_argument("--aruco", action="store_true",
                    help="with --auto: track ArUco markers instead of paper "
                         "edges (see calibrate.py --make-markers)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="with --auto: rotate the output for the camera's "
                         "mounting orientation")
    args = ap.parse_args()
    if args.lock and args.paper_ae:
        ap.error("--lock and --paper-ae are mutually exclusive")
    if args.http and args.on_demand:
        # The browser preview has no UVC host to gate on; it would sit idle.
        print("--http: ignoring --on-demand (no USB host involved).")
        args.on_demand = False
    if args.http:
        run_http(args.http, args)
    else:
        run_loopback(args)


if __name__ == "__main__":
    main()
