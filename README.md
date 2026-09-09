# papercam

Turn a Raspberry Pi Zero 2 W + HQ Camera into a USB webcam that shows a
perspective-corrected view of a piece of paper on your desk. The host
computer sees an ordinary UVC webcam — no drivers or software needed there.

## Pipeline

```
HQ camera (2028x1520, via picamera2)
  └─ webcam.py: cv2.warpPerspective → 1920x1080 → JPEG encode
       └─ /dev/video10 (v4l2loopback)
            └─ uvc-gadget (handles the UVC protocol)
                 └─ USB OTG port → host sees "Paper Camera"
```

The per-frame cost is one warp plus one JPEG encode, both scaling with
*output* pixels. Measured on a Zero 2 W: about 6 fps at 720p, and slower at
the 1080p default. That is a deliberate trade — for a document camera,
resolution beats frame rate, and the captured page spans ~1300-1900 px, so
a 720p output was discarding real detail. To go back to a faster, smaller
image, set `OUTPUT_SIZE` in [config.py](config.py) *and* the frame
descriptor in [usb-gadget.sh](setup/usb-gadget.sh) to 1280x720, then reboot.

The homography comes from either continuous tracking (`--auto`, the default
in the service) or a one-shot calibration (`calibrate.py`).

## Auto-tracking mode

`webcam.py --auto` re-detects the paper in a background thread (about once
a second, on a downscaled frame, so it costs almost nothing). You can drop
a page anywhere on the desk and the view snaps to it — deliberately *snap*,
not chase:

- The homography only updates after the paper has sat still in a new spot
  for a few detection passes (~2 s). No jitter, no chasing your hand.
- While detection fails briefly — hand over the page, page mid-air — the
  last good view is held. If no paper is seen for ~8 s (`AUTO_LOST_TIMEOUT`),
  the view reverts to the raw camera image until a page reappears.
- The output aspect ratio is *recovered exactly* from the detected quad at
  each snap (Zhang & He's closed form: opposite edges of the real rectangle
  are equal, so how unequal they appear encodes the plane's tilt), so A4,
  an open notebook, or any other rectangle displays in true proportion at
  any camera angle, without being told its size. This needs
  `FOCAL_LENGTH_MM` in [config.py](config.py) set to your lens (6.0 for the
  official wide-angle CS lens, 16.0 for the C-mount lens); the nominal spec
  is accurate enough, and if set to `None` it falls back to an edge-length
  approximation that's fine for near-overhead cameras.
- Implausible detections (too small, too big, extreme aspect) are ignored
  so it doesn't lock onto a keyboard or the desk edge.

`--auto --aruco` tracks the printed marker sheet instead of paper edges —
useful on light-colored desks where edge contrast is poor; stick the four
markers on a clipboard and "wherever the clipboard is" defines the view.
Tuning knobs (detection interval, settle count, jitter tolerance) are in
[config.py](config.py); detection logic is in [detection.py](detection.py)
and the snap/hold behavior in [tracker.py](tracker.py).

## One-time Pi setup

Start from **Raspberry Pi OS Lite (64-bit)** (the current Trixie-based
image in Raspberry Pi Imager) with Wi-Fi + SSH configured (you'll want SSH:
the USB port is occupied being a webcam). The Legacy 32-bit Bookworm Lite
image also works — all code here handles both OpenCV generations and both
32/64-bit — and is the known-good fallback if the newer camera stack
misbehaves on the Zero 2 W.

Flash the card with Raspberry Pi Imager, setting hostname, user, Wi-Fi and
SSH in its customisation dialog. Then, on the Pi:

```bash
git clone https://github.com/colin71745/paper-cam.git papercam
cd papercam && ./setup.sh
sudo reboot
```

`setup.sh` does everything below and is safe to re-run. It also rewrites the
systemd units for wherever you cloned the project and whichever user you're
running as, so the `/home/pi` assumption below stops being your problem.
Then connect the Pi to the computer with its **USB data port** — the middle
micro-USB socket labelled "USB", not "PWR IN". One cable carries both power
and video.

### What setup.sh does (and how to do it by hand)

**1. Enable USB gadget mode.** In `/boot/firmware/config.txt`, add under `[all]`:

```
dtoverlay=dwc2
```

**2. Install packages:**

```bash
sudo apt update && sudo apt install -y python3-opencv python3-picamera2 v4l2loopback-dkms git meson ninja-build build-essential pkg-config
```

**3. Get this project onto the Pi**, by `git clone` as above. If you copy
from a working machine instead, use `rsync -a ./ <user>@<host>:papercam/`
and not `scp` — `scp setup/foo.service host:papercam/` silently flattens
the path and drops the file in the project root, where it will be missed
and a stale copy installed instead.

**4. Build uvc-gadget** (the userspace app that speaks the UVC protocol).
This needs the patches from step 3, so do it in this order:

```bash
git clone https://gitlab.freedesktop.org/camera/uvc-gadget.git
cd uvc-gadget
git apply ~/papercam/setup/uvc-gadget-cpu-copy.patch
git apply ~/papercam/setup/uvc-gadget-stream-flag.patch
meson setup build && ninja -C build && sudo ninja -C build install && sudo ldconfig
```

The first patch is required: stock uvc-gadget streams by exporting the
source's buffers as DMABUFs, which v4l2loopback doesn't support (`Failed to
export buffers on source: Inappropriate ioctl for device`) — the stream
silently delivers nothing. The patch detects this and falls back to copying
frames through the CPU (the same ENCODED path uvc-gadget's libcamera/MJPEG
source uses).

The second patch makes uvc-gadget publish whether the host is streaming
(see **On-demand capture** below). It's a no-op unless
`UVC_GADGET_STREAM_FLAG` is set, so it's safe either way.

The `ldconfig` matters: without it the freshly installed `libuvcgadget.so`
isn't in the linker cache and uvc-gadget exits instantly with "cannot open
shared object file". Because the kernel keeps the UVC gadget's USB
connection deactivated until uvc-gadget opens it, the symptom is the host
seeing *no USB device at all* (UDC state stuck at `not attached`) — which
looks exactly like a bad cable.

**5. Install the loopback + service config on the Pi:**

```bash
cd ~/papercam
sudo cp setup/v4l2loopback.conf /etc/modprobe.d/papercam-v4l2loopback.conf
echo v4l2loopback | sudo tee /etc/modules-load.d/papercam.conf
chmod +x setup/usb-gadget.sh
sudo cp setup/papercam-gadget.service setup/papercam.service setup/papercam-uvc.service /etc/systemd/system/
sudo systemctl enable papercam-gadget papercam papercam-uvc
sudo reboot
```

**6. Connect to the host** using the Pi's **USB data port** (the inner
micro-USB port labeled "USB", *not* "PWR IN") — a single cable carries both
power and the webcam stream.

## Fixed calibration (alternative to --auto)

If you'd rather pin the view to one exact spot (e.g. a taped-down outline),
remove `--auto` from `papercam.service` and calibrate once. Put a blank
piece of paper where your content will go, then over SSH:

```bash
cd ~/papercam
sudo systemctl stop papercam        # frees the camera
python3 calibrate.py                # or: --paper letter, --rotate 180, ...
sudo systemctl start papercam
```

It writes `calibration.json` plus two debug images: `calibration_capture.jpg`
(what the camera saw, detected quad in red) and `calibration_preview.jpg`
(exactly what the webcam will output). `scp` them over if something looks off.

If contour detection struggles (dark desk helps — white paper on a white desk
doesn't), use ArUco markers instead:

```bash
python3 calibrate.py --make-markers    # creates markers.png; print at 100%
python3 calibrate.py --aruco           # with the printed sheet in place
```

Common fixes: image upside down → `--rotate 180`; wrong aspect ratio →
`--paper a4` or `--paper letter` (add `--portrait` if applicable).

While the webcam is running, re-running `calibrate.py` takes effect within a
second — `webcam.py` hot-reloads the calibration file.

## On-demand capture

Unlike a USB webcam with an activity light, nothing about this device tells
you whether it is looking at your desk - and the HQ camera has no LED at
all. Left to itself the pipeline would capture, detect and encode from boot
to shutdown, using about two of the Pi's four cores continuously whether or
not anything was watching.

`webcam.py --on-demand` (in the service by default) fixes that. uvc-gadget
creates `/run/papercam-streaming` when the host starts streaming and
removes it when it stops; webcam.py watches that file and calls
`picam2.stop()` in between, so between calls the sensor is not capturing
and the CPU is essentially idle. Starting a call brings it back in about
half a second: the exposure it had settled on is reused rather than
re-converged, and the tracker keeps its homography, so the paper lock is
immediate.

One constraint worth knowing if you modify this: while idle, webcam.py
keeps writing a **blank frame a few times a second**. That is deliberate and
must not be optimised away. v4l2loopback only presents a usable capture
device while a producer is writing, and uvc-gadget opens `/dev/video10` at
its own startup - starve it and it fails with `unable to enumerate formats`,
crash-loops, and the USB function never stays activated, so the host sees a
malfunctioning device rather than a webcam. Only the *camera* stops when
idle; the pipe stays alive at negligible cost (one pre-encoded JPEG).

Check what it's doing at any time:

```bash
ls /run/papercam-streaming        # exists only while a host is streaming
journalctl -u papercam | tail     # logs each "camera live"/"camera idle"
```

This depends on `setup/uvc-gadget-stream-flag.patch` being built in. If
uvc-gadget is unpatched the flag never appears and the camera stays idle
forever (a black image on the host); `journalctl -u papercam` says
`On-demand: idle until the host streams` in that case. Drop `--on-demand`
from the service to go back to always-on.

`--http` ignores `--on-demand`, since the browser preview has no USB host
to gate on.

## Getting the sharpest image

The lens is manual focus *and* manual aperture, so both need setting by
hand once the camera is mounted at its final height. `focus.py` measures
sharpness live so you aren't guessing:

```bash
sudo systemctl stop papercam
python3 focus.py                 # then open http://<pi-address>:8000
```

The page shows the framing, a **1:1 pixel crop** (the only view that shows
true focus - a downscaled preview looks sharp even when it isn't), and a
sharpness score with peak-hold. The score is measured on real capture
pixels over the detected paper and normalised for brightness, so moderate
lighting changes don't move it - focus does. (It is not immune to sensor
noise, which reads as fine detail; see the aperture note below.)

Procedure:

1. Put a **printed page of small text** under the camera (fine detail gives
   the metric something to bite on; a blank sheet cannot be focused).
2. **Fill the frame with the page.** This is free sharpness: the paper's
   pixels get mapped to a 1280x720 output, so a page covering most of the
   frame arrives at roughly 1:1, while a small page in a big frame is
   upscaled and can never look crisp. Adjust the arm height for this first.
3. The 6 mm lens has two rings, each with a small locking grub screw
   (loosen before turning, tighten after): **NEAR-FAR** is focus,
   **OPEN-CLOSE** is the aperture. There are no f-numbers marked - set the
   aperture by feel.
4. **Aperture fully to OPEN** while focusing: the shallow depth of field
   makes focus errors obvious and the peak easy to find.
5. Turn **NEAR-FAR slowly** past the peak and back; settle where the number
   is highest. Confirm on the 1:1 crop.
6. **Turn OPEN-CLOSE about a third of the way toward CLOSE** (roughly f/4
   on this f/1.2 lens). Wide open is soft in the corners and too shallow to
   hold a whole page; past ~f/8 diffraction softens everything again.
   Re-check focus afterwards - stopping down can shift it slightly - then
   tighten both locking screws.

   Judge the aperture on the 1:1 crop and the page corners, *not* on the
   score: closing down darkens the image, exposure compensates with gain,
   and sensor noise reads as "detail" to the metric. Grainy rather than
   crisp means you've stopped down further than your lighting supports.
7. If focus can't be reached at all, adjust the HQ camera's **back-focus
   ring** (the knurled ring on the body, with its locking screw) - the 6 mm
   CS lens needs it roughly at the CS position.
8. `sudo systemctl start papercam` when done.

Two things no amount of focusing fixes: **glare** (a specular reflection of
a lamp or window off the page - tilt the camera or the light a few degrees;
matte paper beats glossy) and **motion blur** in dim light (the exposure
lengthens until the page is dim-and-smeared; add light rather than gain).

`JPEG_QUALITY` in [config.py](config.py) is 90 - below about 85, JPEG
ringing shows around text. Raise it further if you like; if you ever see
torn or dropped frames, the USB isoc bandwidth is the limit, so come back
down.

## Debugging without a host computer

`webcam.py --http 8000` streams the corrected view over Wi-Fi — open
`http://<pi-address>:8000` in a browser. Great for framing, focus (the HQ
lens is manual focus — tune it while watching this), and lighting, without
plugging into anything. Stop the `papercam` service first, since both need
the camera.

## Tuning knobs

- [config.py](config.py): capture/output resolution, fps, JPEG quality.
  If you change `OUTPUT_SIZE`, update the frame descriptor in
  [usb-gadget.sh](setup/usb-gadget.sh) to match, then reboot.
- `webcam.py --paper-ae` (the service uses it) runs a slow software
  auto-exposure metered on the paper itself: it holds the page at constant
  brightness, adapting over a few seconds when room light changes (sun in
  and out of clouds) while barely reacting to a hand passing through the
  frame. White balance stays on camera auto. `--lock` instead freezes
  exposure entirely — only sensible under perfectly constant lighting.
- Low light → drop `FPS` in config.py so exposure time can grow.

## Troubleshooting

- **Host doesn't see a webcam:** `systemctl status papercam-gadget papercam-uvc`;
  confirm `dtoverlay=dwc2` took effect (`ls /sys/class/udc` should show a
  controller); confirm you're on the data USB port.
- **Webcam appears but no video:** check `journalctl -u papercam` for the
  Python side (it logs fps every 100 frames) and `-u papercam-uvc` for the
  bridge. `v4l2-ctl -d /dev/video10 --all` shows whether the loopback is
  being fed. If uvc-gadget rejects the source, its CLI may differ by
  version — `uvc-gadget --help`.
- **Image looks mirrored (text reads right-to-left):** almost certainly the
  viewer, not the stream. Photo Booth and video-call self-views mirror the
  preview like a mirror; the saved photo and what other participants see are
  the true image. Take a photo (or use `webcam.py --http 8000` in a browser)
  before "fixing" it — a mirrored stream would show correctly in your
  mirrored preview and backwards to everyone else. `--rotate 180` is for a
  genuinely upside-down camera mounting; rotation never mirrors.
- **Image is the raw desk view, not corrected:** no homography yet —
  webcam.py falls back to the uncorrected view. In `--auto` mode the paper
  hasn't been detected (check lighting/contrast, or try `--aruco`); in
  fixed mode, run `calibrate.py`.
