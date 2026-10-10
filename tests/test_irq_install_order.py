"""Raster IRQ installs mask both IRQ sources before they upload anything.

A teardown whose `$0314` restore never landed leaves the vector on the old
handler's address. An install that uploads over that address while CIA #1 or
the raster source is still live lets an IRQ run a half-written handler.
"""

from __future__ import annotations

import unittest
from typing import Any, cast

from _fakes import FakeAPI

from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import CIA1, VECTORS
from c64cast.scenes.overlays.big_text import BigTextOverlay
from c64cast.video import modes_irq

_CIA1_ICR = f"{CIA1.ICR:04X}"
_VECTOR = f"{VECTORS.IRQ:04X}"


def _first(ops: list[tuple[Any, ...]], name: str, address: str, value: Any = None) -> int:
    for i, op in enumerate(ops):
        if op[0] == name and op[1] == address and (value is None or op[2] == value):
            return i
    raise AssertionError(f"no {name} to {address}")


class InstallOrderTest(unittest.TestCase):
    def _assert_masked_before_uploads(self, ops: list[tuple[Any, ...]], mask_value: str) -> None:
        mask = _first(ops, "write_memory", _CIA1_ICR, mask_value)
        disable = _first(ops, "write_memory", "D01A", "00")
        uploads = [i for i, op in enumerate(ops) if op[0] in ("write_memory_file", "write_regs")]
        self.assertTrue(uploads)
        self.assertLess(max(mask, disable), min(uploads), "every upload follows both masks")
        self.assertLess(max(uploads[:-1]), _first(ops, "write_regs", _VECTOR), "hooked last")

    def test_bank_swap_install_masks_before_it_uploads(self):
        for pump in (False, True):
            with self.subTest(audio_pump_active=pump):
                api = FakeAPI()
                modes_irq.install_bank_swap_irq(
                    cast(Ultimate64API, api),
                    modes_irq.BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER
                    if pump
                    else modes_irq.BANK_SWAP_IRQ_HANDLER,
                    audio_pump_active=pump,
                )
                self._assert_masked_before_uploads(
                    api.ops, f"{modes_irq._CIA1_ICR_DISABLE_TIMER_A:02X}"
                )

    def test_big_text_install_masks_before_it_uploads(self):
        api = FakeAPI()
        overlay: Any = BigTextOverlay(messages=[{"text": "HI"}], charset_path="")
        overlay._install_raster_irq(api)
        self._assert_masked_before_uploads(api.ops, "7F")


if __name__ == "__main__":
    unittest.main()
