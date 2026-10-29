"""MCMDisplayMode tests — focus on the $3000 custom-charset upload.

The charset lives at $3000, inside the $2000-$3F3F bitmap area that
hires/mhires scenes overwrite. Because display-mode instances are built
once and reused across playlist loops, the charset MUST be re-uploaded on
every setup() — an earlier one-time guard left stale bitmap bytes as the
character set on the second loop (visible as a corrupted charset).
"""

# FakeAPI is a duck-typed stub of Ultimate64API.
# pyright: reportArgumentType=false
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from _fakes import FakeAPI  # noqa: E402

from c64cast.video.modes import MCMDisplayMode  # noqa: E402
from c64cast.video.palette import C64_PALETTE_BGR  # noqa: E402


def _expected_charset() -> bytes:
    charset = bytearray(2048)
    for i in range(256):
        tl, tr, bl, br = (i >> 6) & 3, (i >> 4) & 3, (i >> 2) & 3, i & 3
        row_top = (tl << 6) | (tl << 4) | (tr << 2) | tr
        row_bot = (bl << 6) | (bl << 4) | (br << 2) | br
        charset[i * 8 : i * 8 + 4] = [row_top] * 4
        charset[i * 8 + 4 : i * 8 + 8] = [row_bot] * 4
    return bytes(charset)


class MCMCharsetUploadTest(unittest.TestCase):
    def test_first_setup_uploads_charset(self):
        api = FakeAPI()
        MCMDisplayMode(palette_mode="grayscale").setup(api)
        self.assertEqual(api.mem_files.get("3000"), _expected_charset())

    def test_setup_reuploads_charset_after_clobber(self):
        mode = MCMDisplayMode(palette_mode="grayscale")
        api = FakeAPI()
        mode.setup(api)

        # A bitmap scene (hires/mhires) clobbers $3000 between appearances.
        api.mem_files["3000"] = b"\xde\xad\xbe\xef" * 512

        mode.setup(api)
        self.assertEqual(api.mem_files.get("3000"), _expected_charset())


def _bands(*colors: int) -> np.ndarray:
    """A frame of equal horizontal bands, one per C64 palette index."""
    rows = np.repeat(np.arange(len(colors)), -(-200 // len(colors)))[:200]
    bgr = C64_PALETTE_BGR[np.array(colors)][rows].astype(np.uint8)
    return np.broadcast_to(bgr[:, None, :], (200, 320, 3)).copy()


class MCMBgPickTest(unittest.TestCase):
    def test_a_revisited_scene_picks_from_its_own_first_frame(self):
        """A looping playlist sets the same mode up again; the last lap's
        colors must not hold $D021-$D023 over the new lap's opening frames."""
        mode = MCMDisplayMode()
        mode.setup(FakeAPI())
        for _ in range(10):
            mode.compose(_bands(2, 5, 6))
        mode.setup(FakeAPI())
        fresh = MCMDisplayMode()
        fresh.setup(FakeAPI())
        np.testing.assert_array_equal(
            mode.compose(_bands(7, 10, 14))["bg"], fresh.compose(_bands(7, 10, 14))["bg"]
        )


if __name__ == "__main__":
    unittest.main()
