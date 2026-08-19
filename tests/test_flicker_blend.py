"""Temporal color blending ([color].flicker_tolerance).

Three layers: the blend-pair vocabulary in video/flicker.py, the 6502 handler
that alternates the pages (verified by executing it, not just comparing bytes),
and the compose/push wiring that produces and uploads a second screen page.
"""

# FakeAPI is a structural stand-in (not a nominal C64Backend) — fake at the
# boundary, same pattern as test_bitmap_compose.py.
# pyright: reportArgumentType=false
from __future__ import annotations

import collections
import sys
import unittest
from pathlib import Path
from typing import cast

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI, quiet_logging  # noqa: E402

from c64cast.app.scene_factory import resolve_flicker_tolerance  # noqa: E402
from c64cast.hw.c64 import (  # noqa: E402
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
    RegionID,
)
from c64cast.video import flicker, palette  # noqa: E402
from c64cast.video.flicker import FLICKER_TOLERANCES  # noqa: E402
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

# Structural cases just need a populated table; the tier is the point only
# where a test says so.
ALL_TIERS = "visible"


class BlendTableTest(unittest.TestCase):
    def setUp(self):
        # Several cases below swap the palette to check the rule follows it.
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))

    def test_pair_yield_at_each_tier(self):
        # The numbers each tolerance rests on, against the VIC-II rendering that
        # is the process palette here. A change to the middle columns is a
        # change to how much scored flicker the setting admits.
        for cap, clean, subtle, visible in (
            (0.05, 3, 6, 9),
            (DEFAULT_LUMA_DELTA, 3, 8, 12),
            (0.10, 3, 9, 14),
        ):
            with self.subTest(cap=cap):
                counts = {t: len(flicker.blend_pairs(cap, tolerance=t)) for t in FLICKER_TOLERANCES}
                self.assertEqual(
                    counts, {"off": 0, "clean": clean, "subtle": subtle, "visible": visible}
                )

    def test_the_table_is_the_scoring_run_as_recorded(self):
        """The tiers are data, so the assertion is that they still say what the
        sitting said — 33 pairs in the distribution it produced."""
        counts = collections.Counter(flicker.SCORED_FLICKER.values())
        self.assertEqual(
            {t: counts[t] for t in flicker.FLICKER_TIERS},
            {"none": 1, "verymild": 7, "mild": 6, "moderate": 9, "intense": 10},
        )
        self.assertTrue(all(a < b for a, b in flicker.SCORED_FLICKER), "keys must be ordered")

    def test_an_unscored_pair_is_never_admitted(self):
        """The VIC-II rendering brings pairs under the clamp that the Ultimate
        64 sitting never had to judge, Cyan+Yellow among them — which ΔY
        refused there and which the docstring calls as violent as anything on
        the chart. Nothing unscored may ride in on a palette swap."""
        for tolerance in (t for t in FLICKER_TOLERANCES if t != "off"):
            for pair in flicker.blend_pairs(
                flicker.FLASH_CRITERION_LUMA_DELTA, tolerance=tolerance
            ):
                self.assertIsNotNone(flicker.pair_flicker_tier(*pair), f"{pair} unscored")
        self.assertLessEqual(flicker.pair_luma_delta(3, 7), flicker.FLASH_CRITERION_LUMA_DELTA)
        self.assertIsNone(flicker.pair_flicker_tier(3, 7))
        self.assertNotIn(
            (3, 7), flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance=ALL_TIERS)
        )

    def test_an_unknown_tolerance_raises_rather_than_disabling_blending(self):
        """It returned [] for anything unrecognized, which is indistinguishable
        from "off" — a typo turned the feature off instead of failing."""
        with self.assertRaises(ValueError):
            flicker.blend_pairs(DEFAULT_LUMA_DELTA, tolerance="strobe")

    def test_off_admits_nothing(self):
        self.assertEqual(
            flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance="off"), []
        )
        self.assertEqual(flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="off").size, 16)

    def test_each_tier_contains_the_quieter_ones(self):
        """A tolerance is a cut down an ordered scale, so loosening it may only
        add — otherwise raising it could silently drop a pair already in use."""
        previous: set[tuple[int, int]] = set()
        for tolerance in FLICKER_TOLERANCES:
            current = set(
                flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance=tolerance)
            )
            with self.subTest(tolerance=tolerance):
                self.assertLessEqual(previous, current)
            previous = current

    def test_the_luma_cap_only_ever_removes_pairs(self):
        for cap in (0.05, DEFAULT_LUMA_DELTA, 0.10):
            self.assertLessEqual(
                set(flicker.blend_pairs(cap, tolerance=ALL_TIERS)),
                set(flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance=ALL_TIERS)),
            )

    def test_the_two_settings_get_separate_cache_entries(self):
        strict = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="clean")
        loose = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        self.assertEqual(strict.tolerance, "clean")
        self.assertEqual(loose.tolerance, ALL_TIERS)
        self.assertLess(strict.size, loose.size)
        self.assertIs(strict, flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="clean"))

    def test_the_luma_cap_advises_rather_than_refuses(self):
        """It used to clamp to the flash criterion, which put a computed number
        above a pair a human had looked at and accepted. On the VIC-II rendering
        that withheld five of the eight pairs scored as fusing cleanly."""
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        capped = flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance="clean")
        wide = flicker.blend_pairs(1.0, tolerance="clean")
        self.assertLess(len(capped), len(wide))
        self.assertLessEqual(set(capped), set(wide))

    def test_a_wide_cap_still_cannot_reach_an_unscored_pair(self):
        """Removing the clamp must not turn the cap into a way in for pairs
        nobody has judged — the tier table is what bounds admission."""
        for pair in flicker.blend_pairs(1.0, tolerance=ALL_TIERS):
            self.assertIsNotNone(flicker.pair_flicker_tier(*pair))

    def test_no_tolerance_admits_the_intense_tier(self):
        """Scored and kept as a record, but offered by no setting: measured
        against the plain palette they reconstruct no better than the tier
        below, so a setting for them would trade flicker for nothing."""
        intense = {p for p, t in flicker.SCORED_FLICKER.items() if t == "intense"}
        self.assertTrue(intense)
        for tolerance in FLICKER_TOLERANCES:
            admitted = set(flicker.blend_pairs(1.0, tolerance=tolerance))
            self.assertFalse(admitted & intense, f"{tolerance} admitted an intense pair")

    def test_every_eligible_pair_respects_the_cap(self):
        for cap in (0.05, DEFAULT_LUMA_DELTA, 0.10):
            for a, b in flicker.blend_pairs(cap, tolerance=ALL_TIERS):
                self.assertLessEqual(flicker.pair_luma_delta(a, b), cap)

    def test_the_dark_end_is_eligible(self):
        """The regression that retired Michelson contrast: a ratio divides by
        the pair's own brightness, so black against anything scored 1.0 and the
        darkest pairs — which fuse best of all — could never qualify."""
        pairs = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS))
        black = sorted(b for a, b in pairs if a == 0)
        self.assertTrue(black, "no pair with black is eligible")
        for other in black:
            self.assertLess(flicker.pair_luma_delta(0, other), DEFAULT_LUMA_DELTA)

    def test_a_solid_is_the_pair_c_c(self):
        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        for i in range(16):
            self.assertEqual(tuple(table.pairs[i]), (i, i))

    def test_solids_round_trip_exactly(self):
        """Fusing a color with itself must return that color, or the linear
        round-trip is lossy and every solid drifts."""
        from c64cast.video.palette import C64_PALETTE_BGR

        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        np.testing.assert_allclose(table.bgr[:16], C64_PALETTE_BGR, atol=1e-3)

    def test_fusion_is_linear_light_not_srgb(self):
        """Black + white fuses to sRGB ~188, not the ~128 an encoded-space
        average would give — the eye integrates emitted light."""
        mid = flicker.fuse(0, 1)
        self.assertTrue(np.all(mid > 180), f"too dark, looks like an sRGB average: {mid}")

    def test_blends_are_distinct_from_every_solid(self):
        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
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

        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        idx = flicker.quantize_flat_blend(C64_PALETTE_BGR, table, perceptual=True)
        np.testing.assert_array_equal(idx, np.arange(16))


class ScoringPairsTest(unittest.TestCase):
    """[color].flicker_score_pairs — the escape hatch the scoring grid needs.

    Filtering by tier is right for playback and wrong for the tool that produces
    the tiers: a pair scored `intense` is in no blend table, so without this it
    could never be rendered to be re-judged and a wrong tier would be permanent.
    """

    def setUp(self):
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")

    def test_it_reaches_a_pair_no_tolerance_admits(self):
        loud = (6, 8)  # Blue + Orange, scored intense
        self.assertEqual(flicker.pair_flicker_tier(*loud), "intense")
        for tolerance in FLICKER_TOLERANCES:
            self.assertNotIn(loud, flicker.blend_pairs(1.0, tolerance=tolerance))
        table = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="clean", score_pairs=[loud])
        self.assertEqual([tuple(p) for p in table.pairs[16:]], [loud])
        self.assertTrue(table.scoring)

    def test_it_ignores_the_luma_cap_too(self):
        """Black+White is the widest ΔY there is. The cap only warns now, but
        the scoring path must not be subject to it at all."""
        table = flicker.build_blend_table(0.001, tolerance="clean", score_pairs=[(0, 1)])
        self.assertEqual([tuple(p) for p in table.pairs[16:]], [(0, 1)])
        self.assertGreater(flicker.pair_luma_delta(0, 1), flicker.FLASH_CRITERION_LUMA_DELTA)

    def test_it_cannot_switch_blending_on_by_itself(self):
        """Otherwise a stray diagnostic key in a config would start alternating
        the screen with no tolerance ever having been set."""
        mode = HiresDisplayMode("normal", flicker_score_pairs=["Blue+Orange"])
        self.assertIsNone(mode._blend_table)
        mode = HiresDisplayMode(
            "normal", flicker_tolerance="clean", flicker_score_pairs=["Blue+Orange"]
        )
        self.assertIsNotNone(mode._blend_table)

    def test_specs_take_names_or_indices_and_normalize(self):
        self.assertEqual(flicker.parse_scoring_pairs(["Blue+Brown"]), [(6, 9)])
        self.assertEqual(flicker.parse_scoring_pairs(["9+6"]), [(6, 9)])
        self.assertEqual(flicker.parse_scoring_pairs([" orange + blue "]), [(6, 8)])
        self.assertEqual(flicker.parse_scoring_pairs(["6+9", "Blue+Brown"]), [(6, 9)])

    def test_a_malformed_spec_raises(self):
        for bad in ("bogus", "6", "6+9+11", "6+6", "6+nosuchcolor"):
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                flicker.parse_scoring_pairs([bad])

    def test_a_scoring_table_does_not_collide_with_a_normal_one(self):
        plain = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="clean")
        scored = flicker.build_blend_table(
            DEFAULT_LUMA_DELTA, tolerance="clean", score_pairs=[(6, 8)]
        )
        self.assertIsNot(plain, scored)
        self.assertFalse(plain.scoring)
        self.assertIs(plain, flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance="clean"))


class ObservedFlickerTest(unittest.TestCase):
    """What the display actually did, and which rule accounts for it.

    Two rules were fitted here and both were refuted by a run they had not seen:
    a ΔY threshold (six flat bands, then r=+0.26 over the full set) and a warmth
    axis (four colors, then r=+0.32). The cases kept here are the ones that
    discriminate, not the ones any rule would get right."""

    # (pair, flickered) as scored by eye on a 1702 driven by an Ultimate 64.
    OBSERVED = (
        ((12, 14), True),  # Medium Gray + Light Blue — ΔY 0.0002, the smallest possible
        ((0, 11), False),  # Black + Dark Gray — ΔY 0.0685, near the top of the range
        ((6, 9), False),  # Blue + Brown
        ((6, 8), True),  # Blue + Orange — Δchroma within 0.2 of Blue + Brown
    )

    def setUp(self):
        # set_host_palette is process-wide, so put it back or every other color
        # test in the suite inherits this one's table.
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")

    def test_no_luma_threshold_can_classify_these(self):
        """The refutation, held in code so the ΔY rule cannot quietly come back:
        a flickering pair sits below a solid one, so no cut on ΔY separates
        them and no default is the 'right' one."""
        flickered = flicker.pair_luma_delta(12, 14)
        solid = flicker.pair_luma_delta(0, 11)
        self.assertLess(flickered, solid)

    def test_no_chroma_distance_can_classify_these_either(self):
        """Blue+Brown and Blue+Orange differ by 0.2 counts in Δchroma and by the
        whole scale, none against intense — so a chroma distance cannot split
        them any more than ΔY can."""
        lab = flicker._PALETTE_LAB
        import numpy as _np

        quiet = float(_np.linalg.norm(lab[6][1:] - lab[9][1:]))
        loud = float(_np.linalg.norm(lab[6][1:] - lab[8][1:]))
        self.assertLess(abs(quiet - loud), 1.0)
        self.assertEqual(flicker.pair_flicker_tier(6, 9), "none")
        self.assertEqual(flicker.pair_flicker_tier(6, 8), "intense")

    def test_warm_against_warm_is_what_retired_the_warmth_rule(self):
        """The warmth cap excluded Red, Purple and Orange wherever they appeared.
        Paired with each other they are among the steadiest pairs scored, so the
        rule was dropping the quiet end of its own evidence."""
        clean = set(flicker.blend_pairs(flicker.FLASH_CRITERION_LUMA_DELTA, tolerance="clean"))
        for pair in ((2, 4), (2, 8), (4, 8)):
            self.assertIn(pair, clean)

    def test_a_mildly_flickering_pair_needs_the_tier_above_clean(self):
        """Medium Gray + Light Blue has the smallest ΔY the palette offers and
        was still scored mild, so no luma cut reaches it — only the tier does."""
        cap = flicker.FLASH_CRITERION_LUMA_DELTA
        self.assertEqual(flicker.pair_flicker_tier(12, 14), "mild")
        self.assertNotIn((12, 14), flicker.blend_pairs(cap, tolerance="clean"))
        self.assertIn((12, 14), flicker.blend_pairs(cap, tolerance="subtle"))

    def test_the_advisory_thresholds_sit_under_the_flash_criterion(self):
        """Both thresholds come from photosensitivity guidance, which is the one
        justification for a luma limit here that survived."""
        self.assertLess(flicker.FLASH_CRITERION_LUMA_DELTA, 0.20)
        self.assertLess(flicker.WARN_LUMA_DELTA, flicker.FLASH_CRITERION_LUMA_DELTA)


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
        before = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        after = flicker.build_blend_table(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS)
        self.assertIsNot(before, after)

    def test_the_eligible_pair_set_changes(self):
        """Not a rescaling — a different machine has a different answer about
        which two colors fuse, which is why this cannot be computed once."""
        # Over the full capped set, not the warm-excluded one: the exclusion
        # removes the same four colors on every machine, so filtering first
        # would test how much of the disagreement it happens to have deleted.
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        pepto = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS))
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        u64 = set(flicker.blend_pairs(DEFAULT_LUMA_DELTA, tolerance=ALL_TIERS))
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
        the rest of the scene — invisible on a still, a color shift on motion."""
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
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
        b = cast(FlickerComposeBuffers, mode.compose(gradient()))
        self.assertEqual(b["screen"].shape, (1000,))
        self.assertEqual(b["screen_b"].shape, (1000,))
        self.assertEqual(b["bitmap"].shape, (8000,))
        self.assertTrue(
            (b["screen"] != b["screen_b"]).any(), "no cell blends on a chromatic gradient"
        )

    def test_the_two_pages_differ_only_in_colour(self):
        """A differing mask would flicker geometry rather than color."""
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
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
            FlickerComposeBuffers,
            HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS).compose(flat),
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
        blend = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
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
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS, perceptual=False)
        self.assertTrue(mode._perceptual)
        self.assertIn("pinned", mode.set_color_match("rgb"))
        self.assertTrue(mode._perceptual)

    def test_edges_styles_never_blend(self):
        for style in ("edges", "edges_inverted"):
            with self.subTest(style=style):
                mode = HiresDisplayMode(style, flicker_tolerance=ALL_TIERS)
                self.assertIsNone(mode._blend_table)


class FlickerSetupWarningTest(unittest.TestCase):
    """The loosest tier is the one nobody should stream, so setup says so.
    The structural tests below run at that tier for its pair yield and treat
    the warning as incidental — this is where it gets asserted instead."""

    def test_the_loosest_tier_warns_about_the_seizure_band(self):
        with self.assertLogs("c64cast.video.modes.hires", level="WARNING") as cm:
            HiresDisplayMode("normal", flicker_tolerance="visible").setup(FakeAPI())
        self.assertIn("photosensitive-seizure band", cm.records[0].getMessage())

    def test_a_fusing_tier_warns_about_nothing(self):
        with self.assertNoLogs("c64cast.video.modes.hires", level="WARNING"):
            HiresDisplayMode("normal", flicker_tolerance="clean").setup(FakeAPI())


class FlickerPushTest(unittest.TestCase):
    def setUp(self):
        # ALL_TIERS makes every setup() here emit the seizure-band warning;
        # FlickerSetupWarningTest owns that assertion.
        self.enterContext(quiet_logging())

    def test_setup_installs_the_flicker_handler_and_seeds_the_pages(self):
        api = FakeAPI()
        HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS).setup(api)
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
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
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
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
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
        mode = HiresDisplayMode("normal", flicker_tolerance=ALL_TIERS)
        mode.setup(api)
        api.memories.clear()
        mode.teardown(api)
        self.assertEqual(api.memories.get("D018"), "14")


class FlickerResolveTest(unittest.TestCase):
    def test_opt_in(self):
        self.assertEqual("off", resolve_flicker_tolerance("off", "hires"))
        self.assertEqual("clean", resolve_flicker_tolerance("clean", "hires"))

    def test_hires_normal_only(self):
        self.assertEqual("off", resolve_flicker_tolerance("clean", "mhires"))
        self.assertEqual("off", resolve_flicker_tolerance("clean", "mcm"))
        self.assertEqual("off", resolve_flicker_tolerance("clean", "petscii"))
        self.assertEqual("off", resolve_flicker_tolerance("clean", "hires_edges"))

    def test_refused_when_a_buffer_overlay_owns_the_second_page(self):
        self.assertEqual(
            "off", resolve_flicker_tolerance("clean", "hires", has_buffer_overlays=True)
        )

    def test_refused_while_the_reu_pump_owns_the_irq_vector(self):
        self.assertEqual(
            "off", resolve_flicker_tolerance("clean", "hires", audio_reu_pump_active=True)
        )

    def test_a_plain_hires_scene_actually_gets_a_blend_table(self):
        """End-to-end through the factory, because the gate agreeing in
        isolation proves nothing about what the caller hands it."""
        from c64cast.app.config import Config, SceneCfg
        from c64cast.app.scene_factory import _display_mode_for_scene

        cfg = Config()
        cfg.color.flicker_tolerance = "clean"
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
