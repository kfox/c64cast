"""Direct contract tests for c64cast.hw.c64's derived-timing helpers
(cpu_clock, frame_rate, kernal_cia1_latch, cia1_latch_for_rate,
actual_rate_for_latch, nmi_rate_safety). These are the tree's single source
of truth for every clock-derived constant (audio ring rates, CIA latches,
NMI safety bands) — this file pins the system-string handling that changed
in the adverse-review pass, not a full re-derivation of the hardware math
(each consumer's own test suite exercises that against real numbers)."""

from __future__ import annotations

import unittest

from c64cast.hw.c64 import (
    actual_rate_for_latch,
    cia1_latch_for_rate,
    cpu_clock,
    frame_rate,
    kernal_cia1_latch,
)


class SystemStringHandlingTest(unittest.TestCase):
    """cpu_clock/frame_rate/kernal_cia1_latch used to fall through to PAL for
    any string that wasn't exactly "NTSC" — including "auto" (a value
    config.SYSTEM_CHOICES explicitly allows) and any typo or trailing
    whitespace — with no diagnostic. They now accept only NTSC/PAL
    (case-insensitive, whitespace-tolerant) and raise otherwise."""

    def test_case_and_whitespace_are_tolerated(self):
        for spelling in ("NTSC", "ntsc", "Ntsc", " NTSC ", "\tntsc\n"):
            with self.subTest(spelling=spelling):
                self.assertEqual(cpu_clock(spelling), cpu_clock("NTSC"))
        for spelling in ("PAL", "pal", "Pal", " PAL "):
            with self.subTest(spelling=spelling):
                self.assertEqual(cpu_clock(spelling), cpu_clock("PAL"))

    def test_unrecognized_strings_raise(self):
        for bogus in ("auto", "AUTO", "ntscc", "pal ntsc", "", "NTSC-50"):
            with self.subTest(bogus=bogus):
                with self.assertRaises(ValueError):
                    cpu_clock(bogus)
                with self.assertRaises(ValueError):
                    frame_rate(bogus)
                with self.assertRaises(ValueError):
                    kernal_cia1_latch(bogus)

    def test_ntsc_and_pal_give_different_answers(self):
        self.assertNotEqual(cpu_clock("NTSC"), cpu_clock("PAL"))
        self.assertNotEqual(frame_rate("NTSC"), frame_rate("PAL"))
        self.assertNotEqual(kernal_cia1_latch("NTSC"), kernal_cia1_latch("PAL"))


class Cia1LatchForRateTest(unittest.TestCase):
    def test_round_trips_a_mid_range_rate(self):
        latch = cia1_latch_for_rate(12000.0, "NTSC")
        self.assertAlmostEqual(actual_rate_for_latch(latch, "NTSC"), 12000.0, delta=50.0)

    def test_rejects_a_non_positive_rate(self):
        with self.assertRaises(ValueError):
            cia1_latch_for_rate(0, "NTSC")
        with self.assertRaises(ValueError):
            cia1_latch_for_rate(-1.0, "NTSC")

    def test_clamps_a_very_low_rate_to_the_16_bit_ceiling(self):
        # Documented in the docstring as a clamp, not an error — no current
        # caller requests a sub-16 Hz consume rate.
        self.assertEqual(cia1_latch_for_rate(1.0, "NTSC"), 0xFFFF)

    def test_never_returns_a_latch_below_one(self):
        self.assertGreaterEqual(cia1_latch_for_rate(1e9, "NTSC"), 1)


class ActualRateForLatchTest(unittest.TestCase):
    def test_rejects_a_negative_latch(self):
        # latch == -1 would otherwise divide by zero; latch < -1 would give a
        # negative "rate" — both fail with a message naming the problem now.
        with self.assertRaises(ValueError):
            actual_rate_for_latch(-1, "NTSC")

    def test_zero_latch_is_the_fastest_possible_rate(self):
        self.assertEqual(actual_rate_for_latch(0, "NTSC"), cpu_clock("NTSC"))


if __name__ == "__main__":
    unittest.main()
