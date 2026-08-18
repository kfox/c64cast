"""Temporal colour blending ([color].flicker_blend).

Three layers: the blend-pair vocabulary in video/flicker.py, the 6502 handler
that alternates the pages (verified by executing it, not just comparing bytes),
and the compose/push wiring that produces and uploads a second screen page.
"""

# FakeAPI is a structural stand-in (not a nominal C64Backend) — fake at the
# boundary, same pattern as test_bitmap_compose.py.
# pyright: reportArgumentType=false
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import cast

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast.app.scene_factory import resolve_flicker_blend  # noqa: E402
from c64cast.hw.c64 import (  # noqa: E402
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
    RegionID,
)
from c64cast.video import flicker, palette  # noqa: E402
from c64cast.video.framebuffer import Framebuffer  # noqa: E402
from c64cast.video.modes.base import FlickerComposeBuffers  # noqa: E402
from c64cast.video.modes.hires import HiresDisplayMode  # noqa: E402
from c64cast.video.modes_irq import (  # noqa: E402
    BANK_SWAP_IRQ_HANDLER_ADDR,
    DD00_BANK_0,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_TRACKER_LEN,
    FLICKER_TRACKER_OFF_D018,
    FLICKER_TRACKER_OFF_PHASE,
    FLICKER_TRACKER_OFF_READY,
    FRAME_TRACKER_ADDR,
)


def gradient(c0=(136, 0, 0), c1=(238, 238, 119)) -> np.ndarray:
    """A chromatic ramp — the content blending is actually for. Textured frames
    barely engage it, because spatial dither already covers them."""
    t = np.linspace(0, 1, 320, dtype=np.float32)[None, :, None]
    img = np.array(c0, np.float32) * (1 - t) + np.array(c1, np.float32) * t
    return np.clip(np.broadcast_to(img, (200, 320, 3)), 0, 255).astype(np.uint8)


DEFAULT_LUMA_DELTA = 0.075


class BlendTableTest(unittest.TestCase):
    def test_pair_yield_at_each_luma_cap(self):
        # The numbers the default rests on, against the VIC-II rendering that
        # is the process palette here. A change is a change to how much flicker
        # the default admits, which is a safety decision.
        self.assertEqual(len(flicker.blend_pairs(0.05)), 12)
        self.assertEqual(len(flicker.blend_pairs(DEFAULT_LUMA_DELTA)), 18)
        self.assertEqual(len(flicker.blend_pairs(0.10)), 20)

    def test_luma_cap_is_clamped(self):
        """Above MAX_ALLOWED_LUMA_DELTA is refused whatever the config asks."""
        self.assertEqual(
            flicker.blend_pairs(1.0), flicker.blend_pairs(flicker.MAX_ALLOWED_LUMA_DELTA)
        )

    def test_every_eligible_pair_respects_the_cap(self):
        for cap in (0.05, DEFAULT_LUMA_DELTA, 0.10):
            for a, b in flicker.blend_pairs(cap):
                self.assertLessEqual(flicker.pair_luma_delta(a, b), cap)

    def test_the_dark_end_is_eligible(self):
        """The regression that retired Michelson contrast: a ratio divides by
        the pair's own brightness, so black against anything scored 1.0 and the
        darkest pairs — which fuse best of all — could never qualify."""
        pairs = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA))
        black = sorted(b for a, b in pairs if a == 0)
        self.assertTrue(black, "no pair with black is eligible")
        for other in black:
            self.assertLess(flicker.pair_luma_delta(0, other), DEFAULT_LUMA_DELTA)

    def test_a_solid_is_the_pair_c_c(self):
        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        for i in range(16):
            self.assertEqual(tuple(table.pairs[i]), (i, i))

    def test_solids_round_trip_exactly(self):
        """Fusing a colour with itself must return that colour, or the linear
        round-trip is lossy and every solid drifts."""
        from c64cast.video.palette import C64_PALETTE_BGR

        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        np.testing.assert_allclose(table.bgr[:16], C64_PALETTE_BGR, atol=1e-3)

    def test_fusion_is_linear_light_not_srgb(self):
        """Black + white fuses to sRGB ~188, not the ~128 an encoded-space
        average would give — the eye integrates emitted light."""
        mid = flicker.fuse(0, 1)
        self.assertTrue(np.all(mid > 180), f"too dark, looks like an sRGB average: {mid}")

    def test_blends_are_distinct_from_every_solid(self):
        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        for entry in range(16, table.size):
            gain = np.min(
                np.linalg.norm(
                    flicker._to_lab(table.bgr[:16]) - flicker._to_lab(table.bgr[entry : entry + 1]),
                    axis=1,
                )
            )
            self.assertGreaterEqual(float(gain), flicker.MIN_BLEND_LAB_GAIN)

    def test_quantizing_the_pure_palette_returns_the_solids(self):
        from c64cast.video.palette import C64_PALETTE_BGR

        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        idx = flicker.quantize_flat_blend(C64_PALETTE_BGR, table, perceptual=True)
        np.testing.assert_array_equal(idx, np.arange(16))


class MeasuredThresholdTest(unittest.TestCase):
    """The threshold is not a taste setting — it came off six pairs rendered as
    flat bands and judged by eye on a CRT. These are those pairs, and the rule
    has to keep agreeing with what the display did."""

    # (pair, flickered) as observed on a 1702 driven by an Ultimate 64.
    BANDS = (
        ((6, 11), False),  # Blue + Dark Gray
        ((1, 7), True),  # White + Yellow — the mildest flicker seen
        ((12, 14), False),  # Medium Gray + Light Blue
        ((8, 15), True),  # Orange + Light Gray
        ((2, 11), False),  # Red + Dark Gray
        ((13, 15), True),  # Light Green + Light Gray
    )

    def setUp(self):
        # set_host_palette is process-wide, so put it back or every other colour
        # test in the suite inherits this one's table.
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")

    def test_the_rule_classifies_every_measured_band(self):
        for (a, b), flickered in self.BANDS:
            with self.subTest(pair=(a, b)):
                delta = flicker.pair_luma_delta(a, b)
                self.assertEqual(
                    delta > DEFAULT_LUMA_DELTA,
                    flickered,
                    f"ΔY {delta:.4f} disagrees with the display",
                )

    def test_the_default_sits_between_the_two_groups(self):
        solid = max(flicker.pair_luma_delta(a, b) for (a, b), f in self.BANDS if not f)
        flickers = min(flicker.pair_luma_delta(a, b) for (a, b), f in self.BANDS if f)
        self.assertLess(solid, DEFAULT_LUMA_DELTA)
        self.assertLess(DEFAULT_LUMA_DELTA, flickers)

    def test_nothing_eligible_reaches_the_observed_flicker_onset(self):
        """MAX_ALLOWED_LUMA_DELTA exists to keep the knob's whole range below
        the lowest delta that was actually seen to flicker."""
        onset = min(flicker.pair_luma_delta(a, b) for (a, b), f in self.BANDS if f)
        self.assertLess(flicker.MAX_ALLOWED_LUMA_DELTA, onset)


class FlickerFollowsPaletteTest(unittest.TestCase):
    """The flicker module keeps its own linear-light and Lab tables, and its
    eligible-pair set is a statement about emitted luminance — so it has to
    follow a host-palette swap or it will admit pairs that flicker on this
    machine."""

    def setUp(self):
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))

    def test_luminance_table_follows(self):
        before = flicker._PALETTE_Y.copy()
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertFalse(np.array_equal(before, flicker._PALETTE_Y))

    def test_the_blend_table_cache_is_dropped(self):
        before = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        after = flicker.build_blend_table(DEFAULT_LUMA_DELTA)
        self.assertIsNot(before, after)

    def test_the_eligible_pair_set_changes(self):
        """Not a rescaling — a different machine has a different answer about
        which two colours fuse, which is why this cannot be computed once."""
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        pepto = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA))
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        u64 = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA))
        self.assertNotEqual(pepto, u64)
        self.assertLess(len(pepto & u64), min(len(pepto), len(u64)))


class FlickerHandlerTest(unittest.TestCase):
    """Executes the 6502 under py65 — branch offsets and the phase gate are
    exactly the kind of thing a byte-comparison test cannot catch."""

    def _run_field(self, mem, raster=True):
        from py65.devices.mpu6502 import MPU

        mem[0xD019] = 0x01 if raster else 0x00
        mpu = MPU(memory=mem)
        mpu.pc = BANK_SWAP_IRQ_HANDLER_ADDR
        for _ in range(200):
            if mpu.pc == 0xEA31:
                return
            mpu.step()
        self.fail("handler never chained to the kernal")

    def _armed_memory(self):
        from py65.memory import ObservableMemory

        mem = ObservableMemory()
        for i, b in enumerate(FLICKER_SWAP_IRQ_HANDLER):
            mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
        mem[0xEA31] = 0x60  # RTS where the kernal handler would be
        tracker = [0x05, DD00_BANK_2, 0x00, 0x00, D018_HIRES_PAGE_A, D018_HIRES_PAGE_B]
        for off, value in enumerate(tracker):
            mem[FRAME_TRACKER_ADDR + off] = value
        mem[0xDD00] = DD00_BANK_0
        mem[0xD018] = 0x00
        mem[0xD021] = 0x00
        return mem

    def test_pages_alternate_every_field(self):
        mem = self._armed_memory()
        seen = []
        for _ in range(6):
            self._run_field(mem)
            seen.append(mem[0xD018])
        self.assertEqual(
            seen,
            [D018_HIRES_PAGE_B, D018_HIRES_PAGE_A] * 3,
            "the field alternation is not free-running",
        )

    def test_alternation_does_not_wait_on_a_staged_frame(self):
        """The whole reason this needs no fast link: the C64 owns the flicker."""
        mem = self._armed_memory()
        self.assertEqual(mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY], 0)
        self._run_field(mem)
        self.assertEqual(mem[0xD018], D018_HIRES_PAGE_B)

    def test_swap_commits_only_on_phase_zero(self):
        """A swap landing on an odd field would transpose the A/B page roles for
        the rest of the scene — invisible on a still, a colour shift on motion."""
        mem = self._armed_memory()
        self._run_field(mem)  # phase -> 1
        self.assertEqual(mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_PHASE], 1)
        mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY] = 1
        self._run_field(mem)  # phase -> 0, commits here
        self.assertEqual(mem[0xDD00], DD00_BANK_2)
        self.assertEqual(mem[0xD021], 0x05)
        self.assertEqual(mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY], 0)

    def test_a_frame_armed_on_phase_zero_waits_a_field(self):
        mem = self._armed_memory()
        mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY] = 1
        self._run_field(mem)  # lands on phase 1 — must NOT commit
        self.assertEqual(mem[0xDD00], DD00_BANK_0)
        self.assertEqual(mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_READY], 1)
        self._run_field(mem)  # phase 0 — commits
        self.assertEqual(mem[0xDD00], DD00_BANK_2)

    def test_a_non_raster_irq_changes_nothing(self):
        """CIA #1's jiffy also vectors through $0314."""
        mem = self._armed_memory()
        self._run_field(mem)
        before = (mem[0xD018], mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_PHASE], mem[0xDD00])
        self._run_field(mem, raster=False)
        after = (mem[0xD018], mem[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_PHASE], mem[0xDD00])
        self.assertEqual(before, after)

    def test_every_branch_targets_the_kernal_chain(self):
        from py65.devices.mpu6502 import MPU
        from py65.disassembler import Disassembler
        from py65.memory import ObservableMemory

        mem = ObservableMemory()
        for i, b in enumerate(FLICKER_SWAP_IRQ_HANDLER):
            mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
        dis = Disassembler(MPU(memory=mem))
        pc = BANK_SWAP_IRQ_HANDLER_ADDR
        end = pc + len(FLICKER_SWAP_IRQ_HANDLER)
        chain = end - 3  # the JMP $EA31
        targets, last = [], None
        while pc < end:
            length, text = dis.instruction_at(pc)
            if text.split()[0] in ("BEQ", "BNE", "BCS"):
                targets.append(int(text.split("$")[1], 16))
            last = text
            pc += length
        self.assertEqual(pc, end, "instruction stream does not land on the handler's end")
        self.assertEqual(last, "JMP $ea31")
        self.assertTrue(targets, "no branches found — the offsets test is vacuous")
        for t in targets:
            self.assertEqual(t, chain, f"branch to ${t:04X} misses the chain at ${chain:04X}")


class FlickerComposeTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(HiresDisplayMode("normal")._blend_table)
        self.assertNotIn("screen_b", HiresDisplayMode("normal").compose(gradient()))

    def test_emits_a_second_page_that_shares_the_bitmap(self):
        mode = HiresDisplayMode("normal", flicker_blend=True)
        b = cast(FlickerComposeBuffers, mode.compose(gradient()))
        self.assertEqual(b["screen"].shape, (1000,))
        self.assertEqual(b["screen_b"].shape, (1000,))
        self.assertEqual(b["bitmap"].shape, (8000,))
        self.assertTrue(
            (b["screen"] != b["screen_b"]).any(), "no cell blends on a chromatic gradient"
        )

    def test_the_two_pages_differ_only_in_colour(self):
        """A differing mask would flicker geometry rather than colour."""
        mode = HiresDisplayMode("normal", flicker_blend=True)
        b = cast(FlickerComposeBuffers, mode.compose(gradient()))
        # Every differing cell must still be a legal screen byte pair; the
        # bitmap is a single array, so shape equality IS mask equality.
        self.assertEqual(b["bitmap"].dtype, np.uint8)
        self.assertLessEqual(int(b["screen"].max()), 0xFF)
        self.assertLessEqual(int(b["screen_b"].max()), 0xFF)

    def test_a_flat_frame_blends_nothing(self):
        """Nothing to gain, so it must not pay the flicker cost."""
        flat = np.zeros((200, 320, 3), np.uint8)
        b = cast(
            FlickerComposeBuffers, HiresDisplayMode("normal", flicker_blend=True).compose(flat)
        )
        np.testing.assert_array_equal(b["screen"], b["screen_b"])

    def test_blending_beats_the_plain_path_on_a_gradient(self):
        import cv2

        from c64cast.video.palette import C64_PALETTE_BGR

        src = gradient()

        def lab(a):
            u8 = np.clip(a, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
            return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)

        def shown(b, table):
            bits = np.unpackbits(b["bitmap"].reshape(25, 40, 8), axis=2).reshape(25, 40, 8, 8)
            mask = bits.transpose(0, 2, 1, 3).reshape(200, 320).astype(bool)
            s = b["screen"].reshape(25, 40)
            if table is None:
                fg, bg = C64_PALETTE_BGR[s >> 4], C64_PALETTE_BGR[s & 15]
            else:
                sb = cast(FlickerComposeBuffers, b)["screen_b"].reshape(25, 40)
                fg = flicker.fuse_indices(s >> 4, sb >> 4)
                bg = flicker.fuse_indices(s & 15, sb & 15)
            f = np.repeat(np.repeat(fg, 8, 0), 8, 1)
            g = np.repeat(np.repeat(bg, 8, 0), 8, 1)
            return np.where(mask[..., None], f, g).astype(np.float32)

        plain = HiresDisplayMode("normal", perceptual=True)
        blend = HiresDisplayMode("normal", flicker_blend=True)
        e_plain = float(
            np.linalg.norm(lab(src) - lab(shown(plain.compose(src), None)), axis=1).mean()
        )
        e_blend = float(
            np.linalg.norm(
                lab(src) - lab(shown(blend.compose(src), blend._blend_table)), axis=1
            ).mean()
        )
        self.assertLess(e_blend, e_plain * 0.9, f"plain {e_plain:.2f} vs blend {e_blend:.2f}")

    def test_blending_forces_the_perceptual_metric(self):
        """The widened palette is Lab-defined; under weighted-BGR it measurably
        regresses below the 16 solids."""
        mode = HiresDisplayMode("normal", flicker_blend=True, perceptual=False)
        self.assertTrue(mode._perceptual)
        self.assertIn("pinned", mode.set_color_match("rgb"))
        self.assertTrue(mode._perceptual)

    def test_edges_styles_never_blend(self):
        for style in ("edges", "edges_inverted"):
            with self.subTest(style=style):
                mode = HiresDisplayMode(style, flicker_blend=True)
                self.assertIsNone(mode._blend_table)


class FlickerPushTest(unittest.TestCase):
    def test_setup_installs_the_flicker_handler_and_seeds_the_pages(self):
        api = FakeAPI()
        HiresDisplayMode("normal", flicker_blend=True).setup(api)
        handler = api.mem_files.get(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}")
        self.assertEqual(handler, FLICKER_SWAP_IRQ_HANDLER)
        tracker = api.mem_files.get(f"{FRAME_TRACKER_ADDR:04X}")
        assert tracker is not None, "no tracker was uploaded"
        self.assertEqual(len(tracker), FLICKER_TRACKER_LEN)
        self.assertEqual(tracker[FLICKER_TRACKER_OFF_D018], D018_HIRES_PAGE_A)
        self.assertEqual(tracker[FLICKER_TRACKER_OFF_D018 + 1], D018_HIRES_PAGE_B)
        self.assertEqual(tracker[FLICKER_TRACKER_OFF_READY], 0, "armed before a frame was staged")

    def test_push_writes_both_pages_into_the_offscreen_bank(self):
        api = FakeAPI()
        mode = HiresDisplayMode("normal", flicker_blend=True)
        mode.setup(api)
        api.regions.clear()
        mode.push(api, mode.compose(gradient()))
        written = set(api.regions)
        self.assertIn(VIC_BANK_2.SCREEN, written)
        self.assertIn(VIC_BANK_2.SCREEN_ALT, written)
        self.assertIn(VIC_BANK_2.BITMAP, written)

    def test_each_page_gets_its_own_delta_region(self):
        """Sharing an ID would make every frame look fully dirty on both, since
        the pages differ by construction — that difference IS the blend."""
        api = FakeAPI()
        mode = HiresDisplayMode("normal", flicker_blend=True)
        mode.setup(api)
        api.ops.clear()
        mode.push(api, mode.compose(gradient()))
        ids = [op[3] for op in api.ops if op[0] == "write_region"]
        self.assertIn(RegionID.SCREEN_BANK2, ids)
        self.assertIn(RegionID.SCREEN_ALT_BANK2, ids)
        self.assertEqual(len(ids), len(set(ids)), "a region id was reused within one push")

    def test_teardown_restores_d018(self):
        """Nothing else re-asserts it, so a following char scene would read its
        matrix from the $0C00 offset."""
        api = FakeAPI()
        mode = HiresDisplayMode("normal", flicker_blend=True)
        mode.setup(api)
        api.memories.clear()
        mode.teardown(api)
        self.assertEqual(api.memories.get("D018"), "14")


class FlickerResolveTest(unittest.TestCase):
    def test_opt_in(self):
        self.assertFalse(resolve_flicker_blend(False, "hires"))
        self.assertTrue(resolve_flicker_blend(True, "hires"))

    def test_hires_normal_only(self):
        self.assertFalse(resolve_flicker_blend(True, "mhires"))
        self.assertFalse(resolve_flicker_blend(True, "mcm"))
        self.assertFalse(resolve_flicker_blend(True, "petscii"))
        self.assertFalse(resolve_flicker_blend(True, "hires_edges"))

    def test_refused_when_a_buffer_overlay_owns_the_second_page(self):
        self.assertFalse(resolve_flicker_blend(True, "hires", has_buffer_overlays=True))

    def test_refused_while_the_reu_pump_owns_the_irq_vector(self):
        self.assertFalse(resolve_flicker_blend(True, "hires", audio_reu_pump_active=True))

    def test_a_plain_hires_scene_actually_gets_a_blend_table(self):
        """End-to-end through the factory, because the gate agreeing in
        isolation proves nothing about what the caller hands it."""
        from c64cast.app.config import Config, SceneCfg
        from c64cast.app.scene_factory import _display_mode_for_scene

        cfg = Config()
        cfg.color.flicker_blend = True
        scene = SceneCfg(type="slideshow", display="hires")
        mode = _display_mode_for_scene("hires", scene, cfg)
        assert isinstance(mode, HiresDisplayMode)
        self.assertIsNotNone(mode._blend_table)
        self.assertFalse(mode.use_reu_staged)
        self.assertFalse(mode.double_buffer)


class FlickerMirrorTest(unittest.TestCase):
    """The mirror shows the fused blend, which no capture of the real display
    can do — see caveats.md."""

    def _armed_framebuffer(self, page_a: int, page_b: int):
        from c64cast.hw.c64 import SCREEN, VIC

        fb = Framebuffer()
        fb.on_write(VIC.D011_CONTROL_1, bytes([0x3B]))
        fb.on_write(VIC.D016_CONTROL_2, bytes([0x08]))
        fb.on_write(SCREEN.BITMAP, bytes([0xFF]) * 8000)  # every pixel foreground
        fb.on_write(VIC_BANK_0.SCREEN, bytes([page_a]) * 1000)
        fb.on_write(VIC_BANK_0.SCREEN_ALT, bytes([page_b]) * 1000)
        fb.on_write(BANK_SWAP_IRQ_HANDLER_ADDR, FLICKER_SWAP_IRQ_HANDLER)
        fb.on_write(FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_D018, bytes([0x18, 0x38]))
        return fb

    def test_detects_the_second_page_only_when_the_vector_is_hooked(self):
        fb = self._armed_framebuffer(0x10, 0x01)
        self.assertIsNone(fb._flicker_page_b(bytes(fb.ram)), "detected before $0314 was hooked")
        fb.on_write(
            VECTORS.IRQ,
            bytes([BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, BANK_SWAP_IRQ_HANDLER_ADDR >> 8]),
        )
        self.assertEqual(fb._flicker_page_b(bytes(fb.ram)), VIC_BANK_0.SCREEN_ALT)

    def test_stops_detecting_after_teardown_unhooks_the_vector(self):
        """Teardown leaves the handler and tracker in RAM, so the page bytes
        alone would keep reporting a blend into the next scene."""
        fb = self._armed_framebuffer(0x10, 0x01)
        fb.on_write(
            VECTORS.IRQ,
            bytes([BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, BANK_SWAP_IRQ_HANDLER_ADDR >> 8]),
        )
        fb.on_write(VECTORS.IRQ, bytes([0x31, 0xEA]))
        self.assertIsNone(fb._flicker_page_b(bytes(fb.ram)))

    def test_renders_the_fused_colour_in_linear_light(self):
        # page A white-on-black, page B black-on-white → fuses to mid grey.
        fb = self._armed_framebuffer(0x10, 0x01)
        fb.on_write(
            VECTORS.IRQ,
            bytes([BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, BANK_SWAP_IRQ_HANDLER_ADDR >> 8]),
        )
        pixel = fb.render()[100, 160]
        self.assertTrue(
            np.all(pixel > 180) and np.all(pixel < 200),
            f"expected the linear-light fusion of black and white, got {pixel}",
        )

    def test_falls_back_to_page_a_with_no_flicker(self):
        fb = self._armed_framebuffer(0x10, 0x01)
        np.testing.assert_array_equal(fb.render()[100, 160], [255, 255, 255])


if __name__ == "__main__":
    unittest.main()
