"""Unit tests for the buffered ASID ring player (c64cast/asid_player.py).

Two layers, both hardware-free:
  * the pure wire-format + 6502 builders (serialize_frame / pack_slot /
    slot_size_for_chips / build_player and the CIA-latch helpers), and
  * AsidRingPlayer's ring math against the shared FakeAPI (slot placement,
    ring wrap, read-head accounting, set_frame_rate re-anchor, teardown restore).

Real-hardware behavior (sound out of the SID under multispeed) is covered by a
Tier-2 smoke run against an ASID host, not here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast import asid_player as ap  # noqa: E402
from c64cast.backend import C64Backend  # noqa: E402
from c64cast.c64 import CLOCK_NTSC  # noqa: E402


def _fake_backend() -> tuple[C64Backend, Any]:
    """A FakeAPI cast to the backend type (it duck-types the write surface),
    plus the same object typed as Any for asserting on its fake attributes."""
    api = FakeAPI()
    return cast(C64Backend, api), api


class SlotSizeTest(unittest.TestCase):
    def test_single_sid_is_128(self):
        self.assertEqual(ap.slot_size_for_chips(1), 128)

    def test_grows_with_chip_count_and_is_aligned(self):
        for n in range(1, 9):
            size = ap.slot_size_for_chips(n)
            self.assertEqual(size % ap._SLOT_ALIGN, 0)
            self.assertGreaterEqual(size, 1 + n * ap.MAX_OPS_PER_CHIP * ap.OP_BYTES)
        # 8-SID worst case fits the documented ~1 KB (912 B).
        self.assertEqual(ap.slot_size_for_chips(8), 912)

    def test_zero_clamps_to_one(self):
        self.assertEqual(ap.slot_size_for_chips(0), 128)


class SerializeFrameTest(unittest.TestCase):
    def test_default_order_noncontrol_then_control(self):
        # Voice-1 freq lo/hi (offsets 0,1) + a single control write (offset 4).
        ops = ap.serialize_frame({0x00: 0x34, 0x01: 0x12, 0x04: 0x41}, {}, 0xD400)
        self.assertEqual(
            ops,
            [(0xD400, 0x34, 0), (0xD401, 0x12, 0), (0xD404, 0x41, 0)],
        )

    def test_absolute_address_from_base(self):
        # A chip mapped at $D420 bakes that base into every op.
        ops = ap.serialize_frame({0x00: 0x11}, {}, 0xD420)
        self.assertEqual(ops, [(0xD420, 0x11, 0)])

    def test_hard_restart_emits_two_control_ops(self):
        # Voice 1: gate-off first (0x08), final gate-on (0x41). First carries the
        # default hard-restart wait, then the final write.
        ops = ap.serialize_frame({0x04: 0x41}, {0: 0x08}, 0xD400)
        self.assertEqual(
            ops,
            [
                (0xD404, 0x08, ap.DEFAULT_HARD_RESTART_WAIT_UNITS),
                (0xD404, 0x41, 0),
            ],
        )

    def test_recipe_reorders_and_applies_waits(self):
        # Recipe writes id 1 (offset 0x01) before id 0 (offset 0x00) and assigns
        # per-op waits (cycles → delay units via DELAY_CYCLES_PER_UNIT).
        ops = ap.serialize_frame({0x00: 0xAA, 0x01: 0xBB}, {}, 0xD400, recipe=[(1, 10), (0, 20)])
        self.assertEqual(
            ops,
            [
                (0xD401, 0xBB, ap._wait_units_for_cycles(10)),
                (0xD400, 0xAA, ap._wait_units_for_cycles(20)),
            ],
        )

    def test_recipe_appends_registers_it_omits(self):
        # Recipe mentions only id 0; the frame's id-1 register still gets written
        # (default order, after the recipe-ordered ones).
        ops = ap.serialize_frame({0x00: 0xAA, 0x01: 0xBB}, {}, 0xD400, recipe=[(0, 0)])
        self.assertEqual(ops[0], (0xD400, 0xAA, 0))
        self.assertIn((0xD401, 0xBB, 0), ops)
        self.assertEqual(len(ops), 2)


class PackSlotTest(unittest.TestCase):
    def test_layout_and_padding(self):
        slot = ap.pack_slot([(0xD404, 0x41, 2), (0xD400, 0x34, 0)], 128)
        self.assertEqual(len(slot), 128)
        self.assertEqual(slot[0], 2)  # n_ops
        self.assertEqual(tuple(slot[1:5]), (0x04, 0xD4, 0x41, 0x02))  # op0
        self.assertEqual(tuple(slot[5:9]), (0x00, 0xD4, 0x34, 0x00))  # op1
        self.assertTrue(all(b == 0 for b in slot[9:]))  # zero-padded tail

    def test_hold_slot_is_all_zero(self):
        slot = ap.hold_slot(128)
        self.assertEqual(len(slot), 128)
        self.assertEqual(slot[0], 0)  # n_ops == 0 → hold tick

    def test_overfull_ops_truncated(self):
        # More ops than the slot can hold are dropped (never happens in practice).
        many = [(0xD400, 0, 0)] * 100
        slot = ap.pack_slot(many, 128)
        self.assertEqual(slot[0], (128 - 1) // ap.OP_BYTES)


class BuildPlayerTest(unittest.TestCase):
    def test_deterministic_and_structure_stable(self):
        # Byte layout is identical across slot sizes / dividers — only operands
        # differ — so the length is a structural invariant.
        blob1 = ap.build_player(128, 1)
        blob2 = ap.build_player(928, 16)
        self.assertEqual(ap.build_player(128, 1), blob1)  # deterministic
        self.assertEqual(len(blob1), len(blob2))

    def test_starts_by_loading_the_tracker_into_reu_src(self):
        # LDA $C800 ; STA $DF04  (reload REU src LO from the main-RAM tracker).
        blob = ap.build_player(128, 1)
        self.assertEqual(
            tuple(blob[:6]),
            (0xAD, ap.TRACKER_ADDR & 0xFF, (ap.TRACKER_ADDR >> 8) & 0xFF, 0x8D, 0x04, 0xDF),
        )

    def test_contains_kernal_chain_and_lean_exit(self):
        blob = ap.build_player(128, 4)
        self.assertIn(bytes([0x4C, 0x31, 0xEA]), blob)  # JMP $EA31 (full tail)
        self.assertIn(bytes([0x4C, 0x81, 0xEA]), blob)  # JMP $EA81 (lean exit)
        self.assertEqual(blob[-1], 0x60)  # delay subroutine RTS


class LatchHelpersTest(unittest.TestCase):
    def test_latch_round_trip(self):
        latch = ap.cia1_latch_for_rate(60.0, "NTSC")
        self.assertEqual(latch, round(CLOCK_NTSC / 60.0) - 1)
        # actual rate recovers close to the request.
        self.assertAlmostEqual(ap.actual_rate_for_latch(latch, "NTSC"), 60.0, delta=0.01)

    def test_latch_clamped_and_rejects_nonpositive(self):
        self.assertLessEqual(ap.cia1_latch_for_rate(1.0, "NTSC"), 0xFFFF)  # clamps
        self.assertGreaterEqual(ap.cia1_latch_for_rate(1e9, "NTSC"), 1)  # never 0
        with self.assertRaises(ValueError):
            ap.cia1_latch_for_rate(0, "NTSC")

    def test_tick_divider(self):
        self.assertEqual(ap.tick_divider_for_rate(60.0), 1)
        self.assertEqual(ap.tick_divider_for_rate(960.0), 16)
        self.assertGreaterEqual(ap.tick_divider_for_rate(1.0), 1)


class RingMathTest(unittest.TestCase):
    def _player(self, n_chips=1):
        api, fake = _fake_backend()
        return ap.AsidRingPlayer(api, system="NTSC", n_chips=n_chips), fake

    def test_write_slots_places_at_slot_offsets(self):
        p, fake = self._player()
        a = bytes([1]) + bytes(p.slot_size - 1)
        b = bytes([2]) + bytes(p.slot_size - 1)
        p._write_slots(0, [a, b])
        offs = [off for off, _ in fake.socket_dma.reuwrites]
        # Two contiguous slots → one transfer at ring_base (b"".join).
        self.assertEqual(offs, [ap.RING_BASE])
        self.assertEqual(fake.socket_dma.reuwrites[0][1], a + b)

    def test_write_slots_splits_at_ring_wrap(self):
        p, fake = self._player()
        s = bytes([9]) + bytes(p.slot_size - 1)
        # Start two slots before the wrap; the 4-slot run splits 2 + 2.
        p._write_slots(ap.RING_SLOTS - 2, [s, s, s, s])
        offs = [off for off, _ in fake.socket_dma.reuwrites]
        self.assertEqual(
            offs,
            [
                ap.RING_BASE + (ap.RING_SLOTS - 2) * p.slot_size,
                ap.RING_BASE,  # wrapped back to the ring start
            ],
        )

    def test_read_head_zero_until_armed(self):
        p, _ = self._player()
        self.assertEqual(p._read_head(), 0)


class BringUpTeardownTest(unittest.TestCase):
    def _player(self, **kw):
        api, fake = _fake_backend()
        return ap.AsidRingPlayer(api, system="NTSC", n_chips=1, **kw), fake

    def test_start_installs_handler_tracker_latch_and_vector(self):
        p, api = self._player(prebuffer_seconds=0.0)
        # Seed a frame so the (tiny) prebuffer collect returns immediately.
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            # Handler uploaded at $C000.
            self.assertIn(f"{ap.HANDLER_ADDR:04X}", api.mem_files)
            # Tracker seeded to the ring base (LO/MI/HI).
            self.assertEqual(
                api.memories[f"{ap.TRACKER_ADDR:04X}"],
                f"{ap.RING_BASE & 0xFF:02X}"
                f"{(ap.RING_BASE >> 8) & 0xFF:02X}"
                f"{(ap.RING_BASE >> 16) & 0xFF:02X}",
            )
            # CIA #1 Timer A latch programmed + $0314 vector swapped to $C000.
            self.assertIn("DC04", api.memories)
            self.assertEqual(
                api.regs["0314"], (ap.HANDLER_ADDR & 0xFF, (ap.HANDLER_ADDR >> 8) & 0xFF)
            )
            self.assertTrue(p._armed)
        finally:
            p.stop()

    def test_stop_restores_vector_and_latch(self):
        p, api = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        p.stop()
        from c64cast.c64 import KERNAL

        self.assertEqual(
            api.regs["0314"], (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        )
        self.assertFalse(p._armed)

    def test_set_frame_rate_reanchors_without_losing_alignment(self):
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            head_before = p._read_head()
            p.set_frame_rate(120.0)
            # The consumed estimate is frozen at the change point (monotone), and
            # the rate roughly doubled.
            self.assertGreaterEqual(p._consumed_base, head_before)
            self.assertAlmostEqual(p._rate, 120.0, delta=1.0)
        finally:
            p.stop()

    def test_reinit_changes_slot_size(self):
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            self.assertEqual(p.slot_size, 128)
            p.reinit(3)
            self.assertEqual(p.n_chips, 3)
            self.assertEqual(p.slot_size, ap.slot_size_for_chips(3))
        finally:
            p.stop()


if __name__ == "__main__":
    unittest.main()
