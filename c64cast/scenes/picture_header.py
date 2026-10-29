"""Read a still picture's pixel dimensions from its header, without decoding it.

The slideshow decodes whatever a `file =` spec names, and a full-role web
console user can upload into that directory. A few-megabyte PNG can declare a
raster just under OpenCV's own limit of 2^30 pixels, about 3 GB of BGR, so the
size has to be known before `cv2.imread` allocates it.

The format is identified by signature, not by extension, because that is how
OpenCV picks its decoder: a `.png` holding TIFF bytes decodes as TIFF. Only the
formats a slideshow accepts are recognized (`PICTURE_EXTS`); anything else
returns None, and the slideshow skips a picture it cannot size.
"""

from __future__ import annotations

import struct
from typing import BinaryIO

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8"
# SOF0-SOF15 carry the frame size; C4 (DHT), C8 (JPG) and CC (DAC) share
# the range but are not frame headers.
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers with no length field after them.
_JPEG_STANDALONE_MARKERS = frozenset(range(0xD0, 0xD8)) | {0x01}
# The length-prefixed segments libjpeg reads past on its way to the frame
# header: DHT, DAC, DQT, DNL, DRI, APP0-APP15 and COM. libjpeg takes FF 00
# for stray data and rescans byte by byte, so walking it as a segment could
# land on a decoy frame header libjpeg never reads.
_JPEG_APP_MARKERS = frozenset(range(0xE0, 0xF0))
_JPEG_SKIPPED_MARKERS = frozenset({0xC4, 0xCC, 0xDB, 0xDC, 0xDD, 0xFE}) | _JPEG_APP_MARKERS
# Bounds on a header walk over untrusted bytes: real files carry a few dozen
# segments and a handful of fill bytes, while an upload of up to
# media_store.MAX_UPLOAD_BYTES of empty segments or of 0xFF would be walked
# a few bytes at a time.
_JPEG_MAX_SEGMENTS = 1024
_JPEG_MAX_FILL_BYTES = 64


def picture_size(path: str) -> tuple[int, int] | None:
    """`(width, height)` from the header of the picture at `path`, or None
    when the file is not a PNG, JPEG, BMP or WebP or its header is cut short."""
    with open(path, "rb") as f:
        head = f.read(30)
        try:
            if head.startswith(_PNG_SIGNATURE):
                return _png_size(head)
            if head.startswith(_JPEG_SOI):
                return _jpeg_size(f)
            if head.startswith(b"BM"):
                return _bmp_size(head)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _webp_size(head)
        except struct.error:
            return None
    return None


def is_jpeg(path: str) -> bool:
    """Whether the file at `path` starts with a JPEG signature."""
    with open(path, "rb") as f:
        return f.read(2) == _JPEG_SOI


def _png_size(head: bytes) -> tuple[int, int] | None:
    if head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _jpeg_size(f: BinaryIO) -> tuple[int, int] | None:
    f.seek(2)
    for _ in range(_JPEG_MAX_SEGMENTS):
        marker = f.read(2)
        fill = 0
        while marker == b"\xff\xff" and fill < _JPEG_MAX_FILL_BYTES:
            marker = b"\xff" + f.read(1)
            fill += 1
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        code = marker[1]
        if code in _JPEG_STANDALONE_MARKERS:
            continue
        if code not in _JPEG_SOF_MARKERS and code not in _JPEG_SKIPPED_MARKERS:
            return None
        (length,) = struct.unpack(">H", f.read(2))
        if code in _JPEG_SOF_MARKERS:
            _precision, height, width = struct.unpack(">BHH", f.read(5))
            return width, height
        f.seek(length - 2, 1)
    return None


def _bmp_size(head: bytes) -> tuple[int, int] | None:
    width, height = struct.unpack("<ii", head[18:26])
    return abs(width), abs(height)


def _webp_size(head: bytes) -> tuple[int, int] | None:
    chunk = head[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", head[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        (bits,) = struct.unpack("<I", head[21:25])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return width, height
    return None
