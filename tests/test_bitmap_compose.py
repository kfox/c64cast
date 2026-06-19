"""Byte-identity regression guard for the bitmap-mode compose/push split.

HiresDisplayMode + MultiHiresDisplayMode were refactored from a single
render() into compose() (build bitmap/screen/color buffers + a text
surface) + push() (host-DMA or REU bank-swap upload), so overlays can fold
text into the buffers before they go to the U64. With no overlay attached,
the production render path (_render_with_overlays, which now takes the
compose+push branch for these modes) MUST produce byte-identical writes to
the pre-split render(). The golden hashes below were captured from the
inline render() before the split.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast.modes import HiresDisplayMode, MultiHiresDisplayMode  # noqa: E402
from c64cast.scenes import Scene, _render_with_overlays  # noqa: E402


def _frame() -> np.ndarray:
    """Deterministic synthetic BGR gradient (240x320)."""
    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    b = xx / w * 255
    g = yy / h * 255
    r = (xx + yy) / (h + w) * 255
    return np.clip(np.stack([b, g, r], axis=-1), 0, 255).astype(np.uint8)


def _h(b: bytes) -> str:
    return hashlib.sha256(bytes(b)).hexdigest()[:16]


def _render(mode, api) -> None:
    scene = cast(Scene, SimpleNamespace(effect=None))
    _render_with_overlays(mode, api, _frame(), [], 0.0, scene)


# Golden region hashes (addr -> sha256[:16]) and register writes, captured
# from the inline render() before the compose/push split.
_GOLDEN_HOSTDMA = {
    "hires-normal": (
        HiresDisplayMode,
        ("normal",),
        {1024: "d56f71ae4769d98f", 8192: "332778dfb57023e4"},
        {"D020": (12, 12)},
    ),
    "hires-edges": (
        HiresDisplayMode,
        ("edges",),
        {1024: "35fed957a50c1967", 8192: "668946bab9868b28"},
        {"D020": (0, 0)},
    ),
    "mhires-percell": (
        MultiHiresDisplayMode,
        ("percell",),
        {1024: "4fd901acfc2fa87f", 8192: "68857fba356d449a", 55296: "4ebd0814dae0e43e"},
        {"D021": (4,)},
    ),
    "mhires-cheap": (
        MultiHiresDisplayMode,
        ("cheap",),
        {1024: "5aaf072ae0c926a2", 8192: "c7d2fcbcf4a527e8", 55296: "afe05e870798f6ae"},
        {"D021": (4,)},
    ),
    "mhires-grayscale": (
        MultiHiresDisplayMode,
        ("grayscale",),
        {1024: "d52d8c0b230aaa63", 8192: "5f9b89b3e2cc1e81", 55296: "6c15d1fa1194d147"},
        {"D021": (0,)},
    ),
}


class BitmapHostDMAByteIdentityTest(unittest.TestCase):
    def test_host_dma_writes_byte_identical(self):
        for label, (cls, args, regions, regs) in _GOLDEN_HOSTDMA.items():
            with self.subTest(label=label):
                mode = cls(*args)
                api = FakeAPI()
                mode.setup(api)
                _render(mode, api)
                got = {a: _h(d) for a, d in api.regions.items()}
                self.assertEqual(got, regions, f"{label} region bytes drifted")
                for base, vals in regs.items():
                    self.assertEqual(api.regs.get(base), vals, f"{label} reg {base} drifted")

    def test_second_frame_stable(self):
        # EMA/hysteresis modes must produce identical bytes on a repeated frame.
        for label, (cls, args, regions, _regs) in _GOLDEN_HOSTDMA.items():
            with self.subTest(label=label):
                mode = cls(*args)
                api = FakeAPI()
                mode.setup(api)
                _render(mode, api)
                _render(mode, api)
                got = {a: _h(d) for a, d in api.regions.items()}
                self.assertEqual(got, regions, f"{label} drifted on 2nd frame")


_GOLDEN_REU = {
    "hires-normal": (
        HiresDisplayMode,
        ("normal",),
        {"use_reu_staged": True},
        [(14745600, "332778dfb57023e4"), (14753792, "d56f71ae4769d98f")],
        {"C700": "42640c48f2cb0075"},
    ),
    "mhires-percell": (
        MultiHiresDisplayMode,
        ("percell",),
        {"use_reu_staged": True},
        [
            (14745600, "68857fba356d449a"),
            (14753792, "4fd901acfc2fa87f"),
            (14757888, "4ebd0814dae0e43e"),
        ],
        {"C700": "d92c6cd2ed9dbd4a"},
    ),
}


class BitmapREUByteIdentityTest(unittest.TestCase):
    def test_reu_staged_writes_byte_identical(self):
        for label, (cls, args, kw, reuwrites, tracker) in _GOLDEN_REU.items():
            with self.subTest(label=label):
                mode = cls(*args, **kw)
                api = FakeAPI()
                mode.setup(api)
                _render(mode, api)
                got_reu = [(o, _h(d)) for o, d in api.socket_dma.reuwrites]
                self.assertEqual(got_reu, reuwrites, f"{label} REU staging drifted")
                got_tracker = {k: _h(v) for k, v in api.mem_files.items() if k == "C700"}
                self.assertEqual(got_tracker, tracker, f"{label} frame tracker drifted")


if __name__ == "__main__":
    unittest.main()
