"""Tests for the C64 palette quantizer + the colorfulness helpers
(boost_saturation, make_gray_penalty, pick_diverse_top_n)."""

# FakeAPI is a duck-typed stub of Ultimate64API; silence pyright's
# argument-type complaints in the grayscale MHires test.
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

import numpy as np

from c64cast.palette import (
    _PALETTE_HUES_DEG,
    C64_COLOR_NAMES,
    C64_COLORS,
    C64_PALETTE_BGR,
    CHANNEL_BOOST,
    CHROMATIC_INDICES,
    DEFAULT_GRAY_PENALTY,
    DEFAULT_HUE_CORRECTIONS,
    DEFAULT_PALE_PENALTY,
    GRAY_INDICES,
    PALE_INDICES,
    ColorFit,
    ColorFitAccumulator,
    ColorMapAccumulator,
    HueCorrection,
    _hungarian,
    apply_color_fit,
    apply_hue_corrections,
    boost_saturation,
    color_display_name,
    make_gray_penalty,
    parse_channel_boost,
    parse_hue_corrections,
    pick_diverse_top_n,
    quantize_distances,
    quantize_flat,
    resolve_color,
)


class ResolveColorTest(unittest.TestCase):
    def test_display_names_align_with_palette(self):
        self.assertEqual(len(C64_COLOR_NAMES), 16)
        self.assertEqual(C64_COLOR_NAMES[0], "Black")
        self.assertEqual(C64_COLOR_NAMES[12], "Medium Gray")
        self.assertEqual(C64_COLOR_NAMES[14], "Light Blue")
        self.assertEqual(color_display_name(13), "Light Green")

    def test_int_passthrough_in_range(self):
        for i in range(16):
            self.assertEqual(resolve_color(i), i)

    def test_int_out_of_range_raises_without_default(self):
        with self.assertRaises(ValueError):
            resolve_color(16)
        with self.assertRaises(ValueError):
            resolve_color(-1)

    def test_canonical_and_int_valued_string(self):
        self.assertEqual(resolve_color("light blue"), 14)
        self.assertEqual(resolve_color("14"), 14)

    def test_case_insensitive(self):
        self.assertEqual(resolve_color("BLK"), 0)
        self.assertEqual(resolve_color("Light Blue"), 14)
        self.assertEqual(resolve_color("LIGHT GREEN"), 13)

    def test_fuzzy_abbreviations(self):
        self.assertEqual(resolve_color("lgrn"), 13)
        self.assertEqual(resolve_color("blk"), 0)
        self.assertEqual(resolve_color("dgry"), 11)
        self.assertEqual(resolve_color("med gry"), 12)

    def test_gray_variants_all_medium(self):
        for spelling in ("gray", "grey", "gry", "mgry", "mgray", "mgrey"):
            self.assertEqual(resolve_color(spelling), 12, spelling)

    def test_grey_gray_equivalence_for_modifiers(self):
        self.assertEqual(resolve_color("light grey"), resolve_color("light gray"))
        self.assertEqual(resolve_color("dark grey"), 11)

    def test_separators_normalized(self):
        self.assertEqual(resolve_color("light-blue"), 14)
        self.assertEqual(resolve_color("light_green"), 13)

    def test_default_fallback_logs_warning(self):
        with self.assertLogs("c64cast.palette", level="WARNING") as cm:
            self.assertEqual(resolve_color("chartreuse", default=1), 1)
        self.assertTrue(any("chartreuse" in m for m in cm.output))

    def test_unknown_without_default_raises_with_names(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_color("chartreuse")
        self.assertIn("chartreuse", str(ctx.exception))
        self.assertIn("Light Blue", str(ctx.exception))

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            resolve_color(True)

    def test_every_canonical_name_resolves_to_its_index(self):
        for name, idx in C64_COLORS.items():
            self.assertEqual(resolve_color(name), idx, name)


class QuantizeTest(unittest.TestCase):
    def test_palette_entry_quantizes_to_itself(self):
        # Every palette color should be its own nearest neighbor (zero
        # distance to itself, > 0 to everything else).
        for i, bgr in enumerate(C64_PALETTE_BGR):
            idx = quantize_flat(bgr.reshape(1, 3).astype(np.float32))[0]
            self.assertEqual(int(idx), i, f"palette entry {i} did not self-map")

    def test_distances_have_correct_shape_and_self_zero(self):
        d = quantize_distances(C64_PALETTE_BGR)
        self.assertEqual(d.shape, (16, 16))
        # Diagonal is zero — distance from each palette entry to itself.
        np.testing.assert_allclose(np.diag(d), 0.0, atol=1e-3)


class GrayPenaltyTest(unittest.TestCase):
    def test_default_shape_and_values(self):
        p = make_gray_penalty()
        self.assertEqual(p.shape, (16,))
        for i in GRAY_INDICES:
            self.assertEqual(p[i], DEFAULT_GRAY_PENALTY)
        for i in PALE_INDICES:
            self.assertEqual(p[i], DEFAULT_PALE_PENALTY)
        # All other slots untouched.
        touched = set(GRAY_INDICES) | set(PALE_INDICES)
        for i in range(16):
            if i not in touched:
                self.assertEqual(p[i], 0.0)

    def test_zero_disables_penalty(self):
        p = make_gray_penalty(0.0, 0.0)
        np.testing.assert_array_equal(p, np.zeros(16, dtype=np.float32))

    def test_chromatic_strength_penalizes_chromatic_only(self):
        # grayscale palette_mode uses a very large chromatic_strength to
        # force every argmin onto the gray axis. The penalty vector should
        # only touch chromatic indices; gray-axis entries stay untouched.
        p = make_gray_penalty(gray_strength=0.0, pale_strength=0.0, chromatic_strength=1000.0)
        for i in CHROMATIC_INDICES:
            self.assertEqual(p[i], 1000.0)
        for i in GRAY_INDICES:
            self.assertEqual(p[i], 0.0)

    def test_penalty_flips_borderline_pixel_from_gray_to_chromatic(self):
        # A near-gray but slightly-blue-leaning pixel — pick one where
        # default-quantization picks gray. Use a large penalty (10× the
        # default) to guarantee the bias flips it to a chromatic neighbor
        # regardless of where the chosen pixel sits in the BGR space —
        # we're testing the *mechanism*, not the default tuning.
        px = np.array([[140.0, 120.0, 120.0]], dtype=np.float32)  # slight blue
        unbiased = int(quantize_flat(px)[0])
        big_penalty = make_gray_penalty(gray_strength=50000.0, pale_strength=0.0)
        biased = int(np.argmin(quantize_distances(px) + big_penalty, axis=1)[0])
        # Unbiased winner is on the gray axis; biased winner is off it.
        self.assertIn(unbiased, GRAY_INDICES)
        self.assertNotIn(biased, GRAY_INDICES)


class BoostSaturationTest(unittest.TestCase):
    def test_identity_when_factor_is_one(self):
        img = np.random.default_rng(0).integers(0, 256, (10, 10, 3)).astype(np.uint8)
        out = boost_saturation(img, 1.0)
        # Identity means same object back (cheap shortcut in the impl).
        self.assertIs(out, img)

    def test_gray_is_unchanged_by_any_factor(self):
        # Pure gray has zero saturation; multiplying by anything keeps it
        # zero, so the BGR output must match the input.
        gray = np.full((4, 4, 3), 128, dtype=np.uint8)
        out = boost_saturation(gray, 2.5)
        np.testing.assert_array_equal(out, gray)

    def test_saturation_increases_for_chromatic_pixel(self):
        # A muted red — boosting saturation should push it toward pure red
        # (B and G channels drop, R climbs toward 255).
        muted_red = np.array([[[80, 80, 180]]], dtype=np.uint8)  # BGR
        boosted = boost_saturation(muted_red, 2.0)
        # Red channel ≥ original red, blue/green ≤ original.
        self.assertGreaterEqual(int(boosted[0, 0, 2]), int(muted_red[0, 0, 2]))
        self.assertLessEqual(int(boosted[0, 0, 0]), int(muted_red[0, 0, 0]))
        self.assertLessEqual(int(boosted[0, 0, 1]), int(muted_red[0, 0, 1]))


class DiverseTopNTest(unittest.TestCase):
    def test_picks_most_populated_first(self):
        counts = np.zeros(16, dtype=np.int64)
        counts[2] = 1000  # red — most populated
        counts[5] = 500  # green
        counts[6] = 250  # blue
        picks = pick_diverse_top_n(counts, 3)
        self.assertEqual(picks[0], 2)
        self.assertEqual(len(picks), 3)
        self.assertEqual(len(set(picks)), 3)  # all unique

    def test_skips_near_hue_neighbor_in_favor_of_distant_hue(self):
        # Construct counts where slots 2 (red) and 10 (light red) are most
        # populated but very close in hue. Slot 5 (green) is less populated
        # but far in hue. We expect: red → green → light red — green jumps
        # ahead of light red because of the diversity rule.
        counts = np.zeros(16, dtype=np.int64)
        counts[2] = 1000  # red
        counts[10] = 900  # light red (similar hue to red)
        counts[5] = 100  # green (far hue)
        picks = pick_diverse_top_n(counts, 3, min_hue_gap_deg=45.0)
        self.assertEqual(picks[0], 2)
        self.assertEqual(picks[1], 5, f"expected green after red, got {picks}")
        self.assertEqual(picks[2], 10)

    def test_fallback_when_no_diverse_candidate_exists(self):
        # Only red has counts. Diversity rule can't be satisfied, so the
        # picker must fall back to frequency order rather than returning
        # fewer than n slots.
        counts = np.zeros(16, dtype=np.int64)
        counts[2] = 1000
        picks = pick_diverse_top_n(counts, 4)
        self.assertEqual(len(picks), 4)
        self.assertEqual(len(set(picks)), 4)
        self.assertEqual(picks[0], 2)

    def test_gray_axis_entries_filled_via_fallback(self):
        # When chromatic entries run out, gray-axis entries should still
        # appear via the fallback path so n slots are always filled.
        counts = np.zeros(16, dtype=np.int64)
        counts[0] = 500  # black
        counts[1] = 400  # white
        counts[12] = 300  # gray
        picks = pick_diverse_top_n(counts, 3)
        self.assertEqual(sorted(picks), [0, 1, 12])


class PaletteHueClassificationTest(unittest.TestCase):
    def test_gray_axis_entries_marked_nan(self):
        for i in GRAY_INDICES:
            self.assertTrue(
                np.isnan(_PALETTE_HUES_DEG[i]), f"gray-axis entry {i} should have NaN hue"
            )

    def test_chromatic_entries_have_real_hue(self):
        chromatic = set(range(16)) - set(GRAY_INDICES)
        for i in chromatic:
            self.assertFalse(
                np.isnan(_PALETTE_HUES_DEG[i]), f"chromatic entry {i} should have a hue"
            )


class HueCorrectionTest(unittest.TestCase):
    """The global [color] hue-correction pre-quantization step."""

    def _pipeline_index(self, img_bgr, corrections):
        """Run the verified render pipeline order: boost_saturation →
        apply_hue_corrections → CHANNEL_BOOST → quantize, return the index."""
        x = boost_saturation(img_bgr, 1.8)
        x = apply_hue_corrections(x, corrections)
        flat = np.clip(x.reshape(-1, 3).astype(np.float32) * CHANNEL_BOOST, 0, 255)
        return int(quantize_flat(flat)[0])

    def test_rescues_dark_violet_to_purple(self):
        # A dark, blue-leaning violet (TRON arena-wall glyph color, BGR) maps
        # to blue/gray without correction and to C64 purple (index 4) with it.
        violet = np.array([[[120, 30, 70]]], dtype=np.uint8)  # B=120 G=30 R=70
        self.assertNotEqual(
            self._pipeline_index(violet, ()), 4, "violet should NOT reach purple without correction"
        )
        self.assertEqual(
            self._pipeline_index(violet, DEFAULT_HUE_CORRECTIONS),
            4,
            "violet should reach C64 purple with the default rescue",
        )

    def test_empty_table_is_identity(self):
        img = np.array([[[120, 30, 70], [10, 200, 200]]], dtype=np.uint8)
        out = apply_hue_corrections(img, ())
        # Identity AND the same object (no cvtColor roundtrip drift).
        self.assertIs(out, img)

    def test_leaves_colors_outside_band_untouched(self):
        # Saturated red and green sit well outside the violet→magenta band, so
        # the default purple rescue must not alter them at all.
        for bgr in ((20, 20, 220), (20, 200, 20)):
            img = np.array([[bgr]], dtype=np.uint8)
            out = apply_hue_corrections(img, DEFAULT_HUE_CORRECTIONS)
            np.testing.assert_array_equal(out, img)

    def test_parse_validates_and_roundtrips(self):
        hc = parse_hue_corrections(
            [{"hue_lo_deg": 10, "hue_hi_deg": 40, "sat_mult": 1.5, "name": "x"}]
        )
        self.assertEqual(len(hc), 1)
        self.assertIsInstance(hc[0], HueCorrection)
        self.assertEqual(hc[0].name, "x")
        self.assertEqual(hc[0].sat_mult, 1.5)
        # Missing required key.
        with self.assertRaises(ValueError):
            parse_hue_corrections([{"hue_hi_deg": 40}])
        # Out-of-range hue.
        with self.assertRaises(ValueError):
            parse_hue_corrections([{"hue_lo_deg": -5, "hue_hi_deg": 40}])
        # Non-positive multiplier.
        with self.assertRaises(ValueError):
            parse_hue_corrections([{"hue_lo_deg": 10, "hue_hi_deg": 40, "sat_mult": 0}])
        # Unknown key.
        with self.assertRaises(ValueError):
            parse_hue_corrections([{"hue_lo_deg": 10, "hue_hi_deg": 40, "bogus": 1}])


class ColorFitTest(unittest.TestCase):
    """Adaptive per-source color fit ([color].auto_fit)."""

    @staticmethod
    def _solid(bgr: tuple[int, int, int], shape=(40, 40)) -> np.ndarray:
        img = np.empty((*shape, 3), dtype=np.uint8)
        img[:] = bgr
        return img

    def test_apply_color_fit_brightens_and_preserves_hue(self):
        # A dark, low-contrast blue patch.
        dark = self._solid((60, 20, 10))
        fit = ColorFit(black=8.0, white=72.0, sat_mult=1.2)
        out = apply_color_fit(dark, fit)
        # Contrast stretch lifts brightness.
        self.assertGreater(int(out[0, 0].max()), int(dark[0, 0].max()))
        # Blue stays the dominant channel (hue preserved, not recolored).
        self.assertEqual(int(np.argmax(out[0, 0])), 0)

    def test_apply_identity_is_noop(self):
        img = self._solid((30, 120, 200))
        fit = ColorFit(black=0.0, white=255.0, sat_mult=1.0)
        self.assertTrue(fit.is_identity())
        out = apply_color_fit(img, fit)
        self.assertTrue(np.array_equal(out, img))

    def test_accumulator_stretches_dark_flat_source(self):
        # Mid-dark, low-contrast, somewhat desaturated frame → expect a fit
        # that lifts the white point down toward the content and a sat lift.
        acc = ColorFitAccumulator(strength=1.0)
        acc.add(self._solid((50, 60, 70)))
        fit = acc.result()
        assert fit is not None
        self.assertLess(fit.white, 255.0)
        self.assertGreaterEqual(fit.sat_mult, 1.0)
        self.assertLessEqual(fit.sat_mult, 1.6 + 1e-6)

    def test_strength_zero_is_identity_none(self):
        acc = ColorFitAccumulator(strength=0.0)
        acc.add(self._solid((50, 60, 70)))
        self.assertIsNone(acc.result())

    def test_no_samples_returns_none(self):
        self.assertIsNone(ColorFitAccumulator(strength=1.0).result())

    def test_full_range_source_is_near_identity(self):
        # A frame already spanning black→white (full luma range) needs no fit.
        acc = ColorFitAccumulator(strength=1.0)
        img = np.zeros((40, 40, 3), dtype=np.uint8)
        img[:, :20] = (0, 0, 0)  # black half
        img[:, 20:] = (255, 255, 255)  # white half → luma spans 0..255
        acc.add(img)
        fit = acc.result()
        # Either identity (None) or a fit that is effectively identity.
        self.assertTrue(fit is None or fit.is_identity())

    def test_strength_clamped(self):
        self.assertEqual(ColorFitAccumulator(strength=5.0)._strength, 1.0)
        self.assertEqual(ColorFitAccumulator(strength=-1.0)._strength, 0.0)


class HungarianTest(unittest.TestCase):
    """Min-cost assignment used by the forced-palette bijection."""

    def test_identity_assignment(self):
        cost = np.array([[1, 9, 9], [9, 1, 9], [9, 9, 1]], dtype=np.float32)
        np.testing.assert_array_equal(_hungarian(cost), [0, 1, 2])

    def test_picks_min_total_not_min_per_row(self):
        # Greedy per-row would take col 0 for both rows; optimal swaps row 1.
        cost = np.array([[1.0, 2.0], [2.0, 9.0]], dtype=np.float32)
        col = _hungarian(cost)
        self.assertEqual(list(col), [1, 0])

    def test_is_a_bijection(self):
        rng = np.random.default_rng(0)
        cost = rng.random((6, 6)).astype(np.float32)
        col = _hungarian(cost)
        self.assertEqual(sorted(col.tolist()), list(range(6)))


class ColorMapTest(unittest.TestCase):
    """Forced-palette remap ([color].force_palette)."""

    @staticmethod
    def _three_region_image() -> np.ndarray:
        # Three flat, well-separated color regions (BGR): dark blue, dark red,
        # light gray — a TRON-like gamut cluster plus a highlight.
        img = np.zeros((90, 60, 3), dtype=np.uint8)
        img[:30] = (90, 30, 10)
        img[30:60] = (10, 10, 100)
        img[60:] = (200, 200, 200)
        return img

    def test_apply_emits_only_palette_colors(self):
        acc = ColorMapAccumulator(n_colors=8)
        acc.add(self._three_region_image())
        cmap = acc.result()
        assert cmap is not None
        out = cmap.apply(self._three_region_image())
        palette = {tuple(c) for c in C64_PALETTE_BGR.astype(np.uint8).tolist()}
        for color in np.unique(out.reshape(-1, 3), axis=0):
            self.assertIn(tuple(int(x) for x in color), palette)

    def test_distinct_regions_map_to_distinct_colors(self):
        acc = ColorMapAccumulator(n_colors=8)
        acc.add(self._three_region_image())
        cmap = acc.result()
        assert cmap is not None
        out = cmap.apply(self._three_region_image())
        # Each of the 3 flat regions is one solid color; they must differ.
        top, mid, bot = out[10, 30], out[45, 30], out[75, 30]
        self.assertFalse(np.array_equal(top, mid))
        self.assertFalse(np.array_equal(mid, bot))
        self.assertFalse(np.array_equal(top, bot))

    def test_explicit_indices_constrain_output_palette(self):
        whitelist = [0, 2, 6]  # black, red, blue
        acc = ColorMapAccumulator(indices=whitelist)
        acc.add(self._three_region_image())
        cmap = acc.result()
        assert cmap is not None
        self.assertTrue(set(cmap.indices).issubset(set(whitelist)))
        out = cmap.apply(self._three_region_image())
        allowed = {tuple(C64_PALETTE_BGR[i].astype(np.uint8).tolist()) for i in whitelist}
        for color in np.unique(out.reshape(-1, 3), axis=0):
            self.assertIn(tuple(int(x) for x in color), allowed)

    def test_no_samples_returns_none(self):
        self.assertIsNone(ColorMapAccumulator(n_colors=8).result())


class DisplayModePaletteTest(unittest.TestCase):
    """Smoke tests for MCMDisplayMode / MultiHiresDisplayMode palette_mode."""

    def _fake_frame(self):
        # 240×320 BGR frame with a few distinct color regions.
        f = np.zeros((240, 320, 3), dtype=np.uint8)
        f[:, :80] = (40, 40, 200)  # red-ish
        f[:, 80:160] = (40, 180, 40)  # green-ish
        f[:, 160:240] = (180, 40, 40)  # blue-ish
        f[:, 240:] = (180, 180, 180)  # light gray
        return f

    def test_mcm_palette_mode_validation(self):
        from c64cast.modes import MCMDisplayMode

        with self.assertRaises(ValueError):
            MCMDisplayMode(palette_mode="bogus")
        # All valid modes construct cleanly. percell is accepted as an
        # alias for cheap (MCM already picks fg per cell).
        for mode in ("cheap", "vivid", "grayscale", "percell"):
            MCMDisplayMode(palette_mode=mode)
        # Color shaping is global now: every mode carries the default purple
        # rescue + a 3-vector channel boost, regardless of palette_mode.
        m = MCMDisplayMode(palette_mode="percell")
        self.assertEqual(m._hue_corrections, DEFAULT_HUE_CORRECTIONS)
        self.assertEqual(tuple(m._channel_boost), tuple(CHANNEL_BOOST))

    def test_mhires_palette_mode_validation(self):
        from c64cast.modes import MultiHiresDisplayMode

        with self.assertRaises(ValueError):
            MultiHiresDisplayMode(palette_mode="bogus")
        for mode in ("cheap", "vivid", "grayscale", "percell"):
            MultiHiresDisplayMode(palette_mode=mode)
        m = MultiHiresDisplayMode(palette_mode="percell")
        self.assertEqual(m._hue_corrections, DEFAULT_HUE_CORRECTIONS)
        self.assertEqual(tuple(m._channel_boost), tuple(CHANNEL_BOOST))

    def test_color_shaping_is_global_and_configurable(self):
        # [color] shaping applies to every chromatic mode and is fully
        # configurable: bands extend the defaults, replace+[] disables them,
        # and channel_boost overrides the built-in BGR gain.
        from c64cast.modes import MCMDisplayMode, MultiHiresDisplayMode

        for cls in (MCMDisplayMode, MultiHiresDisplayMode):
            extended = cls(
                palette_mode="percell", hue_corrections=[{"hue_lo_deg": 10, "hue_hi_deg": 40}]
            )
            self.assertEqual(len(extended._hue_corrections), len(DEFAULT_HUE_CORRECTIONS) + 1)
            disabled = cls(palette_mode="vivid", hue_corrections=[], hue_corrections_replace=True)
            self.assertEqual(disabled._hue_corrections, ())
            boosted = cls(palette_mode="cheap", channel_boost=[1.0, 1.0, 1.0])
            self.assertEqual(tuple(boosted._channel_boost), (1.0, 1.0, 1.0))

    def test_parse_channel_boost(self):
        # Empty / None → built-in default; valid → float32 (3,); bad → raise.
        self.assertIs(parse_channel_boost(None), CHANNEL_BOOST)
        self.assertIs(parse_channel_boost([]), CHANNEL_BOOST)
        out = parse_channel_boost([1.1, 1.2, 1.3])
        self.assertEqual(out.shape, (3,))
        self.assertEqual(out.dtype, np.float32)
        with self.assertRaises(ValueError):
            parse_channel_boost([1.0, 1.0])  # wrong length
        with self.assertRaises(ValueError):
            parse_channel_boost([1.0, 0.0, 1.0])  # non-positive

    def test_mhires_percell_writes_nonconstant_screen_and_color_ram(self):
        # The global modes uploaded one repeated byte to $0400 and $D800.
        # percell uses both as per-cell c1/c2/c3 carriers, so the 1000-byte
        # writes must contain more than one distinct value on a frame with
        # varied per-cell content.
        #
        # NOTE: the shared _fake_frame's 80-px-wide solid color bands leave
        # every 4×8 cell single-colored, so the per-cell histogram has only
        # one nonzero bin (and zero nonzero bins inside the bg0 band).
        # np.argpartition then fills the top-3 from tied-at-zero indices,
        # whose order is implementation-defined — on some numpy builds every
        # cell picks the same arbitrary trio and color RAM collapses to one
        # byte. Use a smooth BGR gradient instead so each cell has multiple
        # distinct palette indices and the top-3 picks are driven by real
        # signal rather than argpartition tiebreaks.
        from _fakes import FakeAPI

        from c64cast.modes import MultiHiresDisplayMode

        f = np.zeros((240, 320, 3), dtype=np.uint8)
        yy = np.linspace(0, 255, 240, dtype=np.uint8)[:, None]
        xx = np.linspace(0, 255, 320, dtype=np.uint8)[None, :]
        f[..., 0] = xx
        f[..., 1] = yy
        f[..., 2] = 255 - xx
        api = FakeAPI()
        m = MultiHiresDisplayMode(palette_mode="percell")
        m.setup(api)
        m.render(api, f)
        screen = api.regions[0x0400]
        color = api.regions[0xD800]
        self.assertEqual(len(screen), 1000)
        self.assertEqual(len(color), 1000)
        self.assertGreater(
            len(set(screen)), 1, "percell should write varied screen RAM, not constant"
        )
        self.assertGreater(
            len(set(color)), 1, "percell should write varied color RAM, not constant"
        )

    def test_mhires_percell_is_stable_on_identical_frames(self):
        # Per-cell EMA on the top-3 picks means rendering the same frame
        # twice in a row must produce byte-identical screen + color + bitmap
        # output from frame 2 onwards (once the EMA state is seeded), so a
        # static webcam scene doesn't flicker. The old unsmoothed path
        # could flip the 3rd top-3 slot on borderline-tied cells every frame.
        from _fakes import FakeAPI

        from c64cast.modes import MultiHiresDisplayMode

        api = FakeAPI()
        m = MultiHiresDisplayMode(palette_mode="percell")
        m.setup(api)
        frame = self._fake_frame()
        m.render(api, frame)
        screen1 = bytes(api.regions[0x0400])
        color1 = bytes(api.regions[0xD800])
        bitmap1 = bytes(api.regions[0x2000])
        # Re-render the same frame; outputs must match byte-for-byte.
        m.render(api, frame)
        self.assertEqual(bytes(api.regions[0x0400]), screen1)
        self.assertEqual(bytes(api.regions[0xD800]), color1)
        self.assertEqual(bytes(api.regions[0x2000]), bitmap1)

    def test_mhires_percell_hysteresis_suppresses_noisy_pixel_flicker(self):
        # Per-pixel bitmap-code hysteresis: pixels at a near-tied chromatic
        # boundary used to flip code every frame as sensor noise nudged
        # them across. With hysteresis, the previous code "sticks" unless
        # an alternative is meaningfully better. Build a frame whose first
        # render quantises to a stable {bg0, c1, c2, c3} set, then perturb
        # a handful of pixels by a tiny BGR delta — the bitmap output
        # should be byte-identical, demonstrating the sticky behaviour.
        from _fakes import FakeAPI

        from c64cast.modes import MultiHiresDisplayMode

        rng = np.random.default_rng(42)
        api = FakeAPI()
        m = MultiHiresDisplayMode(palette_mode="percell")
        m.setup(api)
        frame = self._fake_frame()
        # Two passes to seed both the EMA (cell counts) and the hysteresis
        # state (_last_codes / _last_cand).
        m.render(api, frame)
        m.render(api, frame)
        bitmap0 = bytes(api.regions[0x2000])

        # Perturb a small number of pixels by ±1 in each BGR channel — the
        # kind of noise a webcam sensor adds. Without hysteresis this would
        # flip the bitmap code for any pixel sitting on a palette boundary.
        noisy = frame.copy()
        mask = rng.random(noisy.shape[:2]) < 0.02
        noise = rng.integers(-1, 2, size=(*noisy.shape[:2], 3), dtype=np.int16)
        perturbed = np.clip(noisy.astype(np.int16) + noise * mask[..., None], 0, 255).astype(
            np.uint8
        )
        m.render(api, perturbed)
        bitmap1 = bytes(api.regions[0x2000])
        # Most bytes should still match — hysteresis filters the noise.
        same = sum(a == b for a, b in zip(bitmap0, bitmap1, strict=True))
        # 8000 bytes total; allow modest drift for cells where the noise
        # genuinely pushes the cell histogram. ≥95% byte match is the bar.
        self.assertGreaterEqual(
            same,
            7600,
            f"hysteresis allowed too many bitmap flips: {8000 - same} bytes changed under ±1 noise",
        )

    def test_mhires_percell_fills_unused_slots_with_bg0_not_garbage(self):
        # Per-cell slot rules:
        #   * a cell with >=3 distinct non-bg0 colors fills c1/c2/c3 with REAL
        #     colors (bg0 is free via the %00 code, so it never displaces one);
        #   * a cell with FEWER present non-bg0 colors pads the leftover slots
        #     with bg0 — NEVER an absent ("garbage") palette index. The old
        #     padding grabbed arbitrary zero-count indices, which leaked an
        #     out-of-palette color (e.g. green) that tore into view on a slow
        #     transport. So screen/color RAM only ever carries bg0 or a color
        #     genuinely present in that cell.
        from c64cast.modes import MultiHiresDisplayMode

        m = MultiHiresDisplayMode(palette_mode="percell")
        # Synthetic per-cell layout (so the present set per cell is exact, no
        # quantization ambiguity): half the cells are "rich" (bg0=0 + the three
        # colors 4/6/14), half are "sparse" (bg0=0 + only purple=4). Build in
        # cell-major (1000, 32) order, then invert compose's cell reshape to the
        # flat (32000,) pixel order and feed a clean one-hot distance matrix.
        cells = np.zeros((1000, 32), dtype=np.int64)
        cells[:500, :16] = 0
        cells[:500, 16:22] = 4
        cells[:500, 22:28] = 6
        cells[:500, 28:] = 14
        cells[500:, :26] = 0
        cells[500:, 26:] = 4
        quantized = cells.reshape(25, 40, 8, 4).transpose(0, 2, 1, 3).reshape(32000)
        d = np.full((32000, 16), 1e6, dtype=np.float32)
        d[np.arange(32000), quantized] = 0.0

        flat = np.zeros((32000, 3), dtype=np.float32)  # unused: dither_method="none"
        _bitmap, screen, color, bg0 = m._compose_percell(d, flat)
        self.assertEqual(bg0, 0)
        for i in range(1000):
            slots = {(screen[i] >> 4) & 0x0F, screen[i] & 0x0F, color[i] & 0x0F}
            present = set(cells[i].tolist())  # {0,4,6,14} or {0,4}
            self.assertTrue(
                slots <= present,
                f"cell {i} slots {sorted(slots)} carry a color absent from {sorted(present)}",
            )
            if i < 500:
                # All three real colors present → no slot wasted on bg0.
                self.assertEqual(slots, {4, 6, 14}, f"rich cell {i} dropped a real color")
            else:
                # Only purple present → it must win a slot, rest padded bg0.
                self.assertIn(4, slots, f"sparse cell {i} lost its only color")
                self.assertTrue(slots <= {0, 4}, f"sparse cell {i} leaked garbage")

    def test_mcm_cheap_compose_produces_three_bg_colors(self):
        from c64cast.modes import MCMDisplayMode

        m = MCMDisplayMode(palette_mode="cheap")
        out = m.compose(self._fake_frame())
        self.assertIn("screen", out)
        self.assertIn("color", out)
        self.assertIn("bg", out)
        self.assertEqual(out["screen"].shape, (1000,))
        self.assertEqual(out["color"].shape, (1000,))
        self.assertEqual(out["bg"].shape, (3,))

    def test_mcm_vivid_picks_more_diverse_bgs_than_cheap(self):
        # On a 4-region frame with gray + 3 chromatic, the vivid picker
        # should reach for the chromatic entries instead of letting any
        # remaining gray-axis variants take 2 of the 3 bg slots.
        from c64cast.modes import MCMDisplayMode

        cheap = MCMDisplayMode(palette_mode="cheap").compose(self._fake_frame())
        vivid = MCMDisplayMode(palette_mode="vivid").compose(self._fake_frame())
        cheap_gray = sum(1 for i in cheap["bg"] if int(i) in GRAY_INDICES)
        vivid_gray = sum(1 for i in vivid["bg"] if int(i) in GRAY_INDICES)
        # Vivid mode should pick no more gray-axis bgs than cheap mode.
        self.assertLessEqual(vivid_gray, cheap_gray)


class GrayscaleModeTest(unittest.TestCase):
    """grayscale palette_mode forces every per-pixel argmin onto the gray
    axis. The bg picker has no special path, but counts will only be
    non-zero for gray-axis entries, so it picks gray entries naturally."""

    def _fake_frame(self):
        # Same 4-region frame as DisplayModePaletteTest — chromatic + gray.
        f = np.zeros((240, 320, 3), dtype=np.uint8)
        f[:, :80] = (40, 40, 200)
        f[:, 80:160] = (40, 180, 40)
        f[:, 160:240] = (180, 40, 40)
        f[:, 240:] = (180, 180, 180)
        return f

    def test_mcm_grayscale_picks_only_gray_axis_bgs(self):
        from c64cast.modes import MCMDisplayMode

        m = MCMDisplayMode(palette_mode="grayscale")
        out = m.compose(self._fake_frame())
        for slot in out["bg"]:
            self.assertIn(int(slot), GRAY_INDICES, f"bg slot {int(slot)} is not on the gray axis")
        # Per-cell FG: low 3 bits of color RAM = FG (MCM uses bit 3 as the
        # multicolor flag, so only palette indices 0..7 are reachable as
        # FG). The two gray-axis entries in that range are 0 (black) and
        # 1 (white) — every FG should be one of those.
        gray_in_fg_range = {i for i in GRAY_INDICES if i < 8}
        for i, fg in enumerate(out["color"]):
            self.assertIn(
                int(fg) & 0x07, gray_in_fg_range, f"cell {i} FG {int(fg) & 0x07} is not gray-axis"
            )

    def test_mhires_grayscale_picks_only_gray_axis_globals(self):
        # MultiHires.render() doesn't return buffers; capture via FakeAPI.
        from _fakes import FakeAPI

        from c64cast.modes import MultiHiresDisplayMode

        api = FakeAPI()
        m = MultiHiresDisplayMode(palette_mode="grayscale")
        m.setup(api)
        m.render(api, self._fake_frame())
        # bg0 is written via write_regs("d021", bg0).
        bg0 = api.regs["D021"][0]
        self.assertIn(int(bg0), GRAY_INDICES)
        # Screen RAM byte = (c1 << 4) | c2.
        screen_byte = api.regions[0x0400][0]
        c1 = (screen_byte >> 4) & 0x0F
        c2 = screen_byte & 0x0F
        # Color RAM byte = c3 (broadcast across all cells).
        c3 = api.regions[0xD800][0] & 0x0F
        for label, idx in (("c1", c1), ("c2", c2), ("c3", c3)):
            self.assertIn(idx, GRAY_INDICES, f"global slot {label}={idx} is not on the gray axis")


class CycleStyleTest(unittest.TestCase):
    """SHIFT-driven cycle: each display mode advances through its own list."""

    def test_mcm_cycle_rotates_palette_mode(self):
        from _fakes import FakeAPI

        from c64cast.modes import PALETTE_MODES, MCMDisplayMode

        api = FakeAPI()
        m = MCMDisplayMode(palette_mode="cheap")
        # Cycle through every mode + back to the start.
        seen = [m.palette_mode]
        for _ in range(len(PALETTE_MODES)):
            label = m.cycle_style(api)
            self.assertIsNotNone(label)
            self.assertIn(m.palette_mode, label)
            seen.append(m.palette_mode)
        # Visited each palette mode at least once, returned to start.
        self.assertEqual(set(seen), set(PALETTE_MODES))
        self.assertEqual(seen[0], seen[-1])
        # Cache invalidated on each cycle so the next push fully repaints.
        self.assertEqual(api.cache_invalidations, len(PALETTE_MODES))

    def test_mcm_cycle_into_grayscale_sets_fixed_bg(self):
        # Cycling into grayscale must populate _fixed_bg so the next
        # compose() uses the fixed slot assignment, not adaptive picks.
        from _fakes import FakeAPI

        from c64cast.modes import MCMDisplayMode

        api = FakeAPI()
        m = MCMDisplayMode(palette_mode="vivid")
        self.assertIsNone(m._fixed_bg)
        while m.palette_mode != "grayscale":
            m.cycle_style(api)
        self.assertIsNotNone(m._fixed_bg)
        # Cycling out of grayscale clears it again.
        while m.palette_mode == "grayscale":
            m.cycle_style(api)
        self.assertIsNone(m._fixed_bg)

    def test_mhires_cycle_into_grayscale_populates_lut(self):
        from _fakes import FakeAPI

        from c64cast.modes import MultiHiresDisplayMode

        api = FakeAPI()
        m = MultiHiresDisplayMode(palette_mode="cheap")
        self.assertIsNone(m._fixed_lut)
        while m.palette_mode != "grayscale":
            m.cycle_style(api)
        self.assertIsNotNone(m._fixed_lut)
        # And clears it when leaving grayscale.
        while m.palette_mode == "grayscale":
            m.cycle_style(api)
        self.assertIsNone(m._fixed_lut)

    def test_hires_cycle_rotates_through_styles(self):
        from _fakes import FakeAPI

        from c64cast.modes import HIRES_STYLES, HiresDisplayMode

        api = FakeAPI()
        m = HiresDisplayMode(style="normal")
        seen = [m.style]
        for _ in range(len(HIRES_STYLES)):
            label = m.cycle_style(api)
            self.assertIsNotNone(label)
            self.assertIn(m.style, label)
            seen.append(m.style)
        self.assertEqual(set(seen), set(HIRES_STYLES))
        self.assertEqual(seen[0], seen[-1])

    def test_hires_style_validation(self):
        from c64cast.modes import HiresDisplayMode

        with self.assertRaises(ValueError):
            HiresDisplayMode(style="bogus")
        HiresDisplayMode(style="normal")
        HiresDisplayMode(style="edges")
        HiresDisplayMode(style="edges_inverted")


if __name__ == "__main__":
    unittest.main()
