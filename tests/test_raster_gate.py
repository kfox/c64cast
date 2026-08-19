"""The $D012 window gate shared by both host-DMA bank-swap IRQ handlers.

A host DMA write halts the 6510 for ~1 us/byte, so a bitmap push can defer the
line-248 raster IRQ ~128 lines into the visible frame. The gate is what stops
the swap from committing there and splitting the picture between two frames.

These execute the real handler bytes under py65: a byte-comparison test cannot
catch a wrong branch displacement or an off-by-one on a window edge, and the
window wraps through 0, which is exactly where an off-by-one would hide.
"""

from __future__ import annotations

import unittest

from c64cast.hw.c64 import (
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    RASTER_COMMIT_LAST_SAFE_LINE,
    RASTER_VBLANK_LINE,
)
from c64cast.video.modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    DD00_BANK_0,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_TRACKER_OFF_PHASE,
    FLICKER_TRACKER_OFF_READY,
    FRAME_TRACKER_ADDR,
    HOSTDMA_SWAP_IRQ_HANDLER,
    HOSTDMA_TRACKER_OFF_READY,
)

BG0 = 0x05

# Lines on which a commit is invisible. The set wraps through 0, and both edges
# are included because the handler's rotate-then-compare has to get both right.
IN_WINDOW = (RASTER_VBLANK_LINE, 0xFB, 0xFF, 0, 1, RASTER_COMMIT_LAST_SAFE_LINE)
# Lines inside the picture, where a commit tears. The first is one past the
# window's far edge; the last is one short of the IRQ line.
OUT_OF_WINDOW = (RASTER_COMMIT_LAST_SAFE_LINE + 1, 51, 128, 200, RASTER_VBLANK_LINE - 1)


class _HandlerHarness:
    """Runs one handler for a field at a chosen raster position."""

    CODE: bytes
    READY_OFF: int
    TRACKER: list[int]

    def _memory(self):
        from py65.memory import ObservableMemory

        mem = ObservableMemory()
        for i, b in enumerate(self.CODE):
            mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
        mem[0xEA31] = 0x60  # RTS where the kernal handler would be
        for off, value in enumerate(self.TRACKER):
            mem[FRAME_TRACKER_ADDR + off] = value
        mem[0xDD00] = DD00_BANK_0
        mem[0xD021] = 0x00
        mem[0xD018] = 0x00
        return mem

    def _run_field(self, mem, line, raster=True):
        from py65.devices.mpu6502 import MPU

        mem[0xD019] = 0x01 if raster else 0x00
        mem[0xD012] = line
        mpu = MPU(memory=mem)
        mpu.pc = BANK_SWAP_IRQ_HANDLER_ADDR
        for _ in range(200):
            if mpu.pc == 0xEA31:
                return
            mpu.step()
        raise AssertionError("handler never chained to the kernal")

    def _stage(self, mem):
        """Arm a frame and advance to the field on which it would commit."""
        mem[FRAME_TRACKER_ADDR + self.READY_OFF] = 1

    def _committed(self, mem):
        return mem[0xDD00] == DD00_BANK_2

    def _still_staged(self, mem):
        return mem[FRAME_TRACKER_ADDR + self.READY_OFF] == 1


class HostdmaGateTest(_HandlerHarness, unittest.TestCase):
    CODE = HOSTDMA_SWAP_IRQ_HANDLER
    READY_OFF = HOSTDMA_TRACKER_OFF_READY
    TRACKER = [BG0, DD00_BANK_2, 0x00]

    def test_commits_inside_the_window(self):
        for line in IN_WINDOW:
            with self.subTest(line=line):
                mem = self._memory()
                self._stage(mem)
                self._run_field(mem, line)
                self.assertTrue(self._committed(mem), f"no swap at raster {line}")
                self.assertEqual(mem[0xD021], BG0)
                self.assertFalse(self._still_staged(mem), "ready flag survived a commit")

    def test_a_late_irq_leaves_the_frame_staged(self):
        for line in OUT_OF_WINDOW:
            with self.subTest(line=line):
                mem = self._memory()
                self._stage(mem)
                self._run_field(mem, line)
                self.assertFalse(self._committed(mem), f"swap committed at raster {line}")
                self.assertEqual(mem[0xD021], 0x00, "bg0 committed without the bank")
                self.assertTrue(self._still_staged(mem), "a deferred frame was dropped")

    def test_a_deferred_frame_commits_on_a_later_field(self):
        """Deferral must cost a field of latency, not the frame."""
        mem = self._memory()
        self._stage(mem)
        self._run_field(mem, 128)
        self.assertFalse(self._committed(mem))
        self._run_field(mem, RASTER_VBLANK_LINE)
        self.assertTrue(self._committed(mem))
        self.assertFalse(self._still_staged(mem))


class FlickerGateTest(_HandlerHarness, unittest.TestCase):
    CODE = FLICKER_SWAP_IRQ_HANDLER
    READY_OFF = FLICKER_TRACKER_OFF_READY
    TRACKER = [BG0, DD00_BANK_2, 0x00, 0x00, D018_HIRES_PAGE_A, D018_HIRES_PAGE_B]

    def _stage(self, mem):
        """Flicker commits only on phase 0, so arm on the field before one."""
        self._run_field(mem, RASTER_VBLANK_LINE)  # phase -> 1
        assert mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_PHASE] == 1
        mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY] = 1

    def test_commits_inside_the_window(self):
        for line in IN_WINDOW:
            with self.subTest(line=line):
                mem = self._memory()
                self._stage(mem)
                self._run_field(mem, line)
                self.assertTrue(self._committed(mem), f"no swap at raster {line}")
                self.assertEqual(mem[0xD021], BG0)
                self.assertFalse(self._still_staged(mem), "ready flag survived a commit")

    def test_a_late_irq_leaves_the_frame_staged(self):
        for line in OUT_OF_WINDOW:
            with self.subTest(line=line):
                mem = self._memory()
                self._stage(mem)
                self._run_field(mem, line)
                self.assertFalse(self._committed(mem), f"swap committed at raster {line}")
                self.assertTrue(self._still_staged(mem), "a deferred frame was dropped")

    def test_a_deferred_frame_waits_for_the_next_phase_zero(self):
        """Both gates apply, so the retry is two fields out, not one."""
        mem = self._memory()
        self._stage(mem)
        self._run_field(mem, 128)  # phase 0, but late — defer
        self.assertFalse(self._committed(mem))
        self._run_field(mem, RASTER_VBLANK_LINE)  # in window, but phase 1
        self.assertFalse(self._committed(mem), "committed on the wrong phase")
        self._run_field(mem, RASTER_VBLANK_LINE)  # phase 0, in window
        self.assertTrue(self._committed(mem))

    def test_the_alternation_free_runs_while_a_commit_is_deferred(self):
        """The gate covers the commit only. If it ever caught the $D018 toggle,
        a slow link would drop fields out of the fusion cadence and the blend
        would break down exactly when the host is busiest."""
        mem = self._memory()
        self._stage(mem)
        seen = []
        for _ in range(6):
            self._run_field(mem, 128)  # every field lands deep in the picture
            seen.append(mem[0xD018])
        self.assertEqual(
            seen,
            [D018_HIRES_PAGE_A, D018_HIRES_PAGE_B] * 3,
            "the alternation stalled while a frame was deferred",
        )
        self.assertFalse(self._committed(mem), "a commit slipped through the gate")


class GateShapeTest(unittest.TestCase):
    def test_every_branch_targets_the_kernal_chain(self):
        """Includes the gate's BCS. A branch that lands mid-handler instead of
        on the chain would skip the kernal's SCNKEY and kill the keyboard."""
        from py65.devices.mpu6502 import MPU
        from py65.disassembler import Disassembler
        from py65.memory import ObservableMemory

        for name, code in (
            ("hostdma", HOSTDMA_SWAP_IRQ_HANDLER),
            ("flicker", FLICKER_SWAP_IRQ_HANDLER),
        ):
            with self.subTest(handler=name):
                mem = ObservableMemory()
                for i, b in enumerate(code):
                    mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
                dis = Disassembler(MPU(memory=mem))
                pc, end = BANK_SWAP_IRQ_HANDLER_ADDR, BANK_SWAP_IRQ_HANDLER_ADDR + len(code)
                chain = end - 3
                targets, last = [], None
                while pc < end:
                    length, text = dis.instruction_at(pc)
                    if text.split()[0] in ("BEQ", "BNE", "BCS", "BCC", "BMI", "BPL"):
                        targets.append(int(text.split("$")[1], 16))
                    last, pc = text, pc + length
                self.assertEqual(pc, end, "instruction stream overruns the handler")
                self.assertEqual(last, "JMP $ea31")
                self.assertIn(len(targets), (3, 4), f"expected every exit branch, got {targets}")
                for t in targets:
                    self.assertEqual(t, chain, f"branch to ${t:04X} misses chain ${chain:04X}")

    def test_no_aliased_raster_line_opens_the_window_on_a_visible_line(self):
        """$D012 is 8 bits and cannot tell line n from n+256. The window is only
        safe if every line that aliases into it really is in vblank on both
        systems — PAL has 312 lines, NTSC 262."""
        bias = (0x100 - RASTER_VBLANK_LINE) & 0xFF
        limit = bias + RASTER_COMMIT_LAST_SAFE_LINE + 1

        def accepts(line):
            return ((line & 0xFF) + bias) & 0xFF < limit

        for total in (262, 312):
            with self.subTest(system=total):
                for line in range(total):
                    visible = RASTER_COMMIT_LAST_SAFE_LINE < line < RASTER_VBLANK_LINE
                    if visible:
                        self.assertFalse(
                            accepts(line), f"line {line} is in the picture but passes the gate"
                        )


if __name__ == "__main__":
    unittest.main()
