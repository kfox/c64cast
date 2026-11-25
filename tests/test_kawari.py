"""hw/kawari.py: finding a VIC-II Kawari and writing its palette, and
host_palette = "auto" choosing its palette on a machine that is not an
Ultimate."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from c64cast.app.config import Config, SceneCfg
from c64cast.hw import hardware_palette as hp
from c64cast.hw import hw_provision, kawari

# 6-bit channels as the hardware reported them, with a set high bit on white's
# unused upper bits to show they are masked off.
_PALETTE_6BIT = [(0, 0, 0), (0xFF, 63, 63), (43, 10, 10), (24, 54, 51)] + [(8, 16, 32)] * 12


class FakeKawari:
    """The registers c64cast touches on a Kawari, as the hardware answered them.

    `stale_unlocks` is how many unlock sequences are ignored before one takes:
    on hardware the first after a deactivate never did. A write to overlay
    offset `drop_offset` is lost, as a TeensyROM+ write the link absorbed is.

    Video memory, port A's auto-increment and the VMEM copy (into the overlay
    too) follow the Kawari's register doc; `copies` counts the copies. Any
    other address is plain C64 RAM, and `commit` runs a page-flip handler's
    palette cue against it, as the swap IRQ does when it shows a frame."""

    def __init__(
        self,
        *,
        model: int = 0,
        stale_unlocks: int = 0,
        stock: bool = False,
        overlay_reads_answered: int | None = None,
        active: bool = False,
        hires_mode: int = 0,
        cap_lo: int = 0b11001110,
        drop_offset: int | None = None,
        version: tuple[int, int] = (1, 17),
        lose_trigger: bool = False,
    ):
        self.stock = stock
        self.overlay_reads_answered = overlay_reads_answered
        self.stale_unlocks = stale_unlocks
        self.active = active
        self.flags = 0
        self.port_addr = 0
        self.hires_mode = hires_mode
        self.drop_offset = drop_offset
        self.overlay = bytearray(256)
        self.overlay[kawari.OVERLAY_CHIP_MODEL] = model
        self.overlay[kawari.OVERLAY_VERSION : kawari.OVERLAY_VERSION + 2] = bytes(version)
        self.overlay[kawari.OVERLAY_CAP_LO] = cap_lo
        self.overlay[kawari.OVERLAY_VARIANT : kawari.OVERLAY_VARIANT + 6] = b"MAINLG"
        for i, rgb in enumerate(_PALETTE_6BIT):
            self.overlay[
                kawari.OVERLAY_PALETTE_RGB + 4 * i : kawari.OVERLAY_PALETTE_RGB + 4 * i + 3
            ] = bytes(rgb)
            self.overlay[kawari.OVERLAY_PALETTE_RGB + 4 * i + 3] = 0x3F
        self._sequence = b""
        self.flag_writes: list[int] = []
        self.unlock_bytes_while_active = 0
        self.persist_while_active = False
        self.written: set[int] = set()
        self.vram = bytearray(0x10000)
        self.port_b_addr = 0
        self.dma_count = 0
        self.copies = 0
        self.lose_trigger = lose_trigger
        self.ram = bytearray(0x10000)

    def write_memory(self, address: str, data_hex: str) -> None:
        if self.stock:
            return
        for i, value in enumerate(bytes.fromhex(data_hex)):
            self._write(int(address, 16) + i, value)

    def _write(self, addr: int, value: int) -> None:
        if not _KAWARI_REGISTERS[0] <= addr <= _KAWARI_REGISTERS[1]:
            self.ram[addr] = value
            return
        self.written.add(addr)
        if addr == kawari.VIDEO_MEM_FLAGS:
            self.flag_writes.append(value)
            if self.active:
                if value in kawari.UNLOCK:
                    self.unlock_bytes_while_active += 1
                if value & kawari.FLAGS_DEACTIVATE:
                    self.active = False
                else:
                    self.persist_while_active |= bool(value & kawari.FLAGS_PERSIST)
                    self.flags = value
                return
            self._sequence = (self._sequence + bytes([value]))[-4:]
            if self._sequence == kawari.UNLOCK:
                self._sequence = b""
                if self.stale_unlocks:
                    self.stale_unlocks -= 1
                else:
                    self.active, self.flags = True, 0
        elif not self.active:
            return
        elif addr == kawari.VIDEO_MEM_1_ADDR:
            self.port_addr = (self.port_addr & 0xFF00) | value
        elif addr == kawari.VIDEO_MEM_1_ADDR + 1:
            self.port_addr = (self.port_addr & 0x00FF) | (value << 8)
        elif addr in (kawari.DMA_COUNT, kawari.DMA_COUNT + 1):
            shift = 8 * (addr - kawari.DMA_COUNT)
            self.dma_count = (self.dma_count & ~(0xFF << shift)) | (value << shift)
        elif addr == kawari.VIDEO_MEM_2_ADDR:
            self.port_b_addr = (self.port_b_addr & 0xFF00) | value
        elif addr == kawari.VIDEO_MEM_2_ADDR + 1:
            self.port_b_addr = (self.port_b_addr & 0x00FF) | (value << 8)
        elif addr == kawari.VIDEO_MEM_1_VAL and (
            self.flags & kawari.FLAGS_BOTH_PORTS_DMA == kawari.FLAGS_BOTH_PORTS_DMA
        ):
            if value == kawari.DMA_COPY_VMEM and not self.lose_trigger:
                self._copy()
        elif addr == kawari.VIDEO_MEM_1_VAL and self.flags & kawari.FLAGS_OVERLAY:
            if self.port_addr == self.drop_offset:
                self.drop_offset = None
            else:
                self.overlay[self.port_addr & 0xFF] = value
        elif addr == kawari.VIDEO_MEM_1_VAL:
            self.vram[self.port_addr] = value
            if self.flags & 0x03 == kawari.FLAGS_PORT_A_INC:
                self.port_addr = (self.port_addr + 1) & 0xFFFF
        elif addr == kawari.HIRES_MODE:
            self.hires_mode = value

    def _copy(self) -> None:
        self.copies += 1
        for i in range(self.dma_count):
            value = self.vram[(self.port_b_addr + i) & 0xFFFF]
            if self.flags & kawari.FLAGS_OVERLAY:
                self.overlay[(self.port_addr + i) & 0xFF] = value
            else:
                self.vram[(self.port_addr + i) & 0xFFFF] = value
        self.dma_count = 0

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        if not _KAWARI_REGISTERS[0] <= address <= _KAWARI_REGISTERS[1]:
            return bytes(self.ram[address : address + length])
        if self.stock or not self.active:
            return b"\xff" * length
        if address == kawari.VIDEO_MEM_FLAGS:
            return bytes([self.flags])
        if address == kawari.HIRES_MODE:
            return bytes([self.hires_mode])
        if address == kawari.VIDEO_MEM_1_VAL and self.flags & kawari.FLAGS_OVERLAY:
            if self.overlay_reads_answered is not None:
                if self.overlay_reads_answered == 0:
                    return None
                self.overlay_reads_answered -= 1
            return bytes([self.overlay[self.port_addr & 0xFF]])
        return b"\xff" * length

    def flush(self) -> None:
        pass

    def palette_6bit(self) -> list[int]:
        return [self.overlay[o] & kawari.CHANNEL_MAX for o in kawari._PALETTE_OFFSETS]

    def commit(self) -> None:
        """Run the page flips' palette cue (modes_irq._PAGE_FLIP_CUE) on a
        6502 whose memory is this C64."""
        from py65.devices.mpu6502 import MPU
        from py65.memory import ObservableMemory

        from c64cast.video import modes_irq

        mem = ObservableMemory()
        cue = modes_irq.PALETTE_CUE_ADDR
        mem[cue] = self.ram[cue]
        mem.subscribe_to_write(range(0xD000, 0xD100), lambda a, v: self._write(a, v))
        mem.subscribe_to_write([cue], lambda a, v: self.ram.__setitem__(a, v))
        code = modes_irq._PAGE_FLIP_CUE
        for i, b in enumerate(code):
            mem[0x1000 + i] = b
        mpu = MPU(memory=mem)
        mpu.pc = 0x1000
        while mpu.pc != 0x1000 + len(code):
            mpu.step()

    def cue(self) -> None:
        """What a cued frame's tracker write leaves in RAM."""
        from c64cast.video import modes_irq

        self.ram[modes_irq.PALETTE_CUE_ADDR] = 1


_KAWARI_REGISTERS = (0xD02F, 0xD03F)


class ProbeTest(unittest.TestCase):
    def test_reads_what_the_kawari_says_about_itself(self):
        found = kawari.probe(FakeKawari())  # type: ignore[arg-type]
        assert found is not None
        info = found.info
        self.assertEqual(info.model, "6567R8")
        self.assertEqual(info.version, (1, 17))
        self.assertEqual(info.variant, "MAINLG")
        self.assertTrue(info.configurable_rgb)
        self.assertEqual(len(info.palette_rgb), 16)
        self.assertEqual(info.palette_rgb[1], (255, 255, 255))
        self.assertEqual(info.palette_rgb[2], (174, 40, 40))

    def test_without_the_capability_the_palette_is_not_configurable(self):
        found = kawari.probe(FakeKawari(cap_lo=0b11000110))
        assert found is not None
        self.assertFalse(found.info.configurable_rgb)

    def test_a_second_unlock_after_a_stale_one_still_reads(self):
        self.assertIsNotNone(kawari.probe(FakeKawari(stale_unlocks=1)))

    def test_an_already_active_kawari_is_not_sent_the_unlock(self):
        bus = FakeKawari(active=True)
        self.assertIsNotNone(kawari.probe(bus))
        self.assertEqual(bus.unlock_bytes_while_active, 0)
        self.assertFalse(bus.persist_while_active)

    def test_leaves_the_extra_registers_locked(self):
        bus = FakeKawari()
        kawari.probe(bus)
        self.assertFalse(bus.active)
        self.assertEqual(bus.flag_writes[-1], kawari.FLAGS_DEACTIVATE)

    def test_a_hires_mode_left_on_is_turned_off(self):
        bus = FakeKawari(hires_mode=0x90)
        with self.assertLogs("c64cast.hw.kawari", level="INFO"):
            kawari.probe(bus)
        self.assertEqual(bus.hires_mode, 0)

    def test_a_stock_vic_is_not_a_kawari(self):
        bus = FakeKawari(stock=True)
        self.assertIsNone(kawari.probe(bus))

    def test_an_unknown_chip_model_is_not_read(self):
        bus = FakeKawari(model=len(kawari.CHIP_MODELS), hires_mode=0x90)
        self.assertIsNone(kawari.probe(bus))
        self.assertEqual(bus.hires_mode, 0x90)

    def test_an_unanswered_chip_model_read_is_no_answer_and_relocks(self):
        bus = FakeKawari(overlay_reads_answered=0)
        self.assertIsNone(kawari.probe(bus))
        self.assertFalse(bus.active)

    def test_an_unanswered_palette_byte_is_no_answer_and_relocks(self):
        bus = FakeKawari(overlay_reads_answered=20)
        self.assertIsNone(kawari.probe(bus))
        self.assertFalse(bus.active)

    def test_a_backend_that_cannot_read_is_no_answer(self):
        bus = SimpleNamespace(
            write_memory=lambda a, d: None,
            read_memory=lambda *a, **k: None,
            flush=lambda: None,
        )
        self.assertIsNone(kawari.probe(bus))  # type: ignore[arg-type]


class WritePaletteTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeKawari()
        found = kawari.probe(self.bus)
        assert found is not None
        self.chip = found
        self.table = [(7 * i) % 64 for i in range(kawari.PALETTE_CHANNELS)]

    def test_writes_every_channel_and_reads_it_back(self):
        self.assertEqual(self.chip.write_palette(self.table), tuple(self.table))
        self.assertEqual(self.bus.palette_6bit(), self.table)

    def test_never_persists_and_locks_again(self):
        self.chip.write_palette(self.table)
        self.assertFalse(self.bus.persist_while_active)
        self.assertFalse(self.bus.active)

    def test_touches_no_register_past_port_a(self):
        self.bus.written.clear()
        self.chip.write_palette(self.table)
        self.assertLessEqual(
            self.bus.written,
            {
                kawari.VIDEO_MEM_FLAGS,
                kawari.VIDEO_MEM_1_ADDR,
                kawari.VIDEO_MEM_1_ADDR + 1,
                kawari.VIDEO_MEM_1_VAL,
            },
        )

    def test_a_dropped_write_shows_in_the_read_back(self):
        self.bus.drop_offset = kawari.OVERLAY_PALETTE_RGB + 1
        back = self.chip.write_palette(self.table)
        self.assertNotEqual(back, tuple(self.table))

    def test_a_kawari_that_will_not_activate_is_no_answer(self):
        self.bus.stock = True
        self.assertIsNone(self.chip.write_palette(self.table))


class _KawariApi(FakeKawari):
    profile = SimpleNamespace(supports_read=True, supports_system_mode=False, family="tr")
    kawari: kawari.Kawari | None = None


class AutoPaletteTest(unittest.TestCase):
    def setUp(self):
        from c64cast.video import palette as pal

        before = pal.C64_PALETTE_BGR.copy(), pal.active_host_palette_name()
        self.addCleanup(lambda: pal.set_host_palette(before[0], name=before[1]))
        hw_provision.forget_resolved_palette()
        self.addCleanup(hw_provision.forget_resolved_palette)
        env = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        self.pal = pal

    def test_auto_takes_a_kawaris_live_palette_as_bgr(self):
        hw_provision.resolve_palette(Config(), _KawariApi())
        self.assertTrue(self.pal.active_host_palette_name().startswith("kawari-live:"))
        self.assertEqual(tuple(self.pal.C64_PALETTE_BGR[2]), (40.0, 40.0, 174.0))

    def test_a_configured_palette_still_finds_the_kawari(self):
        cfg = Config()
        cfg.hardware.host_palette = "pepto"
        api = _KawariApi()
        with self.assertLogs("c64cast.hw.hw_provision", level="INFO"):
            hw_provision.resolve_palette(cfg, api)
        self.assertIsInstance(api.kawari, kawari.Kawari)
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_auto_assumes_pepto_for_a_stock_vic(self):
        hw_provision.resolve_palette(Config(), _KawariApi(stock=True))
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_a_backend_without_reads_asks_nothing(self):
        api = _KawariApi()
        api.profile = SimpleNamespace(supports_read=False, supports_system_mode=False)
        hw_provision.resolve_palette(Config(), api)
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")
        self.assertEqual(api.flag_writes, [])

    def test_skip_probe_asks_nothing(self):
        cfg = Config()
        cfg.debug.skip_probe = True
        api = _KawariApi()
        hw_provision.resolve_palette(cfg, api)
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")
        self.assertEqual(api.flag_writes, [])


def _bgr(rgb6: list[tuple[int, int, int]]) -> np.ndarray:
    return np.asarray(
        [[kawari.to_8bit(b), kawari.to_8bit(g), kawari.to_8bit(r)] for r, g, b in rgb6],
        dtype=np.uint8,
    )


class KawariWriterTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeKawari()
        found = kawari.probe(self.bus)
        assert found is not None
        self.chip = found
        self.record = Path(tempfile.mkdtemp()) / "kawari-palette.json"
        self.machine = _bgr(_PALETTE_6BIT)
        self.scene = self.machine.copy()
        self.scene[8] = (kawari.to_8bit(10), kawari.to_8bit(30), kawari.to_8bit(60))

    def _writer(self) -> hp.KawariPaletteWriter:
        return hp.KawariPaletteWriter(self.chip, self.record, playback_reads=self.bus)

    def test_the_machine_palette_is_the_one_found_at_startup(self):
        machine = self._writer().read_machine()
        assert machine is not None
        np.testing.assert_array_equal(machine, self.machine)

    def test_a_push_lands_in_six_bits_and_reads_back(self):
        self.assertTrue(self._writer().push(self.scene))
        self.assertEqual(self.bus.palette_6bit()[24:27], [60, 30, 10])

    def test_a_write_the_link_dropped_fails_the_push(self):
        self.bus.drop_offset = kawari.OVERLAY_PALETTE_RGB + 4 * 8
        self.assertFalse(self._writer().push(self.scene))

    def test_a_pushed_table_is_recorded_and_putting_the_machines_back_clears_it(self):
        writer = self._writer()
        writer.push(self.scene)
        saved = json.loads(self.record.read_text())
        np.testing.assert_array_equal(saved["shows"], [self.scene])
        np.testing.assert_array_equal(saved["machine"], self.machine)
        writer.push(self.machine)
        self.assertFalse(self.record.exists())

    def test_a_run_that_died_has_its_palette_put_back(self):
        self._writer().push(self.scene)
        found = kawari.probe(self.bus)
        assert found is not None
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            machine = hp.put_back_a_dead_runs_kawari_palette(found, self.record)
        assert machine is not None
        np.testing.assert_array_equal(machine, self.machine)
        self.assertEqual(self.bus.palette_6bit(), [c & 63 for rgb in _PALETTE_6BIT for c in rgb])
        self.assertFalse(self.record.exists())

    def test_a_record_still_matching_the_machine_is_not_taken_as_its_palette(self):
        self._writer().push(self.scene)
        found = kawari.probe(self.bus)
        assert found is not None
        self.assertIsNone(
            hp.KawariPaletteWriter(found, self.record, playback_reads=self.bus).read_machine()
        )
        self.assertTrue(self.record.exists())

    def test_an_unreadable_record_is_ignored(self):
        self.record.write_text("{")
        self.assertIsNone(hp.put_back_a_dead_runs_kawari_palette(self.chip, self.record))

    def test_a_record_the_machine_no_longer_matches_is_dropped(self):
        self.record.write_text(
            json.dumps({"machine": self.scene.tolist(), "shows": self.scene.tolist()})
        )
        machine = self._writer().read_machine()
        assert machine is not None
        np.testing.assert_array_equal(machine, self.machine)
        self.assertFalse(self.record.exists())


class ShotPushTest(unittest.TestCase):
    """A shot's table: staged by `load`, copied in by the commit that shows
    the cued frame, checked by `ack`."""

    def setUp(self):
        self.bus = FakeKawari()
        found = kawari.probe(self.bus)
        assert found is not None
        self.chip = found
        self.record = Path(tempfile.mkdtemp()) / "kawari-palette.json"
        self.machine = _bgr(_PALETTE_6BIT)
        self.shot = self.machine.copy()
        self.shot[5] = (kawari.to_8bit(1), kawari.to_8bit(2), kawari.to_8bit(3))
        self.shot[9] = (kawari.to_8bit(40), kawari.to_8bit(50), kawari.to_8bit(60))
        self.writer = hp.KawariPaletteWriter(self.chip, self.record, playback_reads=self.bus)

    def _record_shows(self) -> list[np.ndarray]:
        return [np.asarray(t) for t in json.loads(self.record.read_text())["shows"]]

    def _show_shot(self) -> None:
        self.writer.load(self.shot, lambda: False)
        self.bus.cue()
        self.writer.go()
        self.bus.commit()

    def _cue_state(self) -> bytes:
        from c64cast.video import modes_irq

        start = modes_irq.PALETTE_CUE_STATE_ADDR
        return bytes(self.bus.ram[start : start + modes_irq.PALETTE_CUE_STATE_LEN])

    def test_a_shot_changes_the_palette_and_leaves_the_kawari_locked(self):
        self.assertTrue(self.writer.load(self.shot, lambda: False))
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.machine))
        self.bus.cue()
        self.writer.go()
        self.bus.commit()
        self.writer.ack()
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.shot))
        self.assertEqual(self.bus.copies, 1)
        self.assertFalse(self.bus.active)
        self.assertFalse(self.bus.persist_while_active)

    def test_the_palette_waits_for_the_commit_that_shows_the_cued_frame(self):
        self.writer.load(self.shot, lambda: False)
        self.writer.go()
        self.bus.commit()
        self.assertEqual(self.bus.copies, 0, "a commit with no cue started the copy")
        self.bus.cue()
        self.assertEqual(self.bus.copies, 0, "the cue alone started the copy")
        self.bus.commit()
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.shot))
        self.assertEqual(self._cue_state(), bytes(2))

    def test_the_ack_waits_for_the_commit(self):
        self.writer.load(self.shot, lambda: False)
        self.bus.cue()
        self.writer.go()
        with mock.patch.object(hp.time, "sleep", side_effect=lambda _s: self.bus.commit()) as nap:
            self.writer.ack()
        nap.assert_called()
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.shot))

    def test_a_cue_the_c64_never_takes_fails_the_ack_and_is_withdrawn(self):
        self.writer.load(self.shot, lambda: False)
        self.bus.cue()
        self.writer.go()
        with mock.patch.object(hp, "COPY_WAIT_S", 0.0), self.assertRaises(RuntimeError):
            self.writer.ack()
        self.assertEqual(self._cue_state(), bytes(2))
        self.assertFalse(self.bus.active)
        self.bus.commit()
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.machine))
        self.assertTrue(any(np.array_equal(t, self.shot) for t in self._record_shows()))

    def test_a_shot_keeps_the_spare_bytes_the_kawari_had(self):
        self._show_shot()
        self.writer.ack()
        spare = [self.bus.overlay[o] for o in kawari._SPARE_OFFSETS]
        self.assertEqual(spare, [0x3F] * kawari.PALETTE_COLORS)

    def test_a_copy_that_never_ran_fails_the_ack(self):
        self.bus.lose_trigger = True
        self._show_shot()
        with self.assertRaises(RuntimeError):
            self.writer.ack()
        self.assertFalse(self.bus.active)

    def test_a_stopped_load_is_dropped(self):
        self.assertFalse(self.writer.load(self.shot, lambda: True))
        self.writer.abort()
        self.assertFalse(self.bus.active)
        self.assertEqual(self.bus.copies, 0)

    def test_an_abort_withdraws_the_cue(self):
        self.writer.load(self.shot, lambda: False)
        self.bus.cue()
        self.writer.go()
        self.writer.abort()
        self.bus.commit()
        self.assertEqual(self.bus.copies, 0)
        self.assertEqual(self._cue_state(), bytes(2))

    def test_until_the_ack_the_record_names_both_tables(self):
        self.writer.load(self.shot, lambda: False)
        shows = self._record_shows()
        self.assertEqual(len(shows), 2)
        np.testing.assert_array_equal(shows[0], self.machine)
        np.testing.assert_array_equal(shows[1], self.shot)
        self.bus.cue()
        self.writer.go()
        self.bus.commit()
        self.writer.ack()
        shows = self._record_shows()
        self.assertEqual(len(shows), 1)
        np.testing.assert_array_equal(shows[0], self.shot)

    def test_an_abort_after_the_copy_keeps_its_table_in_the_record(self):
        self._show_shot()
        self.writer.abort()
        later = self.machine.copy()
        later[3] = (0, 0, 0)
        self.writer.load(later, lambda: False)
        shows = self._record_shows()
        self.assertTrue(any(np.array_equal(t, self.shot) for t in shows))

    def test_a_run_that_died_mid_shot_has_its_palette_put_back(self):
        self._show_shot()
        found = kawari.probe(self.bus)
        assert found is not None
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            machine = hp.put_back_a_dead_runs_kawari_palette(found, self.record)
        assert machine is not None
        np.testing.assert_array_equal(machine, self.machine)
        self.assertEqual(self.bus.palette_6bit(), hp._kawari_channels(self.machine))


class _KawariBackend(_KawariApi):
    hardware_palette = None


class KawariProvisionTest(unittest.TestCase):
    def setUp(self):
        hw_provision.forget_resolved_palette()
        self.addCleanup(hw_provision.forget_resolved_palette)
        env = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)

    def _provision(self, api) -> hp.HardwarePalette | None:
        cfg = Config()
        cfg.color.hardware_palette = "source"
        cfg.scenes = [SceneCfg(type="video")]
        api.kawari = kawari.probe(api)
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            return hp.provision_hardware_palette(api, cfg, is_ensemble=False)

    def test_a_kawari_with_a_configurable_palette_gets_its_writer(self):
        api = _KawariBackend()
        control = self._provision(api)
        assert control is not None
        self.assertEqual(control.channel_bits, 6)
        self.assertTrue(control.follows_shots)
        self.assertIs(api.hardware_palette, control)

    def test_a_kawari_without_one_is_skipped(self):
        self.assertIsNone(self._provision(_KawariBackend(cap_lo=0b11000110)))

    def test_a_kawari_that_cannot_copy_into_its_palette_keeps_one_per_video(self):
        control = self._provision(_KawariBackend(version=(1, 15)))
        assert control is not None
        self.assertFalse(control.follows_shots)

    def test_the_first_firmware_that_can_copy_follows_shots(self):
        control = self._provision(_KawariBackend(version=kawari.OVERLAY_COPY_FIRMWARE))
        assert control is not None
        self.assertTrue(control.follows_shots)

    def test_a_kawari_behind_an_ultimate_keeps_one_palette_per_video(self):
        api = _KawariBackend()
        api.profile = SimpleNamespace(
            supports_read=True, supports_system_mode=False, family="ultimate"
        )
        control = self._provision(api)
        assert control is not None
        self.assertFalse(control.follows_shots)


class DeadRunTest(unittest.TestCase):
    """A run that died left its scene's colors on the Kawari."""

    def setUp(self):
        from c64cast.app.paths import kawari_palette_record_path
        from c64cast.video import palette as pal

        before = pal.C64_PALETTE_BGR.copy(), pal.active_host_palette_name()
        self.addCleanup(lambda: pal.set_host_palette(before[0], name=before[1]))
        hw_provision.forget_resolved_palette()
        self.addCleanup(hw_provision.forget_resolved_palette)
        env = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        self.pal = pal
        self.api = _KawariBackend()
        self.machine = _bgr(_PALETTE_6BIT)
        scene = self.machine.copy()
        scene[8] = (kawari.to_8bit(10), kawari.to_8bit(30), kawari.to_8bit(60))
        found = kawari.probe(self.api)
        assert found is not None
        hp.KawariPaletteWriter(found, kawari_palette_record_path(), playback_reads=self.api).push(
            scene
        )
        self.record = kawari_palette_record_path()

    def _machine_6bit(self) -> list[int]:
        return [c & 63 for rgb in _PALETTE_6BIT for c in rgb]

    def test_auto_aims_at_the_palette_the_dead_run_found(self):
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            hw_provision.resolve_palette(Config(), self.api)
        np.testing.assert_array_equal(self.pal.C64_PALETTE_BGR, self.machine)
        self.assertEqual(self.api.palette_6bit(), self._machine_6bit())
        self.assertFalse(self.record.exists())

    def test_a_run_that_pushes_nothing_still_puts_it_back(self):
        cfg = Config()
        cfg.hardware.host_palette = "pepto"
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            hw_provision.resolve_palette(cfg, self.api)
        self.assertEqual(self.api.palette_6bit(), self._machine_6bit())

    def test_a_put_back_that_fails_keeps_the_record_and_pushes_nothing(self):
        self.api.drop_offset = kawari.OVERLAY_PALETTE_RGB + 4 * 8
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            hw_provision.resolve_palette(Config(), self.api)
        self.assertTrue(self.record.exists())
        cfg = Config()
        cfg.color.hardware_palette = "source"
        cfg.scenes = [SceneCfg(type="video")]
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            self.assertIsNone(hp.provision_hardware_palette(self.api, cfg, is_ensemble=False))
        self.assertTrue(self.record.exists())


if __name__ == "__main__":
    unittest.main()
