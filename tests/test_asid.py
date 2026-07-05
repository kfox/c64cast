"""Unit tests for the pure ASID decoder (c64cast/asid.py).

The decoder has no mido / hardware dependencies, so these feed raw SysEx byte
sequences and assert the resulting AsidUpdate — register map (incl. MSB
reconstruction and the double control-register write), start/stop, character
display, speed (PAL/NTSC + multiplier + frame delta), and SID type. The scene's
use of the decoder is covered in test_asid_scene.py.
"""

from __future__ import annotations

import unittest

from c64cast import asid


def _reg_msg(values: dict[int, int]) -> tuple[int, ...]:
    """Build a 0x4E SysEx payload from {asid_register_id: 8-bit value}.

    Mirrors the packing in the spec: 4 mask bytes (bit per id), 4 MSB bytes
    (8th data bit per id), then the low 7 bits of each present register in
    ascending id order.
    """
    mask = [0, 0, 0, 0]
    msb = [0, 0, 0, 0]
    data: list[int] = []
    for reg_id in sorted(values):
        byte_idx, bit = divmod(reg_id, 7)
        mask[byte_idx] |= 1 << bit
        value = values[reg_id]
        if value & 0x80:
            msb[byte_idx] |= 1 << bit
        data.append(value & 0x7F)
    return (asid.ASID_MANUFACTURER_ID, asid.CMD_REG, *mask, *msb, *data)


def _ok(msg) -> asid.AsidUpdate:
    """decode() the message and assert it's a recognized ASID message (narrows
    the Optional for the type checker)."""
    u = asid.decode(msg)
    assert u is not None
    return u


class DecodeDispatchTest(unittest.TestCase):
    def test_non_asid_sysex_returns_none(self):
        # Wrong manufacturer id (0x7E = universal non-realtime).
        self.assertIsNone(asid.decode((0x7E, 0x00, 0x01)))

    def test_too_short_returns_none(self):
        self.assertIsNone(asid.decode((asid.ASID_MANUFACTURER_ID,)))

    def test_start_stop(self):
        self.assertIs(_ok((asid.ASID_MANUFACTURER_ID, asid.CMD_START)).playing, True)
        self.assertIs(_ok((asid.ASID_MANUFACTURER_ID, asid.CMD_STOP)).playing, False)

    def test_unsupported_commands_dropped(self):
        # Multi-SID (0x50-0x5F) is now honored (see MultiSidTest); only OPL-FM
        # and the timing recipe remain dropped.
        for cmd in (asid.CMD_TIMING, asid.CMD_OPL):
            u = _ok((asid.ASID_MANUFACTURER_ID, cmd, 0x00))
            self.assertTrue(u.dropped, f"command 0x{cmd:02X} should be dropped")
            self.assertFalse(u.regs)


class MultiSidTest(unittest.TestCase):
    def test_primary_reg_command_is_chip_zero(self):
        self.assertEqual(_ok(_reg_msg({0: 0x34})).chip_index, 0)

    def test_multi_sid_low_is_chip_one(self):
        # 0x50 carries SID2 = chip index 1, same packed format as 0x4E.
        payload = (asid.ASID_MANUFACTURER_ID, asid.CMD_MULTI_SID_LO, *_reg_msg({0: 0x34})[2:])
        u = _ok(payload)
        self.assertEqual(u.chip_index, 1)
        self.assertEqual(u.regs, {0x00: 0x34})

    def test_multi_sid_indices_span_the_range(self):
        # 0x50..0x5F → chips 1..16.
        for cmd in range(asid.CMD_MULTI_SID_LO, asid.CMD_MULTI_SID_HI + 1):
            payload = (asid.ASID_MANUFACTURER_ID, cmd, *_reg_msg({1: 0x12})[2:])
            u = _ok(payload)
            self.assertEqual(u.chip_index, cmd - asid.CMD_MULTI_SID_LO + 1)
            self.assertEqual(u.regs, {0x01: 0x12})

    def test_multi_sid_hard_restart_still_decodes(self):
        # The double-control-write hard restart works per chip.
        payload = (
            asid.ASID_MANUFACTURER_ID,
            asid.CMD_MULTI_SID_LO,
            *_reg_msg({22: 0x08, 25: 0x41})[2:],
        )
        u = _ok(payload)
        self.assertEqual(u.chip_index, 1)
        self.assertEqual(u.regs[0x04], 0x41)
        self.assertEqual(u.control_first, {0: 0x08})


class RegisterDataTest(unittest.TestCase):
    def test_low_bits_and_offsets(self):
        # ids 0,1 → SID offsets 0x00, 0x01 (voice 1 freq lo/hi).
        u = _ok(_reg_msg({0: 0x34, 1: 0x12}))
        self.assertEqual(u.regs, {0x00: 0x34, 0x01: 0x12})
        self.assertFalse(u.control_first)

    def test_msb_reconstruction(self):
        # 0xFF needs the 8th bit from the MSB byte (low 7 bits = 0x7F).
        u = _ok(_reg_msg({0: 0xFF}))
        self.assertEqual(u.regs[0x00], 0xFF)

    def test_filter_and_volume_offsets(self):
        # ids 21..24 → $D415..$D418 (offsets 0x15..0x18).
        u = _ok(_reg_msg({18: 0xAA, 19: 0x0B, 20: 0xF7, 21: 0x1F}))
        self.assertEqual(u.regs[0x15], 0xAA)  # FC_LO
        self.assertEqual(u.regs[0x16], 0x0B)  # FC_HI
        self.assertEqual(u.regs[0x17], 0xF7)  # RES_FILT
        self.assertEqual(u.regs[0x18], 0x1F)  # MODE_VOL

    def test_control_first_write_maps_to_control_offset(self):
        # id 22 = voice-1 control (first write) → offset 0x04.
        u = _ok(_reg_msg({22: 0x41}))
        self.assertEqual(u.regs[0x04], 0x41)
        self.assertFalse(u.control_first)  # no second write → not a hard restart

    def test_hard_restart_double_control_write(self):
        # Voice 1: first control write (gate off, id 22) then a differing
        # second write (gate on + waveform, id 25). Final block value = second;
        # the first surfaces in control_first for the two-phase emit.
        u = _ok(_reg_msg({22: 0x08, 25: 0x41}))
        self.assertEqual(u.regs[0x04], 0x41)
        self.assertEqual(u.control_first, {0: 0x08})

    def test_equal_double_control_write_is_not_hard_restart(self):
        u = _ok(_reg_msg({22: 0x41, 25: 0x41}))
        self.assertEqual(u.regs[0x04], 0x41)
        self.assertFalse(u.control_first)

    def test_malformed_short_payload(self):
        # Fewer than 8 bytes (no room for mask+msb) → empty, not a crash.
        u = _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_REG, 0x01, 0x00))
        self.assertFalse(u.regs)

    def test_truncated_register_data_stops_cleanly(self):
        # Mask claims two registers but only one data byte is present.
        payload = (asid.ASID_MANUFACTURER_ID, asid.CMD_REG, 0x03, 0, 0, 0, 0, 0, 0, 0, 0x11)
        u = _ok(payload)
        self.assertEqual(u.regs, {0x00: 0x11})


class OtherCommandsTest(unittest.TestCase):
    def test_character_display(self):
        text = "HELLO"
        payload = (asid.ASID_MANUFACTURER_ID, asid.CMD_CHARS, *[ord(c) for c in text])
        self.assertEqual(_ok(payload).text, text)

    def test_character_display_sanitizes_nonprintable(self):
        payload = (asid.ASID_MANUFACTURER_ID, asid.CMD_CHARS, ord("A"), 0x00, ord("B"))
        self.assertEqual(_ok(payload).text, "A B")

    def test_speed_ntsc_multiplier_and_frame_delta(self):
        # data0 = 0b0000_0101 → bit0=1 (NTSC), bits1-4 = 2 → multiplier 3.
        # frame delta 20000 µs packed across 7-bit fields.
        u = _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x05, 32, 28, 1))
        self.assertEqual(u.system, "NTSC")
        self.assertEqual(u.speed_multiplier, 3)
        self.assertEqual(u.frame_delta_us, 20000)

    def test_speed_pal(self):
        u = _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x00))
        self.assertEqual(u.system, "PAL")
        self.assertEqual(u.speed_multiplier, 1)

    def test_sid_type_primary(self):
        u = _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_SID_TYPE, 0, 1))
        self.assertEqual(u.chip_type, "8580")
        self.assertEqual(u.chip_index, 0)
        self.assertEqual(
            _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_SID_TYPE, 0, 0)).chip_type, "6581"
        )

    def test_sid_type_secondary_chip_carries_index(self):
        # Chip index 1 (a second SID) reports its own type against chip_index 1.
        u = _ok((asid.ASID_MANUFACTURER_ID, asid.CMD_SID_TYPE, 1, 1))
        self.assertEqual(u.chip_index, 1)
        self.assertEqual(u.chip_type, "8580")


if __name__ == "__main__":
    unittest.main()
