"""big_text's raster IRQ teardown over a link that loses writes.

A backend reports a lost write by moving `delivery_epoch` rather than by
raising. The kernal handler never acks `$D019`, so restoring `$0314` with the
raster source still live re-enters the IRQ on every RTI, and unmasking CIA #1
with `$0314` still hooked vectors the jiffy IRQ through the overlay's handler.
"""

from __future__ import annotations

import unittest
from typing import Any

from _fakes import FakeAPI

from c64cast.hw.c64 import KERNAL
from c64cast.hw.delivery import CONFIRM_TRIES
from c64cast.scenes.overlays.big_text import BigTextOverlay

_KERNAL_VECTOR = (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)


class _LossyAPI(FakeAPI):
    """Loses the first `losses[address]` writes to `address`: the call
    returns and the epoch moves."""

    def __init__(self, losses: dict[str, int]):
        super().__init__()
        self.losses = dict(losses)
        self.order: list[str] = []

    def _lost(self, address: str) -> bool:
        address = address.upper()
        self.order.append(address)
        if self.losses.get(address, 0) > 0:
            self.losses[address] -= 1
            self.delivery_epoch += 1
            return True
        return False

    def write_memory(self, address, *args, **kwargs):
        return None if self._lost(address) else super().write_memory(address, *args, **kwargs)

    def write_regs(self, address, *args, **kwargs):
        return None if self._lost(address) else super().write_regs(address, *args, **kwargs)


def _uninstall(api: _LossyAPI) -> None:
    overlay: Any = BigTextOverlay(messages=[{"text": "HI"}], charset_path="")
    overlay._uninstall_raster_irq(api)


class BigTextIrqTeardownTest(unittest.TestCase):
    def test_a_write_lost_once_is_written_again(self):
        for address in ("D01A", "0314", "DC0D"):
            with self.subTest(address=address):
                api = _LossyAPI({address: 1})
                _uninstall(api)
                self.assertEqual(api.order.count(address), 2)
                self.assertEqual(api.memories["D01A"], "00")
                self.assertEqual(api.regs["0314"], _KERNAL_VECTOR)
                self.assertEqual(api.memories["DC0D"], "81")

    def test_a_raster_disable_that_never_lands_keeps_the_handler_hooked(self):
        api = _LossyAPI({"D01A": CONFIRM_TRIES})
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR"):
            _uninstall(api)
        self.assertNotIn("0314", api.order)
        self.assertNotIn("DC0D", api.order)

    def test_a_restore_that_never_lands_leaves_cia1_masked(self):
        api = _LossyAPI({"0314": CONFIRM_TRIES})
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR"):
            _uninstall(api)
        self.assertNotIn("DC0D", api.order)

    def test_a_clean_teardown_runs_in_install_reverse_order(self):
        api = _LossyAPI({})
        _uninstall(api)
        self.assertEqual(api.order, ["D01A", "0314", "D019", "DC0D"])


if __name__ == "__main__":
    unittest.main()
