"""picture_size reads a picture's dimensions from its header, for the formats
a slideshow accepts, and gives up on anything else."""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

from c64cast.scenes import picture_header
from c64cast.scenes.picture_header import picture_size

_SOI = b"\xff\xd8"


def _encoded(ext: str, width: int, height: int, *params: int) -> bytes:
    ok, buf = cv2.imencode(ext, np.zeros((height, width, 3), dtype=np.uint8), list(params))
    assert ok
    return buf.tobytes()


def _sof(width: int, height: int) -> bytes:
    return b"\xff\xc0" + struct.pack(">HBHH", 11, 8, height, width) + b"\x01\x01\x11\x00"


class PictureSizeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def _size_of(self, data: bytes) -> tuple[int, int] | None:
        path = os.path.join(self.dir, "picture")
        with open(path, "wb") as f:
            f.write(data)
        return picture_size(path)

    def test_each_accepted_format_reports_its_width_then_height(self):
        cases = {
            "png": _encoded(".png", 37, 21),
            "jpeg": _encoded(".jpg", 37, 21),
            "bmp": _encoded(".bmp", 37, 21),
            "lossy webp": _encoded(".webp", 37, 21, cv2.IMWRITE_WEBP_QUALITY, 80),
            "lossless webp": _encoded(".webp", 37, 21, cv2.IMWRITE_WEBP_QUALITY, 101),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(self._size_of(data), (37, 21))

    def test_an_extended_webp_reports_its_canvas(self):
        vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
        canvas = (20000 - 1).to_bytes(3, "little") + (15000 - 1).to_bytes(3, "little")
        data = b"RIFF" + struct.pack("<I", 30) + b"WEBP" + vp8x + canvas
        self.assertEqual(self._size_of(data), (20000, 15000))

    def test_a_png_header_alone_is_enough(self):
        ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 30000, 30000)
        self.assertEqual(self._size_of(b"\x89PNG\r\n\x1a\n" + ihdr), (30000, 30000))

    def test_a_jpeg_frame_header_is_found_past_other_segments_and_fill(self):
        app1 = b"\xff\xe1" + struct.pack(">H", 1002) + bytes(1000)
        data = _SOI + app1 + b"\xff\xff\xff" + _sof(4032, 3024)
        self.assertEqual(self._size_of(data), (4032, 3024))

    def test_a_jpeg_frame_header_libjpeg_never_reads_does_not_count(self):
        # libjpeg reads FF 00 as stray data and rescans byte by byte, so it
        # finds the COM segment and skips the decoy SOF inside it; a walker
        # that took FF 00 for a segment would jump straight onto the decoy.
        decoy = b"\x00\x00" + _sof(1, 1)
        comment = b"\xff\xfe" + struct.pack(">H", len(decoy) + 2) + decoy
        jpeg = _encoded(".jpg", 37, 21)
        data = jpeg[:2] + b"\xff\x00\x00\x08" + comment + jpeg[2:]
        self.assertIsNone(self._size_of(data))

    def test_anything_else_has_no_size(self):
        cases = {
            "gif": b"GIF89a" + bytes(30),
            "tiff": b"II*\x00" + bytes(30),
            "empty": b"",
            "truncated png": b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR\x00",
            "jpeg with no frame header": _SOI + b"\xff\xda" + bytes(10),
            "jpeg with garbage between segments": _SOI + b"\x00" + _sof(8, 8),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(self._size_of(data))


class JpegWalkBoundTest(unittest.TestCase):
    """An upload is up to media_store.MAX_UPLOAD_BYTES, so a crafted JPEG of
    nothing but tiny segments or 0xFF fill must not be walked to its end."""

    def _reads_to_size(self, data: bytes) -> tuple[tuple[int, int] | None, int]:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "crafted.jpg")
            with open(path, "wb") as f:
                f.write(data)
            reads = 0
            real_open = open

            class CountingFile:
                def __init__(self, f):
                    self._f = f

                def read(self, n=-1):
                    nonlocal reads
                    reads += 1
                    return self._f.read(n)

                def __getattr__(self, name):
                    return getattr(self._f, name)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._f.close()

            def counting_open(*args, **kwargs):
                return CountingFile(real_open(*args, **kwargs))

            with mock.patch.object(picture_header, "open", counting_open, create=True):
                size = picture_size(path)
        return size, reads

    def test_a_run_of_empty_segments_stops_at_the_segment_bound(self):
        segments = 5000
        size, reads = self._reads_to_size(_SOI + b"\xff\xe0\x00\x02" * segments + _sof(8, 8))
        self.assertIsNone(size)
        self.assertLess(reads, segments)

    def test_a_run_of_fill_bytes_stops_at_the_fill_bound(self):
        fill = 5000
        size, reads = self._reads_to_size(_SOI + b"\xff" * fill + _sof(8, 8)[1:])
        self.assertIsNone(size)
        self.assertLess(reads, fill)


if __name__ == "__main__":
    unittest.main()
