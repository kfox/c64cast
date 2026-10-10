"""Tests for the REU-staged video paths.

Two pipelines covered:
  * char-mode single-buffer (PETSCII / Blank): REUWRITE screen → REU→main
    DMA into $0400. Color RAM stays on the regular delta path.
  * hires double-buffer: bitmap + screen REUWRITE → 16-byte frame tracker
    DMAWRITE to $C700. A C64-side raster IRQ handler at $C500 reads the
    tracker at vblank, triggers both REU→main DMAs into the off-screen
    bank ($A000+$8400 if bank 0 is showing, $2000+$0400 otherwise), then
    swaps $DD00 — all on the kernal's deterministic 60 Hz IRQ schedule.

These tests don't require a real U64 — they verify the push/setup/teardown
output of each display mode against the FakeAPI's recorded write log.
"""

from __future__ import annotations

import unittest
from typing import cast
from unittest import mock

import numpy as np
from _fakes import FakeAPI, quiet_logging

from c64cast.app.config import Config, VideoCfg
from c64cast.app.scene_factory import _build_display_mode
from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_ADDR
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    KERNAL,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    RASTER_COMMIT_LAST_SAFE_LINE,
    REU,
    SCREEN,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
    halt_quantum_bytes,
)
from c64cast.video import modes_irq
from c64cast.video.modes import (
    BlankDisplayMode,
    HiresDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
)
from c64cast.video.modes_irq import (
    AUDIO_HANDLER_INSTALL_ADDR,
    AUDIO_HANDLER_STUB,
    BANK_SWAP_CHUNK_SIZE,
    BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER_ADDR,
    FRAME_TRACKER_ADDR,
    FRAME_TRACKER_LEN,
    MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_BANK_SWAP_IRQ_HANDLER,
    MHIRES_FRAME_TRACKER_LEN,
    MHIRES_TRACKER_OFF_BG0,
    MHIRES_TRACKER_OFF_BITMAP_REGS,
    MHIRES_TRACKER_OFF_COLOR_REGS,
    MHIRES_TRACKER_OFF_READY_FLAG,
    MHIRES_TRACKER_OFF_RESERVED,
    MHIRES_TRACKER_OFF_SCREEN_REGS,
    PUMP_BODY_STUB,
    REU_VIDEO_BITMAP_BASE,
    REU_VIDEO_BITMAP_COLOR_BASE,
    REU_VIDEO_BITMAP_COLOR_LEN,
    REU_VIDEO_BITMAP_LEN,
    REU_VIDEO_BITMAP_SCREEN_BASE,
    REU_VIDEO_BITMAP_SCREEN_LEN,
    REU_VIDEO_SCREEN_BASE,
    REU_VIDEO_SCREEN_LEN,
    REU_VIDEO_SLOT_STRIDE,
    REU_VIDEO_SLOTS,
    TRACKER_OFF_BITMAP_REGS,
    TRACKER_OFF_READY_FLAG,
    TRACKER_OFF_RESERVED,
    TRACKER_OFF_SCREEN_REGS,
    uninstall_bank_swap_irq,
)


class ReuStagedFlagDefaultTest(unittest.TestCase):
    """The config flag defaults to the "auto" tri-state, while display modes
    still default to the safe host-DMA path (False) so anything constructing a
    mode without an explicit decision stays off the staged path."""

    def test_video_cfg_default_is_auto(self):
        self.assertEqual(VideoCfg().use_reu_staged, "auto")

    def test_petscii_default(self):
        self.assertFalse(PETSCIIDisplayMode().use_reu_staged)

    def test_blank_default(self):
        self.assertFalse(BlankDisplayMode().use_reu_staged)


class ResolveUseReuStagedTest(unittest.TestCase):
    """scene_factory.resolve_use_reu_staged() maps the tri-state + probe verdict +
    display mode to a concrete bool. "auto" stages bitmap modes only when REU
    is available; explicit true/false ignore the probe."""

    def _resolve(self, setting, display, reu_available):
        from c64cast.app.scene_factory import resolve_use_reu_staged

        return resolve_use_reu_staged(setting, display, reu_available=reu_available)

    def test_auto_bitmap_with_reu_enables(self):
        for d in ("hires", "hires_edges", "mhires"):
            self.assertTrue(self._resolve("auto", d, True), d)

    def test_auto_bitmap_without_reu_stays_off(self):
        for d in ("hires", "mhires"):
            self.assertFalse(self._resolve("auto", d, False), d)

    def test_auto_char_modes_stay_off_even_with_reu(self):
        # Char modes regress under staging, so auto leaves them on host DMA.
        for d in ("petscii", "blank", "mcm"):
            self.assertFalse(self._resolve("auto", d, True), d)

    def test_explicit_true_ignores_probe_and_mode(self):
        self.assertTrue(self._resolve(True, "petscii", False))
        self.assertTrue(self._resolve(True, "mhires", False))

    def test_explicit_false_never_stages(self):
        self.assertFalse(self._resolve(False, "mhires", True))
        self.assertFalse(self._resolve(False, "petscii", True))


class ReuStagedCharModeWithPumpTest(unittest.TestCase):
    """#554: a char mode's REU staging drives the REC from the host, which the
    REU audio pump drives from the C64, so the pump turns it off — loudly when
    it was asked for explicitly."""

    def setUp(self):
        # The warning is logged once per display per process.
        from c64cast.app.scene_factory import _warn_host_rec_staging_dropped

        _warn_host_rec_staging_dropped.cache_clear()
        self.addCleanup(_warn_host_rec_staging_dropped.cache_clear)

    def _resolve(self, setting, display, *, pump):
        from c64cast.app.scene_factory import resolve_use_reu_staged

        return resolve_use_reu_staged(
            setting, display, reu_available=True, audio_reu_pump_active=pump
        )

    def test_explicit_true_char_mode_with_pump_is_refused_with_warning(self):
        for d in ("petscii", "blank"):
            with self.subTest(display=d):
                with self.assertLogs("c64cast.app.scene_factory", "WARNING") as cm:
                    self.assertFalse(self._resolve(True, d, pump=True))
                self.assertIn("use_reu_pump", cm.output[0])
                self.assertIn(d, cm.output[0])

    def test_auto_char_mode_with_pump_stays_off_quietly(self):
        with self.assertNoLogs("c64cast.app.scene_factory", "WARNING"):
            self.assertFalse(self._resolve("auto", "petscii", pump=True))

    def test_bitmap_modes_keep_staging_with_pump(self):
        # Their staging runs C64-side, through the merged dispatcher.
        with self.assertNoLogs("c64cast.app.scene_factory", "WARNING"):
            for d in ("hires", "hires_edges", "mhires"):
                self.assertTrue(self._resolve(True, d, pump=True), d)

    def test_char_mode_without_pump_keeps_explicit_staging(self):
        self.assertTrue(self._resolve(True, "petscii", pump=False))


class WiredModeRecOwnershipTest(unittest.TestCase):
    """Kept apart from the assertLogs tests above: it runs under quiet_logging."""

    def test_no_wired_mode_drives_rec_while_the_pump_is_on(self):
        # The display-name gate in resolve_use_reu_staged against the modes'
        # own drives_rec_from_host, for every concrete display.
        from c64cast.app.config import _DISPLAY_CHOICES
        from c64cast.app.scene_factory import (
            _HOST_REC_STAGED_MODES,
            DisplayWiring,
            build_wired_display_mode,
        )

        for d in (c for c in _DISPLAY_CHOICES if c != "random"):
            for pump in (False, True):
                with self.subTest(display=d, pump=pump):
                    wiring = DisplayWiring(
                        use_reu_staged=True, reu_available=True, audio_reu_pump_active=pump
                    )
                    with quiet_logging():
                        mode = build_wired_display_mode(d, wiring)
                    # Pump on: no mode drives REC. Pump off: exactly the named
                    # set does, so the set and the property cannot drift apart.
                    expected = (not pump) and d in _HOST_REC_STAGED_MODES
                    self.assertEqual(mode.drives_rec_from_host, expected)


class ReuPumpSkipsIrqHookTest(unittest.TestCase):
    """modes_irq.reu_pump_skips_irq_hook: the audio side's pump choice."""

    def test_bank_swap_mode_with_pump_skips_the_hook(self):
        self.assertTrue(
            modes_irq.reu_pump_skips_irq_hook(
                MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
            )
        )

    def test_unstaged_bitmap_mode_with_pump_keeps_the_hook(self):
        # No merged dispatcher without REU staging (no REU confirmed under "auto",
        # --skip-probe, or an explicit false), so the pump must hook $0314.
        for mode in (
            HiresDisplayMode(audio_reu_pump_active=True),
            HiresDisplayMode(style="edges", audio_reu_pump_active=True),
            MultiHiresDisplayMode(audio_reu_pump_active=True),
        ):
            with self.subTest(mode=type(mode).__name__, style=getattr(mode, "style", None)):
                self.assertFalse(modes_irq.reu_pump_skips_irq_hook(mode))

    def test_char_mode_without_staging_keeps_the_hook(self):
        self.assertFalse(modes_irq.reu_pump_skips_irq_hook(PETSCIIDisplayMode()))
        self.assertFalse(modes_irq.reu_pump_skips_irq_hook(None))

    def test_host_rec_staged_mode_is_refused(self):
        for mode in (
            PETSCIIDisplayMode(use_reu_staged=True),
            BlankDisplayMode(use_reu_staged=True),
        ):
            with self.subTest(mode=type(mode).__name__):
                with self.assertRaises(ValueError):
                    modes_irq.reu_pump_skips_irq_hook(mode)


class ScenePumpStartRecOwnershipTest(unittest.TestCase):
    """The scene-side pump starts that ask reu_pump_skips_irq_hook (#554)."""

    def _audio(self, *, use_reu_pump: bool):
        from unittest.mock import MagicMock

        from c64cast.audio.audio import AudioStreamer

        audio = MagicMock(spec=AudioStreamer)
        audio.use_reu_pump = use_reu_pump
        audio.effective_rate = 12000
        return audio

    def _webcam(self, audio):
        from unittest.mock import MagicMock

        from c64cast.scenes.scenes import WebcamScene

        return WebcamScene(
            cast(Ultimate64API, FakeAPI()),
            audio,
            PETSCIIDisplayMode(use_reu_staged=True),
            MagicMock(),
            MagicMock(),
            "cam",
        )

    def test_webcam_without_the_pump_accepts_a_host_rec_mode(self):
        audio = self._audio(use_reu_pump=False)
        self._webcam(audio).setup()
        self.assertIs(audio.start_mic.call_args.kwargs["skip_irq_vector_hook"], False)

    def test_webcam_with_the_pump_refuses_a_host_rec_mode(self):
        audio = self._audio(use_reu_pump=True)
        with self.assertRaises(ValueError):
            self._webcam(audio).setup()
        audio.start_mic.assert_not_called()

    def test_video_reu_pump_start_refuses_a_host_rec_mode(self):
        from unittest import mock

        from c64cast.scenes.scenes import VideoScene

        audio = self._audio(use_reu_pump=True)
        scene = VideoScene(
            cast(Ultimate64API, FakeAPI()),
            audio,
            PETSCIIDisplayMode(use_reu_staged=True),
            "https://stub.invalid/clip.mp4",
            setup_progress=False,
        )
        with (
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource"),
            mock.patch.object(scene, "_preencode_audio_for_reu", return_value=b"\x07"),
            self.assertRaises(ValueError),
        ):
            scene.setup()
        audio.start_for_reu_staged.assert_not_called()


class ValidateUseReuStagedTest(unittest.TestCase):
    """The loader accepts only true/false/"auto" for [video].use_reu_staged."""

    def _load(self, value_literal):
        import tempfile

        from c64cast.app.config import load

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(f"[video]\nuse_reu_staged = {value_literal}\n")
            path = f.name
        return load(path)

    def test_auto_ok(self):
        self.assertEqual(self._load('"auto"').video.use_reu_staged, "auto")

    def test_bool_ok(self):
        self.assertTrue(self._load("true").video.use_reu_staged)
        self.assertFalse(self._load("false").video.use_reu_staged)

    def test_bad_string_rejected(self):
        with self.assertRaises(ValueError):
            self._load('"on"')


class ReuPetsciiPushTest(unittest.TestCase):
    """The opt-in push path must REUWRITE the screen, then trigger a single
    REU→main DMA into $0400. Color RAM stays on the regular delta path."""

    def _push(self, mode, screen_bytes, color_bytes):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        buffers = {
            "screen": np.frombuffer(screen_bytes, dtype=np.uint8),
            "color": np.frombuffer(color_bytes, dtype=np.uint8),
        }
        mode.push(api, buffers)
        return fake

    def test_default_path_uses_dmawrite_region(self):
        mode = PETSCIIDisplayMode(use_reu_staged=False)
        fake = self._push(mode, bytes(1000), bytes(1000))
        self.assertIn(SCREEN.RAM, fake.regions)
        self.assertEqual(fake.socket_dma.reuwrites, [])

    def test_reu_path_stages_screen_to_reu(self):
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        screen = bytes(range(256)) * 4  # 1024 bytes; first 1000 form screen
        screen = screen[:1000]
        fake = self._push(mode, screen, bytes(1000))
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_VIDEO_SCREEN_BASE)
        self.assertEqual(data, screen)
        self.assertEqual(len(data), REU_VIDEO_SCREEN_LEN)

    def test_reu_path_sets_destination_to_screen_ram(self):
        # write_regs stores the packed two-byte payload under the base key as a
        # tuple of byte values.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        key = f"{REU.C64_ADDR_LO:04X}"
        self.assertIn(key, fake.regs)
        self.assertEqual(fake.regs[key], (0x00, 0x04))  # $0400

    def test_reu_path_sets_source_offset(self):
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        key = f"{REU.REU_ADDR_LO:04X}"
        self.assertIn(key, fake.regs)
        # 24-bit REU_VIDEO_SCREEN_BASE = $E00000 → (0x00, 0x00, 0xE0)
        self.assertEqual(
            fake.regs[key],
            (
                REU_VIDEO_SCREEN_BASE & 0xFF,
                (REU_VIDEO_SCREEN_BASE >> 8) & 0xFF,
                (REU_VIDEO_SCREEN_BASE >> 16) & 0xFF,
            ),
        )

    def test_reu_path_sets_length_to_1000(self):
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        key = f"{REU.LENGTH_LO:04X}"
        self.assertIn(key, fake.regs)
        self.assertEqual(
            fake.regs[key], (REU_VIDEO_SCREEN_LEN & 0xFF, (REU_VIDEO_SCREEN_LEN >> 8) & 0xFF)
        )

    def test_reu_path_triggers_dma_with_fetch_exec(self):
        # The trigger byte at $DF01 must be $91: exec + FF00-off + REU→C64. A wrong
        # value silently runs the wrong direction, or not at all.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        key = f"{REU.COMMAND:04X}"
        self.assertIn(key, fake.memories)
        self.assertEqual(fake.memories[key], f"{REU.CMD_FETCH_EXEC:02X}")

    def test_reu_path_still_writes_color_via_dmawrite(self):
        # Color RAM at $D800 isn't VIC-banked, so it stays on write_region's
        # delta cache whatever the REU video flag says.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        color = bytes([5] * 1000)
        fake = self._push(mode, bytes(1000), color)
        self.assertIn(SCREEN.COLOR_RAM, fake.regions)
        self.assertEqual(fake.regions[SCREEN.COLOR_RAM], color)

    def test_reu_path_does_not_write_screen_via_region(self):
        # Doing both double-writes the screen and wastes a frame of bus-halt time.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        self.assertNotIn(SCREEN.RAM, fake.regions, "REU staged path must not also DMAWRITE $0400")


class ReuBlankPushTest(unittest.TestCase):
    """BlankDisplayMode shares the REU-staged path with PETSCIIDisplayMode.
    Re-verify the same wiring against the Blank mode's auto-composed buffers."""

    def test_reu_path_stages_blank_screen(self):
        mode = BlankDisplayMode(use_reu_staged=True)
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        buffers = mode.compose()
        mode.push(api, buffers)
        # Screen RAM is all SC_SPACE ($20).
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_VIDEO_SCREEN_BASE)
        self.assertTrue(all(b == SCREEN.SC_SPACE for b in data))
        self.assertIn(f"{REU.COMMAND:04X}", fake.memories)


class ReuCoexistenceTest(unittest.TestCase):
    """Sanity that validate_scene_cfg accepts every REU flag combination
    on video scenes. The earlier `_raises` variants are obsolete: the
    bank-swap install now picks a merged $C500 dispatcher whose non-raster
    branch JMPs to the audio pump at $C100, so both REU users share one
    $0314 hook and serialize REC access naturally."""

    def setUp(self):
        # The warning is logged once per display per process.
        from c64cast.app.scene_factory import _warn_host_rec_staging_dropped

        _warn_host_rec_staging_dropped.cache_clear()
        self.addCleanup(_warn_host_rec_staging_dropped.cache_clear)

    def test_video_alone_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_audio_alone_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = False
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_video_petscii_is_ok(self):
        # Accepted, but the char push drops to host DMA (#554): it would drive
        # the REC from the host while the pump drives it from the C64.
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        with self.assertLogs("c64cast.app.scene_factory", "WARNING"):
            validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_video_mhires_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="mhires", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_webcam_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="petscii")
        with self.assertLogs("c64cast.app.scene_factory", "WARNING"):
            validate_scene_cfg(sc, cfg, audio_enabled=True)


class ReuBuildDisplayModeTest(unittest.TestCase):
    """_build_display_mode must thread use_reu_staged through to PETSCII
    and Blank modes; other display modes silently ignore the flag (they
    don't have a REU-staged path yet)."""

    def test_petscii_receives_flag(self):
        m = _build_display_mode("petscii", use_reu_staged=True)
        assert isinstance(m, PETSCIIDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_blank_receives_flag(self):
        m = _build_display_mode("blank", use_reu_staged=True)
        assert isinstance(m, BlankDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_hires_receives_flag(self):
        m = _build_display_mode("hires", use_reu_staged=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_hires_edges_receives_flag(self):
        m = _build_display_mode("hires_edges", use_reu_staged=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_hires_default_off(self):
        # Never silently promote an existing hires config onto the experimental path.
        m = _build_display_mode("hires")
        assert isinstance(m, HiresDisplayMode)
        self.assertFalse(m.use_reu_staged)

    def test_mhires_receives_flag(self):
        m = _build_display_mode("mhires", use_reu_staged=True)
        assert isinstance(m, MultiHiresDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_mhires_default_off(self):
        m = _build_display_mode("mhires")
        assert isinstance(m, MultiHiresDisplayMode)
        self.assertFalse(m.use_reu_staged)


class ReuHiresTrackerLayoutTest(unittest.TestCase):
    """The hires tracker the host writes and the dispatcher snapshots.
    BankSwapDispatcherExecutionTest runs the handler against it."""

    def test_tracker_offsets_match_handler(self):
        # The dispatcher is assembled from these offsets, so drift moves both
        # sides together — but the ready flag has to stay the blob's last byte.
        self.assertEqual(TRACKER_OFF_BITMAP_REGS, 0)
        self.assertEqual(TRACKER_OFF_SCREEN_REGS, 7)
        self.assertEqual(TRACKER_OFF_RESERVED, 14)
        self.assertEqual(TRACKER_OFF_READY_FLAG, 15)
        self.assertEqual(FRAME_TRACKER_LEN, 16)


class ReuHiresSetupTest(unittest.TestCase):
    """HiresDisplayMode.setup with use_reu_staged must install the raster
    IRQ + zero both banks + pin $DD00 to bank 0. Matches the install
    sequence in [overlays/big_text.py]'s _install_raster_irq pattern."""

    def _setup(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = HiresDisplayMode(use_reu_staged=True)
        mode.setup(api)
        return fake, mode

    def test_setup_uploads_irq_handler(self):
        fake, _ = self._setup()
        key = f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(fake.mem_files[key], BANK_SWAP_IRQ_HANDLER)

    def test_setup_zeroes_both_banks(self):
        # The first swap brings up the off-screen bank, so post-reset garbage there
        # shows as a frame of noise before the first REU→main DMA lands.
        fake, _ = self._setup()
        for addr in (VIC_BANK_0.BITMAP, VIC_BANK_2.BITMAP):
            key = f"{addr:04X}"
            self.assertIn(key, fake.mem_files)
            self.assertEqual(len(fake.mem_files[key]), REU_VIDEO_BITMAP_LEN)
            self.assertTrue(all(b == 0 for b in fake.mem_files[key]))
        for addr in (VIC_BANK_0.SCREEN, VIC_BANK_2.SCREEN):
            key = f"{addr:04X}"
            self.assertIn(key, fake.mem_files)
            self.assertEqual(len(fake.mem_files[key]), REU_VIDEO_BITMAP_SCREEN_LEN)

    def test_setup_zeroes_frame_tracker(self):
        # The ready flag must start at 0 so the first raster IRQ after install skips
        # the DMA path until the host stages a real frame.
        fake, _ = self._setup()
        key = f"{FRAME_TRACKER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(len(fake.mem_files[key]), FRAME_TRACKER_LEN)
        self.assertTrue(all(b == 0 for b in fake.mem_files[key]))

    def test_setup_pins_dd00_to_bank0(self):
        fake, _ = self._setup()
        self.assertEqual(fake.memories[f"{CIA2.PORT_A:04X}"], f"{CIA2.PORT_A_BANK_0:02X}")

    def test_setup_hooks_irq_vector(self):
        fake, _ = self._setup()
        self.assertIn(f"{VECTORS.IRQ:04X}", fake.regs)
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF),
        )

    def test_setup_programs_raster_line(self):
        # Raster compare at line 251 ($FB) is below the picture on both PAL and NTSC, so
        # the $DD00 swap is tear-free.
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D012"], "FB")

    def test_setup_enables_raster_irq(self):
        # $D01A = $01 enables raster as the only VIC IRQ source.
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D01A"], "01")

    def test_setup_seeds_the_dispatcher_with_bank_0_on_screen(self):
        # Setup pins $DD00 to bank 0, and the dispatcher flips from its own
        # copy of that: a stale one would aim the first copy at the screen.
        fake, _ = self._setup()
        key = f"{modes_irq.BANK_SWAP_STATE_ADDR:04X}"
        self.assertEqual(fake.mem_files[key], modes_irq.BANK_SWAP_STATE_INIT)
        self.assertEqual(fake.mem_files[key][0], CIA2.PORT_A_BANK_0)

    def test_setup_off_path_does_not_install_irq(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = HiresDisplayMode(use_reu_staged=False)
        mode.setup(api)
        self.assertNotIn(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", fake.mem_files)
        self.assertNotIn(f"{VECTORS.IRQ:04X}", fake.regs)
        self.assertNotIn("D012", fake.memories)


class ReuHiresTeardownTest(unittest.TestCase):
    """teardown() must reverse install(): mask sources, restore vector,
    restore bank to 0, re-enable CIA #1. A teardown that leaves the IRQ
    hooked would vector the next scene's kernal IRQ into a stale
    handler at $C500 (likely garbage by then)."""

    def _setup_then_teardown(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = HiresDisplayMode(use_reu_staged=True)
        mode.setup(api)
        mode.teardown(api)
        return fake

    def test_teardown_restores_irq_vector_to_kernal(self):
        fake = self._setup_then_teardown()
        # FakeAPI.regs records the LAST write under a key, so the kernal value
        # winning is what proves teardown ran after setup's hook.
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF),
        )

    def test_teardown_restores_dd00_to_bank0(self):
        fake = self._setup_then_teardown()
        # The next scene's setup expects $DD00 = bank 0. The mode is fresh here, so
        # the post-setup value is already bank 0 and the teardown write is idempotent.
        self.assertEqual(fake.memories[f"{CIA2.PORT_A:04X}"], f"{CIA2.PORT_A_BANK_0:02X}")

    def test_teardown_disables_vic_raster_irq(self):
        fake = self._setup_then_teardown()
        # Setup wrote $D01A = $01; teardown must write $00 last.
        self.assertEqual(fake.memories["D01A"], "00")

    def test_teardown_off_path_is_noop(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = HiresDisplayMode(use_reu_staged=False)
        # No setup() call — this is the non-REU teardown, not teardown-without-setup.
        prior_regs = dict(fake.regs)
        prior_mem = dict(fake.memories)
        mode.teardown(api)
        self.assertEqual(fake.regs, prior_regs)
        self.assertEqual(fake.memories, prior_mem)


class ReuHiresPushTest(unittest.TestCase):
    """Per-frame render() in REU-staged mode must REUWRITE bitmap + screen
    into staging, then DMAWRITE one 16-byte frame tracker to $C700. The
    C64-side IRQ handler does the rest (REU→main triggers + bank swap).
    Target bank alternates each frame."""

    def _render(self, mode, frame):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode.render(api, frame)
        return fake

    def _frame(self):
        # Solid-color frame; render quantizes, but the byte values aren't under test.
        return np.zeros((200, 320, 3), dtype=np.uint8)

    def _tracker(self, fake):
        key = f"{FRAME_TRACKER_ADDR:04X}"
        self.assertIn(key, fake.mem_files, "render() must write the frame tracker at $C700")
        blob = fake.mem_files[key]
        self.assertEqual(len(blob), FRAME_TRACKER_LEN)
        return blob

    def test_frames_rotate_through_the_staging_slots(self):
        # The C64 may still be copying the previous frame's slot, so each
        # frame goes into the next one and the tracker names it.
        mode = HiresDisplayMode(use_reu_staged=True)
        for frame in range(REU_VIDEO_SLOTS + 1):
            offset = (frame % REU_VIDEO_SLOTS) * REU_VIDEO_SLOT_STRIDE
            fake = self._render(mode, self._frame())
            blob = self._tracker(fake)
            src = blob[TRACKER_OFF_BITMAP_REGS + 2 : TRACKER_OFF_BITMAP_REGS + 5]
            self.assertEqual(int.from_bytes(src, "little"), REU_VIDEO_BITMAP_BASE + offset)
            src = blob[TRACKER_OFF_SCREEN_REGS + 2 : TRACKER_OFF_SCREEN_REGS + 5]
            self.assertEqual(int.from_bytes(src, "little"), REU_VIDEO_BITMAP_SCREEN_BASE + offset)
            self.assertEqual(
                {off for off, _ in fake.socket_dma.reuwrites},
                {REU_VIDEO_BITMAP_BASE + offset, REU_VIDEO_BITMAP_SCREEN_BASE + offset},
            )

    def test_tracker_destinations_are_bank_0s(self):
        # The dispatcher re-aims them at whichever bank is hidden.
        mode = HiresDisplayMode(use_reu_staged=True)
        blob = self._tracker(self._render(mode, self._frame()))
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS : TRACKER_OFF_BITMAP_REGS + 2], b"\x00\x20")
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS : TRACKER_OFF_SCREEN_REGS + 2], b"\x00\x04")
        self.assertEqual(blob[TRACKER_OFF_RESERVED], 0)

    def test_tracker_carries_reu_src_and_length_for_both_dmas(self):
        # The IRQ handler copies bitmap regs to $DF02-$DF08 and triggers, then the
        # screen regs and triggers, so the tracker carries src + length for both.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        # Bitmap REU source = $E10000, length = 8000.
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 2], REU_VIDEO_BITMAP_BASE & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 3], (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 4], (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 5], REU_VIDEO_BITMAP_LEN & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 6], (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF)
        # Screen REU source = $E12000, length = 1000.
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS + 2], REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF)
        self.assertEqual(
            blob[TRACKER_OFF_SCREEN_REGS + 3], (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF
        )
        self.assertEqual(
            blob[TRACKER_OFF_SCREEN_REGS + 4], (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF
        )
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS + 5], REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF)
        self.assertEqual(
            blob[TRACKER_OFF_SCREEN_REGS + 6], (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF
        )

    def test_tracker_ready_flag_is_last_byte(self):
        # The ready flag must be the LAST byte: the whole 16-byte blob lands
        # atomically via the socket FIFO, so the IRQ sees all-new regs+ready=1 or
        # all-old, never ready=1 with stale regs.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(TRACKER_OFF_READY_FLAG, FRAME_TRACKER_LEN - 1)
        self.assertEqual(blob[TRACKER_OFF_READY_FLAG], 0x01)

    def test_reuwrite_stages_bitmap_and_screen(self):
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        offs = {off for off, _ in fake.socket_dma.reuwrites}
        self.assertIn(REU_VIDEO_BITMAP_BASE, offs)
        self.assertIn(REU_VIDEO_BITMAP_SCREEN_BASE, offs)
        for off, data in fake.socket_dma.reuwrites:
            if off == REU_VIDEO_BITMAP_BASE:
                self.assertEqual(len(data), REU_VIDEO_BITMAP_LEN)
            elif off == REU_VIDEO_BITMAP_SCREEN_BASE:
                self.assertEqual(len(data), REU_VIDEO_BITMAP_SCREEN_LEN)

    def test_render_does_not_host_trigger_reu_dma(self):
        # A host-side $DF01 / $DF02-$DF08 write would race the C64 IRQ and add
        # Python-paced jitter, defeating the deterministic-vblank win.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(
            f"{REU.COMMAND:04X}", fake.memories, "host must not trigger REU DMA — C64 IRQ does it"
        )
        self.assertNotIn(
            f"{REU.C64_ADDR_LO:04X}", fake.regs, "host must not stage REU regs — they go in tracker"
        )

    def test_render_does_not_dmawrite_displayed_bank(self):
        # A write_region to $2000 / $0400 would tear the displayed frame.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(0x2000, fake.regions, "REU-staged hires must not DMAWRITE bank 0 bitmap")
        self.assertNotIn(0x0400, fake.regions, "REU-staged hires must not DMAWRITE bank 0 screen")

    def test_off_path_still_dmawrites_directly(self):
        mode = HiresDisplayMode(use_reu_staged=False)
        fake = self._render(mode, self._frame())
        self.assertIn(0x2000, fake.regions)
        self.assertIn(0x0400, fake.regions)
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertNotIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)


class ReuHiresWebcamCoexistenceTest(unittest.TestCase):
    """Bank-swap raster IRQ + REU mic pump on the same webcam scene used
    to be rejected (both wanted to own $0314). With the merged dispatcher
    they coexist: the bank-swap install at $C500 uses the +AUDIO handler
    variant whose non-raster branch JMPs to the mic pump at $C100, and
    the mic install is told to skip its own $0314 hook (scenes.py)."""

    def setUp(self):
        # The warning is logged once per display per process.
        from c64cast.app.scene_factory import _warn_host_rec_staging_dropped

        _warn_host_rec_staging_dropped.cache_clear()
        self.addCleanup(_warn_host_rec_staging_dropped.cache_clear)

    def test_webcam_hires_both_on_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="hires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_hires_edges_both_on_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="hires_edges")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_petscii_both_on_ok(self):
        # Accepted, with the char push on host DMA while the mic pump runs (#554).
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="petscii")
        with self.assertLogs("c64cast.app.scene_factory", "WARNING"):
            validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_blank_hires_edges_both_on_ok(self):
        # Blank scenes accept display = "hires_edges" but always build
        # BlankDisplayMode, whose REU push drops to host DMA under the pump (#554).
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="blank", display="hires_edges")
        with self.assertLogs("c64cast.app.scene_factory", "WARNING"):
            validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_hires_audio_off_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="webcam", display="hires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_mhires_both_on_is_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="mhires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_mhires_audio_off_ok(self):
        from c64cast.app.config import SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="webcam", display="mhires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)


class ReuMHiresTrackerLayoutTest(unittest.TestCase):
    """The mhires tracker the host writes and the dispatcher snapshots."""

    def test_tracker_offsets_match_handler(self):
        # 24-byte tracker; bitmap=$C700, screen=$C707, color=$C70E, bg0=$C715,
        # reserved=$C716, ready=$C717.
        self.assertEqual(MHIRES_TRACKER_OFF_BITMAP_REGS, 0)
        self.assertEqual(MHIRES_TRACKER_OFF_SCREEN_REGS, 7)
        self.assertEqual(MHIRES_TRACKER_OFF_COLOR_REGS, 14)
        self.assertEqual(MHIRES_TRACKER_OFF_BG0, 21)
        self.assertEqual(MHIRES_TRACKER_OFF_RESERVED, 22)
        self.assertEqual(MHIRES_TRACKER_OFF_READY_FLAG, 23)
        self.assertEqual(MHIRES_FRAME_TRACKER_LEN, 24)
        # Ready flag must be the LAST byte so the atomic DMAWRITE arrives
        # all-or-nothing — IRQ can't see ready=1 with stale regs.
        self.assertEqual(MHIRES_TRACKER_OFF_READY_FLAG, MHIRES_FRAME_TRACKER_LEN - 1)


class ReuMHiresSetupTest(unittest.TestCase):
    """MultiHiresDisplayMode.setup with use_reu_staged must install the
    mhires raster IRQ + zero both banks + pin $DD00 to bank 0. Parallel
    to ReuHiresSetupTest."""

    def _setup(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        mode.setup(api)
        return fake, mode

    def test_setup_uploads_mhires_irq_handler(self):
        # The 83-byte mhires handler and the 61-byte hires one share $C500. A mix-up
        # either skips the color DMA + bg0, or runs off the end of the shorter
        # handler into uninitialized tracker bytes.
        fake, _ = self._setup()
        key = f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(fake.mem_files[key], MHIRES_BANK_SWAP_IRQ_HANDLER)
        self.assertNotEqual(fake.mem_files[key], BANK_SWAP_IRQ_HANDLER)

    def test_setup_zeroes_both_banks_bitmap_and_screen(self):
        fake, _ = self._setup()
        for addr in (VIC_BANK_0.BITMAP, VIC_BANK_2.BITMAP):
            key = f"{addr:04X}"
            self.assertIn(key, fake.mem_files)
            self.assertEqual(len(fake.mem_files[key]), REU_VIDEO_BITMAP_LEN)
            self.assertTrue(all(b == 0 for b in fake.mem_files[key]))
        for addr in (VIC_BANK_0.SCREEN, VIC_BANK_2.SCREEN):
            key = f"{addr:04X}"
            self.assertIn(key, fake.mem_files)
            self.assertEqual(len(fake.mem_files[key]), REU_VIDEO_BITMAP_SCREEN_LEN)

    def test_setup_zeroes_24_byte_frame_tracker(self):
        # The mhires tracker is 24 bytes to hires's 16, and its ready flag must start
        # at 0 so the first IRQ skips until the host stages a real frame.
        fake, _ = self._setup()
        key = f"{FRAME_TRACKER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(len(fake.mem_files[key]), MHIRES_FRAME_TRACKER_LEN)
        self.assertTrue(all(b == 0 for b in fake.mem_files[key]))

    def test_setup_pins_dd00_to_bank0(self):
        fake, _ = self._setup()
        self.assertEqual(fake.memories[f"{CIA2.PORT_A:04X}"], f"{CIA2.PORT_A_BANK_0:02X}")

    def test_setup_hooks_irq_vector(self):
        fake, _ = self._setup()
        self.assertIn(f"{VECTORS.IRQ:04X}", fake.regs)
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF),
        )

    def test_setup_programs_raster_line(self):
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D012"], "FB")

    def test_setup_enables_raster_irq(self):
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D01A"], "01")

    def test_setup_seeds_the_dispatcher_with_bank_0_on_screen(self):
        # Setup pins $DD00 to bank 0, and the dispatcher flips from its own
        # copy of that: a stale one would aim the first copy at the screen.
        fake, _ = self._setup()
        key = f"{modes_irq.BANK_SWAP_STATE_ADDR:04X}"
        self.assertEqual(fake.mem_files[key], modes_irq.BANK_SWAP_STATE_INIT)
        self.assertEqual(fake.mem_files[key][0], CIA2.PORT_A_BANK_0)

    def test_setup_off_path_does_not_install_irq(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = MultiHiresDisplayMode(use_reu_staged=False)
        mode.setup(api)
        self.assertNotIn(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", fake.mem_files)
        self.assertNotIn(f"{VECTORS.IRQ:04X}", fake.regs)
        self.assertNotIn("D012", fake.memories)


class ReuMHiresTeardownTest(unittest.TestCase):
    """teardown() shares modes_irq.uninstall_bank_swap_irq with hires; verify the
    same reverse-of-install behavior fires for mhires."""

    def _setup_then_teardown(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        mode.setup(api)
        mode.teardown(api)
        return fake

    def test_teardown_restores_irq_vector_to_kernal(self):
        fake = self._setup_then_teardown()
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF),
        )

    def test_teardown_disables_vic_raster_irq(self):
        fake = self._setup_then_teardown()
        self.assertEqual(fake.memories["D01A"], "00")

    def test_teardown_off_path_is_noop(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = MultiHiresDisplayMode(use_reu_staged=False)
        prior_regs = dict(fake.regs)
        prior_mem = dict(fake.memories)
        mode.teardown(api)
        self.assertEqual(fake.regs, prior_regs)
        self.assertEqual(fake.memories, prior_mem)


class BankSwapIrqTeardownGuardTest(unittest.TestCase):
    """Every write in `uninstall_bank_swap_irq` is guarded on its own so one
    link hiccup cannot starve the rest — except the CIA #1 unmask, which is
    not a free-standing promise. Re-arming Timer A while `$0314` still points
    at the `$C500` handler hands every jiffy IRQ to RAM the next scene
    overwrites, and (because `$D019`'s raster flag latches regardless of
    `$D01A`) re-flips `$DD00` to bank 2 on the next frame."""

    _CIA1_ICR = f"{CIA1.ICR:04X}"

    def test_a_failed_vector_restore_leaves_cia1_masked(self):
        fake = FakeAPI()

        def link_down(*args, **kwargs):
            raise RuntimeError("DMA link down")

        fake.write_regs = link_down  # type: ignore[method-assign]
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            uninstall_bank_swap_irq(cast(Ultimate64API, fake))
        self.assertEqual(
            fake.memories[self._CIA1_ICR],
            f"{modes_irq._CIA1_ICR_DISABLE_TIMER_A:02X}",
            "Timer A must stay masked while $0314 is still on the in-RAM handler",
        )

    def test_a_failed_vector_restore_does_not_starve_the_other_writes(self):
        fake = FakeAPI()

        def link_down(*args, **kwargs):
            raise RuntimeError("DMA link down")

        fake.write_regs = link_down  # type: ignore[method-assign]
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            uninstall_bank_swap_irq(cast(Ultimate64API, fake))
        self.assertEqual(fake.memories["D01A"], "00")
        self.assertEqual(fake.memories["D019"], "01")
        self.assertEqual(fake.memories[f"{CIA2.PORT_A:04X}"], f"{CIA2.PORT_A_BANK_0:02X}")

    def test_a_clean_teardown_unmasks_cia1(self):
        fake = FakeAPI()
        uninstall_bank_swap_irq(cast(Ultimate64API, fake))
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF),
        )
        self.assertEqual(fake.memories[self._CIA1_ICR], f"{modes_irq._CIA1_ICR_ENABLE_TIMER_A:02X}")

    def test_an_in_flight_copy_drains_before_the_handler_is_released(self):
        fake = FakeAPI()
        order: list[str] = []
        write_memory, write_regs = fake.write_memory, fake.write_regs

        def logged_write_memory(address, *args, **kwargs):
            order.append(address.upper())
            return write_memory(address, *args, **kwargs)

        def logged_write_regs(address, *args, **kwargs):
            order.append(address.upper())
            return write_regs(address, *args, **kwargs)

        fake.write_memory = logged_write_memory  # type: ignore[method-assign]
        fake.write_regs = logged_write_regs  # type: ignore[method-assign]
        with mock.patch.object(modes_irq, "time") as clock:
            clock.sleep.side_effect = lambda s: order.append(f"sleep {s}")
            uninstall_bank_swap_irq(cast(Ultimate64API, fake))
        drain = order.index(f"sleep {modes_irq._REU_SLOT_MAX_IN_USE_S}")
        self.assertLess(order.index("D01A"), drain, "sources masked before the wait")
        self.assertLess(order.index(self._CIA1_ICR), drain, "sources masked before the wait")
        self.assertLess(drain, order.index(f"{VECTORS.IRQ:04X}"), "vector held until drained")
        self.assertLess(drain, order.index(f"{CIA2.PORT_A:04X}"), "bank held until drained")

    def _teardown_sleeps(self, mode) -> list[float]:
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode.setup(api)
        with mock.patch.object(modes_irq, "time") as clock:
            mode.teardown(api)
        return [call.args[0] for call in clock.sleep.call_args_list]

    def test_a_reu_staged_mode_drains_its_dispatcher(self):
        for mode in (
            HiresDisplayMode(use_reu_staged=True),
            MultiHiresDisplayMode(use_reu_staged=True),
        ):
            with self.subTest(mode=type(mode).__name__):
                self.assertEqual(self._teardown_sleeps(mode), [modes_irq._REU_SLOT_MAX_IN_USE_S])

    def test_a_host_dma_or_flicker_page_flip_does_not_wait_for_a_copy(self):
        for mode in (
            HiresDisplayMode(double_buffer=True),
            MultiHiresDisplayMode(double_buffer=True),
            HiresDisplayMode(flicker_tolerance="clean"),
            MultiHiresDisplayMode(flicker_tolerance="clean"),
        ):
            with self.subTest(mode=type(mode).__name__):
                self.assertEqual(self._teardown_sleeps(mode), [])


class ReuMHiresPushTest(unittest.TestCase):
    """Per-frame render() in REU-staged mhires mode must REUWRITE bitmap +
    screen + color into staging, then DMAWRITE a 24-byte tracker to $C700.
    Target bank alternates each frame, just like hires."""

    def _render(self, mode, frame):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode.render(api, frame)
        return fake

    def _frame(self):
        return np.zeros((200, 320, 3), dtype=np.uint8)

    def _tracker(self, fake):
        key = f"{FRAME_TRACKER_ADDR:04X}"
        self.assertIn(key, fake.mem_files, "render() must write the frame tracker at $C700")
        blob = fake.mem_files[key]
        self.assertEqual(len(blob), MHIRES_FRAME_TRACKER_LEN)
        return blob

    def test_frames_rotate_through_the_staging_slots(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        for frame in range(REU_VIDEO_SLOTS + 1):
            offset = (frame % REU_VIDEO_SLOTS) * REU_VIDEO_SLOT_STRIDE
            fake = self._render(mode, self._frame())
            blob = self._tracker(fake)
            for off, base in (
                (MHIRES_TRACKER_OFF_BITMAP_REGS, REU_VIDEO_BITMAP_BASE),
                (MHIRES_TRACKER_OFF_SCREEN_REGS, REU_VIDEO_BITMAP_SCREEN_BASE),
                (MHIRES_TRACKER_OFF_COLOR_REGS, REU_VIDEO_BITMAP_COLOR_BASE),
            ):
                src = int.from_bytes(blob[off + 2 : off + 5], "little")
                self.assertEqual(src, base + offset)
            self.assertEqual(
                {off for off, _ in fake.socket_dma.reuwrites},
                {
                    REU_VIDEO_BITMAP_BASE + offset,
                    REU_VIDEO_BITMAP_SCREEN_BASE + offset,
                    REU_VIDEO_BITMAP_COLOR_BASE + offset,
                },
            )

    def test_tracker_destinations_are_bank_0s(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        blob = self._tracker(self._render(mode, self._frame()))
        regs = MHIRES_TRACKER_OFF_BITMAP_REGS
        self.assertEqual(blob[regs : regs + 2], b"\x00\x20")
        regs = MHIRES_TRACKER_OFF_SCREEN_REGS
        self.assertEqual(blob[regs : regs + 2], b"\x00\x04")
        self.assertEqual(blob[MHIRES_TRACKER_OFF_RESERVED], 0)

    def test_color_regs_target_d800_regardless_of_bank(self):
        # $D800 isn't VIC-banked, so every frame's color DMA hits the same
        # color RAM, and the dispatcher copies it un-aimed.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 0], 0x00)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 1], 0xD8)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 0], 0x00)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 1], 0xD8)

    def test_tracker_carries_reu_src_and_length_for_all_three_dmas(self):
        # Bitmap: $E10000 / 8000.  Screen: $E12000 / 1000.  Color: $E13000 / 1000.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 2], REU_VIDEO_BITMAP_BASE & 0xFF)
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 3], (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 4], (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF
        )
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 5], REU_VIDEO_BITMAP_LEN & 0xFF)
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 6], (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 2], REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 3], (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 4], (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 5], REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 6], (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_COLOR_REGS + 2], REU_VIDEO_BITMAP_COLOR_BASE & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_COLOR_REGS + 3], (REU_VIDEO_BITMAP_COLOR_BASE >> 8) & 0xFF
        )
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_COLOR_REGS + 4], (REU_VIDEO_BITMAP_COLOR_BASE >> 16) & 0xFF
        )
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 5], REU_VIDEO_BITMAP_COLOR_LEN & 0xFF)
        self.assertEqual(
            blob[MHIRES_TRACKER_OFF_COLOR_REGS + 6], (REU_VIDEO_BITMAP_COLOR_LEN >> 8) & 0xFF
        )

    def test_tracker_carries_bg0_byte(self):
        # bg0 is a palette index 0..15 that the handler writes to $D021 each frame.
        # Predicting its exact value means re-running quantization, so pin the range.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        bg0 = blob[MHIRES_TRACKER_OFF_BG0]
        self.assertGreaterEqual(bg0, 0)
        self.assertLessEqual(bg0, 15)

    def test_tracker_ready_flag_is_last_byte(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_READY_FLAG], 0x01)

    def test_reuwrite_stages_bitmap_screen_and_color(self):
        # Three REUWRITEs per frame: bitmap (8000B), screen (1000B), color (1000B).
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        sizes = {off: len(data) for off, data in fake.socket_dma.reuwrites}
        self.assertIn(REU_VIDEO_BITMAP_BASE, sizes)
        self.assertIn(REU_VIDEO_BITMAP_SCREEN_BASE, sizes)
        self.assertIn(REU_VIDEO_BITMAP_COLOR_BASE, sizes)
        self.assertEqual(sizes[REU_VIDEO_BITMAP_BASE], REU_VIDEO_BITMAP_LEN)
        self.assertEqual(sizes[REU_VIDEO_BITMAP_SCREEN_BASE], REU_VIDEO_BITMAP_SCREEN_LEN)
        self.assertEqual(sizes[REU_VIDEO_BITMAP_COLOR_BASE], REU_VIDEO_BITMAP_COLOR_LEN)

    def test_render_does_not_host_trigger_reu_dma(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(
            f"{REU.COMMAND:04X}", fake.memories, "host must not trigger REU DMA — C64 IRQ does it"
        )
        self.assertNotIn(
            f"{REU.C64_ADDR_LO:04X}", fake.regs, "host must not stage REU regs — they go in tracker"
        )

    def test_render_does_not_dmawrite_displayed_bank(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(0x2000, fake.regions, "REU-staged mhires must not DMAWRITE bank 0 bitmap")
        self.assertNotIn(0x0400, fake.regions, "REU-staged mhires must not DMAWRITE bank 0 screen")
        self.assertNotIn(0xD800, fake.regions, "REU-staged mhires must not DMAWRITE color RAM")
        self.assertNotIn(
            "D021",
            fake.regs,
            "REU-staged mhires must not host-write bg0 — "
            "the IRQ handler writes it from the tracker",
        )

    def test_off_path_still_dmawrites_directly(self):
        mode = MultiHiresDisplayMode(use_reu_staged=False)
        fake = self._render(mode, self._frame())
        self.assertIn(0x2000, fake.regions)
        self.assertIn(0x0400, fake.regions)
        self.assertIn(0xD800, fake.regions)
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertNotIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)

    def test_global_palette_mode_also_uses_reu(self):
        # palette_mode "cheap" takes _render_global, a separate code path from the
        # default "percell" _render_percell; both must honor use_reu_staged.
        mode = MultiHiresDisplayMode(palette_mode="cheap", use_reu_staged=True)
        fake = self._render(mode, self._frame())
        offs = {off for off, _ in fake.socket_dma.reuwrites}
        self.assertIn(REU_VIDEO_BITMAP_BASE, offs)
        self.assertIn(REU_VIDEO_BITMAP_SCREEN_BASE, offs)
        self.assertIn(REU_VIDEO_BITMAP_COLOR_BASE, offs)
        self.assertIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)


class ReuMHiresFlagDefaultTest(unittest.TestCase):
    """Same default-off invariant as hires: the experimental flag must not
    silently change existing mhires users' behavior."""

    def test_mhires_default(self):
        self.assertFalse(MultiHiresDisplayMode().use_reu_staged)


class MergedDispatcherSetupTest(unittest.TestCase):
    """Setup with audio_reu_pump_active=True must:
    (1) write the MERGED handler bytes (not the plain bank-swap) to $C500
    (2) pre-upload the AUDIO_HANDLER_STUB to $C100 BEFORE hooking $0314
        — so the gap between this install completing and audio.start
        writing real bytes doesn't vector into uninitialized RAM."""

    def test_hires_uses_chunked_merged_handler_when_audio_active(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        handler = fake.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"]
        self.assertEqual(handler, BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER)

    def test_mhires_uses_chunked_merged_handler_when_audio_active(self):
        # mhires + REU audio uses the merged variant, whose non-raster branch
        # runs the pump at $C100.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        handler = fake.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"]
        self.assertEqual(handler, MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER)

    def test_hires_uses_plain_handler_when_audio_inactive(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=False)
        m.setup(api)
        handler = fake.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"]
        self.assertEqual(handler, BANK_SWAP_IRQ_HANDLER)
        self.assertNotIn(f"{AUDIO_HANDLER_INSTALL_ADDR:04X}", fake.mem_files)

    def test_mhires_uses_plain_handler_when_audio_inactive(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=False)
        m.setup(api)
        handler = fake.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"]
        self.assertEqual(handler, MHIRES_BANK_SWAP_IRQ_HANDLER)
        self.assertNotIn(f"{AUDIO_HANDLER_INSTALL_ADDR:04X}", fake.mem_files)

    def test_audio_stub_uploaded_when_audio_active_hires(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        stub = fake.mem_files[f"{AUDIO_HANDLER_INSTALL_ADDR:04X}"]
        self.assertEqual(stub, AUDIO_HANDLER_STUB)

    def test_audio_stub_uploaded_when_audio_active_mhires(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        stub = fake.mem_files[f"{AUDIO_HANDLER_INSTALL_ADDR:04X}"]
        self.assertEqual(stub, AUDIO_HANDLER_STUB)

    def test_audio_stub_uploaded_before_irq_vector_hook(self):
        # $0314 must not be patched until the stub is in place at $C100. FakeAPI.ops
        # records every write_memory_file / write_regs call in sequence.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        stub_addr = f"{AUDIO_HANDLER_INSTALL_ADDR:04X}".lower()
        vec_addr = f"{VECTORS.IRQ:04X}".lower()
        stub_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_memory_file" and op[1].lower() == stub_addr
        )
        vec_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_regs" and op[1].lower() == vec_addr
        )
        self.assertLess(stub_idx, vec_idx)

    def test_pump_body_stub_uploaded_before_irq_vector_hook_mhires(self):
        # #551: the chunked dispatchers JSR $C180 themselves, so an RTS has
        # to be there before $0314 is hooked — otherwise the first CIA #1 tick
        # that latches during a REC family calls power-on RAM or a previous
        # scene's pump body.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        body_key = f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}"
        self.assertEqual(fake.mem_files[body_key], PUMP_BODY_STUB)
        self.assertEqual(PUMP_BODY_STUB, bytes([0x60]))
        body_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_memory_file" and op[1].upper() == body_key
        )
        vec_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_regs" and op[1].upper() == f"{VECTORS.IRQ:04X}"
        )
        self.assertLess(body_idx, vec_idx)

    def test_pump_body_stub_not_uploaded_when_audio_inactive(self):
        # Without the merged dispatcher nothing JSRs $C180, and whatever an
        # audio path put there is not this installer's to overwrite.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        MultiHiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=False).setup(api)
        self.assertNotIn(f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", fake.mem_files)


class MergedDispatcherFlagWiringTest(unittest.TestCase):
    """The audio_reu_pump_active flag must reach the display mode whenever
    both REU flags are set in the Config, on both webcam and video
    scene types. Verified through the _build_display_mode entry point."""

    def test_hires_receives_audio_flag(self):
        m = _build_display_mode("hires", use_reu_staged=True, audio_reu_pump_active=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertTrue(m.audio_reu_pump_active)

    def test_hires_edges_receives_audio_flag(self):
        m = _build_display_mode("hires_edges", use_reu_staged=True, audio_reu_pump_active=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertTrue(m.audio_reu_pump_active)

    def test_mhires_receives_audio_flag(self):
        m = _build_display_mode("mhires", use_reu_staged=True, audio_reu_pump_active=True)
        assert isinstance(m, MultiHiresDisplayMode)
        self.assertTrue(m.audio_reu_pump_active)

    def test_audio_flag_default_off(self):
        # Don't silently promote existing configs onto the merged dispatcher path.
        m = _build_display_mode("hires", use_reu_staged=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertFalse(m.audio_reu_pump_active)


class BankSwapDispatcherExecutionTest(unittest.TestCase):
    """Runs every REU bank-swap dispatcher on py65, one IRQ at a time, over
    one persistent machine. The REU controller is modeled at $DF01: each
    trigger records the transfer the REC registers describe, then advances
    the C64 address and zeroes the length, as the REU does. Byte-offset pins
    cannot see a flip outside vblank, a copy into the bank on screen, or a
    commit that reads a newer frame's colors."""

    # Distinct REU sources per family, so a family that re-used another's
    # registers, or a newer frame's, shows up in the transfer log.
    SRC_BANK = 0xE1
    PUMP_CALLS = 0x02A7  # where the clobbering pump-body stub counts its calls
    IN_WINDOW = 255  # below the picture on both systems
    OUT_OF_WINDOW = 100  # mid-picture

    class Machine:
        def __init__(self, test, handler, *, mhires):
            from py65.memory import ObservableMemory

            self.test = test
            self.mhires = mhires
            self.mem = ObservableMemory()
            for i, b in enumerate(handler):
                self.mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
            # The pump body stub: like the real body it rewrites $DF02-$DF06
            # with its own addresses, then counts the call and returns.
            body = [0xA9, 0xEE]  # LDA #$EE
            for reg in range(REU.C64_ADDR_LO, REU.REU_ADDR_HI + 1):
                body += [0x8D, reg & 0xFF, reg >> 8]  # STA reg
            body += [0xEE, test.PUMP_CALLS & 0xFF, test.PUMP_CALLS >> 8, 0x60]
            for i, b in enumerate(body):
                self.mem[REU_PUMP_BODY_SUBROUTINE_ADDR + i] = b
            for i, b in enumerate(modes_irq.BANK_SWAP_STATE_INIT):
                self.mem[modes_irq.BANK_SWAP_STATE_ADDR + i] = b
            # (c64 dest, length) per transfer, plus every $DD00/$D021 write,
            # all in one ordered log.
            self.log: list[tuple[str, int, int]] = []
            # The REC registers as the handler last wrote them (the REU's own
            # advance applied), kept apart from py65's untyped memory.
            self.rec: dict[int, int] = {}
            self.mem.subscribe_to_write(range(REU.C64_ADDR_LO, REU.LENGTH_HI + 1), self._store)
            self.mem.subscribe_to_write([REU.COMMAND], self._trigger)
            self.mem.subscribe_to_write([CIA2.PORT_A], self._register("dd00"))
            self.mem.subscribe_to_write([0xD021], self._register("d021"))

        def _store(self, address, value):
            self.rec[address] = value

        def _register(self, name):
            def write(address, value):
                self.log.append((name, value, 0))

            return write

        def _trigger(self, address, value):
            rec = self.rec
            dst = rec[REU.C64_ADDR_LO] | (rec[REU.C64_ADDR_HI] << 8)
            src = rec[REU.REU_ADDR_LO] | (rec[REU.REU_ADDR_MI] << 8) | (rec[REU.REU_ADDR_HI] << 16)
            length = rec[REU.LENGTH_LO] | (rec[REU.LENGTH_HI] << 8)
            self.test.assertEqual(value, REU.CMD_FETCH_EXEC)
            self.test.assertEqual(
                rec[REU.REU_ADDR_HI], self.test.SRC_BANK, "a chunk fired from the pump's registers"
            )
            self.log.append(("rec", dst, length))
            self.sources.append(src)
            end, src_end = dst + length, src + length
            rec[REU.C64_ADDR_LO], rec[REU.C64_ADDR_HI] = end & 0xFF, (end >> 8) & 0xFF
            rec[REU.REU_ADDR_LO], rec[REU.REU_ADDR_MI] = src_end & 0xFF, (src_end >> 8) & 0xFF
            rec[REU.LENGTH_LO] = rec[REU.LENGTH_HI] = 0

        sources: list[int]

        def stage(self, slot, *, bg0=0):
            """Write a tracker as the host does: one blob, ready flag last."""
            if self.mhires:
                blob = bytearray(MHIRES_FRAME_TRACKER_LEN)
                regs = (
                    (MHIRES_TRACKER_OFF_BITMAP_REGS, VIC_BANK_0.BITMAP, REU_VIDEO_BITMAP_LEN),
                    (
                        MHIRES_TRACKER_OFF_SCREEN_REGS,
                        VIC_BANK_0.SCREEN,
                        REU_VIDEO_BITMAP_SCREEN_LEN,
                    ),
                    (MHIRES_TRACKER_OFF_COLOR_REGS, 0xD800, REU_VIDEO_BITMAP_COLOR_LEN),
                )
                blob[MHIRES_TRACKER_OFF_BG0] = bg0
                blob[MHIRES_TRACKER_OFF_READY_FLAG] = 1
            else:
                blob = bytearray(FRAME_TRACKER_LEN)
                regs = (
                    (TRACKER_OFF_BITMAP_REGS, VIC_BANK_0.BITMAP, REU_VIDEO_BITMAP_LEN),
                    (TRACKER_OFF_SCREEN_REGS, VIC_BANK_0.SCREEN, REU_VIDEO_BITMAP_SCREEN_LEN),
                )
                blob[TRACKER_OFF_READY_FLAG] = 1
            for family, (off, dst, length) in enumerate(regs):
                # Source: slot in bit 15, family in bits 13-14 of the REU
                # address, below which the longest family (8000 bytes) fits,
                # so every chunk's source names both.
                src = (self.test.SRC_BANK << 16) | (slot << 15) | (family << 13)
                blob[off : off + 7] = bytes(
                    [dst & 0xFF, dst >> 8, src & 0xFF, (src >> 8) & 0xFF, src >> 16]
                    + [length & 0xFF, length >> 8]
                )
            for i, b in enumerate(blob):
                self.mem[FRAME_TRACKER_ADDR + i] = b

        def irq(self, *, line, raster=True, cia1_tick=False):
            """One IRQ through $C500. Returns the exit PC, this IRQ's log and
            the REU source of each transfer it made."""
            from py65.devices.mpu6502 import MPU

            self.log, self.sources = [], []
            self.mem[0xD019] = 0x01 if raster else 0x00
            self.mem[0xD012] = line
            self.mem[CIA1.ICR] = 0x01 if cia1_tick else 0x00
            mpu = MPU(memory=self.mem)
            mpu.pc = BANK_SWAP_IRQ_HANDLER_ADDR
            for _ in range(40000):
                if mpu.pc in (KERNAL.IRQ_HANDLER, AUDIO_HANDLER_INSTALL_ADDR):
                    return mpu.pc, self.log, self.sources
                mpu.step()
            raise AssertionError(f"dispatcher never exited (PC=${mpu.pc:04X})")

    CASES = (
        ("hires", BANK_SWAP_IRQ_HANDLER, False, False),
        ("mhires", MHIRES_BANK_SWAP_IRQ_HANDLER, True, False),
        ("hires+pump", BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER, False, True),
        ("mhires+pump", MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER, True, True),
    )

    @staticmethod
    def _slot(src):
        return (src >> 15) & 1

    @staticmethod
    def _spans(log):
        """Coalesce the transfers into contiguous (start, end) spans."""
        spans: list[list[int]] = []
        for kind, dst, n in log:
            if kind != "rec":
                continue
            if spans and spans[-1][1] == dst:
                spans[-1][1] = dst + n
            else:
                spans.append([dst, dst + n])
        return [tuple(s) for s in spans]

    def _banked_spans(self, bank_base):
        return [
            (bank_base | VIC_BANK_0.BITMAP, (bank_base | VIC_BANK_0.BITMAP) + REU_VIDEO_BITMAP_LEN),
            (
                bank_base | VIC_BANK_0.SCREEN,
                (bank_base | VIC_BANK_0.SCREEN) + REU_VIDEO_BITMAP_SCREEN_LEN,
            ),
        ]

    def test_a_staged_frame_is_copied_into_the_hidden_bank_without_a_flip(self):
        budget = halt_quantum_bytes(NMI_SAFE_MIN_PERIOD_CYCLES)
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                # Out of the window on purpose: copying never waits for vblank.
                exit_pc, log, _ = m.irq(line=self.OUT_OF_WINDOW)
                self.assertEqual(exit_pc, KERNAL.IRQ_HANDLER)
                self.assertEqual(self._spans(log), self._banked_spans(0x8000))
                self.assertTrue(all(0 < n <= budget for k, _, n in log if k == "rec"))
                self.assertNotIn("dd00", [k for k, _, _ in log])

    def test_the_flip_waits_for_the_raster_window(self):
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                m.irq(line=self.IN_WINDOW)
                for line in (self.OUT_OF_WINDOW, 46, 247, 249, 250):
                    _, log, _ = m.irq(line=line)
                    self.assertEqual(log, [], f"line {line}")
                _, log, _ = m.irq(line=self.IN_WINDOW)
                self.assertIn(("dd00", CIA2.PORT_A_BANK_2, 0), log)

    def test_every_line_of_the_window_commits(self):
        # [251, 255] and [0, 43] (mhires: 38), and the lines the 8-bit $D012
        # aliases there.
        for name, handler, mhires, _ in self.CASES[:2]:
            last = (
                modes_irq.MHIRES_COMMIT_LAST_SAFE_LINE if mhires else RASTER_COMMIT_LAST_SAFE_LINE
            )
            for line in (251, 255, 0, last):
                with self.subTest(mode=name, line=line):
                    m = self.Machine(self, handler, mhires=mhires)
                    m.stage(slot=0)
                    m.irq(line=self.OUT_OF_WINDOW)
                    _, log, _ = m.irq(line=line)
                    self.assertEqual(log[0 if not mhires else 1][:2], ("dd00", CIA2.PORT_A_BANK_2))

    def test_frames_alternate_banks(self):
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                m.irq(line=self.IN_WINDOW)
                m.stage(slot=1)
                # Commit frame 0 to bank 2, then copy frame 1 into bank 0.
                _, log, _ = m.irq(line=self.IN_WINDOW)
                flips = [v for k, v, _ in log if k == "dd00"]
                self.assertEqual(flips, [CIA2.PORT_A_BANK_2])
                banked = [s for s in self._spans(log) if s[0] != 0xD800]
                self.assertEqual(banked, self._banked_spans(0x0000))
                _, log, _ = m.irq(line=self.IN_WINDOW)
                self.assertEqual([v for k, v, _ in log if k == "dd00"], [CIA2.PORT_A_BANK_0])

    def test_color_ram_and_bg0_follow_the_flip(self):
        # Color RAM is not banked: before the flip it would sit under the old
        # bitmap for a field, so it goes right after, ahead of the raster.
        for name, handler, mhires, _ in self.CASES[1::2]:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0, bg0=0x06)
                m.irq(line=self.IN_WINDOW)
                _, log, _ = m.irq(line=self.IN_WINDOW)
                kinds = [k for k, _, _ in log]
                self.assertEqual(kinds[:2], ["d021", "dd00"])
                self.assertEqual(log[0][1], 0x06)
                self.assertEqual(self._spans(log), [(0xD800, 0xD800 + REU_VIDEO_BITMAP_COLOR_LEN)])

    def test_the_commit_shows_the_copied_frame_not_a_newer_one(self):
        # The host stages again before the copied frame's vblank: the commit
        # must still write that frame's bg0 and color RAM, from its own slot.
        for name, handler, mhires, _ in self.CASES[1::2]:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0, bg0=0x06)
                m.irq(line=self.OUT_OF_WINDOW)
                m.stage(slot=1, bg0=0x0E)
                _, log, sources = m.irq(line=self.IN_WINDOW)
                self.assertEqual(log[0], ("d021", 0x06, 0))
                color = (self.SRC_BANK << 16) | (0 << 15) | (2 << 13)
                self.assertEqual(sources[0], color, "the first commit transfer is slot 0's color")
                # And the newer frame is then copied, from its own slot.
                color_chunks = REU_VIDEO_BITMAP_COLOR_LEN // BANK_SWAP_CHUNK_SIZE
                self.assertTrue(all(self._slot(s) == 1 for s in sources[color_chunks:]))

    def test_a_restage_while_a_frame_waits_is_not_lost(self):
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                m.irq(line=self.OUT_OF_WINDOW)
                m.stage(slot=1)
                # Out of the window nothing happens, not even the newer copy:
                # the hidden bank still holds the frame waiting to be shown.
                _, log, _ = m.irq(line=self.OUT_OF_WINDOW)
                self.assertEqual(log, [])
                _, log, sources = m.irq(line=self.IN_WINDOW)
                self.assertIn(("dd00", CIA2.PORT_A_BANK_2, 0), log)
                banked = [s for s in sources if (s >> 13) & 3 != 2]
                self.assertTrue(banked and all(self._slot(s) == 1 for s in banked))

    def test_a_tracker_written_during_the_snapshot_is_snapshotted_again(self):
        # The host's tracker DMA can land between two of the snapshot loop's
        # reads; the loop re-reads the ready flag and starts over.
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                fired = []

                def host_dma(address, value, m=m, fired=fired):
                    if not fired:
                        fired.append(address)
                        m.stage(slot=1)

                snapshot = modes_irq.BANK_SWAP_STATE_ADDR + len(modes_irq.BANK_SWAP_STATE_INIT)
                m.mem.subscribe_to_write([snapshot + 3], host_dma)
                _, _, sources = m.irq(line=self.OUT_OF_WINDOW)
                self.assertEqual(len(fired), 1)
                self.assertTrue(sources and all(self._slot(s) == 1 for s in sources))

    def test_a_pending_cia1_tick_runs_the_pump_between_families(self):
        # The pump body rewrites $DF02-$DF06, so each family has to reload
        # its own registers from the snapshot after the end-of-family JSR.
        for name, handler, mhires, pump in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                m.irq(line=self.OUT_OF_WINDOW, cia1_tick=True)
                m.irq(line=self.IN_WINDOW, cia1_tick=True)
                families = 3 if mhires else 2
                self.assertEqual(m.mem[self.PUMP_CALLS], families if pump else 0)

    def test_non_raster_irq_goes_to_the_pump_or_the_kernal(self):
        for name, handler, mhires, pump in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                m.stage(slot=0)
                exit_pc, log, _ = m.irq(line=self.IN_WINDOW, raster=False)
                self.assertEqual(
                    exit_pc, AUDIO_HANDLER_INSTALL_ADDR if pump else KERNAL.IRQ_HANDLER
                )
                self.assertEqual(log, [])

    def test_unstaged_frame_chains_without_a_transfer(self):
        for name, handler, mhires, _ in self.CASES:
            with self.subTest(mode=name):
                m = self.Machine(self, handler, mhires=mhires)
                exit_pc, log, _ = m.irq(line=self.IN_WINDOW)
                self.assertEqual(exit_pc, KERNAL.IRQ_HANDLER)
                self.assertEqual(log, [])

    def test_chunk_halt_fits_the_shortest_nmi_period(self):
        self.assertLessEqual(BANK_SWAP_CHUNK_SIZE, halt_quantum_bytes(NMI_SAFE_MIN_PERIOD_CYCLES))


class ReuPumpBodySubroutineTest(unittest.TestCase):
    """The open-loop pump body at $C180 is the tracked pump's one copy:
    REU_IRQ_HANDLER_TRACKED and the chunked bank-swap dispatchers
    both JSR to it, so it ends with RTS. Caller is responsible for
    saving A; subroutine doesn't preserve registers (X / Y aren't
    touched anyway, A is dead at every call site)."""

    def test_length_105(self):
        # 104 body bytes + 1 RTS = 105.
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(len(REU_PUMP_BODY_SUBROUTINE), 105)

    def test_ends_with_rts(self):
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[-1], 0x60)

    def test_address_is_c180(self):
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_ADDR

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE_ADDR, 0xC180)

    def test_no_pha_at_start(self):
        # No PHA ($48): the caller saves A if it needs it. First byte is
        # the LDA #<chunk_size that begins the length-reload sequence.
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[0], 0xA9)

    def test_bcc_displacement_lands_on_rts(self):
        # The dst-wrap BCC at offset 92 skips the 10-byte wrap block and
        # must land on the RTS at offset 104.
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[92], 0x90)  # BCC
        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[93], 0x0A)  # +10
        # Target after BCC = 92 + 2 + 10 = 104. Must be RTS.
        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[104], 0x60)


if __name__ == "__main__":
    unittest.main()
