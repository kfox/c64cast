"""AudioStreamer playing a two-SID DAC pair (c64cast.audio.dac_pair): what
bring-up uploads, what the encoder emits, and what teardown silences."""

from __future__ import annotations

import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI, new_streamer

from c64cast.audio import dac_pair as dp
from c64cast.audio.audio_handlers import NMI_ROUTINE, NMI_ROUTINE_ADDR
from c64cast.audio.dac_curves import NEUTRAL_INDEX

FINE = 0xD420


def _pair() -> dp.DacPair:
    return dp.DacPair(
        fine_base=FINE,
        coarse_table=bytes((i * 3) & 0xFF for i in range(256)),
        fine_table=bytes(i & 0x0F for i in range(256)),
    )


class PairBringUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = new_streamer(dac_curve="calibrated-pair", dac_pair=_pair())
        self.api = cast(FakeAPI, self.s.api)
        self.s._upload_nmi_and_buffers()

    def test_uploads_the_pair_routine_and_both_tables(self):
        files = self.api.mem_files
        self.assertEqual(files[f"{NMI_ROUTINE_ADDR:04X}"], dp.pair_nmi_routine(FINE))
        self.assertEqual(files[f"{dp.COARSE_TABLE_ADDR:04X}"], _pair().coarse_table)
        self.assertEqual(files[f"{dp.FINE_TABLE_ADDR:04X}"], _pair().fine_table)

    def test_parks_both_chips(self):
        # The Mahoney env's last write per chip routes voices 1+2 into its filter.
        self.assertIn("D417", self.api.memories)
        self.assertIn(f"{FINE + 0x17:04X}", self.api.memories)
        self.assertEqual(self.api.regs[f"{FINE + 0x15:04X}"], (0xFF, 0xFF))

    def test_the_encoder_emits_indices(self):
        assert self.s.dac_curve is not None
        np.testing.assert_array_equal(self.s.dac_curve, np.arange(256))
        self.assertEqual(self.s._neutral_byte, NEUTRAL_INDEX)

    def test_teardown_silences_the_fine_chip_too(self):
        names = [name for name, _ in self.s._hardware_teardown_steps()]
        self.assertIn("second SID volume mute", names)
        for name, step in self.s._hardware_teardown_steps():
            if name.startswith("second SID"):
                step()
        self.assertEqual(self.api.memories[f"{FINE + 0x18:04X}"], "00")
        for v in range(3):
            self.assertEqual(self.api.memories[f"{FINE + v * 7 + 4:04X}"], "40")


class OneChipUnchangedTest(unittest.TestCase):
    def test_without_a_pair_the_one_chip_routine_and_no_tables_go_up(self):
        s = new_streamer(dac_curve="mahoney_ultisid")
        api = cast(FakeAPI, s.api)
        s._upload_nmi_and_buffers()
        self.assertEqual(api.mem_files[f"{NMI_ROUTINE_ADDR:04X}"], NMI_ROUTINE)
        self.assertNotIn(f"{dp.COARSE_TABLE_ADDR:04X}", api.mem_files)
        self.assertNotIn(f"{FINE + 0x17:04X}", api.memories)
        names = [name for name, _ in s._hardware_teardown_steps()]
        self.assertFalse([n for n in names if n.startswith("second SID")])


if __name__ == "__main__":
    unittest.main()
