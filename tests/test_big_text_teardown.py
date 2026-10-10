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
    DISARMED_RASTER_IRQ_HANDLER,
    IRQ_HANDLER_ADDR,
    RASTER_IRQ_HANDLER,
    SHADOW_D016_ADDR,
    SHADOW_D018_ADDR,
    BigTextOverlay,
)

_KERNAL_VECTOR = (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
_HANDLER = f"{IRQ_HANDLER_ADDR:04X}"
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

    def test_a_restore_that_never_lands_leaves_cia1_masked_and_disarms_the_handler(self):
        api = _lossy(0x0314, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs:
            _uninstall(api)
        self.assertEqual(api.memories["DC0D"], "7F")
        self.assertTrue(any("leaving CIA #1 Timer A masked" in m for m in logs.output))
        self.assertEqual(api.mem_files[_HANDLER], DISARMED_RASTER_IRQ_HANDLER, "still hooked")
        self.assertTrue(any("stays hooked" in m for m in logs.output))

    def test_a_clean_teardown_runs_the_shared_unhook(self):
        api = FakeAPI()
        _uninstall(api)
        self.assertEqual(_order(api), ["DC0D", "D01A", "0314", "D019", "DC0D"])
        self.assertNotIn(_HANDLER, api.mem_files, "a restored vector leaves the handler alone")


class DisarmedHandlerTest(unittest.TestCase):
    """The disarmed handler, run under py65 from every instruction boundary of
    the armed one: the disarm write can land while the 6510 is anywhere in it."""

    _OPCODE_LEN = {0xAD: 3, 0x8D: 3, 0xA9: 2, 0x4C: 3}

    def _boundaries(self, code: bytes) -> list[int]:
        offsets, pc = [], 0
        while pc < len(code):
            offsets.append(pc)
            pc += self._OPCODE_LEN[code[pc]]
        return offsets

    def test_it_keeps_every_instruction_boundary(self):
        self.assertEqual(
            self._boundaries(DISARMED_RASTER_IRQ_HANDLER), self._boundaries(RASTER_IRQ_HANDLER)
        )

    def test_it_acks_and_chains_without_committing_from_any_boundary(self):
        from py65.devices.mpu6502 import MPU
        from py65.memory import ObservableMemory

        for start in self._boundaries(RASTER_IRQ_HANDLER):
            with self.subTest(start=start):
                mem = ObservableMemory()
                for i, b in enumerate(DISARMED_RASTER_IRQ_HANDLER):
                    mem[IRQ_HANDLER_ADDR + i] = b
                mem[SHADOW_D016_ADDR], mem[SHADOW_D018_ADDR] = 0x08, 0x14
                mem[0xD016], mem[0xD018], mem[0xD019] = 0x18, 0x1C, 0x00
                mpu = MPU(memory=mem)
                mpu.pc = IRQ_HANDLER_ADDR + start
                # Past the LDA #$01 the armed bytes ran it already.
                mpu.a = 0x01 if start > 12 else 0x00
                for _ in range(20):
                    if mpu.pc == 0xEA31:
                        break
                    mpu.step()
                self.assertEqual(mpu.pc, 0xEA31, "chains to the kernal")
                self.assertEqual((mem[0xD016], mem[0xD018]), (0x18, 0x1C), "an MCM scene's")
                if start <= 14:
                    self.assertEqual(mem[0xD019], 0x01, "the raster flag is acked")


if __name__ == "__main__":
    unittest.main()
