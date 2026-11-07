"""hw/kawari.py: reading a VIC-II Kawari's live palette, and host_palette =
"auto" choosing it on a machine that is not an Ultimate."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from c64cast.app.config import Config
from c64cast.hw import hw_provision, kawari

# 6-bit channels as the hardware reported them, with a set high bit on white's
# unused upper bits to show they are masked off.
_PALETTE_6BIT = [(0, 0, 0), (0xFF, 63, 63), (43, 10, 10), (24, 54, 51)] + [(8, 16, 32)] * 12


class FakeKawari:
    """The registers a palette read touches, as the hardware answered them.

    `stale_unlocks` is how many unlock sequences are ignored before one takes:
    on hardware the first after a deactivate never did."""

    def __init__(self, *, model: int = 0, stale_unlocks: int = 0, stock: bool = False):
        self.stock = stock
        self.stale_unlocks = stale_unlocks
        self.active = False
        self.flags = 0
        self.port_addr = 0
        self.overlay = bytearray(256)
        self.overlay[kawari.OVERLAY_CHIP_MODEL] = model
        for i, rgb in enumerate(_PALETTE_6BIT):
            self.overlay[
                kawari.OVERLAY_PALETTE_RGB + 4 * i : kawari.OVERLAY_PALETTE_RGB + 4 * i + 3
            ] = bytes(rgb)
        self._sequence = b""
        self.flag_writes: list[int] = []

    def write_memory(self, address: str, data_hex: str) -> None:
        addr, data = int(address, 16), bytes.fromhex(data_hex)
        if self.stock:
            return
        if addr == kawari.VIDEO_MEM_FLAGS:
            value = data[0]
            self.flag_writes.append(value)
            if self.active:
                if value & kawari.FLAGS_DEACTIVATE:
                    self.active = False
                else:
                    self.flags = value
                return
            self._sequence = (self._sequence + data)[-4:]
            if self._sequence == kawari.UNLOCK:
                self._sequence = b""
                if self.stale_unlocks:
                    self.stale_unlocks -= 1
                else:
                    self.active, self.flags = True, 0
        elif addr == kawari.VIDEO_MEM_1_ADDR and self.active:
            self.port_addr = data[0] | (data[1] << 8)

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        if self.stock or not self.active:
            return b"\xff" * length
        if address == kawari.VIDEO_MEM_FLAGS:
            return bytes([self.flags])
        if address == kawari.VIDEO_MEM_1_VAL and self.flags & kawari.FLAGS_OVERLAY:
            return bytes([self.overlay[self.port_addr & 0xFF]])
        return b"\xff" * length

    def flush(self) -> None:
        pass


class ReadPaletteTest(unittest.TestCase):
    def test_reads_the_model_and_scales_six_bit_channels(self):
        model, rgb = kawari.read_palette(FakeKawari())  # type: ignore[misc]
        self.assertEqual(model, "6567R8")
        self.assertEqual(len(rgb), 16)
        self.assertEqual(rgb[1], (255, 255, 255))
        self.assertEqual(rgb[2], (174, 40, 40))

    def test_a_second_unlock_after_a_stale_one_still_reads(self):
        answer = kawari.read_palette(FakeKawari(stale_unlocks=1))
        self.assertIsNotNone(answer)

    def test_leaves_the_extra_registers_locked(self):
        bus = FakeKawari()
        kawari.read_palette(bus)
        self.assertFalse(bus.active)
        self.assertEqual(bus.flag_writes[-1], kawari.FLAGS_DEACTIVATE)

    def test_a_stock_vic_is_not_a_kawari(self):
        self.assertIsNone(kawari.read_palette(FakeKawari(stock=True)))

    def test_an_unknown_chip_model_is_not_read(self):
        self.assertIsNone(kawari.read_palette(FakeKawari(model=len(kawari.CHIP_MODELS))))

    def test_a_backend_that_cannot_read_is_no_answer(self):
        bus = SimpleNamespace(
            write_memory=lambda a, d: None,
            read_memory=lambda *a, **k: None,
            flush=lambda: None,
        )
        self.assertIsNone(kawari.read_palette(bus))  # type: ignore[arg-type]


class _KawariApi(FakeKawari):
    profile = SimpleNamespace(supports_read=True, supports_system_mode=False)


class AutoPaletteTest(unittest.TestCase):
    def setUp(self):
        from c64cast.video import palette as pal

        before = pal.C64_PALETTE_BGR.copy(), pal.active_host_palette_name()
        self.addCleanup(lambda: pal.set_host_palette(before[0], name=before[1]))
        hw_provision.forget_resolved_palette()
        self.addCleanup(hw_provision.forget_resolved_palette)
        self.pal = pal

    def test_auto_takes_a_kawaris_live_palette_as_bgr(self):
        hw_provision.resolve_palette(Config(), _KawariApi())
        self.assertTrue(self.pal.active_host_palette_name().startswith("kawari-live:"))
        self.assertEqual(tuple(self.pal.C64_PALETTE_BGR[2]), (40.0, 40.0, 174.0))

    def test_auto_assumes_pepto_for_a_stock_vic(self):
        hw_provision.resolve_palette(Config(), _KawariApi(stock=True))
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_skip_probe_asks_nothing(self):
        cfg = Config()
        cfg.debug.skip_probe = True
        api = _KawariApi()
        hw_provision.resolve_palette(cfg, api)
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")
        self.assertEqual(api.flag_writes, [])


if __name__ == "__main__":
    unittest.main()
