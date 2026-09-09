#!/bin/bash
# Configure the Pi as a USB UVC (webcam) gadget via configfs.
# Run as root at boot (see papercam-gadget.service). Requires dtoverlay=dwc2.
#
# Advertises a single MJPEG format: 1280x720 at 15/30 fps. This must match
# OUTPUT_SIZE in config.py.

set -euo pipefail

GADGET=/sys/kernel/config/usb_gadget/papercam

modprobe libcomposite

if [ -d "$GADGET" ]; then
    echo "Gadget already configured."
    exit 0
fi

mkdir -p "$GADGET"
cd "$GADGET"

echo 0x1d6b > idVendor     # Linux Foundation
echo 0x0104 > idProduct    # Multifunction composite gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "papercam-0001"  > strings/0x409/serialnumber
echo "papercam"       > strings/0x409/manufacturer
echo "Paper Camera"   > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "UVC" > configs/c.1/strings/0x409/configuration
echo 500 > configs/c.1/MaxPower

mkdir -p functions/uvc.0
# 1024 (plain isoc) gives 8 MB/s, comfortably above the ~2 MB/s that
# 1080p MJPEG at these frame rates needs. Deliberately not 3072
# (high-bandwidth isoc): macOS rejects those streams from gadgets.
echo 1024 > functions/uvc.0/streaming_maxpacket

# --- MJPEG 1920x1080 frame descriptor (must match OUTPUT_SIZE) ---
FRAME=functions/uvc.0/streaming/mjpeg/m/1080p
mkdir -p "$FRAME"
echo 1920 > "$FRAME/wWidth"
echo 1080 > "$FRAME/wHeight"
echo 29491200  > "$FRAME/dwMinBitRate"
echo 150000000 > "$FRAME/dwMaxBitRate"
echo 4147200   > "$FRAME/dwMaxVideoFrameBufferSize"   # 1920*1080*2
echo 1000000   > "$FRAME/dwDefaultFrameInterval"      # 10 fps (units: 100ns)
# UVC spec: intervals must be listed in ascending order.
printf '666666\n1000000\n2000000\n' > "$FRAME/dwFrameInterval"

# --- wire format -> header -> class descriptors ---
mkdir -p functions/uvc.0/streaming/header/h
(cd functions/uvc.0/streaming/header/h && ln -s ../../mjpeg/m .)
(cd functions/uvc.0/streaming/class/fs && ln -s ../../header/h .)
(cd functions/uvc.0/streaming/class/hs && ln -s ../../header/h .)
(cd functions/uvc.0/streaming/class/ss && ln -s ../../header/h .)

mkdir -p functions/uvc.0/control/header/h
(cd functions/uvc.0/control/class/fs && ln -s ../../header/h .)
(cd functions/uvc.0/control/class/ss && ln -s ../../header/h .)

ln -s functions/uvc.0 configs/c.1/

# Bind to the USB device controller — the host sees the webcam from here on.
ls /sys/class/udc | head -n1 > UDC

echo "UVC gadget configured and bound."
