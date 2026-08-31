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
# 1024 (plain isoc) rather than 3072 (high-bandwidth isoc): still 4x the
# bandwidth MJPEG 720p15 needs, and macOS is picky about high-bandwidth
# isochronous streams from gadgets.
echo 1024 > functions/uvc.0/streaming_maxpacket

# --- MJPEG 1280x720 frame descriptor ---
FRAME=functions/uvc.0/streaming/mjpeg/m/720p
mkdir -p "$FRAME"
echo 1280 > "$FRAME/wWidth"
echo 720  > "$FRAME/wHeight"
echo 29491200  > "$FRAME/dwMinBitRate"
echo 100000000 > "$FRAME/dwMaxBitRate"
echo 1843200   > "$FRAME/dwMaxVideoFrameBufferSize"   # 1280*720*2
echo 666666    > "$FRAME/dwDefaultFrameInterval"      # 15 fps (units: 100ns)
# UVC spec: intervals must be listed in ascending order (30 fps, then 15).
printf '333333\n666666\n' > "$FRAME/dwFrameInterval"

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
