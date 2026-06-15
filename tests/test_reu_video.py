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

import numpy as np
from _fakes import FakeAPI

from c64cast.api import Ultimate64API
from c64cast.c64 import (
    CIA2,
    KERNAL,
    REU,
    SCREEN,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
)
from c64cast.config import Config, VideoCfg, _build_display_mode
from c64cast.modes import (
    AUDIO_HANDLER_INSTALL_ADDR,
    AUDIO_HANDLER_STUB,
    BANK_SWAP_CHUNK_SIZE,
    BANK_SWAP_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER_ADDR,
    BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER,
    FRAME_TRACKER_ADDR,
    FRAME_TRACKER_LEN,
    MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_BANK_SWAP_IRQ_HANDLER,
    MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_FRAME_TRACKER_LEN,
    MHIRES_TRACKER_OFF_BANK_VALUE,
    MHIRES_TRACKER_OFF_BG0,
    MHIRES_TRACKER_OFF_BITMAP_REGS,
    MHIRES_TRACKER_OFF_COLOR_REGS,
    MHIRES_TRACKER_OFF_READY_FLAG,
    MHIRES_TRACKER_OFF_SCREEN_REGS,
    REU_VIDEO_BITMAP_BASE,
    REU_VIDEO_BITMAP_COLOR_BASE,
    REU_VIDEO_BITMAP_COLOR_LEN,
    REU_VIDEO_BITMAP_LEN,
    REU_VIDEO_BITMAP_SCREEN_BASE,
    REU_VIDEO_BITMAP_SCREEN_LEN,
    REU_VIDEO_SCREEN_BASE,
    REU_VIDEO_SCREEN_LEN,
    TRACKER_OFF_BANK_VALUE,
    TRACKER_OFF_BITMAP_REGS,
    TRACKER_OFF_READY_FLAG,
    TRACKER_OFF_SCREEN_REGS,
    BlankDisplayMode,
    HiresDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
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
    """config.resolve_use_reu_staged() maps the tri-state + probe verdict +
    display mode to a concrete bool. "auto" stages bitmap modes only when REU
    is available; explicit true/false ignore the probe."""

    def _resolve(self, setting, display, reu_available):
        from c64cast.config import resolve_use_reu_staged

        return resolve_use_reu_staged(setting, display, reu_available=reu_available)

    def test_auto_bitmap_with_reu_enables(self):
        for d in ("hires", "hires_edges", "mhires"):
            self.assertTrue(self._resolve("auto", d, True), d)

    def test_auto_bitmap_without_reu_stays_off(self):
        for d in ("hires", "mhires"):
            self.assertFalse(self._resolve("auto", d, False), d)

    def test_auto_char_modes_stay_off_even_with_reu(self):
        # Char modes regress under staging — auto must leave them host-DMA
        # regardless of REU availability.
        for d in ("petscii", "blank", "mcm"):
            self.assertFalse(self._resolve("auto", d, True), d)

    def test_explicit_true_ignores_probe_and_mode(self):
        self.assertTrue(self._resolve(True, "petscii", False))
        self.assertTrue(self._resolve(True, "mhires", False))

    def test_explicit_false_never_stages(self):
        self.assertFalse(self._resolve(False, "mhires", True))
        self.assertFalse(self._resolve(False, "petscii", True))


class ValidateUseReuStagedTest(unittest.TestCase):
    """The loader accepts only true/false/"auto" for [video].use_reu_staged."""

    def _load(self, value_literal):
        import tempfile

        from c64cast.config import load

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
        # Screen RAM ($0400) hit via write_region, NOT via REUWRITE.
        self.assertIn(SCREEN.RAM, fake.regions)
        self.assertEqual(fake.socket_dma.reuwrites, [])

    def test_reu_path_stages_screen_to_reu(self):
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        screen = bytes(range(256)) * 4  # 1024 bytes; first 1000 form screen
        screen = screen[:1000]
        fake = self._push(mode, screen, bytes(1000))
        # Exactly one REUWRITE for the 1000-byte screen.
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_VIDEO_SCREEN_BASE)
        self.assertEqual(data, screen)
        self.assertEqual(len(data), REU_VIDEO_SCREEN_LEN)

    def test_reu_path_sets_destination_to_screen_ram(self):
        # REU.C64_ADDR_LO/HI written as a packed two-byte payload pointing
        # at $0400 (low=$00, high=$04). write_regs stores this under the
        # base key as a tuple of byte values.
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
        # The trigger byte at $DF01 must be $91 (exec + FF00-off + REU→C64).
        # A wrong value here either runs the wrong direction or fails to
        # execute, leaving the screen unchanged with no error indication.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        fake = self._push(mode, bytes(1000), bytes(1000))
        key = f"{REU.COMMAND:04X}"
        self.assertIn(key, fake.memories)
        self.assertEqual(fake.memories[key], f"{REU.CMD_FETCH_EXEC:02X}")

    def test_reu_path_still_writes_color_via_dmawrite(self):
        # Color RAM at $D800 isn't VIC-banked, so it doesn't benefit from
        # REU staging. It must continue to flow through write_region's
        # delta cache regardless of the REU video flag.
        mode = PETSCIIDisplayMode(use_reu_staged=True)
        color = bytes([5] * 1000)
        fake = self._push(mode, bytes(1000), color)
        self.assertIn(SCREEN.COLOR_RAM, fake.regions)
        self.assertEqual(fake.regions[SCREEN.COLOR_RAM], color)

    def test_reu_path_does_not_write_screen_via_region(self):
        # The whole point of REU staging is that screen RAM goes through
        # the REU pipe instead of the host DMAWRITE region cache. If we
        # accidentally do both, we double-write the screen and waste a
        # frame's worth of bus halt time.
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
        # Trigger byte present.
        self.assertIn(f"{REU.COMMAND:04X}", fake.memories)


class ReuCoexistenceTest(unittest.TestCase):
    """Sanity that validate_scene_cfg accepts every REU flag combination
    on video scenes. The earlier `_raises` variants are obsolete: the
    bank-swap install now picks a merged $C500 dispatcher whose non-raster
    branch JMPs to the audio pump at $C100, so both REU users share one
    $0314 hook and serialize REC access naturally."""

    def test_video_alone_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_audio_alone_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = False
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_video_petscii_is_ok(self):
        # Char-mode REU video is host-triggered single-buffer (no $0314
        # hook), so the merged-dispatcher branch isn't even taken — but
        # the combination must still build cleanly.
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="petscii", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_video_mhires_is_ok(self):
        # The interesting case the merge enables: REU audio + REU bank-
        # swap video on the same video scene. Before the merge this
        # raised ValueError; after, it builds.
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="video", display="mhires", file="x.mp4")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_both_on_webcam_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="petscii")
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
        # The "hires_edges" alias must thread the flag the same way as
        # "hires" — both pick HiresDisplayMode under the hood.
        m = _build_display_mode("hires_edges", use_reu_staged=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertTrue(m.use_reu_staged)

    def test_hires_default_off(self):
        # Default constructor must leave the bank-swap pipeline disarmed;
        # silently promoting existing configs onto the experimental path
        # would change every hires user's behavior.
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


# ============================================================================
# Hires (double-buffer, bank-swap) tests
# ============================================================================


class ReuHiresHandlerIntegrityTest(unittest.TestCase):
    """The bank-swap IRQ handler is hand-encoded 6502. The four branches
    (2× forward BEQ to JMP $EA31, 2× backward BPL for the reg-copy loops)
    must land on instruction boundaries. The module-level assert catches
    length drift; this test pins the exact bytes so any change has to be
    deliberate."""

    def test_handler_length(self):
        # 61 bytes: ack + ready check + 2× (LDX loop + trigger) +
        # bank swap + clear flag + JMP $EA31.
        self.assertEqual(len(BANK_SWAP_IRQ_HANDLER), 61)

    def test_handler_pinned_bytes(self):
        # Recompute every branch offset (not just the assert) if the
        # design changes — don't paper over a divergence.
        expected = bytes(
            [
                0xAD,
                0x19,
                0xD0,  # LDA $D019
                0x29,
                0x01,  # AND #$01
                0xF0,
                0x33,  # BEQ +51 → JMP $EA31 at offset 58
                0x8D,
                0x19,
                0xD0,  # STA $D019 (ack raster)
                0xAD,
                0x0F,
                0xC7,  # LDA $C70F (ready flag)
                0xF0,
                0x2B,  # BEQ +43 → JMP $EA31
                0xA2,
                0x06,  # LDX #$06
                0xBD,
                0x00,
                0xC7,  # LDA $C700,X (bitmap regs)
                0x9D,
                0x02,
                0xDF,  # STA $DF02,X
                0xCA,  # DEX
                0x10,
                0xF7,  # BPL -9
                0xA9,
                0x91,  # LDA #$91
                0x8D,
                0x01,
                0xDF,  # STA $DF01 (trigger bitmap)
                0xA2,
                0x06,  # LDX #$06
                0xBD,
                0x07,
                0xC7,  # LDA $C707,X (screen regs)
                0x9D,
                0x02,
                0xDF,  # STA $DF02,X
                0xCA,  # DEX
                0x10,
                0xF7,  # BPL -9
                0xA9,
                0x91,  # LDA #$91
                0x8D,
                0x01,
                0xDF,  # STA $DF01 (trigger screen)
                0xAD,
                0x0E,
                0xC7,  # LDA $C70E (bank value)
                0x8D,
                0x00,
                0xDD,  # STA $DD00 (swap)
                0xA9,
                0x00,  # LDA #$00
                0x8D,
                0x0F,
                0xC7,  # STA $C70F (clear flag)
                0x4C,
                0x31,
                0xEA,  # JMP $EA31
            ]
        )
        self.assertEqual(BANK_SWAP_IRQ_HANDLER, expected)

    def test_tracker_offsets_match_handler(self):
        # The handler's hardcoded $C700/$C707/$C70E/$C70F offsets must
        # match the TRACKER_OFF_* constants the host uses to lay out
        # the tracker payload. Drift here would mean the host writes
        # bank value to the byte the handler reads as ready flag, etc.
        self.assertEqual(TRACKER_OFF_BITMAP_REGS, 0)
        self.assertEqual(TRACKER_OFF_SCREEN_REGS, 7)
        self.assertEqual(TRACKER_OFF_BANK_VALUE, 14)
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
        # First-frame swap brings up the off-screen bank; if we left it
        # full of post-reset garbage the user would see one frame of
        # noise before the first render's REU→main DMA lands.
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
        # Ready flag in the tracker must start at 0 so the first raster
        # IRQ after install skips the DMA path until the host stages a
        # real frame. Whole 16-byte tracker zeroed for hygiene.
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
        # Raster compare at line 248 ($F8) puts the IRQ inside vblank
        # on both PAL and NTSC — VIC isn't rendering so $DD00 swap is
        # tear-free.
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D012"], "F8")

    def test_setup_enables_raster_irq(self):
        # $D01A = $01 enables raster as the only VIC IRQ source.
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D01A"], "01")

    def test_setup_displayed_bank_tracker_initialized(self):
        # Internal tracker drives target_bank alternation in render();
        # must start at 0 so the first frame paints bank 2.
        _, mode = self._setup()
        self.assertEqual(mode._displayed_bank, 0)

    def test_setup_off_path_does_not_install_irq(self):
        # use_reu_staged=False must not touch $0314 / $D012 / $D01A —
        # the IRQ install is the experimental opt-in.
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
        # write_regs("0314", ...) records the LAST write under that key —
        # so we need the kernal value to win, meaning teardown ran AFTER
        # setup's hook.
        self.assertEqual(
            fake.regs[f"{VECTORS.IRQ:04X}"],
            (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF),
        )

    def test_teardown_restores_dd00_to_bank0(self):
        fake = self._setup_then_teardown()
        # Whether mode was bank 0 or bank 2 at teardown time, the next
        # scene's setup expects $DD00 = bank 0 (kernal default). Mode
        # is fresh here so the post-setup value is already bank 0, but
        # the teardown write is explicit and idempotent.
        self.assertEqual(fake.memories[f"{CIA2.PORT_A:04X}"], f"{CIA2.PORT_A_BANK_0:02X}")

    def test_teardown_disables_vic_raster_irq(self):
        fake = self._setup_then_teardown()
        # Setup wrote $D01A = $01; teardown must write $00 LAST so the
        # next scene sees raster IRQs masked.
        self.assertEqual(fake.memories["D01A"], "00")

    def test_teardown_off_path_is_noop(self):
        # No raster IRQ to tear down; teardown must not write anything
        # related to the IRQ surface.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = HiresDisplayMode(use_reu_staged=False)
        # No setup call (so we're not testing teardown-without-setup
        # which isn't a supported sequence). Just verify the default
        # teardown is genuinely a no-op for non-REU mode.
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
        # Solid-color frame; render quantizes but the byte values aren't
        # what we're testing here.
        return np.zeros((200, 320, 3), dtype=np.uint8)

    def _tracker(self, fake):
        key = f"{FRAME_TRACKER_ADDR:04X}"
        self.assertIn(key, fake.mem_files, "render() must write the frame tracker at $C700")
        blob = fake.mem_files[key]
        self.assertEqual(len(blob), FRAME_TRACKER_LEN)
        return blob

    def test_first_frame_targets_bank2(self):
        # _displayed_bank starts at 0, so target_bank = 1 → bank 2 dests.
        mode = HiresDisplayMode(use_reu_staged=True)
        mode._displayed_bank = 0
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        # Bank value byte in the tracker = $95 (bank 2).
        self.assertEqual(blob[TRACKER_OFF_BANK_VALUE], CIA2.PORT_A_BANK_2)
        # Bitmap regs slot points at $A000 (bank 2 bitmap).
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 0], VIC_BANK_2.BITMAP & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 1], (VIC_BANK_2.BITMAP >> 8) & 0xFF)
        # Screen regs slot points at $8400 (bank 2 screen).
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS + 0], VIC_BANK_2.SCREEN & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS + 1], (VIC_BANK_2.SCREEN >> 8) & 0xFF)
        # Tracker advances to bank 2.
        self.assertEqual(mode._displayed_bank, 1)

    def test_second_frame_targets_bank0(self):
        # After one render, the tracker is at bank 2; next render should
        # paint into bank 0 and cue a swap back.
        mode = HiresDisplayMode(use_reu_staged=True)
        mode._displayed_bank = 1
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[TRACKER_OFF_BANK_VALUE], CIA2.PORT_A_BANK_0)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 0], VIC_BANK_0.BITMAP & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_BITMAP_REGS + 1], (VIC_BANK_0.BITMAP >> 8) & 0xFF)
        self.assertEqual(blob[TRACKER_OFF_SCREEN_REGS + 0], VIC_BANK_0.SCREEN & 0xFF)
        self.assertEqual(mode._displayed_bank, 0)

    def test_tracker_carries_reu_src_and_length_for_both_dmas(self):
        # The IRQ handler copies bitmap regs to $DF02-$DF08 then triggers,
        # then copies screen regs and triggers. The tracker must carry
        # complete REU source addresses and lengths for both — wrong
        # values and the DMA writes to the wrong place on the C64 or
        # reads from the wrong REU offset.
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
        # Ready flag MUST be the last byte of the DMAWRITE payload — the
        # whole 16-byte blob lands atomically on the C64 side via the
        # socket FIFO, so the IRQ either sees all-new regs+ready=1 or
        # all-old. If we ever switch to splitting the write or
        # rearrange the layout, the IRQ could see ready=1 with stale
        # regs and DMA to the wrong destination.
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
        # Sizes match the constants.
        for off, data in fake.socket_dma.reuwrites:
            if off == REU_VIDEO_BITMAP_BASE:
                self.assertEqual(len(data), REU_VIDEO_BITMAP_LEN)
            elif off == REU_VIDEO_BITMAP_SCREEN_BASE:
                self.assertEqual(len(data), REU_VIDEO_BITMAP_SCREEN_LEN)

    def test_render_does_not_host_trigger_reu_dma(self):
        # The v2 architecture moved the REU→main triggers into the C64
        # IRQ handler. The host MUST NOT write $DF01 (trigger) or
        # $DF02-$DF08 (regs) directly per frame — that would race the
        # C64 IRQ and add Python-paced jitter that defeats the
        # deterministic-vblank perceptual win.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(
            f"{REU.COMMAND:04X}", fake.memories, "host must not trigger REU DMA — C64 IRQ does it"
        )
        self.assertNotIn(
            f"{REU.C64_ADDR_LO:04X}", fake.regs, "host must not stage REU regs — they go in tracker"
        )

    def test_render_does_not_dmawrite_displayed_bank(self):
        # The whole point of bank-swap is that the bitmap/screen
        # writes go to the OFF-SCREEN bank via REU. If we accidentally
        # also wrote $2000 / $0400 via write_region, we'd tear the
        # currently-displayed frame.
        mode = HiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(0x2000, fake.regions, "REU-staged hires must not DMAWRITE bank 0 bitmap")
        self.assertNotIn(0x0400, fake.regions, "REU-staged hires must not DMAWRITE bank 0 screen")

    def test_off_path_still_dmawrites_directly(self):
        # No regression: with use_reu_staged=False, render() must continue
        # the existing bitmap+screen via write_region into bank 0.
        mode = HiresDisplayMode(use_reu_staged=False)
        fake = self._render(mode, self._frame())
        self.assertIn(0x2000, fake.regions)
        self.assertIn(0x0400, fake.regions)
        # And no REUWRITEs.
        self.assertEqual(fake.socket_dma.reuwrites, [])
        # And no frame tracker write.
        self.assertNotIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)


class ReuHiresWebcamCoexistenceTest(unittest.TestCase):
    """Bank-swap raster IRQ + REU mic pump on the same webcam scene used
    to be rejected (both wanted to own $0314). With the merged dispatcher
    they coexist: the bank-swap install at $C500 uses the +AUDIO handler
    variant whose non-raster branch JMPs to the mic pump at $C100, and
    the mic install is told to skip its own $0314 hook (scenes.py)."""

    def test_webcam_hires_both_on_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="hires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_hires_edges_both_on_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="hires_edges")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_petscii_both_on_ok(self):
        # Char modes don't install a raster IRQ — single-buffer REU only.
        # Coexisted with mic REU before the merge too; still should.
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="petscii")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_blank_hires_edges_both_on_ok(self):
        # Quirk preserved: blank scenes accept display = "hires_edges"
        # but the blank branch always builds BlankDisplayMode (single-
        # buffer REU, no IRQ install). No $0314 collision either way.
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="blank", display="hires_edges")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_hires_audio_off_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="webcam", display="hires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_mhires_both_on_is_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = True
        sc = SceneCfg(type="webcam", display="mhires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)

    def test_webcam_mhires_audio_off_ok(self):
        from c64cast.config import SceneCfg, validate_scene_cfg

        cfg = Config()
        cfg.video.use_reu_staged = True
        cfg.audio.use_reu_pump = False
        sc = SceneCfg(type="webcam", display="mhires")
        validate_scene_cfg(sc, cfg, audio_enabled=True)


# ============================================================================
# MultiHires (double-buffer, bank-swap, +color RAM, +bg0) tests
# ============================================================================


class ReuMHiresHandlerIntegrityTest(unittest.TestCase):
    """Like the hires handler, the mhires handler is hand-encoded 6502 with
    pinned branch offsets. Length-pin + byte-pin so changes are deliberate."""

    def test_handler_length(self):
        # 83 bytes: ack + ready check + 3× (LDX loop + trigger) +
        # bg0 reg write + bank swap + clear flag + JMP $EA31.
        self.assertEqual(len(MHIRES_BANK_SWAP_IRQ_HANDLER), 83)

    def test_handler_pinned_bytes(self):
        expected = bytes(
            [
                0xAD,
                0x19,
                0xD0,  # LDA $D019
                0x29,
                0x01,  # AND #$01
                0xF0,
                0x49,  # BEQ +73 → JMP $EA31 at offset 80
                0x8D,
                0x19,
                0xD0,  # STA $D019 (ack raster)
                0xAD,
                0x17,
                0xC7,  # LDA $C717 (ready flag)
                0xF0,
                0x41,  # BEQ +65 → JMP $EA31
                0xA2,
                0x06,  # LDX #$06
                0xBD,
                0x00,
                0xC7,  # LDA $C700,X (bitmap regs)
                0x9D,
                0x02,
                0xDF,  # STA $DF02,X
                0xCA,  # DEX
                0x10,
                0xF7,  # BPL -9
                0xA9,
                0x91,  # LDA #$91
                0x8D,
                0x01,
                0xDF,  # STA $DF01 (trigger bitmap)
                0xA2,
                0x06,  # LDX #$06
                0xBD,
                0x07,
                0xC7,  # LDA $C707,X (screen regs)
                0x9D,
                0x02,
                0xDF,  # STA $DF02,X
                0xCA,  # DEX
                0x10,
                0xF7,  # BPL -9
                0xA9,
                0x91,  # LDA #$91
                0x8D,
                0x01,
                0xDF,  # STA $DF01 (trigger screen)
                0xA2,
                0x06,  # LDX #$06
                0xBD,
                0x0E,
                0xC7,  # LDA $C70E,X (color regs)
                0x9D,
                0x02,
                0xDF,  # STA $DF02,X
                0xCA,  # DEX
                0x10,
                0xF7,  # BPL -9
                0xA9,
                0x91,  # LDA #$91
                0x8D,
                0x01,
                0xDF,  # STA $DF01 (trigger color)
                0xAD,
                0x15,
                0xC7,  # LDA $C715 (bg0)
                0x8D,
                0x21,
                0xD0,  # STA $D021
                0xAD,
                0x16,
                0xC7,  # LDA $C716 (bank value)
                0x8D,
                0x00,
                0xDD,  # STA $DD00 (swap)
                0xA9,
                0x00,  # LDA #$00
                0x8D,
                0x17,
                0xC7,  # STA $C717 (clear flag)
                0x4C,
                0x31,
                0xEA,  # JMP $EA31
            ]
        )
        self.assertEqual(MHIRES_BANK_SWAP_IRQ_HANDLER, expected)

    def test_tracker_offsets_match_handler(self):
        # 24-byte tracker; bitmap=$C700, screen=$C707, color=$C70E, bg0=$C715,
        # bank=$C716, ready=$C717. Drift here means the handler reads
        # the wrong byte at vblank — silent corruption.
        self.assertEqual(MHIRES_TRACKER_OFF_BITMAP_REGS, 0)
        self.assertEqual(MHIRES_TRACKER_OFF_SCREEN_REGS, 7)
        self.assertEqual(MHIRES_TRACKER_OFF_COLOR_REGS, 14)
        self.assertEqual(MHIRES_TRACKER_OFF_BG0, 21)
        self.assertEqual(MHIRES_TRACKER_OFF_BANK_VALUE, 22)
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
        # Must install the 83-byte mhires handler, NOT the 61-byte hires one.
        # The two share the same address ($C500) but different bytes — a
        # mix-up would either skip color DMA + bg0 (hires bytes in mhires
        # mode → no color update) or run off the end into garbage (mhires
        # bytes in hires mode reading uninitialized tracker bytes).
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
        # MHires tracker is longer (24 bytes vs hires's 16). Length must
        # match MHIRES_FRAME_TRACKER_LEN; ready flag (last byte) zero so
        # the first IRQ skips until the host stages a real frame.
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
        self.assertEqual(fake.memories["D012"], "F8")

    def test_setup_enables_raster_irq(self):
        fake, _ = self._setup()
        self.assertEqual(fake.memories["D01A"], "01")

    def test_setup_displayed_bank_tracker_initialized(self):
        _, mode = self._setup()
        self.assertEqual(mode._displayed_bank, 0)

    def test_setup_off_path_does_not_install_irq(self):
        # use_reu_staged=False must leave the IRQ + tracker untouched.
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        mode = MultiHiresDisplayMode(use_reu_staged=False)
        mode.setup(api)
        self.assertNotIn(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", fake.mem_files)
        self.assertNotIn(f"{VECTORS.IRQ:04X}", fake.regs)
        self.assertNotIn("D012", fake.memories)


class ReuMHiresTeardownTest(unittest.TestCase):
    """teardown() shares _uninstall_bank_swap_irq with hires; verify the
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

    def test_first_frame_targets_bank2(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        mode._displayed_bank = 0
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        # Bank value byte in the tracker = $95 (bank 2).
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BANK_VALUE], CIA2.PORT_A_BANK_2)
        # Bitmap regs point at $A000 (bank 2 bitmap).
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 0], VIC_BANK_2.BITMAP & 0xFF)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 1], (VIC_BANK_2.BITMAP >> 8) & 0xFF)
        # Screen regs point at $8400.
        self.assertEqual(blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 0], VIC_BANK_2.SCREEN & 0xFF)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 1], (VIC_BANK_2.SCREEN >> 8) & 0xFF)
        # Tracker advances to bank 2.
        self.assertEqual(mode._displayed_bank, 1)

    def test_second_frame_targets_bank0(self):
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        mode._displayed_bank = 1
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BANK_VALUE], CIA2.PORT_A_BANK_0)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_BITMAP_REGS + 0], VIC_BANK_0.BITMAP & 0xFF)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_SCREEN_REGS + 0], VIC_BANK_0.SCREEN & 0xFF)
        self.assertEqual(mode._displayed_bank, 0)

    def test_color_regs_target_d800_regardless_of_bank(self):
        # $D800 isn't VIC-banked — both bank-0 and bank-2 destinations must
        # hit the same shared color RAM. A mistake here (e.g. pointing at
        # $D800 only when bank==0) would leave c3 stale for half of frames.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        # Frame 1: target_bank=1, color dest must still be $D800.
        mode._displayed_bank = 0
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 0], 0x00)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 1], 0xD8)
        # Frame 2: target_bank=0, color dest must STILL be $D800.
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 0], 0x00)
        self.assertEqual(blob[MHIRES_TRACKER_OFF_COLOR_REGS + 1], 0xD8)

    def test_tracker_carries_reu_src_and_length_for_all_three_dmas(self):
        # Bitmap: $E10000 / 8000.  Screen: $E12000 / 1000.  Color: $E13000 / 1000.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        blob = self._tracker(fake)
        # bitmap
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
        # screen
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
        # color
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
        # bg0 is a palette index 0..15. The handler writes the tracker byte
        # to $D021 unconditionally each frame. The value comes from the
        # rendered frame; we can't predict it exactly without re-running
        # quantization, but it must fit in the palette index range.
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
        # Same as hires: host must NOT drive $DF01 or $DF02-$DF08 directly.
        # The C64 IRQ does it from the tracker.
        mode = MultiHiresDisplayMode(use_reu_staged=True)
        fake = self._render(mode, self._frame())
        self.assertNotIn(
            f"{REU.COMMAND:04X}", fake.memories, "host must not trigger REU DMA — C64 IRQ does it"
        )
        self.assertNotIn(
            f"{REU.C64_ADDR_LO:04X}", fake.regs, "host must not stage REU regs — they go in tracker"
        )

    def test_render_does_not_dmawrite_displayed_bank(self):
        # The whole point: no host-side bitmap/screen/color writes. Even
        # bg0 ($D021) must NOT be host-written; the IRQ handler does it.
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
        # use_reu_staged=False: render() keeps the existing direct-write
        # path (bitmap, screen, color, bg0 all via host). Required for no
        # regression to existing mhires configs.
        mode = MultiHiresDisplayMode(use_reu_staged=False)
        fake = self._render(mode, self._frame())
        self.assertIn(0x2000, fake.regions)
        self.assertIn(0x0400, fake.regions)
        self.assertIn(0xD800, fake.regions)
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertNotIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)

    def test_global_palette_mode_also_uses_reu(self):
        # _render_global and _render_percell are separate code paths; both
        # must honor use_reu_staged. Default palette_mode is "percell"
        # (covered above); test "cheap" (global path) explicitly.
        mode = MultiHiresDisplayMode(palette_mode="cheap", use_reu_staged=True)
        fake = self._render(mode, self._frame())
        # All three REUWRITEs fire on the global path too.
        offs = {off for off, _ in fake.socket_dma.reuwrites}
        self.assertIn(REU_VIDEO_BITMAP_BASE, offs)
        self.assertIn(REU_VIDEO_BITMAP_SCREEN_BASE, offs)
        self.assertIn(REU_VIDEO_BITMAP_COLOR_BASE, offs)
        # And the tracker is staged.
        self.assertIn(f"{FRAME_TRACKER_ADDR:04X}", fake.mem_files)


class ReuMHiresFlagDefaultTest(unittest.TestCase):
    """Same default-off invariant as hires: the experimental flag must not
    silently change existing mhires users' behavior."""

    def test_mhires_default(self):
        self.assertFalse(MultiHiresDisplayMode().use_reu_staged)


# ============================================================================
# Merged dispatcher (REU video bank-swap + REU audio pump on $0314)
# ============================================================================


class MergedDispatcherIntegrityTest(unittest.TestCase):
    """The merged $C500 dispatcher is derived mechanically from the base
    bank-swap handler: the trailing `JMP $EA31` is replaced with a
    JMP $EA31 chain (for raster path) followed by a JMP $C100 audio
    handler fallthrough (for non-raster path). The first BEQ is
    retargeted from chain to the audio JMP.

    See _make_merged_handler in modes.py for the empirical rationale
    behind not inserting a CIA #1 ICR check between chain and
    fallthrough (Cam Link envelope FFT confirmed the check itself
    drove a 60 Hz envelope harmonic that's not present when the
    fallthrough is plain)."""

    EXTENSION = bytes(
        [
            0x4C,
            0x31,
            0xEA,  # JMP $EA31 (chain to kernal)
            0x4C,
            0x00,
            0xC1,  # JMP $C100 (audio handler fallthrough)
        ]
    )

    def test_hires_merged_length(self):
        # 61 - 3 (drop trailing JMP $EA31) + 6 (extension) = 64 bytes.
        self.assertEqual(len(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER), 64)

    def test_mhires_merged_length(self):
        # 83 - 3 + 6 = 86 bytes.
        self.assertEqual(len(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER), 86)

    def test_hires_merged_ends_with_extension(self):
        # Last 6 bytes = JMP $EA31 + JMP $C100.
        self.assertEqual(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[-6:], self.EXTENSION)

    def test_mhires_merged_ends_with_extension(self):
        self.assertEqual(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[-6:], self.EXTENSION)

    def test_hires_merged_first_beq_targets_audio_jmp(self):
        # First BEQ at offset 5/6. Body length = 58 (= 61 base - 3 JMP).
        # JMP $C100 opcode is at body_len + 3 = 61. BEQ target offset
        # is (5+2) + displacement = 7 + displacement = 61, so
        # displacement = 54 = $36.
        self.assertEqual(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[5], 0xF0)
        self.assertEqual(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[6], 0x36)
        target_offset = 7 + BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[6]
        self.assertEqual(target_offset, 61)
        self.assertEqual(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[target_offset], 0x4C)  # JMP opcode

    def test_mhires_merged_first_beq_targets_audio_jmp(self):
        # Body = 80, JMP $C100 at 80 + 3 = 83. Displacement = 83 - 7 = 76 = $4C.
        self.assertEqual(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[5], 0xF0)
        self.assertEqual(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[6], 0x4C)
        target_offset = 7 + MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[6]
        self.assertEqual(target_offset, 83)
        self.assertEqual(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER[target_offset], 0x4C)

    def test_hires_bank_swap_path_bytes_unchanged(self):
        # Every byte EXCEPT the first BEQ displacement (offset 6) and the
        # trailing JMP $EA31 (last 3 bytes of base) should match base. The
        # last 3 bytes of base become offsets 58..60 of merged, replaced
        # by the chain + audio fallthrough extension (6 bytes total).
        base = BANK_SWAP_IRQ_HANDLER
        merged = BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER
        self.assertEqual(merged[:6], base[:6])
        # Bytes 7..len(base)-3 are the bank-swap work bytes; preserved.
        self.assertEqual(merged[7 : len(base) - 3], base[7:-3])
        # Suffix = chain + audio extension (6 bytes).
        self.assertEqual(merged[len(base) - 3 :], self.EXTENSION)

    def test_mhires_bank_swap_path_bytes_unchanged(self):
        base = MHIRES_BANK_SWAP_IRQ_HANDLER
        merged = MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER
        self.assertEqual(merged[:6], base[:6])
        self.assertEqual(merged[7 : len(base) - 3], base[7:-3])
        self.assertEqual(merged[len(base) - 3 :], self.EXTENSION)

    def test_audio_handler_install_addr_matches_jmp_target(self):
        # Doc-style: the merged dispatcher hardcodes $C100; if audio.py
        # ever relocates its handler, both must move together. Pin the
        # invariant here.
        self.assertEqual(AUDIO_HANDLER_INSTALL_ADDR, 0xC100)
        # And the stub is a JMP $EA31 (the kernal IRQ chain target).
        self.assertEqual(AUDIO_HANDLER_STUB, bytes([0x4C, 0x31, 0xEA]))


class MergedDispatcherSetupTest(unittest.TestCase):
    """Setup with audio_reu_pump_active=True must:
    (1) write the MERGED handler bytes (not the plain bank-swap) to $C500
    (2) pre-upload the AUDIO_HANDLER_STUB to $C100 BEFORE hooking $0314
        — so the gap between this install completing and audio.start
        writing real bytes doesn't vector into uninitialized RAM."""

    def test_hires_uses_merged_handler_when_audio_active(self):
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        handler = fake.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"]
        self.assertEqual(handler, BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER)

    def test_mhires_uses_chunked_merged_handler_when_audio_active(self):
        # 2026-05-27: mhires + REU audio defaults to the CHUNKED merged
        # variant (146 B). The monolithic merged variant (86 B) is kept in
        # modes.py for documentation / A/B testing but is no longer used
        # at runtime — chunked is the only way to keep NMI alive across
        # the bitmap's 8 ms REC DMA.
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
        # And the audio stub is NOT pre-uploaded when no audio pump.
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
        # Sequencing: the IRQ vector at $0314 must not be patched until
        # the stub is in place at $C100. Verify the operation order on
        # FakeAPI.ops (which records every write_memory_file and
        # write_regs call in sequence).
        fake = FakeAPI()
        api = cast(Ultimate64API, fake)
        m = HiresDisplayMode(use_reu_staged=True, audio_reu_pump_active=True)
        m.setup(api)
        stub_addr = f"{AUDIO_HANDLER_INSTALL_ADDR:04X}".lower()
        vec_addr = f"{VECTORS.IRQ:04X}".lower()
        # Find the first op that uploads the stub and the first op that
        # writes the IRQ vector. Stub must come first.
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
        # Don't silently promote existing configs onto the merged
        # dispatcher path.
        m = _build_display_mode("hires", use_reu_staged=True)
        assert isinstance(m, HiresDisplayMode)
        self.assertFalse(m.audio_reu_pump_active)


class MhiresChunkedHandlerIntegrityTest(unittest.TestCase):
    """The chunked mhires merged dispatcher splits each per-frame REC
    DMA into 100-byte sub-DMAs so the per-chunk bus halt stays under
    the 125 µs NMI period (fixing NMI loss → restored music pitch).
    After each family's chunk loop ends, a pump check reads $DC0D and
    runs the pump body if CIA #1 was pending — keeping the audio ring
    refilled across the ~14 ms bank-swap I-flag window (fixing the
    ring drain that the split alone makes WORSE). These tests pin the
    byte layout, branch displacements, chunk counts, and the three
    end-of-family pump JSRs."""

    HANDLER = MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER

    def test_length_176(self):
        # 21 header + 3 × 44 family (11 copy + 4 counter + 19 chunk
        # loop + 10 pump check) + 17 tail + 6 exits = 176.
        self.assertEqual(len(self.HANDLER), 176)

    def test_chunk_size_100(self):
        # 1 cyc/byte REU bandwidth × 100 = 100 µs halt per chunk,
        # under the 125 µs NMI period.
        self.assertEqual(BANK_SWAP_CHUNK_SIZE, 100)

    def test_exit_paths_jmp_kernal_then_audio(self):
        # Last 6 bytes: chain to kernal, then audio fallthrough.
        self.assertEqual(
            self.HANDLER[-6:],
            bytes(
                [
                    0x4C,
                    0x31,
                    0xEA,  # JMP $EA31
                    0x4C,
                    AUDIO_HANDLER_INSTALL_ADDR & 0xFF,
                    (AUDIO_HANDLER_INSTALL_ADDR >> 8) & 0xFF,
                ]
            ),
        )

    def test_header_dispatch_uses_bne_jmp_form(self):
        # First branch: BNE +3 / JMP audio_fallthrough.
        # (Plain BEQ would be out-of-range to the audio JMP at offset 173.)
        self.assertEqual(self.HANDLER[5], 0xD0)  # BNE
        self.assertEqual(self.HANDLER[6], 0x03)  # +3 → offset 10
        self.assertEqual(self.HANDLER[7], 0x4C)  # JMP
        # Target = $C500 + 173 = $C5AD.
        self.assertEqual(self.HANDLER[8], 0xAD)
        self.assertEqual(self.HANDLER[9], 0xC5)

    def test_ready_flag_gate_uses_bne_jmp_form(self):
        # Second branch: BNE +3 / JMP chain_to_kernal.
        self.assertEqual(self.HANDLER[16], 0xD0)
        self.assertEqual(self.HANDLER[17], 0x03)
        self.assertEqual(self.HANDLER[18], 0x4C)
        # Target = $C500 + 170 = $C5AA.
        self.assertEqual(self.HANDLER[19], 0xAA)
        self.assertEqual(self.HANDLER[20], 0xC5)

    def test_bitmap_chunk_count(self):
        # LDA #80 at offset 32 (= 8000 / 100).
        self.assertEqual(self.HANDLER[32], 0xA9)
        self.assertEqual(self.HANDLER[33], 80)

    def test_screen_chunk_count(self):
        # LDA #10 at offset 76 (= 1000 / 100).
        self.assertEqual(self.HANDLER[76], 0xA9)
        self.assertEqual(self.HANDLER[77], 10)

    def test_color_chunk_count(self):
        # LDA #10 at offset 120.
        self.assertEqual(self.HANDLER[120], 0xA9)
        self.assertEqual(self.HANDLER[121], 10)

    def test_each_chunk_loop_uses_bne_back_19(self):
        # Three BNE -19 branches at offsets 53, 97, 141 — the chunk-loop
        # bodies are 19 bytes long, so the displacement is 256-19 = $ED.
        for bne_off in (53, 97, 141):
            with self.subTest(bne_off=bne_off):
                self.assertEqual(self.HANDLER[bne_off], 0xD0, f"expected BNE opcode at {bne_off}")
                self.assertEqual(self.HANDLER[bne_off + 1], 0xED)

    def test_each_chunk_loop_triggers_df01_with_91(self):
        # Per chunk: LDA #$91 / STA $DF01 (the REU exec command).
        # The STA is at offsets 48, 92, 136 (start of chunk loop body + 12).
        for sta_off in (48, 92, 136):
            with self.subTest(sta_off=sta_off):
                # Preceding LDA #$91.
                self.assertEqual(self.HANDLER[sta_off - 2], 0xA9)
                self.assertEqual(self.HANDLER[sta_off - 1], 0x91)
                # STA $DF01.
                self.assertEqual(self.HANDLER[sta_off], 0x8D)
                self.assertEqual(self.HANDLER[sta_off + 1], 0x01)
                self.assertEqual(self.HANDLER[sta_off + 2], 0xDF)

    def test_each_chunk_loop_reloads_length(self):
        # Per chunk: write chunk_size to $DF07 (length lo). Verifies the
        # REC's auto-decrement-on-transfer is being countered correctly
        # (without the reload, the 2nd+ trigger transfers 0 / 64K bytes).
        for sta_off in (38, 82, 126):
            with self.subTest(sta_off=sta_off):
                self.assertEqual(self.HANDLER[sta_off - 2], 0xA9)
                self.assertEqual(self.HANDLER[sta_off - 1], BANK_SWAP_CHUNK_SIZE)
                self.assertEqual(self.HANDLER[sta_off], 0x8D)
                self.assertEqual(self.HANDLER[sta_off + 1], 0x07)
                self.assertEqual(self.HANDLER[sta_off + 2], 0xDF)

    def test_each_family_runs_pump_check_jsr_at_end(self):
        # End-of-family pump check: LDA $DC0D / AND #$01 / BEQ +3 /
        # JSR $C180. The LDA $DC0D opcode is at offsets 55 (bitmap),
        # 99 (screen), 143 (color) — immediately after each chunk loop
        # BNE.
        for lda_off in (55, 99, 143):
            with self.subTest(lda_off=lda_off):
                # LDA $DC0D
                self.assertEqual(self.HANDLER[lda_off], 0xAD)
                self.assertEqual(self.HANDLER[lda_off + 1], 0x0D)
                self.assertEqual(self.HANDLER[lda_off + 2], 0xDC)
                # AND #$01
                self.assertEqual(self.HANDLER[lda_off + 3], 0x29)
                self.assertEqual(self.HANDLER[lda_off + 4], 0x01)
                # BEQ +3 (skip the 3-byte JSR if CIA #1 not pending)
                self.assertEqual(self.HANDLER[lda_off + 5], 0xF0)
                self.assertEqual(self.HANDLER[lda_off + 6], 0x03)
                # JSR $C180 (pump body in audio.py)
                self.assertEqual(self.HANDLER[lda_off + 7], 0x20)
                self.assertEqual(self.HANDLER[lda_off + 8], 0x80)
                self.assertEqual(self.HANDLER[lda_off + 9], 0xC1)


class ReuPumpBodySubroutineTest(unittest.TestCase):
    """The pump body at $C180 mirrors the inline pump work in
    REU_IRQ_HANDLER_TRACKED but ends with RTS so the chunked mhires
    bank-swap dispatcher can JSR to it. Caller is responsible for
    saving A; subroutine doesn't preserve registers (X / Y aren't
    touched anyway, A is dead at every call site)."""

    def test_length_105(self):
        # 104 body bytes + 1 RTS = 105.
        from c64cast.audio import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(len(REU_PUMP_BODY_SUBROUTINE), 105)

    def test_ends_with_rts(self):
        from c64cast.audio import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[-1], 0x60)

    def test_address_is_c180(self):
        from c64cast.audio import REU_PUMP_BODY_SUBROUTINE_ADDR

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE_ADDR, 0xC180)

    def test_no_pha_at_start(self):
        # The TRACKED handler starts with PHA ($48); the subroutine
        # drops it (caller saves A if needed). First byte is the LDA
        # #<chunk_size that begins the length-reload sequence.
        from c64cast.audio import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[0], 0xA9)

    def test_bcc_displacement_lands_on_rts(self):
        # Original TRACKED handler had BCC at offset 93 → target offset
        # 105 (PLA). Subroutine shifts everything by −1 (no leading PHA)
        # → BCC at offset 92 → target offset 104 (RTS). Displacement
        # byte stays +10 because the shift is uniform.
        from c64cast.audio import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[92], 0x90)  # BCC
        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[93], 0x0A)  # +10
        # Target after BCC = 92 + 2 + 10 = 104. Must be RTS.
        self.assertEqual(REU_PUMP_BODY_SUBROUTINE[104], 0x60)


if __name__ == "__main__":
    unittest.main()
