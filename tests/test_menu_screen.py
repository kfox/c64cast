"""Tests for the decoder of GET /v1/machine:menu_screen. The fixture is a
capture from an Ultimate 64-II on firmware 3.15a with the menu open at its
home screen; the HDMI frame captured at the same moment showed the same text."""

from __future__ import annotations

import unittest
from pathlib import Path

from c64cast.hw.menu_screen import CELLS, PAYLOAD_BYTES, decode_menu_screen

_FIXTURE = Path(__file__).parent / "fixtures" / "menu_screen_u64ii_315a.bin"


class DecodeCapturedMenuTest(unittest.TestCase):
    def setUp(self):
        self.screen = decode_menu_screen(_FIXTURE.read_bytes())

    def test_text_is_the_menu_font_read_as_ascii(self):
        lines = self.screen.text().splitlines()
        self.assertEqual(lines[0], "  *** Ultimate 64-II (V1.50) 3.15a ***")
        self.assertEqual(lines[1], "─" * 40)
        self.assertEqual(lines[2], "SD      SD Card                No media")
        self.assertEqual(lines[6], "Net0    IP: 192.168.2.64       Link Up")
        self.assertEqual(lines[24], "/                              ─F3=HELP─")

    def test_dimensions_and_color_plane(self):
        self.assertEqual(len(self.screen.lines), 25)
        self.assertTrue(all(len(line) == 40 for line in self.screen.lines))
        self.assertEqual(len(self.screen.colors), CELLS)
        # The highlighted row is light green (13) on black; the rest is white or gray.
        self.assertEqual(set(self.screen.colors[80:120]), {13})

    def test_no_cell_is_reversed_on_the_home_screen(self):
        self.assertFalse(any(any(row) for row in self.screen.reverse))


class DecodeSyntheticTest(unittest.TestCase):
    def test_bit_7_is_reverse_video_not_part_of_the_character(self):
        payload = bytearray(b" " * CELLS + b"\x01" * CELLS)
        payload[0:2] = bytes([ord("A") | 0x80, ord("A")])
        screen = decode_menu_screen(bytes(payload))
        self.assertEqual(screen.lines[0][:2], "AA")
        self.assertEqual(screen.reverse[0][:2], (True, False))

    def test_unknown_control_code_is_a_placeholder(self):
        payload = bytes([0x08]) + b" " * (PAYLOAD_BYTES - 1)
        self.assertEqual(decode_menu_screen(payload).lines[0][0], "?")

    def test_wrong_size_is_refused(self):
        for size in (0, PAYLOAD_BYTES - 1, PAYLOAD_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode_menu_screen(bytes(size))


if __name__ == "__main__":
    unittest.main()
