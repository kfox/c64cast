"""big_text's raster IRQ teardown over a link that loses writes.

The teardown runs the shared `hw/irq_unhook.unhook_raster_irq` sequence, whose
retry rules `tests/test_bank_swap_teardown.py` pins in full; these check what
big_text adds to it and that a lost write still ends with the handler off.
"""

from __future__ import annotations

import unittest
from typing import Any

from _fakes import FakeAPI, lose_writes_to

from c64cast.hw.c64 import KERNAL
from c64cast.hw.delivery import CONFIRM_TRIES
from c64cast.scenes.overlays.big_text import (
    DEFAULT_D016,
    DEFAULT_D018,
    SHADOW_D016_ADDR,
    BigTextOverlay,
)

_KERNAL_VECTOR = (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
_SHADOWS = f"{SHADOW_D016_ADDR:04X}"
_WRITES = ("write_memory", "write_regs", "lost")


def _lossy(address: int, times: int) -> FakeAPI:
    api = FakeAPI()
    lose_writes_to(api, address, times)
    return api


def _order(api: FakeAPI) -> list[str]:
    """Every write the teardown attempted, lost or landed, by address."""
    return [op[1] for op in api.ops if op[0] in _WRITES]


def _uninstall(api: FakeAPI) -> None:
    overlay: Any = BigTextOverlay(messages=[{"text": "HI"}], charset_path="")
    overlay._uninstall_raster_irq(api)


class BigTextIrqTeardownTest(unittest.TestCase):
    def test_a_write_lost_once_is_written_again(self):
        # $DC0D carries the mask and the unmask, so a lost mask makes it three.
        for address, count in ((0xD01A, 2), (0x0314, 2), (0xDC0D, 3)):
            with self.subTest(address=f"{address:04X}"):
                api = _lossy(address, 1)
                _uninstall(api)
                self.assertEqual(_order(api).count(f"{address:04X}"), count)
                self.assertEqual(api.memories["D01A"], "00")
                self.assertEqual(api.regs["0314"], _KERNAL_VECTOR)
                self.assertEqual(api.memories["DC0D"], "81")

    def test_a_raster_disable_that_never_lands_is_written_again_behind_the_restore(self):
        api = _lossy(0xD01A, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR"):
            _uninstall(api)
        order = _order(api)
        restore = order.index("0314")
        self.assertEqual(order[restore:].count("D01A"), 1)
        self.assertEqual(api.memories["D01A"], "00")
        self.assertEqual(api.memories["DC0D"], "81")

    def test_a_restore_that_never_lands_leaves_cia1_masked_and_resets_the_shadows(self):
        api = _lossy(0x0314, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs:
            _uninstall(api)
        self.assertEqual(api.memories["DC0D"], "7F")
        self.assertTrue(any("leaving CIA #1 Timer A masked" in m for m in logs.output))
        self.assertEqual(api.regs[_SHADOWS], (DEFAULT_D016, DEFAULT_D018), "still hooked")
        self.assertTrue(any("stays hooked" in m for m in logs.output))

    def test_a_clean_teardown_runs_the_shared_unhook(self):
        api = FakeAPI()
        _uninstall(api)
        self.assertEqual(_order(api), ["DC0D", "D01A", "0314", "D019", "DC0D"])
        self.assertNotIn(_SHADOWS, api.regs, "a restored vector leaves the shadows alone")


if __name__ == "__main__":
    unittest.main()
