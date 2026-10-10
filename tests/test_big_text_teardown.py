"""big_text's raster IRQ teardown over a link that loses writes.

A backend reports a lost write by moving `delivery_epoch` rather than by
raising. The kernal handler never acks `$D019`, so restoring `$0314` with the
raster source still live re-enters the IRQ on every RTI, and unmasking CIA #1
with `$0314` still hooked vectors the jiffy IRQ through the overlay's handler.
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
        for address in (0xD01A, 0x0314, 0xDC0D):
            with self.subTest(address=f"{address:04X}"):
                api = _lossy(address, 1)
                _uninstall(api)
                self.assertEqual(_order(api).count(f"{address:04X}"), 2)
                self.assertEqual(api.memories["D01A"], "00")
                self.assertEqual(api.regs["0314"], _KERNAL_VECTOR)
                self.assertEqual(api.memories["DC0D"], "81")

    def test_a_raster_disable_that_never_lands_keeps_the_handler_hooked(self):
        api = _lossy(0xD01A, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs:
            _uninstall(api)
        self.assertNotIn("0314", _order(api))
        self.assertNotIn("DC0D", _order(api))
        self.assertTrue(any("skipping the vector restore" in m for m in logs.output))
        self.assertTrue(any("skipping the CIA1 unmask" in m for m in logs.output))

    def test_a_hooked_handler_commits_the_default_registers(self):
        api = _lossy(0xD01A, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs:
            _uninstall(api)
        self.assertEqual(api.regs[_SHADOWS], (DEFAULT_D016, DEFAULT_D018))
        self.assertTrue(any("stays hooked" in m for m in logs.output))

    def test_a_restore_that_never_lands_leaves_cia1_masked(self):
        api = _lossy(0x0314, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs:
            _uninstall(api)
        self.assertNotIn("DC0D", _order(api))
        self.assertTrue(any("skipping the CIA1 unmask" in m for m in logs.output))
        self.assertEqual(api.regs[_SHADOWS], (DEFAULT_D016, DEFAULT_D018), "still hooked")
        self.assertTrue(any("stays hooked" in m for m in logs.output))

    def test_a_clean_teardown_runs_in_install_reverse_order(self):
        api = FakeAPI()
        _uninstall(api)
        self.assertEqual(_order(api), ["D01A", "0314", "D019", "DC0D"])


if __name__ == "__main__":
    unittest.main()
