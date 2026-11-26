"""After a swap handler pushes an Ultimate's loaded palette command, the IRQs
that follow accept the interface's answer: once the push's handshake has
cleared and the interface has left BUSY, and only while one is pending.

These run the real handler bytes under py65, with $DF1C reading back the
status each case sets."""

from __future__ import annotations

import unittest

from py65.devices.mpu6502 import MPU
from py65.memory import ObservableMemory
from test_commit_window import DISPATCHERS

from c64cast.hw.c64 import KERNAL, RASTER_VBLANK_LINE
from c64cast.hw.uci import (
    CONTROL_NEXT_DATA,
    STATE_BUSY,
    STATE_IDLE,
    UCI_CONTROL,
)
from c64cast.video import modes_irq
from c64cast.video.modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_SWAP_REPORTING_IRQ_HANDLER,
    HOSTDMA_SWAP_IRQ_HANDLER,
    HOSTDMA_SWAP_REPORTING_IRQ_HANDLER,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
)

# The two states that carry an answer: the last of it, and more to come.
STATE_DATA_LAST = 0x20
STATE_DATA_MORE = 0x30
# The push's own handshake bit, still set until the interface takes it.
HANDSHAKE_PUSH = 0x01

HANDLERS = (
    *((name, handler, True) for name, handler, _ in DISPATCHERS),
    ("hostdma", HOSTDMA_SWAP_IRQ_HANDLER, False),
    ("flicker", FLICKER_SWAP_IRQ_HANDLER, False),
    ("hostdma reporting", HOSTDMA_SWAP_REPORTING_IRQ_HANDLER, False),
    ("flicker reporting", FLICKER_SWAP_REPORTING_IRQ_HANDLER, False),
)


def run_idle_field(handler: bytes, *, reu: bool, pending: int, status: int):
    """One raster IRQ with no frame staged; the $DF1C writes it made and
    the answer-pending byte it left."""
    mem = ObservableMemory()
    for i, b in enumerate(handler):
        mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
    if reu:
        for i, b in enumerate(modes_irq.BANK_SWAP_STATE_INIT):
            mem[modes_irq.BANK_SWAP_STATE_ADDR + i] = b
        mem[REU_PUMP_BODY_SUBROUTINE_ADDR] = 0x60  # RTS
    mem[modes_irq._ANSWER_PENDING] = pending
    mem[0xD019] = 0x01
    mem[0xD012] = RASTER_VBLANK_LINE
    writes: list[int] = []
    mem.subscribe_to_read([UCI_CONTROL], lambda _a: status)
    mem.subscribe_to_write([UCI_CONTROL], lambda _a, v: writes.append(v))
    mpu = MPU(memory=mem)
    mpu.pc = BANK_SWAP_IRQ_HANDLER_ADDR
    for _ in range(2000):
        if mpu.pc == KERNAL.IRQ_HANDLER:
            return writes, mem[modes_irq._ANSWER_PENDING]
        mpu.step()
    raise AssertionError("handler never chained to the kernal")


class AnswerAcceptTest(unittest.TestCase):
    def _each(self, *, pending: int, status: int):
        for name, handler, reu in HANDLERS:
            with self.subTest(handler=name, status=hex(status)):
                yield run_idle_field(handler, reu=reu, pending=pending, status=status)

    def test_an_answer_is_accepted_once_the_interface_has_one(self):
        for status in (STATE_DATA_LAST, STATE_DATA_MORE):
            for writes, pending in self._each(pending=1, status=status):
                self.assertEqual(writes, [CONTROL_NEXT_DATA])
                self.assertEqual(pending, 0)

    def test_nothing_is_accepted_while_the_push_is_unanswered(self):
        for status in (STATE_DATA_LAST | HANDSHAKE_PUSH, STATE_BUSY):
            for writes, pending in self._each(pending=1, status=status):
                self.assertEqual(writes, [])
                self.assertEqual(pending, 1, "the answer was given up before it came")

    def test_an_interface_back_at_idle_has_nothing_to_accept(self):
        for writes, pending in self._each(pending=1, status=STATE_IDLE):
            self.assertEqual(writes, [])
            self.assertEqual(pending, 0)

    def test_nothing_is_accepted_without_a_push(self):
        for writes, pending in self._each(pending=0, status=STATE_DATA_LAST):
            self.assertEqual(writes, [])
            self.assertEqual(pending, 0)


if __name__ == "__main__":
    unittest.main()
