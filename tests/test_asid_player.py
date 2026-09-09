"""Unit tests for the buffered ASID ring player (c64cast/sid/asid_player.py).

Two layers, both hardware-free:
  * the pure wire-format + 6502 builders (serialize_frame / pack_slot /
    slot_size_for_chips / build_player and the CIA-latch helpers), and
  * AsidRingPlayer's ring math against the shared FakeAPI (slot placement,
    ring wrap, read-head accounting, set_frame_rate re-anchor, teardown restore).

Real-hardware behavior (sound out of the SID under multispeed) is covered by a
Tier-2 smoke run against an ASID host, not here.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI, frozen_throttle, frozen_throttles  # noqa: E402

from c64cast.hw.backend import C64Backend  # noqa: E402
from c64cast.hw.c64 import CLOCK_NTSC  # noqa: E402
from c64cast.sid import asid_player as ap  # noqa: E402


def _fake_backend() -> tuple[C64Backend, Any]:
    """A FakeAPI cast to the backend type (it duck-types the write surface),
    plus the same object typed as Any for asserting on its fake attributes."""
    api = FakeAPI()
    return cast(C64Backend, api), api


# The largest wait a `0x30` pair can carry, in delay-loop units. Derived, not
# written down: `_wait_units_for_cycles` is what converts the wire's 255-cycle
# ceiling, and a changed DELAY_CYCLES_PER_UNIT moves this with it. The guard
# against drift must not carry an un-derived literal of its own -- with 51
# hardcoded, a DELAY_CYCLES_PER_UNIT of 4 would have had this file computing a
# maximal frame of 7896 against a true 9352 and directing a maintainer to write
# the wrong number into both prose sites, blessed by a green test.
_MAX_WIRE_WAIT_UNITS = ap._wait_units_for_cycles(255)  # 255 = the wire's one wait byte


def _packed_latch(latch: int) -> str:
    """The little-endian hex pair write_memory sends for a CIA Timer A latch."""
    return f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"


# The 6502's own numbers for the opcodes build_player emits: (base cycles,
# instruction length). Branches are listed at their not-taken cost; a taken
# branch inside a page costs one more, which _taken_path_cycles adds. This is
# the only literal left in the derivation, and it is a property of the CPU
# rather than of this code -- the 6502 does not get a new revision.
_OPCODES: dict[int, tuple[int, int]] = {
    0x18: (2, 1),  # CLC
    0x20: (6, 3),  # JSR abs
    0x4C: (3, 3),  # JMP abs
    0x60: (6, 1),  # RTS
    0x69: (2, 2),  # ADC #
    0x85: (3, 2),  # STA zp
    0x88: (2, 1),  # DEY
    0x8D: (4, 3),  # STA abs
    0x90: (2, 2),  # BCC
    0xA0: (2, 2),  # LDY #
    0xA5: (3, 2),  # LDA zp
    0xA8: (2, 1),  # TAY
    0xA9: (2, 2),  # LDA #
    0xAD: (4, 3),  # LDA abs
    0xB1: (5, 2),  # LDA (zp),Y   (+1 on a page cross; see the docstring)
    0xC9: (2, 2),  # CMP #
    0xCE: (6, 3),  # DEC abs
    0xD0: (2, 2),  # BNE
    0xE6: (5, 2),  # INC zp
    0xF0: (2, 2),  # BEQ
}
_BRANCHES = frozenset({0x90, 0xD0, 0xF0})


def _instructions(blob: bytes, origin: int, start: int, end: int):
    """(address, opcode) for each instruction in the address range [start, end).

    Raises on an opcode the table above does not carry, so a new instruction in
    the player forces the table to grow instead of being silently skipped."""
    addr = start
    while addr < end:
        op = blob[addr - origin]
        if op not in _OPCODES:
            raise AssertionError(f"no cycle count for opcode {op:#04x} at {addr:#06x}")
        yield addr, op
        addr += _OPCODES[op][1]


def _branch_target(blob: bytes, origin: int, addr: int) -> int:
    disp = blob[addr - origin + 1]
    return addr + 2 + (disp - 256 if disp > 127 else disp)


def _taken_path_cycles(blob: bytes, origin: int, start: int, end: int) -> int:
    """Cycles the 6510 spends walking [start, end) with every branch taken.

    Taking a branch means the instructions it jumps over never execute, so they
    are skipped rather than summed. Every branch in the player's inner loop is
    taken on the ordinary path: the wait is zero, the slot pointer does not
    carry, and the op counter has not reached zero."""
    total = 0
    skip_until = start
    for addr, op in _instructions(blob, origin, start, end):
        if addr < skip_until:
            continue
        total += _OPCODES[op][0]
        if op in _BRANCHES:
            target = _branch_target(blob, origin, addr)
            if (addr + 2) & 0xFF00 != target & 0xFF00:
                raise AssertionError(f"branch at {addr:#06x} crosses a page (costs 2 more)")
            total += 1
            skip_until = target
    return total


def _straight_cycles(blob: bytes, origin: int, start: int, end: int) -> int:
    """Cycles of [start, end) executed in order, no branch taken."""
    return sum(_OPCODES[op][0] for _addr, op in _instructions(blob, origin, start, end))


# The assembled IRQ player for build_player(128, 1), byte for byte. Every other
# assertion in this file is symbolic or structural, which is why two mutations
# that corrupt the generated 6502 — the op loop's `STA $0000` (0x8D) flipped to
# `STX` (0x8E), so every SID write stores X instead of the value, and
# HANDLER_ADDR relocated onto LANDING_BUF, where the REU pull overwrites the
# handler every tick — both left the whole ASID suite green. Regenerate this blob
# only when the handler deliberately changes, and read the diff opcode by opcode.
_GOLDEN_PLAYER_128_1 = bytes.fromhex(
    "ad00c88d04dfad01c88d05dfad02c88d06dfa9008d02dfa9c48d03dfa9808d07"
    "dfa9008d08dfa9008d0adfa9918d01df18ad00c869808d00c8ad01c869008d01"
    "c8ad02c869008d02c8ad02c8c9319021d010ad01c8c9009018d007ad00c8c900"
    "900fa9008d00c8a9008d01c8a9308d02c8a90085fba9c485fca000b1fbf0378d"
    "04c8e6fbd002e6fca000b1fb8d9bc0a001b1fb8d9cc0a002b1fb8d0000a003b1"
    "fbf00320c9c0a5fb18690485fb9002e6fcce04c8d0d2ce03c8d008a9018d03c8"
    "4c31eaad0ddc4c81eaa888d0fd60"
)


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
        # per-op waits. The expected units are literal (10 and 20 cycles at
        # DELAY_CYCLES_PER_UNIT) — see CostModelConstantsTest for why.
        ops = ap.serialize_frame({0x00: 0xAA, 0x01: 0xBB}, {}, 0xD400, recipe=[(1, 10), (0, 20)])
        self.assertEqual(
            ops,
            [
                (0xD401, 0xBB, 2),
                (0xD400, 0xAA, 4),
            ],
        )

    def test_recipe_repeating_an_id_emits_that_write_once(self):
        # Op count must track the frame's write count, not the recipe's length.
        # A 28-pair recipe (spec-legal) all naming id 0 used to emit 28 ops for
        # one register, pushing the slot past MAX_OPS_PER_CHIP.
        ops = ap.serialize_frame({0x00: 0xAA, 0x01: 0xBB}, {}, 0xD400, recipe=[(0, 10)] * 28)
        self.assertEqual(ops[0], (0xD400, 0xAA, 2))
        self.assertEqual(len(ops), 2)  # + the id-1 register the recipe omits

    def test_a_full_frame_under_any_legal_recipe_fits_one_chips_ops(self):
        # The bound pack_slot's slot sizing assumes: one chip's serialized frame
        # never exceeds MAX_OPS_PER_CHIP, whatever order a 0x30 asks for.
        regs = dict.fromkeys(range(0x19), 0x11)
        control_first = {0: 0x08, 1: 0x08, 2: 0x08}
        recipe = [(rid, 255) for rid in range(28)] + [(0, 255)] * 28
        ops = ap.serialize_frame(regs, control_first, 0xD400, recipe=recipe)
        self.assertLessEqual(len(ops), ap.MAX_OPS_PER_CHIP)

    def test_a_recipe_naming_the_final_control_id_cannot_invert_a_hard_restart(self):
        """A voice's hard restart is two writes to one register — gate-off, then
        the re-attack — and ids 25-27 are the *ordinary* control ids, so a stream
        using them is the common case, not an exotic one. Ordering the writes by
        ASID register id let a recipe naming 25+v but not 22+v emit the re-attack
        at the recipe position and the gate-off after it, in the tail append: the
        voice ended the frame gated off and never sounded."""
        ops = ap.serialize_frame(
            {0x00: 0x10, 0x01: 0x20, 0x04: 0x41},
            {0: 0x08},
            0xD400,
            recipe=[(0, 0), (1, 0), (25, 0)],
        )
        self.assertEqual(
            [(a, v) for a, v, _w in ops if a == 0xD404], [(0xD404, 0x08), (0xD404, 0x41)]
        )

    def test_no_recipe_can_reorder_a_voices_two_control_writes(self):
        """The same repro with one field varied: which of the pair's ids the
        recipe names, in which order, for each voice. The correct order has to be
        a property of the serializer, because the recipe is wire-supplied and
        arbitrary — a well-formed one is not something the input can be trusted
        to be."""
        for voice, offset in enumerate(ap._CONTROL_OFFSETS):
            first_id, final_id = 22 + voice, 25 + voice
            for named in ([], [first_id], [final_id], [first_id, final_id], [final_id, first_id]):
                recipe = [(0, 0)] + [(rid, 40) for rid in named] + [(1, 0)]
                with self.subTest(voice=voice, named=named):
                    ops = ap.serialize_frame(
                        {0x00: 0x10, 0x01: 0x20, offset: 0x41},
                        {voice: 0x08},
                        0xD400,
                        recipe=recipe,
                    )
                    written = [v for a, v, _w in ops if a == 0xD400 + offset]
                    self.assertEqual(written, [0x08, 0x41])

    def test_a_recipe_naming_both_control_ids_keeps_both_waits(self):
        # The pair is positioned as a unit, but each write still takes the wait
        # the recipe gave its own id — so a well-formed recipe loses nothing.
        ops = ap.serialize_frame({0x04: 0x41}, {0: 0x08}, 0xD400, recipe=[(22, 100), (25, 50)])
        self.assertEqual(
            ops,
            [
                (0xD404, 0x08, 20),
                (0xD404, 0x41, 10),
            ],
        )

    def test_a_recipe_id_outside_the_register_table_is_skipped(self):
        # Recipe ids arrive as `data0 & 0x3F`, so 28-63 are reachable from the
        # wire and name no register.
        ops = ap.serialize_frame({0x00: 0xAA}, {}, 0xD400, recipe=[(63, 10), (0, 20)])
        self.assertEqual(ops, [(0xD400, 0xAA, 4)])

    def test_recipe_appends_registers_it_omits(self):
        # Recipe mentions only id 0; the frame's id-1 register still gets written
        # (default order, after the recipe-ordered ones).
        ops = ap.serialize_frame({0x00: 0xAA, 0x01: 0xBB}, {}, 0xD400, recipe=[(0, 0)])
        self.assertEqual(ops[0], (0xD400, 0xAA, 0))
        self.assertIn((0xD401, 0xBB, 0), ops)
        self.assertEqual(len(ops), 2)


class CostModelConstantsTest(unittest.TestCase):
    """The three constants of the frame cost model, re-derived from the 6502
    `build_player` actually emits.

    These used to be hand-counted literals sitting beside a golden blob, with
    nothing relating the two. That detects a constant being *edited* and not
    the assembly moving underneath it — which is the direction that produces a
    6510 lockup, and it was demonstrated: inserting a NOP into `oploop`,
    regenerating `_GOLDEN_PLAYER_128_1` to match, and leaving
    `PER_OP_CYCLES = 65` alone left the whole ASID suite green while every op was
    under-charged by 2 cycles. The docstring's closing instruction to
    "re-derive these numbers off the new oploop/dloop" was a request to a
    human, not a check.

    So the numbers are now walked out of the emitted bytes, between the labels
    `build_player_symbols` hands back. The only literals left are the 6502's
    own per-opcode cycle counts in `_OPCODES`, which are a property of the CPU
    rather than of this code.
    """

    def setUp(self):
        self.blob, self.sym = ap.build_player_symbols(128, 1)
        self.origin = ap.HANDLER_ADDR

    def _first(self, opcode: int, start: int, end: int) -> int:
        """Address of the first `opcode` in [start, end)."""
        for addr, op in _instructions(self.blob, self.origin, start, end):
            if op == opcode:
                return addr
        raise AssertionError(f"no {opcode:#04x} between {start:#06x} and {end:#06x}")

    def _dloop_end(self) -> int:
        """Address just past the branch that closes `dloop`.

        The loop and the subroutine's return tail must be split here and not at
        the RTS. There is no label between them, so walking `dloop` up to the
        RTS charges anything inserted in that gap to the per-*unit* cost — an
        instruction added before the return costs once per delay CALL, and
        blaming DELAY_CYCLES_PER_UNIT for it is a red test at the wrong
        constant."""
        for addr, op in _instructions(
            self.blob, self.origin, self.sym["dloop"], self.origin + len(self.blob)
        ):
            if (
                op in _BRANCHES
                and _branch_target(self.blob, self.origin, addr) == self.sym["dloop"]
            ):
                return addr + 2
        raise AssertionError("dloop does not branch back to itself")

    def test_delay_cycles_per_unit_is_what_dloop_emits(self):
        # `dloop`: DEY + BNE taken. The last iteration's BNE falls through one
        # cycle cheaper, so the real loop costs 5N-1 — the model rounds up,
        # which errs toward calling a frame too expensive.
        self.assertEqual(
            _taken_path_cycles(self.blob, self.origin, self.sym["dloop"], self._dloop_end()),
            ap.DELAY_CYCLES_PER_UNIT,
        )

    def test_per_op_cycles_is_what_one_pass_of_oploop_emits(self):
        # One unwaited op: unpack the address and value, store it, skip the
        # delay call, advance the slot pointer, decrement the op counter and
        # loop. Every branch on that path is taken, so the walk skips what each
        # jumps over.
        self.assertEqual(
            _taken_path_cycles(self.blob, self.origin, self.sym["oploop"], self.sym["tail"]),
            ap.PER_OP_CYCLES,
        )

    def test_per_op_cycles_is_a_best_case_the_budget_fraction_covers(self):
        # It is the best case, and unlike DELAY_CYCLES_PER_UNIT that was never
        # written down. Two paths cost more and neither is exotic: each
        # `LDA ($FB),Y` costs one extra when the slot pointer's low byte plus Y
        # crosses a page (four per op), and when that low byte wraps the BCC
        # falls through to an `INC $FC` instead of branching. So the model
        # under-charges a worst-case op — in the unsafe direction. What makes
        # that survivable is FRAME_BUDGET_FRACTION, and this pins the margin
        # rather than asserting it in prose.
        page_crossings = sum(
            1
            for _addr, op in _instructions(
                self.blob, self.origin, self.sym["oploop"], self.sym["skipdelay"]
            )
            if op == 0xB1
        )
        bcc = self._first(0x90, self.sym["skipdelay"], self.sym["op_noinc"])
        wrapped = _straight_cycles(self.blob, self.origin, bcc, self.sym["op_noinc"])
        worst = ap.PER_OP_CYCLES + page_crossings + (wrapped - (_OPCODES[0x90][0] + 1))
        self.assertGreater(worst, ap.PER_OP_CYCLES, "the best case is not the only case")
        self.assertLess(
            worst / ap.PER_OP_CYCLES,
            1.0 / ap.FRAME_BUDGET_FRACTION,
            "a worst-case op must still fit in the headroom FRAME_BUDGET_FRACTION reserves",
        )

    def test_waited_op_extra_cycles_is_the_delay_call_around_the_loop(self):
        # A nonzero wait falls THROUGH the BEQ and pays the JSR, the TAY that
        # loads the counter and the RTS — on top of DELAY_CYCLES_PER_UNIT per
        # unit, and minus the taken BEQ that PER_OP_CYCLES already charged.
        beq = self._first(0xF0, self.sym["oploop"], self.sym["skipdelay"])
        # The whole return tail is walked, not a bare RTS charged: everything
        # after the loop's branch costs once per delay CALL, so a terminator
        # that changed — or anything inserted ahead of it — has to land here
        # rather than be assumed away.
        extra = (
            _straight_cycles(self.blob, self.origin, beq, self.sym["skipdelay"])
            + _straight_cycles(self.blob, self.origin, self.sym["delay"], self.sym["dloop"])
            + _straight_cycles(
                self.blob, self.origin, self._dloop_end(), self.origin + len(self.blob)
            )
            - (_OPCODES[0xF0][0] + 1)
        )
        self.assertEqual(extra, ap.WAITED_OP_EXTRA_CYCLES)

    def test_the_wire_maximum_wait_converts_to_a_literal_unit_count(self):
        # 255 C64 cycles is the largest wait a `0x30` pair can carry; at 5
        # cycles a unit that is 51 delay-loop iterations.
        self.assertEqual(ap._wait_units_for_cycles(255), 51)
        self.assertEqual(ap._wait_units_for_cycles(10), 2)
        self.assertEqual(ap._wait_units_for_cycles(0), 0)
        self.assertEqual(ap._wait_units_for_cycles(-1), 0)

    def test_the_unit_count_saturates_at_the_slots_one_wait_byte(self):
        # A recipe wait far above the spec's 255 still has to pack into the
        # slot's single wait byte.
        self.assertEqual(ap._wait_units_for_cycles(100_000), 255)

    def test_a_maximal_chip_frame_costs_a_literal_number_of_cycles(self):
        # MAX_OPS_PER_CHIP ops each carrying the wire maximum: 65 + 13 + 51x5
        # = 333 cycles an op, 9324 for the frame. That is over half a 60 Hz
        # NTSC frame (17045 cycles) for ONE chip — the arithmetic
        # FRAME_BUDGET_FRACTION exists for, and the number an under-counted
        # PER_OP_CYCLES would quietly shrink.
        #
        # The 9324 is the literal and the op count is not: this test and
        # CostModelProseTest below quote the same figure, and hardcoding `* 28`
        # here let them disagree — a changed MAX_OPS_PER_CHIP turned the prose
        # test red while this one stayed green at 9324, its comment now
        # describing a frame size that no longer existed. Both go red together.
        frame = [(0xD400, 0x11, _MAX_WIRE_WAIT_UNITS)] * ap.MAX_OPS_PER_CHIP
        self.assertEqual(ap.frame_cycle_cost(frame), 9324)

    def test_an_unwaited_frame_costs_the_op_count_alone(self):
        # No wait, so neither WAITED_OP_EXTRA_CYCLES nor the delay loop is
        # charged: MAX_OPS_PER_CHIP x 65.
        self.assertEqual(ap.frame_cycle_cost([(0xD400, 0x11, 0)] * ap.MAX_OPS_PER_CHIP), 1820)


class CostModelProseTest(unittest.TestCase):
    """The maximal-frame figure is quoted in prose, and prose does not run.

    `asid_player`'s `FRAME_BUDGET_FRACTION` comment and the architecture note
    both cite the cost of a maximal chip frame to argue the wait column is a
    real amplifier. Both said 8820 — a figure matching no constant the module
    ships — while `frame_cycle_cost` returned 9324, and the literal assertion
    above pinned only the code. A cited number nothing recomputes is the same
    defect as a comment claiming test coverage it does not have.
    """

    _SITES = (
        Path("c64cast/sid/asid_player.py"),
        Path("docs/architecture/sid.md"),
    )

    def test_both_prose_sites_cite_the_cost_the_model_computes(self):
        root = Path(__file__).resolve().parent.parent
        frame = [(0xD400, 0x11, _MAX_WIRE_WAIT_UNITS)] * ap.MAX_OPS_PER_CHIP
        cost = ap.frame_cycle_cost(frame)

        for site in self._SITES:
            text = (root / site).read_text(encoding="utf-8")
            # Every quotation, not the first. `sid.md` is long and already
            # restates the cost model in more than one place, so a second copy
            # of the figure added later would drift unguarded — which is the
            # exact failure this guard was written against. The subTest keeps a
            # failure on one site from hiding whether the other drifted too.
            quoted = re.findall(r"wait cost ([\d,]+)", text)
            self.assertTrue(quoted, f"{site} no longer quotes the figure")
            for n, raw in enumerate(quoted):
                with self.subTest(site=str(site), occurrence=n):
                    self.assertEqual(
                        int(raw.replace(",", "")),
                        cost,
                        f"{site} quotes a maximal-frame cost the model does not compute",
                    )


class FrameBudgetTest(unittest.TestCase):
    """A `0x30` supplies a wait per write straight off the wire, so a frame's
    *cost* — not just its op count — has to be bounded against the consume
    period. An overrunning frame does not queue politely: the CIA fires again
    before the handler returns, so the 6510 never leaves the ASID IRQ and the
    kernal tail (jiffy clock, SCNKEY) stops."""

    def _worst_case_frame(self, n_chips: int) -> list[tuple[int, int, int]]:
        """The fullest legal frame under the fullest legal recipe, concatenated
        across `n_chips` exactly as `_emit_buffered_frame` builds it."""
        regs = dict.fromkeys(range(0x19), 0x11)
        control_first = {0: 0x08, 1: 0x08, 2: 0x08}
        recipe = [(rid, 255) for rid in range(28)]
        ops: list[tuple[int, int, int]] = []
        for chip in range(n_chips):
            ops += ap.serialize_frame(regs, control_first, 0xD400 + chip * 0x20, recipe=recipe)
        return ops

    def test_a_maximal_recipe_frame_outruns_the_tick_it_has_to_run_in(self):
        # The premise the budget exists for: two chips of spec-legal maximum
        # waits ask for more than a whole 60 Hz NTSC frame of 6510 time.
        ops = self._worst_case_frame(2)
        self.assertEqual(len(ops), 2 * ap.MAX_OPS_PER_CHIP)
        self.assertGreater(ap.frame_cycle_cost(ops), CLOCK_NTSC / 60.0)

    def test_fitting_holds_the_frame_to_the_budget_and_keeps_every_write(self):
        ops = self._worst_case_frame(2)
        budget = int(CLOCK_NTSC / 60.0 * ap.FRAME_BUDGET_FRACTION)
        fitted = ap.fit_frame_to_budget(ops, budget)
        self.assertLessEqual(ap.frame_cycle_cost(fitted), budget)
        # Only the waits give: every register write still reaches the SID, in
        # the same order. Dropping ops would mangle the tune outright.
        self.assertEqual([(a, v) for a, v, _w in fitted], [(a, v) for a, v, _w in ops])

    def test_a_frame_already_inside_the_budget_is_untouched(self):
        ops = [(0xD400, 0x11, 4), (0xD404, 0x41, 0)]
        self.assertEqual(ap.fit_frame_to_budget(ops, 100_000), ops)

    def test_ops_that_alone_overrun_come_back_with_every_wait_zeroed(self):
        # A 16x multispeed leaves so little per tick that the op loop alone
        # exceeds it. Zero is as far as scaling can go; the writes still land.
        ops = [(0xD400 + i, 0x11, 51) for i in range(28)]
        fitted = ap.fit_frame_to_budget(ops, 100)
        self.assertEqual([w for *_, w in fitted], [0] * 28)
        self.assertEqual([(a, v) for a, v, _w in fitted], [(a, v) for a, v, _w in ops])

    def test_the_budget_follows_the_consume_rate_and_the_machine_clock(self):
        api, _ = _fake_backend()
        player = ap.AsidRingPlayer(api, system="NTSC", n_chips=1)
        for rate in (60.0, 960.0):
            player._rate = rate
            with self.subTest(rate=rate):
                self.assertAlmostEqual(
                    player.frame_cycle_budget(),
                    CLOCK_NTSC / rate * ap.FRAME_BUDGET_FRACTION,
                    delta=1.0,
                )


class PackSlotTest(unittest.TestCase):
    # No setUp resetting a shared throttle: `_pack` builds a fresh one per call
    # unless the test hands it one. The reset existed because the budget was
    # module state, i.e. per process rather than per stream.

    @staticmethod
    def _pack(ops, slot_size, truncation_log=None):
        return ap.pack_slot(
            ops, slot_size, truncation_log=truncation_log or ap.new_truncation_log()
        )

    def test_layout_and_padding(self):
        slot = self._pack([(0xD404, 0x41, 2), (0xD400, 0x34, 0)], 128)
        self.assertEqual(len(slot), 128)
        self.assertEqual(slot[0], 2)  # n_ops
        self.assertEqual(tuple(slot[1:5]), (0x04, 0xD4, 0x41, 0x02))  # op0
        self.assertEqual(tuple(slot[5:9]), (0x00, 0xD4, 0x34, 0x00))  # op1
        self.assertTrue(all(b == 0 for b in slot[9:]))  # zero-padded tail

    def test_hold_slot_is_all_zero(self):
        slot = ap.hold_slot(128)
        self.assertEqual(len(slot), 128)
        self.assertEqual(slot[0], 0)  # n_ops == 0 → hold tick

    def test_overfull_ops_truncated_loudly(self):
        # More ops than the slot can hold are dropped — and said out loud. The
        # ops past the cut belong to the later chips in a multi-SID slot, so a
        # silent truncation deleted a whole chip's frame.
        many = [(0xD400, 0, 0)] * 100
        with self.assertLogs("c64cast.sid.asid_player", "WARNING") as caught:
            slot = self._pack(many, 128)
        self.assertEqual(slot[0], (128 - 1) // ap.OP_BYTES)
        self.assertIn("later chips in this slot lose their writes", caught.output[0])

    def test_each_call_for_a_stream_budget_answers_with_a_new_one(self):
        # "Per stream" in one line, the same as asid.new_recipe_log's: a
        # factory answering with a shared instance is the module-level throttle
        # again, wearing a function's name.
        self.assertIsNot(ap.new_truncation_log(), ap.new_truncation_log())

    def test_one_streams_flood_does_not_spend_another_streams_budget(self):
        # The ensemble regression, on the player's side of it: one AsidScene per
        # system, each packing its own frames, and with a module-level throttle
        # the first system to truncate took the only report. Built through the
        # factory inside `frozen_throttles` so the factory stays in the path —
        # two throttles built directly would be distinct whatever production
        # does.
        many = [(0xD400, 0, 0)] * 100
        with frozen_throttles(ap):
            stream_a = ap.new_truncation_log()
            stream_b = ap.new_truncation_log()

        with self.assertLogs("c64cast.sid.asid_player", "DEBUG") as caught:
            for _ in range(960):
                self._pack(many, 128, stream_a)
            spent_by_a = len(caught.records)
            self._pack(many, 128, stream_b)

        self.assertEqual(spent_by_a, 1)
        self.assertEqual(len(caught.records), 2)
        self.assertEqual([r.levelname for r in caught.records], ["WARNING", "WARNING"])

    def test_a_permanent_truncation_reports_once_not_once_per_frame(self):
        # There is a reachable state in which this condition holds for the rest
        # of the scene, and it is evaluated once per ASID frame — 60 to 960 Hz
        # on the MIDI reader thread. One record per frame is the whole defect,
        # so the throttle must be *consulted* here, not merely defined.
        many = [(0xD400, 0, 0)] * 100
        # One throttle across all 960 frames — one stream, one budget — held by
        # the test rather than patched into the module.
        throttle = frozen_throttle(ap.log)
        with self.assertLogs("c64cast.sid.asid_player", "DEBUG") as caught:
            for _ in range(960):
                self.assertEqual(self._pack(many, 128, throttle)[0], (128 - 1) // ap.OP_BYTES)
        self.assertEqual(len(caught.records), 1)
        self.assertEqual(caught.records[0].levelname, "WARNING")


class MemoryMapTest(unittest.TestCase):
    """The $C000 memory map by literal address, not by symbol. The symbolic
    assertions elsewhere move with the constants, so relocating HANDLER_ADDR
    onto LANDING_BUF — where the REU pull overwrites the handler every tick —
    left the whole suite green."""

    def test_literal_addresses(self):
        self.assertEqual(ap.HANDLER_ADDR, 0xC000)
        self.assertEqual(ap.LANDING_BUF, 0xC400)
        self.assertEqual(ap.TRACKER_ADDR, 0xC800)
        self.assertEqual(ap.TICK_COUNTER_ADDR, 0xC803)
        self.assertEqual(ap.NOPS_COUNTER_ADDR, 0xC804)

    def test_handler_does_not_overlap_the_landing_buffer_or_tracker(self):
        # The 8-SID worst case is the biggest landing buffer and the longest
        # handler, so it is the case that has to fit.
        handler_end = ap.HANDLER_ADDR + len(ap.build_player(ap.slot_size_for_chips(8), 16))
        self.assertLessEqual(handler_end, ap.LANDING_BUF)
        self.assertLessEqual(ap.LANDING_BUF + ap.slot_size_for_chips(8), ap.TRACKER_ADDR)


class BuildPlayerTest(unittest.TestCase):
    def test_matches_the_golden_blob(self):
        self.assertEqual(ap.build_player(128, 1).hex(), _GOLDEN_PLAYER_128_1.hex())

    def test_rejects_a_tick_divider_the_immediate_cannot_hold(self):
        # It becomes an `LDA #N`; masking to 8 bits turned 333 into 77 and any
        # multiple of 256 into "chain once every 256 ticks".
        for bad in (0, 256, 333):
            with self.assertRaises(ValueError):
                ap.build_player(128, bad)

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

    def test_tick_divider_never_exceeds_the_immediate(self):
        # It is emitted as `LDA #N`. 20000 Hz used to return 333 (truncated to
        # 77) and any exact multiple of 256 to emit 0.
        for rate in (20000.0, 15360.0, 1e6):
            self.assertLessEqual(ap.tick_divider_for_rate(rate), 255)
            self.assertGreaterEqual(ap.tick_divider_for_rate(rate), 1)


class ClampFrameRateTest(unittest.TestCase):
    """One 0x31 must not be able to set an arbitrary consume rate: the CIA latch
    clamps the *latch*, not the rate, so an out-of-band request becomes the
    fastest timer the chip can run rather than an error."""

    def test_in_band_rates_pass_through(self):
        for rate in (50.0, 60.0, 960.0):
            self.assertEqual(ap.clamp_frame_rate(rate), rate)

    def test_a_frame_delta_of_one_microsecond_clamps_to_the_ceiling(self):
        with self.assertLogs("c64cast.sid.asid_player", "WARNING") as caught:
            clamped = ap.clamp_frame_rate(1_000_000.0)  # F0 2D 31 00 01 00 00 F7
        self.assertEqual(clamped, ap.MAX_FRAME_RATE_HZ)
        self.assertIn("outside the", caught.output[0])

    def test_below_the_band_clamps_to_the_floor(self):
        with self.assertLogs("c64cast.sid.asid_player", "WARNING"):
            self.assertEqual(ap.clamp_frame_rate(0.001), ap.MIN_FRAME_RATE_HZ)

    def test_a_malformed_rate_fails_slow(self):
        # NaN fails both comparisons; the safe direction is the floor.
        with self.assertLogs("c64cast.sid.asid_player", "WARNING"):
            self.assertEqual(ap.clamp_frame_rate(float("nan")), ap.MIN_FRAME_RATE_HZ)


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

    def test_write_slots_caps_the_burst(self):
        # A 256-slot prebuffer or catch-up run at the 8-SID slot size would
        # otherwise be a single quarter-megabyte transfer.
        p, fake = self._player(n_chips=8)
        slots = [bytes(p.slot_size)] * 64
        p._write_slots(0, slots)
        for _off, payload in fake.socket_dma.reuwrites:
            self.assertLessEqual(len(payload), ap._MAX_DMA_BURST_BYTES)
        self.assertEqual(
            sum(len(payload) for _off, payload in fake.socket_dma.reuwrites),
            len(slots) * p.slot_size,
        )

    def test_write_slots_keeps_one_stride_even_if_the_layout_moves_mid_call(self):
        # A payload list and the stride it was built for must never disagree
        # half way through a call: re-reading self.slot_size per burst put
        # 912-byte slots at the 128-byte stride, so the 6502 read n_ops from a
        # mid-op byte and executed the op stream shifted — STA anywhere in the
        # C64's 64K, from the value bytes of attacker-supplied SID writes.
        p, fake = self._player(n_chips=8)
        slot_size = p.slot_size
        per_burst = ap._MAX_DMA_BURST_BYTES // slot_size
        slots = [bytes(slot_size)] * (per_burst + 5)
        direct = fake.reu_write

        def shrink_then_write(offset, data):
            p.slot_size = ap.slot_size_for_chips(1)  # a reinit landing mid-burst
            direct(offset, data)

        fake.reu_write = shrink_then_write
        p._write_slots(0, slots)
        self.assertEqual(
            [off for off, _payload in fake.socket_dma.reuwrites],
            [ap.RING_BASE, ap.RING_BASE + per_burst * slot_size],
        )


class ArmGateTest(unittest.TestCase):
    """The lazy-arm prebuffer gate — "Symptom 1" in docs/caveats.md. Arming
    before real frames exist starts the read-head clock against an empty ring
    and every real frame lands in an already-consumed slot (heard as unbroken
    holds). Driven without start(), so no writer thread races the assertions."""

    def _unstarted(self, target: int):
        api, fake = _fake_backend()
        p = ap.AsidRingPlayer(api, system="NTSC", n_chips=1)
        p._prebuffer_target = target
        return p, fake

    def test_does_not_arm_below_the_prebuffer_target(self):
        p, fake = self._unstarted(4)
        for _ in range(3):
            p.push_frame(ap.hold_slot(p.slot_size))
        self.assertFalse(p._try_arm())
        self.assertFalse(p._armed)
        self.assertEqual(p._read_head(), 0)
        self.assertNotIn("0314", fake.regs)
        self.assertEqual(fake.socket_dma.reuwrites, [])

    def test_arms_and_swaps_the_vector_once_the_prebuffer_is_full(self):
        p, fake = self._unstarted(4)
        frames = [bytes([n + 1]) + bytes(p.slot_size - 1) for n in range(4)]
        for frame in frames:
            p.push_frame(frame)
        self.assertTrue(p._try_arm())
        self.assertTrue(p._armed)
        self.assertEqual(p._write_pos, 4)
        self.assertEqual(fake.regs["0314"], (ap.HANDLER_ADDR & 0xFF, (ap.HANDLER_ADDR >> 8) & 0xFF))
        # One contiguous transfer, not one per slot: the whole arm runs under
        # the lock set_frame_rate needs on the MIDI reader thread, and a 16x
        # stream pins the prebuffer at 256 slots.
        self.assertEqual(fake.socket_dma.reuwrites, [(ap.RING_BASE, b"".join(frames))])

    def test_does_not_arm_when_stale_slot_sizes_ate_the_prebuffer(self):
        # A chip-count reinit leaves stragglers packed at the old slot size in
        # flight from the reader thread; they satisfy qsize but not the ring.
        p, fake = self._unstarted(4)
        p.push_frame(ap.hold_slot(p.slot_size))
        for _ in range(3):
            p.push_frame(ap.hold_slot(ap.slot_size_for_chips(3)))
        with self.assertLogs("c64cast.sid.asid_player", "WARNING") as caught:
            self.assertFalse(p._try_arm())
        self.assertFalse(p._armed)
        self.assertNotIn("0314", fake.regs)
        self.assertIn("discarded 3 prebuffer slot(s)", caught.output[0])

    def test_does_not_arm_once_teardown_has_asked_the_writer_to_stop(self):
        # _try_arm does several blocking DMA calls before it hooks $0314, which
        # is exactly how it outlives _teardown_player's bounded join. If it
        # armed anyway, the C64 would run the ASID IRQ into the next scene
        # against a ring nobody feeds — and $C000 is where a later scene's
        # DAC/NMI handler lands.
        p, fake = self._unstarted(4)
        for _ in range(4):
            p.push_frame(ap.hold_slot(p.slot_size))
        p._writer.stop()  # the first thing _teardown_player does
        self.assertFalse(p._try_arm())
        self.assertFalse(p._armed)
        self.assertNotIn("0314", fake.regs)


class BringUpTeardownTest(unittest.TestCase):
    def _player(self, system="NTSC", **kw):
        api, fake = _fake_backend()
        return ap.AsidRingPlayer(api, system=system, n_chips=1, **kw), fake

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
        # The latch half is the one guard on a CHANGELOG-recorded regression:
        # writing PAL's $4025 back on an NTSC machine ran the jiffy clock ~3.8%
        # fast. Asserting the vector alone let a hardcoded 0x4025 stay green.
        from c64cast.hw.c64 import KERNAL, kernal_cia1_latch

        for system in ("NTSC", "PAL"):
            with self.subTest(system=system):
                p, api = self._player(system=system, prebuffer_seconds=0.0)
                p.push_frame(ap.hold_slot(p.slot_size))
                p.start(60.0)
                p.stop()
                self.assertEqual(
                    api.regs["0314"],
                    (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF),
                )
                self.assertEqual(
                    api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"],
                    _packed_latch(kernal_cia1_latch(system)),
                )
                self.assertFalse(p._armed)

    def test_stop_restores_the_kernal_latch_even_when_it_never_armed(self):
        # start() programs CIA #1 immediately; the $0314 swap waits for a real
        # frame. A sender controls that absolutely — one 0x31 and no 0x4E at
        # all — so restoring on _armed left Timer A at the wire's rate for every
        # scene after: at 960 Hz the machine burns a third of its cycles in
        # $EA31 and the jiffy clock runs 16x fast until a power cycle.
        from c64cast.hw.c64 import KERNAL, kernal_cia1_latch

        p, api = self._player()  # real prebuffer, empty queue → never arms
        p.start(60.0)
        p.set_frame_rate(960.0)
        self.assertFalse(p._armed)
        self.assertEqual(
            api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"],
            _packed_latch(ap.cia1_latch_for_rate(960.0, "NTSC")),
        )
        p.stop()
        self.assertEqual(
            api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"], _packed_latch(kernal_cia1_latch("NTSC"))
        )
        self.assertEqual(
            api.regs["0314"], (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        )

    def test_a_writer_that_outlives_the_join_cannot_arm_behind_teardown(self):
        # PollThread.stop() is documented to return with the worker still alive
        # after a timed-out join, and _try_arm blocks in DMA before it hooks
        # $0314. The abandoned writer used to finish and hook the vector AFTER
        # the scene had silenced the SID and moved on — leaving an ASID IRQ
        # handler that rewrites the whole REU control block at up to 960 Hz into
        # the next scene's audio pump.
        from c64cast.hw.c64 import KERNAL

        api, fake = _fake_backend()
        p = ap.AsidRingPlayer(api, system="NTSC", n_chips=1, prebuffer_seconds=0.0)
        p.start(60.0)  # empty queue → installed but not armed
        # Shorten the join rather than sleeping past the real 1 s one; the code
        # path (join times out with the worker still inside a DMA call) is the
        # same one, and BLOCK_S below keeps it blocked well past the timeout.
        join_s, block_s = 0.05, 0.25
        p._writer._join_timeout = join_s
        inside = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        direct = fake.reu_write

        def blocking_reu_write(offset, data):
            inside.set()
            release.wait(5.0)
            direct(offset, data)

        fake.reu_write = blocking_reu_write
        p.push_frame(ap.hold_slot(p.slot_size))  # the writer picks this up and blocks
        self.assertTrue(inside.wait(5.0), "the writer never reached the arm transfer")
        threading.Timer(block_s, release.set).start()
        # The timed-out join is the precondition, so assert it rather than
        # assuming it (and keep PollThread's warning out of the test output).
        with self.assertLogs("c64cast._pollthread", "WARNING") as caught:
            p.stop()  # the writer is still inside the arm transfer
        self.assertIn("did not stop", caught.output[0])
        kernal = (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        self.assertEqual(fake.regs["0314"], kernal)
        deadline = time.monotonic() + 5.0
        while p._writer.is_running() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(p._writer.is_running(), "the writer never exited")
        self.assertFalse(p._armed)
        self.assertEqual(fake.regs["0314"], kernal, "the abandoned writer hooked $0314")

    def test_start_refuses_to_install_over_a_writer_that_outlived_its_join(self):
        # PollThread.start() declines a duplicate and therefore never clears the
        # stop event, so the abandoned worker exits on its next check and
        # nothing spawns a replacement: the player installs, never arms, and the
        # scene is silent for its whole run — while the stranded worker keeps
        # REUWRITEing at the old slot size into a ring this install just
        # re-described.
        p, api = self._player(prebuffer_seconds=0.0)
        with mock.patch.object(p._writer, "is_running", return_value=True):
            with self.assertLogs("c64cast.sid.asid_player", "WARNING") as caught:
                p.start(60.0)
        self.assertIn("refusing to install", caught.output[0])
        self.assertFalse(p._installed)
        self.assertEqual(api.memories, {})
        self.assertEqual(api.mem_files, {})
        self.assertEqual(api.socket_dma.reuwrites, [])

    def test_reinit_refuses_to_move_the_layout_under_a_live_writer(self):
        # _write_slots derives its ring offsets from slot_size, so assigning a
        # new one while a writer is blocked mid-burst puts the rest of that
        # burst's old-sized payloads at the new stride. One 0x5F SysEx reaches
        # reinit, so the precondition is checked rather than assumed.
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            with mock.patch.object(p._writer, "is_running", return_value=True):
                with self.assertLogs("c64cast.sid.asid_player", "WARNING") as caught:
                    p.reinit(8)
            self.assertIn("refusing to re-init", caught.output[0])
            self.assertEqual(p.n_chips, 1)
            self.assertEqual(p.slot_size, ap.slot_size_for_chips(1))
        finally:
            p.stop()

    def test_reset_returns_a_stopped_player_to_a_fresh_layout(self):
        # Playlists reuse scene instances. Lap 2 starting on lap 1's chip count
        # is what lets a later remap SHRINK the ring (reinit's guard only
        # compares against the count it already holds), and lap 1's queued
        # frames would otherwise become lap 2's prebuffer.
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        p.reinit(8)
        p.stop()
        p.push_frame(ap.hold_slot(p.slot_size))
        p.reset()
        self.assertEqual(p.n_chips, 1)
        self.assertEqual(p.slot_size, ap.slot_size_for_chips(1))
        self.assertEqual(p._q.qsize(), 0)
        self.assertEqual(p._write_pos, 0)

    def test_take_slot_is_the_only_gate_and_it_drops_a_stale_size(self):
        # Two of the three slot consumers filtered and the third did not: the
        # blocking get in the writer's pad branch appended whatever it got. All
        # three go through _take_slot now, so the check cannot be forgotten at a
        # fourth site.
        p, _ = self._player()
        stale = bytes(ap.slot_size_for_chips(3))
        good = bytes([1]) + bytes(p.slot_size - 1)
        p.push_frame(stale)
        p.push_frame(good)
        self.assertEqual(p._take_slot(), good)
        self.assertEqual(p._stale_slots, 1)
        p.push_frame(stale)
        self.assertIsNone(p._take_slot(timeout=0.01))
        self.assertEqual(p._stale_slots, 2)

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

    def test_set_frame_rate_before_arming_retunes_everything(self):
        # "Symptom 2" in docs/caveats.md: a 0x31 almost always arrives at stream
        # start, before the prebuffer fills. Dropping it pre-arm makes the player
        # arm at the initial video-rate cadence and decimate the tune to it.
        p, api = self._player()  # real prebuffer, empty queue → never arms
        p.start(60.0)
        try:
            self.assertFalse(p._armed)
            before = p._prebuffer_target
            p.set_frame_rate(120.0)
            self.assertAlmostEqual(p._rate, 120.0, delta=1.0)
            self.assertEqual(p._divider, 2)
            self.assertGreater(p._prebuffer_target, before)
            # The CIA latch and the handler's baked-in divider both follow.
            self.assertEqual(
                api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"],
                _packed_latch(ap.cia1_latch_for_rate(120.0, "NTSC")),
            )
            self.assertEqual(
                api.mem_files[f"{ap.HANDLER_ADDR:04X}"],
                ap.build_player(p.slot_size, 2),
            )
        finally:
            p.stop()

    def test_a_hostile_speed_message_cannot_set_an_arbitrary_rate(self):
        # frame_delta_us = 1 → 1 MHz. Unclamped this became CIA latch 1, i.e.
        # _rate 511,364 Hz: an IRQ every 2 cycles on the C64 and a permanently
        # flooded DMA socket on the host.
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            with self.assertLogs("c64cast.sid.asid_player", "WARNING"):
                p.set_frame_rate(1_000_000.0)
            self.assertLessEqual(p._rate, ap.MAX_FRAME_RATE_HZ + 1.0)
            self.assertGreaterEqual(p._rate, ap.MIN_FRAME_RATE_HZ)
            self.assertLessEqual(p._divider, 255)
        finally:
            p.stop()

    def test_hold_padding_is_paced_not_link_limited(self):
        # A spec-legal 16x stream that then goes quiet leaves the lead negative
        # forever. Unpaced, the pad branch appended one hold per iteration with
        # no sleep and ran at the link's maximum rate indefinitely (measured 705
        # reu_write/s against the ~200/s the U64 DMA socket can carry, taken
        # from the render path that shares it). Batched + paced, holds cost
        # `rate / lead_panic` writes per second whatever the rate.
        # Needs a link slow enough that the read head genuinely outruns it —
        # that is the whole condition, and an instant fake link hides it.
        api, fake = _fake_backend()
        direct = fake.reu_write

        def slow_reu_write(offset, data):
            time.sleep(0.001)
            direct(offset, data)

        fake.reu_write = slow_reu_write
        p = ap.AsidRingPlayer(api, system="NTSC", n_chips=1, prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(960.0)  # F0 2D 31 1E F7 — a spec-legal 16x, then the host goes quiet
        try:
            baseline = len(fake.socket_dma.reuwrites)
            time.sleep(0.2)
            pad_writes = len(fake.socket_dma.reuwrites) - baseline
        finally:
            p.stop()
        self.assertGreater(p._underrun_pads, 0, "the pad branch never ran")
        # ~3 paced against this 1 ms link; unpaced it was ~200, i.e. flat out.
        self.assertLess(pad_writes, 30, f"{pad_writes} pad writes in 0.2 s")

    def test_reinit_keeps_the_writer_object_so_a_duplicate_is_refused(self):
        # PollThread's "already running" guard lives on the object: discarding
        # it let a writer still blocked in reu_write past the join timeout race
        # a freshly started one over self._write_pos and one REU ring.
        p, _ = self._player(prebuffer_seconds=0.0)
        p.push_frame(ap.hold_slot(p.slot_size))
        p.start(60.0)
        try:
            writer = p._writer
            p.reinit(3)
            self.assertIs(p._writer, writer)
        finally:
            p.stop()
        self.assertFalse(p._writer.is_running())

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
