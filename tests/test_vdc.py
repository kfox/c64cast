"""Offline contract tests for c64cast.hw.vdc — the C128 VDC support core.

No hardware: a ``FakeVdc`` emulates the ``$D600``/``$D601`` porthole (register
select, R31 auto-incrementing VRAM data, 16 KiB address aliasing, block
fill/copy, version + ready status), and the packer/simulator/CRT helpers are
pure. This pins the probe algorithms and the bitmap conversion so hardware time
is spent on hardware questions, not on debugging the byte math.
"""

from __future__ import annotations

import unittest

import numpy as np

from c64cast.hw import vdc


class FakeVdc:
    """A minimal VDC behind the porthole. Construct with ``ram_kib`` (16 or 64)
    and ``version`` (0/1/2); pass ``.write`` / ``.read`` to ``VdcPorthole``."""

    def __init__(self, ram_kib: int = 64, version: int = 2) -> None:
        self.regs = [0] * 38
        self.ram = bytearray(65536)
        self._mask = 0x3FFF if ram_kib == 16 else 0xFFFF
        self._version = version
        self._selected = 0

    # -- address aliasing on 16 KiB parts --
    def _addr(self) -> int:
        return ((self.regs[vdc.R.UPDATE_HI] << 8) | self.regs[vdc.R.UPDATE_LO]) & self._mask

    def _bump_addr(self) -> None:
        a = ((self.regs[vdc.R.UPDATE_HI] << 8) | self.regs[vdc.R.UPDATE_LO]) + 1
        self.regs[vdc.R.UPDATE_HI], self.regs[vdc.R.UPDATE_LO] = (a >> 8) & 0xFF, a & 0xFF

    def _run_block(self, count: int) -> None:
        copy = bool(self.regs[vdc.R.V_SCROLL_CTRL] & vdc.V_SCROLL_COPY_BIT)
        for _ in range(count):
            dst = self._addr()
            if copy:
                src = (
                    (self.regs[vdc.R.BLOCK_COPY_SRC_HI] << 8) | self.regs[vdc.R.BLOCK_COPY_SRC_LO]
                ) & self._mask
                self.ram[dst] = self.ram[src]
                s = src + 1
                self.regs[vdc.R.BLOCK_COPY_SRC_HI], self.regs[vdc.R.BLOCK_COPY_SRC_LO] = (
                    (s >> 8) & 0xFF,
                    s & 0xFF,
                )
            else:
                self.ram[dst] = self.regs[vdc.R.DATA]
            self._bump_addr()

    # -- the porthole --
    def write(self, addr: int, data: bytes) -> None:
        for b in data:
            if addr == vdc.D600_ADDR_STATUS:
                self._selected = b & 0x3F
            elif addr == vdc.D601_DATA:
                self.regs[self._selected] = b
                if self._selected == vdc.R.DATA:
                    self.ram[self._addr()] = b
                    self._bump_addr()
                elif self._selected == vdc.R.WORD_COUNT:
                    self._run_block(b)

    def read(self, addr: int, n: int) -> bytes:
        out = bytearray()
        for _ in range(n):
            if addr == vdc.D600_ADDR_STATUS:
                out.append(vdc.STATUS_READY | (self._version & 0x07))
            elif addr == vdc.D601_DATA and self._selected == vdc.R.DATA:
                out.append(self.ram[self._addr()])
                self._bump_addr()
            else:
                out.append(self.regs[self._selected])
        return bytes(out)


def porthole(fake: FakeVdc) -> vdc.VdcPorthole:
    return vdc.VdcPorthole(fake.write, fake.read, block_settle_s=0)


class ProbeTest(unittest.TestCase):
    def test_version_from_status_bits(self):
        for v, name in vdc.VDC_VERSIONS.items():
            self.assertEqual(vdc.probe_version(porthole(FakeVdc(version=v))), name)

    def test_present_true_for_a_real_vdc_and_restores_r18_r19(self):
        fake = FakeVdc()
        fake.regs[vdc.R.UPDATE_HI], fake.regs[vdc.R.UPDATE_LO] = 0x12, 0x34
        self.assertTrue(vdc.probe_present(porthole(fake)))
        self.assertEqual((fake.regs[vdc.R.UPDATE_HI], fake.regs[vdc.R.UPDATE_LO]), (0x12, 0x34))

    def test_present_false_on_open_bus(self):
        # A plain C64: $D600/$D601 float, reads come back 0xFF, writes vanish.
        port = vdc.VdcPorthole(lambda *_: None, lambda _, n: b"\xff" * n)
        self.assertFalse(vdc.probe_present(port))

    def test_ram_size_16k_and_64k(self):
        self.assertEqual(vdc.probe_ram_size_kib(porthole(FakeVdc(ram_kib=16))), 16)
        self.assertEqual(vdc.probe_ram_size_kib(porthole(FakeVdc(ram_kib=64))), 64)

    def test_ram_size_probe_restores_the_byte_at_0x3fff(self):
        fake = FakeVdc(ram_kib=64)
        fake.ram[0x3FFF] = 0x5A
        vdc.probe_ram_size_kib(porthole(fake))
        self.assertEqual(fake.ram[0x3FFF], 0x5A)


class PortholeRamTest(unittest.TestCase):
    def test_write_then_read_ram_round_trips(self):
        fake = FakeVdc()
        port = porthole(fake)
        port.write_ram(0x1000, bytes(range(32)))
        self.assertEqual(port.read_ram(0x1000, 32), bytes(range(32)))

    def test_block_fill_uses_hardware_and_is_cheap(self):
        fake = FakeVdc()
        calls = 0
        real_write = fake.write

        def counting_write(a, d):
            nonlocal calls
            calls += 1
            real_write(a, d)

        vdc.VdcPorthole(counting_write, fake.read, block_settle_s=0).block_fill(0x0000, 0xAA, 16000)
        self.assertEqual(fake.ram[:16000], b"\xaa" * 16000)
        self.assertLess(calls, 200)  # ~1 write per 256 bytes, not per byte

    def test_word_count_is_chunked_to_255_and_never_zero(self):
        fake = FakeVdc()
        seen: list[int] = []
        selected = 0

        def spy(addr, data):
            nonlocal selected
            for b in data:
                if addr == vdc.D600_ADDR_STATUS:
                    selected = b & 0x3F
                elif addr == vdc.D601_DATA and selected == vdc.R.WORD_COUNT:
                    seen.append(b)
            fake.write(addr, data)

        vdc.VdcPorthole(spy, fake.read, block_settle_s=0).block_fill(0x0000, 0x11, 1000)
        self.assertEqual(sum(seen), 999)  # the R31 write placed the first byte
        self.assertTrue(all(0 < c <= 255 for c in seen), seen)
        self.assertEqual(fake.ram[:1000], b"\x11" * 1000)
        self.assertEqual(fake.ram[1000], 0)  # and not one byte further

    def test_block_copy_spans_more_than_one_word_count_write(self):
        fake = FakeVdc()
        want = bytes((i * 3) & 0xFF for i in range(600))
        fake.ram[0:600] = want
        porthole(fake).block_copy(0x0000, 0x4000, 600)
        self.assertEqual(fake.ram[0x4000 : 0x4000 + 600], want)
        self.assertEqual(fake.ram[0x4000 + 600], 0)

    def test_block_copy_moves_a_span_within_vram(self):
        fake = FakeVdc()
        fake.ram[0:100] = bytes(range(100))
        porthole(fake).block_copy(0x0000, 0x4000, 100)
        self.assertEqual(fake.ram[0x4000 : 0x4000 + 100], bytes(range(100)))


class PackBitmapTest(unittest.TestCase):
    def test_shapes_and_sizes(self):
        idx = np.zeros((vdc.BITMAP_H, vdc.BITMAP_W), dtype=np.uint8)
        bitmap, attr = vdc.pack_bitmap_frame(idx)
        self.assertEqual(len(bitmap), vdc.BITMAP_BYTES)
        self.assertEqual(len(attr), vdc.ATTR_BYTES)

    def test_solid_image_is_all_background_bits(self):
        idx = np.full((vdc.BITMAP_H, vdc.BITMAP_W), 4, dtype=np.uint8)  # green
        bitmap, attr = vdc.pack_bitmap_frame(idx)
        self.assertEqual(bitmap, b"\x00" * vdc.BITMAP_BYTES)
        self.assertEqual(set(attr), {0x44})  # bg=fg=4

    def test_two_color_image_round_trips_through_the_simulator(self):
        idx = np.zeros((vdc.BITMAP_H, vdc.BITMAP_W), dtype=np.uint8)
        idx[:, ::2] = 15  # white vertical stripes on black — 2 colors per block
        bitmap, attr = vdc.pack_bitmap_frame(idx)
        shown = vdc.simulate_frame(bitmap, attr)
        self.assertEqual(shown.shape, (vdc.BITMAP_H, vdc.BITMAP_W, 3))
        expected = vdc.VDC_PALETTE[idx].astype(np.uint8)
        np.testing.assert_array_equal(shown, expected)

    def test_rejects_wrong_shape(self):
        with self.assertRaises(ValueError):
            vdc.pack_bitmap_frame(np.zeros((100, 100), dtype=np.uint8))


class BitmapRegisterProgramTest(unittest.TestCase):
    def test_program_is_self_contained(self):
        # Entered from C64 mode there is no working base timing to inherit, so
        # the program has to carry the timing registers itself.
        for reg in (
            vdc.R.H_TOTAL,
            vdc.R.H_DISPLAYED,
            vdc.R.V_TOTAL,
            vdc.R.V_DISPLAYED,
            vdc.R.V_SYNC_POS,
            vdc.R.CHAR_V_TOTAL,
            vdc.R.H_SCROLL_CTRL,
            vdc.R.CHARSET_ADDR,
        ):
            self.assertIn(reg, vdc.BITMAP_640x200_REGS)

    def test_timing_adds_up_to_264_scanlines(self):
        regs = vdc.BITMAP_640x200_REGS
        lines_per_row = regs[vdc.R.CHAR_V_TOTAL] + 1
        self.assertEqual((regs[vdc.R.V_TOTAL] + 1) * lines_per_row, 264)
        self.assertEqual(regs[vdc.R.V_DISPLAYED] * lines_per_row, vdc.BITMAP_H)

    def test_selects_bitmap_attributes_and_64k(self):
        regs = vdc.BITMAP_640x200_REGS
        self.assertTrue(regs[vdc.R.H_SCROLL_CTRL] & vdc.H_SCROLL_BITMAP_BIT)
        self.assertTrue(regs[vdc.R.H_SCROLL_CTRL] & vdc.H_SCROLL_ATTR_BIT)
        self.assertEqual(regs[vdc.R.CHARSET_ADDR] & vdc.CHARSET_64K_BITS, vdc.CHARSET_64K_BITS)
        self.assertEqual(regs[vdc.R.ATTR_HI] << 8 | regs[vdc.R.ATTR_LO], vdc.ATTR_BASE)


class QuantizeTest(unittest.TestCase):
    def test_exact_palette_colors_map_to_their_index(self):
        rgb = vdc.VDC_PALETTE.astype(np.uint8).reshape(1, 16, 3)
        np.testing.assert_array_equal(vdc.quantize_to_vdc(rgb)[0], np.arange(16))


class Crt128Test(unittest.TestCase):
    def test_autostart_signature_matches_the_789010_reference(self):
        self.assertEqual(
            vdc.c128_autostart_signature(0x800A),
            bytes([0x4C, 0x0A, 0x80, 0x4C, 0x0A, 0x80, 0x02, 0x43, 0x42, 0x4D]),
        )

    def test_container_structure(self):
        rom = vdc.c128_autostart_signature() + b"\x60"  # sig + RTS
        crt = vdc.build_c128_crt(rom)
        self.assertEqual(len(crt), 0x40 + 0x10 + 0x2000)
        self.assertEqual(crt[:16], b"C128 CARTRIDGE  ")
        self.assertEqual(int.from_bytes(crt[0x10:0x14], "big"), 0x40)  # header len
        self.assertEqual(crt[0x18], 0)  # EXROM
        self.assertEqual(crt[0x19], 0)  # GAME
        self.assertEqual(crt[0x40:0x44], b"CHIP")
        self.assertEqual(int.from_bytes(crt[0x4E:0x50], "big"), 0x2000)  # ROM size
        self.assertEqual(crt[0x50 : 0x50 + len(rom)], rom)  # payload at $8000

    def test_rejects_oversized_rom(self):
        with self.assertRaises(ValueError):
            vdc.build_c128_crt(b"\x00" * 0x2001)


if __name__ == "__main__":
    unittest.main()
