"""A double-buffered bitmap mode carries a palette cue on the frame after
`cue_palette_change`: the cue byte and the tracker go in one write from
PALETTE_CUE_ADDR, and every other frame leaves the cue alone."""

from __future__ import annotations

import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI, quiet_logging

from c64cast.hw.api import Ultimate64API
from c64cast.video.modes import BitmapDisplayMode, HiresDisplayMode, MultiHiresDisplayMode
from c64cast.video.modes_irq import (
    FRAME_TRACKER_ADDR,
    KAWARI_PALETTE_CUE,
    PALETTE_CUE_ADDR,
    PALETTE_CUE_STATE_ADDR,
    PALETTE_CUE_STATE_LEN,
    ULTIMATE_PALETTE_CUE,
)

CUE_KEY = f"{PALETTE_CUE_ADDR:04X}"
TRACKER_KEY = f"{FRAME_TRACKER_ADDR:04X}"


def _modes():
    return (
        ("hires reu", lambda: HiresDisplayMode(use_reu_staged=True), False),
        ("mhires reu", lambda: MultiHiresDisplayMode(use_reu_staged=True), False),
        ("hires page flip", lambda: HiresDisplayMode(double_buffer=True), True),
        ("mhires page flip", lambda: MultiHiresDisplayMode(double_buffer=True), True),
        ("flicker", lambda: HiresDisplayMode("normal", flicker_tolerance="visible"), True),
    )


class PaletteCueArmTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(quiet_logging())

    def _frame(self, shade: int) -> np.ndarray:
        return np.full((200, 320, 3), shade, dtype=np.uint8)

    def _arm(self, mode, api: FakeAPI, shade: int) -> tuple[str, bytes]:
        """Render one frame; its tracker write."""
        api.writes.clear()
        mode.render(cast(Ultimate64API, api), self._frame(shade))
        arms = [(key, data) for key, data in api.writes if key in (CUE_KEY, TRACKER_KEY)]
        self.assertEqual(len(arms), 1, f"expected one tracker write, got {arms}")
        return arms[0]

    def _ready(self, name, make, set_up) -> tuple[BitmapDisplayMode, FakeAPI]:
        api = FakeAPI()
        mode = make()
        if set_up:
            mode.setup(cast(Ultimate64API, api))
        return mode, api

    def test_the_frame_after_a_cue_carries_it_ahead_of_its_tracker(self):
        for name, make, set_up in _modes():
            with self.subTest(mode=name):
                mode, api = self._ready(name, make, set_up)
                key, plain = self._arm(mode, api, 10)
                self.assertEqual(key, TRACKER_KEY)
                for cue in (KAWARI_PALETTE_CUE, ULTIMATE_PALETTE_CUE):
                    mode.cue_palette_change(cue)
                    key, cued = self._arm(mode, api, 200)
                    self.assertEqual(key, CUE_KEY)
                    self.assertEqual(cued[0], cue)
                self.assertEqual(len(cued), 1 + len(plain))

    def test_a_cue_rides_one_frame_only(self):
        for name, make, set_up in _modes():
            with self.subTest(mode=name):
                mode, api = self._ready(name, make, set_up)
                mode.cue_palette_change(KAWARI_PALETTE_CUE)
                self._arm(mode, api, 10)
                key, _ = self._arm(mode, api, 200)
                self.assertEqual(key, TRACKER_KEY)

    def test_a_setup_clears_the_cue_on_both_sides(self):
        for name, make, set_up in _modes():
            if not set_up:
                continue
            with self.subTest(mode=name):
                mode, api = self._ready(name, make, False)
                mode.cue_palette_change(KAWARI_PALETTE_CUE)
                mode.setup(cast(Ultimate64API, api))
                self.assertEqual(
                    api.mem_files[f"{PALETTE_CUE_STATE_ADDR:04X}"], bytes(PALETTE_CUE_STATE_LEN)
                )
                key, _ = self._arm(mode, api, 10)
                self.assertEqual(key, TRACKER_KEY)

    def test_only_a_set_up_double_buffer_takes_cues(self):
        for name, make, set_up in _modes():
            if not set_up:
                continue
            with self.subTest(mode=name):
                mode, api = self._ready(name, make, False)
                self.assertFalse(mode.cues_palette_changes)
                mode.setup(cast(Ultimate64API, api))
                self.assertTrue(mode.cues_palette_changes)
        with self.subTest(mode="single-buffered hires"):
            mode = HiresDisplayMode()
            mode.setup(cast(Ultimate64API, FakeAPI()))
            self.assertFalse(mode.cues_palette_changes)


if __name__ == "__main__":
    unittest.main()
