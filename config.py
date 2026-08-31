"""Shared configuration for papercam.

calibrate.py and webcam.py must agree on CAPTURE_SIZE, since the saved
homography is expressed in capture-pixel coordinates.
"""

# IMX477 (HQ camera) 2x2-binned mode. Full res is 4056x3040 but binned
# capture is much cheaper on a Zero 2 W and still plenty of detail for paper.
CAPTURE_SIZE = (2028, 1520)

# What the host computer sees. Must match the frame descriptor configured
# in setup/usb-gadget.sh.
OUTPUT_SIZE = (1280, 720)

FPS = 15
JPEG_QUALITY = 90   # text shows JPEG ringing below ~85; 90 costs
                    # ~3 MB/s at 720p15, well inside USB 2.0 isoc

CALIBRATION_FILE = "calibration.json"

# --- continuous tracking (webcam.py --auto) ---
AUTO_DETECT_INTERVAL = 0.7    # seconds between detection passes
AUTO_DETECT_SCALE = 0.25      # detect on a downscaled frame (cheap)
AUTO_STABLE_DETECTIONS = 3    # detections in the same spot before snapping
AUTO_MOVE_TOLERANCE = 12.0    # capture-resolution pixels; movement below
                              # this is treated as jitter, not a move
AUTO_LOST_TIMEOUT = 8.0       # seconds without any paper detection before
                              # reverting to the raw (uncorrected) view; a
                              # hand briefly covering the page stays held

# v4l2loopback device that webcam.py writes to and uvc-gadget reads from.
LOOPBACK_DEVICE = "/dev/video10"

# --- optics ---
# Knowing the focal length lets us recover the paper's TRUE aspect ratio
# from its perspective-distorted quad (Zhang & He's closed form), instead of
# approximating it from edge lengths. Set to your lens's focal length:
# 6.0 for the official wide-angle CS-mount lens, 16.0 for the C-mount lens.
# None falls back to the edge-length approximation (fine for near-overhead
# camera angles).
FOCAL_LENGTH_MM = 6.0
SENSOR_WIDTH_MM = 6.287  # IMX477 active-area width; CAPTURE_SIZE spans it


def focal_px():
    """Focal length in capture-resolution pixels, or None if not configured."""
    if FOCAL_LENGTH_MM is None:
        return None
    return FOCAL_LENGTH_MM * CAPTURE_SIZE[0] / SENSOR_WIDTH_MM


def principal_point():
    return (CAPTURE_SIZE[0] / 2, CAPTURE_SIZE[1] / 2)
