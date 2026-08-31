"""Minimal V4L2 output-device writer for pushing MJPEG frames into v4l2loopback.

We only need one ioctl (VIDIOC_S_FMT) to declare the format, after which
v4l2loopback accepts frames via plain write(). ctypes computes struct layout
and size natively, so the ioctl request number is correct on both 32-bit and
64-bit ARM without hardcoding.
"""

import ctypes
import fcntl
import os

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_JPEG = 7
V4L2_PIX_FMT_MJPEG = ord("M") | (ord("J") << 8) | (ord("P") << 16) | (ord("G") << 24)


class _v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class _fmt_union(ctypes.Union):
    # raw_data pads to the kernel's 200-byte union; the c_void_p member gives
    # the union pointer alignment, matching struct v4l2_format's real layout
    # (204 bytes on 32-bit, 208 on 64-bit).
    _fields_ = [
        ("pix", _v4l2_pix_format),
        ("raw_data", ctypes.c_uint8 * 200),
        ("_align", ctypes.c_void_p),
    ]


class _v4l2_format(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("fmt", _fmt_union),
    ]


def _IOWR(magic: str, nr: int, struct_type) -> int:
    return (3 << 30) | (ctypes.sizeof(struct_type) << 16) | (ord(magic) << 8) | nr


VIDIOC_S_FMT = _IOWR("V", 5, _v4l2_format)


class MJPEGOutput:
    """Opens a v4l2loopback device for MJPEG output and writes JPEG frames."""

    def __init__(self, device: str, width: int, height: int):
        self.fd = os.open(device, os.O_WRONLY)
        fmt = _v4l2_format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        fmt.fmt.pix.width = width
        fmt.fmt.pix.height = height
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG
        fmt.fmt.pix.field = V4L2_FIELD_NONE
        # Upper bound on a single compressed frame; real JPEGs are far smaller.
        fmt.fmt.pix.sizeimage = width * height
        fmt.fmt.pix.colorspace = V4L2_COLORSPACE_JPEG
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)

    def write(self, jpeg_bytes: bytes) -> None:
        os.write(self.fd, jpeg_bytes)

    def close(self) -> None:
        os.close(self.fd)
