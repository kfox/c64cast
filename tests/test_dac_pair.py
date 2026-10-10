"""The two-SID $D418 DAC (c64cast.audio.dac_pair): the pair NMI routine run
on py65, the fold from two measured ladders to its tables, and the parse of
[audio].dac_second_sid."""

from __future__ import annotations

import unittest

import numpy as np
from _fakes import IRQ_ENTRY_A, RTI_RETURN_ADDR, run_irq_handler, written_addresses

from c64cast.audio import dac_pair as dp
from c64cast.audio.audio_handlers import (
    NMI_ROUTINE_ADDR,
    READ_PTR_HI_ADDR,
    READ_PTR_LO_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
)
from c64cast.hw.c64 import CIA2


def _tables() -> dict[int, bytes]:
    """Distinct, recognizable bytes at every index of both tables."""
    coarse = bytes((i * 7 + 3) & 0xFF for i in range(256))
    fine = bytes(i & 0x0F for i in range(256))
    return {dp.COARSE_TABLE_ADDR: coarse, dp.FINE_TABLE_ADDR: fine}


class PairNmiRoutineTest(unittest.TestCase):
    """The routine runs once per sample; a branch that misses its PLA, a
    clobbered X/Y, or a store to the wrong chip is a crash or garbage on
    hardware. Executed from an interrupt frame until its RTI."""

    FINE = 0xD420

    def _run(self, r: int, index: int, fine: int = FINE):
        seed = {READ_PTR_LO_ADDR: r & 0xFF, READ_PTR_HI_ADDR: r >> 8, r: index}
        return run_irq_handler(
            dp.pair_nmi_routine(fine),
            addr=NMI_ROUTINE_ADDR,
            seed=seed,
            images=_tables(),
            rti=True,
        )

    def _r(self, run) -> int:
        ram = run.memory.ram
        return ram[READ_PTR_LO_ADDR] | (ram[READ_PTR_HI_ADDR] << 8)

    def _assert_clean_return(self, run, fine: int = FINE) -> None:
        self.assertEqual(run.exit_pc, RTI_RETURN_ADDR)
        self.assertEqual(run.mpu.sp, 0xFF, "PHA/PLA and the RTI must balance the stack")
        self.assertEqual(run.mpu.a, IRQ_ENTRY_A, "A must be restored")
        assert run.memory.access is not None
        self.assertTrue(run.memory.access[CIA2.ICR], "the NMI must be acked at $DD0D")
        operands = {NMI_ROUTINE_ADDR + 0x0E, NMI_ROUTINE_ADDR + 0x14}
        self.assertLessEqual(
            written_addresses(run),
            {0xD418, fine + 0x18, READ_PTR_LO_ADDR, READ_PTR_HI_ADDR, 0x01FC, *operands},
            "the routine stores the two samples, its own pointer and operands, and its PHA",
        )

    def test_looks_the_index_up_in_both_tables_and_writes_both_chips(self):
        tables = _tables()
        for index in (0x00, 0x80, 0xFF, 0x37):
            with self.subTest(index=index):
                run = self._run(RING_BUFFER_ADDR + 0x10, index)
                ram = run.memory.ram
                self.assertEqual(ram[0xD418], tables[dp.COARSE_TABLE_ADDR][index])
                self.assertEqual(ram[self.FINE + 0x18], tables[dp.FINE_TABLE_ADDR][index])
                self._assert_clean_return(run)

    def test_the_fine_chip_address_is_the_one_asked_for(self):
        for fine in (0xD420, 0xD500, 0xDE00):
            with self.subTest(fine=f"${fine:04X}"):
                run = self._run(RING_BUFFER_ADDR, 0x42, fine)
                self.assertEqual(run.memory.ram[fine + 0x18], 0x42 & 0x0F)
                self._assert_clean_return(run, fine)

    def test_advances_r_across_a_page_and_wraps_at_the_ring_end(self):
        run = self._run(RING_BUFFER_ADDR + 0x1FF, 1)
        self.assertEqual(self._r(run), RING_BUFFER_ADDR + 0x200)
        self._assert_clean_return(run)
        run = self._run(RING_BUFFER_END - 1, 2)
        self.assertEqual(self._r(run), RING_BUFFER_ADDR)
        self._assert_clean_return(run)

    def test_refuses_an_address_no_second_sid_can_sit_at(self):
        for bad in (0xD400, 0xD410, 0xDF00, 0xD800):
            with self.subTest(bad=f"${bad:04X}"), self.assertRaises(ValueError):
                dp.pair_nmi_routine(bad)


class ParseSecondSidTest(unittest.TestCase):
    def test_off_is_none(self):
        self.assertIsNone(dp.parse_second_sid("off"))
        self.assertIsNone(dp.parse_second_sid(" OFF "))

    def test_accepts_the_spellings_of_a_base(self):
        for text in ("$D420", "$d420", "D420", "0xD420"):
            with self.subTest(text=text):
                self.assertEqual(dp.parse_second_sid(text), 0xD420)

    def test_refuses_what_is_not_a_second_sid_base(self):
        for text in ("$D400", "$D430", "$DF20", "on", "", "$E000"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                dp.parse_second_sid(text)


class FoldPairTableTest(unittest.TestCase):
    def _coarse(self) -> np.ndarray:
        # A 16-level volume ladder per upper nibble with uneven gaps, like a
        # measured Mahoney ladder: plenty of holes for a fine chip to fill.
        return np.array([(c & 0x0F) * (1.0 + 0.4 * (c >> 4) / 15) - 3.0 for c in range(256)])

    def test_the_pair_beats_the_coarse_chip_alone(self):
        coarse = self._coarse()
        fine = np.arange(16) * (1.0 / 16)
        ct, ft, metrics = dp.fold_pair_table(coarse, fine)
        self.assertEqual((len(ct), len(ft)), (256, 256))
        self.assertTrue(all(b in dp.FINE_CODES for b in ft))
        self.assertGreater(metrics["ladder_bits"], metrics["single_chip_ladder_bits"] + 1)

    def test_each_index_plays_the_nearest_sum(self):
        coarse, fine = self._coarse(), np.arange(16) * 0.05
        ct, ft, _ = dp.fold_pair_table(coarse, fine)
        targets = np.linspace(coarse.min(), coarse.max(), 256)
        sums = coarse[:, None] + fine[None, :]
        for i in (0, 77, 128, 255):
            best = float(np.min(np.abs(sums - targets[i])))
            self.assertAlmostEqual(abs(coarse[ct[i]] + fine[ft[i]] - targets[i]), best)

    def test_a_silent_fine_chip_reduces_to_the_one_chip_ladder(self):
        coarse = self._coarse()
        _, ft, metrics = dp.fold_pair_table(coarse, np.zeros(16))
        self.assertEqual(metrics["ladder_bits"], metrics["single_chip_ladder_bits"])
        self.assertEqual(set(ft), {0})

    def test_refuses_the_wrong_shapes(self):
        with self.assertRaises(ValueError):
            dp.fold_pair_table(np.zeros(255), np.zeros(16))
        with self.assertRaises(ValueError):
            dp.fold_pair_table(np.zeros(256), np.zeros(15))


class SidPlayerKeepsClearTest(unittest.TestCase):
    """A SID scene before a two-SID DAC scene must not leave its player where
    the DAC's tables go, so the relocator keeps the tables' range clear."""

    def _parsed(self):
        from c64cast.hw.api import ParsedPsid

        return ParsedPsid(
            load_addr=0x1000,
            init_addr=0x1000,
            play_addr=0x1003,
            num_songs=1,
            start_song=1,
            song_to_play=1,
            payload=bytes(0x10),
        )

    def test_the_relocator_never_lands_on_the_tables(self):
        from c64cast.hw.api import _find_free_layout

        # The tables' range is the largest hole on offer; a smaller one must win.
        avoid = bytearray(b"\x01" * 65536)
        avoid[dp.COARSE_TABLE_ADDR : dp.FINE_TABLE_ADDR + 256] = bytes(512)
        avoid[0x0900 : 0x0900 + 200] = bytes(200)
        self.assertEqual(_find_free_layout(self._parsed(), avoid).player_base, 0x0900)

    def test_the_default_layout_check_refuses_the_tables(self):
        from c64cast.hw.api import _layout_fits, _PlayerLayout

        on_tables = _PlayerLayout(player_base=dp.FINE_TABLE_ADDR, stub_base=0xCF50)
        self.assertFalse(_layout_fits(on_tables, self._parsed(), bytearray(65536)))


class DacPairTest(unittest.TestCase):
    def test_refuses_a_fine_code_with_filter_bits(self):
        with self.assertRaises(ValueError):
            dp.DacPair(0xD420, bytes(256), bytes([0x10]) * 256)

    def test_refuses_short_tables(self):
        with self.assertRaises(ValueError):
            dp.DacPair(0xD420, bytes(255), bytes(256))


if __name__ == "__main__":
    unittest.main()
