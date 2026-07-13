"""Tests for the composable-scene building blocks: generative frame sources,
pixel effects, the FrameSource/AudioSource protocols, SourceScene, and the
config wiring for `type = "generative"` + per-scene `effect`."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import cast

import numpy as np

from c64cast import generators
from c64cast.audio import AudioStreamer
from c64cast.audio_source import MicAudioSource, NullAudioSource
from c64cast.backend import C64Backend, HardwareProfile
from c64cast.config import AudioCfg, Config, SceneCfg, build_scene, validate_scene_cfg
from c64cast.effects import (
    BlurEffect,
    FrameEffect,
    PulseEffect,
    RgbShiftEffect,
    TrailsEffect,
    build_effect,
)
from c64cast.frame_source import BaseFrameSource, FrameSource
from c64cast.generators import build_generator, generator_names
from c64cast.modes import DisplayMode
from c64cast.scenes import Scene, SourceScene, _render_with_overlays


class GeneratorTest(unittest.TestCase):
    def test_registry_nonempty_and_named(self):
        names = generator_names()
        self.assertIn("plasma", names)
        self.assertIn("tunnel", names)
        self.assertIn("fire", names)
        self.assertIn("mandelbrot", names)
        self.assertIn("moire2", names)
        self.assertIn("halo", names)
        self.assertIn("epicycle", names)
        self.assertIn("hopalong", names)
        self.assertIn("rorschach", names)
        self.assertIn("hiphotic", names)
        self.assertIn("metaballs", names)
        self.assertIn("rotozoomer", names)

    def test_live_params_declared_with_valid_ranges(self):
        # midi_control.py scales a CC into each declared (min, max) range and
        # setattr()s it directly — a malformed range would silently corrupt
        # a live-performance param sweep, so pin the shape here.
        expected = {
            "plasma": {"speed", "scale"},
            "tunnel": {"speed", "scale"},
            "fire": {"scroll_speed", "intensity"},
            "mandelbrot": {"zoom_speed", "cycle_speed"},
            "moire2": {"ring_freq", "drift_speed"},
            "halo": {"drift_speed", "pulse_speed"},
            "epicycle": {"speed"},
            "hopalong": {"a", "drift_speed"},
            "rorschach": {"grow_speed"},
            "hiphotic": {"speed", "scale"},
            "metaballs": {"speed"},
            "rotozoomer": {"speed", "scale"},
        }
        for name in generator_names():
            g = build_generator(name)
            live_params = g.LIVE_PARAMS
            self.assertEqual(set(live_params), expected.get(name, set()), name)
            for param, (lo, hi) in live_params.items():
                self.assertLess(lo, hi, f"{name}.{param}")
                self.assertTrue(hasattr(g, param), f"{name}.{param} not a real attribute")

    def test_live_params_settable_via_generic_setattr(self):
        # The exact mechanism midi_control.py uses: setattr(obj, name, val)
        # with no per-class wiring. Constructed directly (not via the
        # registry) so the concrete type declares `speed` for pyright.
        g = generators.PlasmaSource()
        lo, hi = g.LIVE_PARAMS["speed"]
        mid = lo + 0.5 * (hi - lo)
        setattr(g, "speed", mid)  # noqa: B010 — exercises the dynamic-name path deliberately
        self.assertAlmostEqual(g.speed, mid)

    def test_plasma_frame_shape_and_determinism(self):
        g = build_generator("plasma")
        f0 = g.render(0.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        # Deterministic in t, but varies as t advances.
        np.testing.assert_array_equal(f0, g.render(0.0))
        self.assertFalse(np.array_equal(f0, g.render(1.0)))

    def test_is_frame_source(self):
        g = build_generator("tunnel")
        self.assertIsInstance(g, FrameSource)
        self.assertFalse(g.finished)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            build_generator("does-not-exist")

    def test_unmodulated_path_identical_to_pure_time(self):
        # The determinism guard: render(t, None) and read(t) must be byte-for-byte
        # the historical pure-time output for every generator (the offline
        # renderer + drift tests rely on this — even fire, whose scroll is a
        # pure function of t rather than a stateful cellular sim).
        for name in generator_names():
            g = build_generator(name)
            np.testing.assert_array_equal(g.render(0.7), g.render(0.7, None))
            np.testing.assert_array_equal(g.read(0.7), g.render(0.7, None))
            self.assertFalse(np.array_equal(g.render(0.0), g.render(1.0)))  # animates

    def test_fire_flares_with_level_and_onset(self):
        # Fire's headline reaction: a transient + loudness push the heat field
        # toward the white-hot end of COLORMAP_HOT, so the reactive frame is
        # strictly brighter than the resting fire — the flames leap on the beat.
        from c64cast.modulation import MusicModulation

        g = build_generator("fire")
        rest = g.render(0.5)  # pure path
        flare = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(0.5, flare).sum()), int(rest.sum()))

    def test_fire_intensity_raises_heat(self):
        # The ix live knob: a higher intensity scales the whole heat field up,
        # so more of the frame reaches the white-hot end (brighter overall);
        # a lower one dims it. Default 1.0 is the baseline.
        base = generators.FireSource().render(0.5)
        hot = generators.FireSource(intensity=2.0).render(0.5)
        cool = generators.FireSource(intensity=0.3).render(0.5)
        self.assertGreater(int(hot.sum()), int(base.sum()))
        self.assertLess(int(cool.sum()), int(base.sum()))

    def test_tunnel_scale_changes_ring_density(self):
        # The ix live knob: `scale` multiplies the depth coefficient, changing
        # the concentric-ring density, so the rendered frame differs from the
        # baseline. Default 1.0 reproduces the historical output.
        base = generators.TunnelSource().render(0.5)
        dense = generators.TunnelSource(scale=4.0).render(0.5)
        self.assertEqual(base.shape, dense.shape)
        self.assertFalse(np.array_equal(base, dense))

    def test_modulation_changes_output(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("plasma")
        base = g.render(1.0)  # pure path
        mod = MusicModulation(
            level=0.5,
            onset=1.0,
            beat_phase=5.0,
            bpm=140.0,
            voice_freqs=(440.0, 0.0, 0.0),
            voice_gates=(True, False, False),
        )
        self.assertFalse(np.array_equal(base, g.render(1.0, mod)))

    def test_onset_flashes_brightness(self):
        # A transient (onset=1) must brighten the frame versus the same modulation
        # with onset=0 (the "color pulse / flash" behavior).
        from c64cast.modulation import MusicModulation

        g = build_generator("plasma")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_beat_phase_advances_hue(self):
        # A larger accumulated beat_phase shifts the hue (tempo-driven cycling),
        # so frames at different beat_phase differ.
        from c64cast.modulation import MusicModulation

        g = build_generator("plasma")
        m0 = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        m1 = MusicModulation(0.3, 0.0, 2.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertFalse(np.array_equal(g.render(1.0, m0), g.render(1.0, m1)))

    def test_mandelbrot_frame_shape_and_determinism(self):
        g = build_generator("mandelbrot")
        f0 = g.render(0.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.0))
        self.assertFalse(np.array_equal(f0, g.render(30.0)))  # zoom has advanced

    def test_mandelbrot_interior_is_black(self):
        # The starting (scale=1) view frames the whole set, so some pixels
        # must land strictly inside it (never escape) and render pure black
        # regardless of the cycling hue.
        g = build_generator("mandelbrot")
        frame = g.render(0.0)
        self.assertTrue((frame.sum(axis=-1) == 0).any())

    def test_mandelbrot_onset_flashes_brightness(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("mandelbrot")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_moire2_frame_shape_and_reacts_to_voice_freq(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("moire2")
        f0 = g.render(2.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(2.0))
        base = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        driven = MusicModulation(0.3, 0.0, 0.0, 120.0, (200.0, 0.0, 0.0), (True, False, False))
        # A driving voice pitch nudges ring_a's frequency, changing the field.
        self.assertFalse(np.array_equal(g.render(2.0, base), g.render(2.0, driven)))

    def test_halo_frame_shape_and_onset_spawns_extra_halo(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("halo")
        f0 = g.render(1.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(1.0))
        rest = MusicModulation(0.2, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.2, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        # The onset-triggered center halo only appears when onset > 0.
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_halo_level_grows_radius(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("halo")
        quiet = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        loud = MusicModulation(1.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        # Bigger halos ⇒ more lit pixels overall.
        self.assertGreater(int(g.render(1.0, loud).sum()), int(g.render(1.0, quiet).sum()))

    def test_epicycle_frame_shape_and_voice_freq_changes_shape(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("epicycle")
        f0 = g.render(3.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(3.0))
        base = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        driven = MusicModulation(0.3, 0.0, 0.0, 120.0, (300.0, 150.0, 0.0), (True, True, False))
        self.assertFalse(np.array_equal(g.render(3.0, base), g.render(3.0, driven)))

    def test_epicycle_onset_flashes_brightness(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("epicycle")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_hopalong_frame_shape_and_determinism(self):
        g = build_generator("hopalong")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(5.0)))  # `a` has drifted

    def test_hopalong_reacts_to_level_and_onset(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("hopalong")
        rest = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.8, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        # Perturbing `a` reshapes the attractor entirely (chaotic sensitivity).
        self.assertFalse(np.array_equal(g.render(1.0, rest), g.render(1.0, hit)))

    def test_rorschach_frame_shape_and_grows_over_time(self):
        g = build_generator("rorschach")
        f0 = g.render(0.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.0))
        # More of the walk is revealed partway into the grow cycle than at
        # the very start ⇒ strictly more lit pixels.
        self.assertGreater(int(g.render(5.0).sum()), int(f0.sum()))

    def test_rorschach_mirror_symmetric(self):
        g = build_generator("rorschach")
        frame = g.render(5.0)
        mask = frame.sum(axis=-1) > 0
        # Mirrored across the vertical center column.
        np.testing.assert_array_equal(mask, mask[:, ::-1])

    def test_rorschach_onset_jumps_reveal(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("rorschach")
        rest = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(0.0, hit).sum()), int(g.render(0.0, rest).sum()))

    def test_hiphotic_frame_shape_and_determinism(self):
        g = build_generator("hiphotic")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_hiphotic_reacts_to_beat_phase(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("hiphotic")
        m0 = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        m1 = MusicModulation(0.3, 0.0, 2.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertFalse(np.array_equal(g.render(1.0, m0), g.render(1.0, m1)))

    def test_metaballs_frame_shape_and_determinism(self):
        g = build_generator("metaballs")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_metaballs_onset_flashes_brightness(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("metaballs")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_rotozoomer_frame_shape_and_determinism(self):
        g = build_generator("rotozoomer")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_rotozoomer_scale_changes_frame(self):
        # The ix live knob: `scale` feeds the affine zoom factor directly.
        base = generators.RotozoomerSource().render(0.5)
        zoomed = generators.RotozoomerSource(scale=3.0).render(0.5)
        self.assertEqual(base.shape, zoomed.shape)
        self.assertFalse(np.array_equal(base, zoomed))

    def test_rotozoomer_onset_flashes_brightness(self):
        from c64cast.modulation import MusicModulation

        g = build_generator("rotozoomer")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))


class EffectTest(unittest.TestCase):
    def test_live_params_declared_with_valid_ranges(self):
        # midi_control.py scales a CC into each declared (min, max) range and
        # setattr()s it directly — pin the shape for every registered
        # effect. pulse/rgb_shift expose `intensity` (the sx/ix reaction-depth
        # knob; a visible no-op only because they're inert without modulation).
        expected = {
            "trails": {"decay"},
            "pulse": {"intensity"},
            "rgb_shift": {"intensity"},
            "blur": {"intensity"},
        }
        for name in expected:
            eff = build_effect(name)
            live_params = eff.LIVE_PARAMS
            self.assertEqual(set(live_params), expected[name], name)
            for param, (lo, hi) in live_params.items():
                self.assertLess(lo, hi, f"{name}.{param}")
                self.assertTrue(hasattr(eff, param), f"{name}.{param} not a real attribute")

    def test_live_params_settable_via_generic_setattr(self):
        eff = TrailsEffect()
        lo, hi = eff.LIVE_PARAMS["decay"]
        mid = lo + 0.5 * (hi - lo)
        setattr(eff, "decay", mid)  # noqa: B010 — exercises the dynamic-name path deliberately
        self.assertAlmostEqual(eff.decay, mid)

    def test_trails_first_frame_passthrough_then_blends(self):
        eff = build_effect("trails")
        a = np.zeros((4, 4, 3), np.uint8)
        a[0, 0] = 255
        # First frame: returned unchanged (no prior state).
        np.testing.assert_array_equal(eff.apply(a, 0.0), a)
        # Next frame all-black: should still show a decayed trail of `a`.
        out = eff.apply(np.zeros((4, 4, 3), np.uint8), 1.0)
        self.assertGreater(int(out[0, 0].max()), 0)

    def test_trails_reset_clears_state(self):
        eff = TrailsEffect()
        eff.apply(np.full((2, 2, 3), 200, np.uint8), 0.0)
        eff.reset()
        self.assertIsNone(eff._prev)
        # After reset, an all-black frame comes back black (no trail).
        out = eff.apply(np.zeros((2, 2, 3), np.uint8), 0.0)
        self.assertEqual(int(out.max()), 0)

    def test_unknown_effect_raises(self):
        with self.assertRaises(ValueError):
            build_effect("nope")

    def test_trails_reactive_decay_lengthens_tail(self):
        # A transient + loudness raise the effective decay, so more of a prior
        # bright frame survives into the next — a brighter/longer tail than the
        # unmodulated baseline. Drives "the trail blooms on the beat".
        from c64cast.modulation import MusicModulation

        bright = np.full((2, 2, 3), 200, np.uint8)
        black = np.zeros((2, 2, 3), np.uint8)
        hot = MusicModulation(0.8, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))

        plain = build_effect("trails")
        plain.apply(bright, 0.0)
        plain_tail = plain.apply(black, 0.1)  # no modulation = baseline decay

        react = build_effect("trails")
        react.apply(bright, 0.0)
        react_tail = react.apply(black, 0.1, hot)  # higher decay

        self.assertGreater(int(react_tail.max()), int(plain_tail.max()))

    def test_pulse_identity_without_modulation(self):
        # No modulation ⇒ identity (the determinism guard for non-reactive scenes).
        eff = build_effect("pulse")
        f = np.random.default_rng(1).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        np.testing.assert_array_equal(eff.apply(f, 0.0), f)
        np.testing.assert_array_equal(eff.apply(f, 0.0, None), f)

    def test_pulse_zooms_on_onset(self):
        from c64cast.modulation import MusicModulation

        eff = build_effect("pulse")
        f = np.zeros((8, 8, 3), np.uint8)
        f[3:5, 3:5] = 255  # non-uniform so a zoom is detectable
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        out = eff.apply(f, 0.0, hit)
        self.assertEqual(out.shape, f.shape)
        self.assertFalse(np.array_equal(out, f))

    def test_pulse_silent_modulation_is_noop(self):
        # A modulation present but with no transient/loudness ⇒ scale 1.0 ⇒ no-op.
        from c64cast.modulation import MusicModulation

        eff = build_effect("pulse")
        f = np.random.default_rng(4).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        silent = MusicModulation(0.0, 0.0, 0.0, 0.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, silent), f)

    def test_pulse_intensity_zero_is_identity_under_modulation(self):
        # intensity=0 scales the whole reaction away ⇒ scale collapses to 1.0
        # ⇒ identity even with a full transient present.
        from c64cast.modulation import MusicModulation

        eff = PulseEffect(intensity=0.0)
        f = np.random.default_rng(6).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        hit = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, hit), f)

    def test_pulse_intensity_scales_reaction(self):
        # A higher intensity zooms harder for the same transient — the frame
        # diverges further from the source than at the baseline intensity.
        from c64cast.modulation import MusicModulation

        f = np.zeros((16, 16, 3), np.uint8)
        f[6:10, 6:10] = 255
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        base = PulseEffect()  # intensity 1.0
        hot = PulseEffect(intensity=2.5)
        base_diff = int(np.abs(base.apply(f, 0.0, hit).astype(int) - f).sum())
        hot_diff = int(np.abs(hot.apply(f, 0.0, hit).astype(int) - f).sum())
        self.assertGreater(hot_diff, base_diff)

    def test_effect_intensity_default_is_baseline(self):
        # The default intensity=1.0 is what build_effect ships — the multiply is
        # bit-exact identity against the pre-knob response.
        self.assertEqual(PulseEffect().intensity, 1.0)
        self.assertEqual(RgbShiftEffect().intensity, 1.0)

    def test_rgb_shift_identity_without_modulation(self):
        eff = build_effect("rgb_shift")
        f = np.random.default_rng(2).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        np.testing.assert_array_equal(eff.apply(f, 0.0), f)

    def test_rgb_shift_silent_modulation_is_noop(self):
        # Present-but-silent modulation rounds the shift to 0 ⇒ no-op.
        from c64cast.modulation import MusicModulation

        eff = build_effect("rgb_shift")
        f = np.random.default_rng(5).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        silent = MusicModulation(0.0, 0.0, 0.0, 0.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, silent), f)

    def test_rgb_shift_intensity_zero_is_identity_under_modulation(self):
        # intensity=0 zeros the separation ⇒ identity even with a full transient.
        from c64cast.modulation import MusicModulation

        eff = RgbShiftEffect(intensity=0.0)
        f = np.random.default_rng(7).integers(0, 256, (8, 16, 3)).astype(np.uint8)
        hit = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, hit), f)

    def test_rgb_shift_separates_channels_on_onset(self):
        # A transient slews blue + red apart horizontally; green stays put.
        from c64cast.modulation import MusicModulation

        eff = build_effect("rgb_shift")
        f = np.random.default_rng(3).integers(0, 256, (8, 16, 3)).astype(np.uint8)
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        out = eff.apply(f, 0.0, hit)
        np.testing.assert_array_equal(out[..., 1], f[..., 1])  # green untouched
        np.testing.assert_array_equal(out[..., 0], np.roll(f[..., 0], 6, axis=1))  # blue +6
        np.testing.assert_array_equal(out[..., 2], np.roll(f[..., 2], -6, axis=1))  # red -6

    def test_blur_default_instance_is_noop(self):
        # Unlike pulse/rgb_shift, blur's identity guarantee comes from the
        # default `intensity=0.0`, not from `modulation is None` — verify both.
        eff = build_effect("blur")
        f = np.random.default_rng(8).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        np.testing.assert_array_equal(eff.apply(f, 0.0), f)
        np.testing.assert_array_equal(eff.apply(f, 0.0, None), f)

    def test_blur_applies_gaussian_blur_when_intensity_set(self):
        eff = BlurEffect(intensity=2.0)
        f = np.zeros((16, 16, 3), np.uint8)
        f[7:9, 7:9] = 255  # a sharp point to blur out
        out = eff.apply(f, 0.0)
        self.assertEqual(out.shape, f.shape)
        self.assertEqual(out.dtype, f.dtype)
        self.assertFalse(np.array_equal(out, f))

    def test_blur_reactive_kick_increases_with_onset(self):
        # Same base intensity, more onset ⇒ more blur (base + reactive kick,
        # same shape as trails' reactive decay boost).
        from c64cast.modulation import MusicModulation

        f = np.zeros((16, 16, 3), np.uint8)
        f[7:9, 7:9] = 255
        rest = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        eff = BlurEffect(intensity=0.5)
        rest_out = eff.apply(f, 0.0, rest)
        hit_out = eff.apply(f, 0.0, hit)
        # More blur spreads the bright point's energy over more pixels, so the
        # peak value drops further under the stronger (onset-kicked) blur.
        self.assertLess(int(hit_out.max()), int(rest_out.max()))

    def test_render_with_overlays_threads_modulation_to_effect(self):
        # The render path must hand the per-frame modulation snapshot to the
        # effect (mirrors the frame-source threading) so reactive effects react.
        from c64cast.modulation import MusicModulation

        snap = MusicModulation(0.4, 0.9, 1.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        seen: dict[str, object] = {}

        class _RecordingEffect(FrameEffect):
            name = "rec"

            def apply(self, frame, t, modulation=None):
                seen["mod"] = modulation
                seen["t"] = t
                return frame

        mode = _FakeMode()
        scene = SimpleNamespace(name="s", effect=_RecordingEffect(), overlays=[])
        frame = np.zeros((2, 2, 3), np.uint8)
        _render_with_overlays(
            cast(DisplayMode, mode),
            cast(C64Backend, SimpleNamespace()),
            frame,
            [],
            0.5,
            cast(Scene, scene),
            snap,
        )
        self.assertIs(seen["mod"], snap)
        self.assertEqual(seen["t"], 0.5)

    def test_render_with_overlays_modulation_defaults_none(self):
        # Non-reactive callers omit modulation; the effect must see None.
        seen: dict[str, object] = {"mod": "unset"}

        class _RecordingEffect(FrameEffect):
            name = "rec"

            def apply(self, frame, t, modulation=None):
                seen["mod"] = modulation
                return frame

        mode = _FakeMode()
        scene = SimpleNamespace(name="s", effect=_RecordingEffect(), overlays=[])
        _render_with_overlays(
            cast(DisplayMode, mode),
            cast(C64Backend, SimpleNamespace()),
            np.zeros((2, 2, 3), np.uint8),
            [],
            0.0,
            cast(Scene, scene),
        )
        self.assertIsNone(seen["mod"])


class BaseFrameSourceTest(unittest.TestCase):
    def test_defaults(self):
        bs = BaseFrameSource()
        self.assertFalse(bs.finished)
        self.assertIsNone(bs.setup())
        self.assertIsNone(bs.teardown())
        with self.assertRaises(NotImplementedError):
            bs.read(0.0)


class _FakeStreamer:
    def __init__(self):
        self.started: dict[str, object] | None = None
        self.stopped = False

    def start_mic(self, device, sensitivity, noise_gate, *, skip_irq_vector_hook=False):
        self.started = {
            "device": device,
            "sens": sensitivity,
            "gate": noise_gate,
            "skip": skip_irq_vector_hook,
        }

    def stop(self):
        self.stopped = True

    def set_pre_emphasis(self, amount):  # called by Scene.setup
        pass


class AudioSourceTest(unittest.TestCase):
    def test_null_source(self):
        n = NullAudioSource()
        self.assertFalse(n.wants_audio_lock)
        self.assertIsNone(n.position_seconds())
        self.assertIsNone(n.setup())
        self.assertIsNone(n.teardown())
        self.assertIsNone(n.features())  # no feature stream

    def test_mic_source_starts_and_stops_with_skip_hook(self):
        streamer = _FakeStreamer()
        cfg = SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.02)
        mode = SimpleNamespace(audio_reu_pump_active=True)
        mic = MicAudioSource(
            cast(AudioStreamer, streamer), cast(AudioCfg, cfg), display_mode=cast(DisplayMode, mode)
        )
        self.assertFalse(mic.wants_audio_lock)
        self.assertIsNone(mic.features())  # live mic has no SID feature stream
        mic.setup()
        assert streamer.started is not None
        self.assertEqual(streamer.started["device"], -1)
        self.assertTrue(streamer.started["skip"])  # mirrors REU-pump coordination
        mic.teardown()
        self.assertTrue(streamer.stopped)


class _FakeMode:
    name = "fake"
    supports_compose = False

    def __init__(self):
        self.rendered = []

    def setup(self, api):
        pass

    def teardown(self, api):
        pass

    def render(self, api, frame):
        self.rendered.append(frame)


class _CountingSource(BaseFrameSource):
    def __init__(self):
        self.frame = np.zeros((2, 2, 3), np.uint8)
        self._finished = False
        self.setup_called = False
        self.teardown_called = False

    def setup(self):
        self.setup_called = True

    @property
    def finished(self):
        return self._finished

    def read(self, t, modulation=None):
        self.last_modulation = modulation
        return self.frame

    def teardown(self):
        self.teardown_called = True


class SourceSceneTest(unittest.TestCase):
    def _scene(self, audio_source=None):
        mode = _FakeMode()
        src = _CountingSource()
        asrc = audio_source or NullAudioSource()
        scene = SourceScene(
            cast(C64Backend, SimpleNamespace()), None, cast(DisplayMode, mode), src, asrc, "Test"
        )
        scene.duration_s = 5.0
        return scene, mode, src

    def test_setup_brings_up_source_and_audio(self):
        streamer = _FakeStreamer()
        cfg = SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.0)
        mic = MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, cfg),
            display_mode=cast(DisplayMode, SimpleNamespace(audio_reu_pump_active=False)),
        )
        scene, _mode, src = self._scene(audio_source=mic)
        scene.setup()
        self.assertTrue(src.setup_called)
        self.assertIsNotNone(streamer.started)

    def test_process_frame_renders_and_respects_duration(self):
        scene, mode, _src = self._scene()
        scene.setup()
        scene.start_time = 0.0
        self.assertTrue(scene.process_frame(0.0))
        self.assertEqual(len(mode.rendered), 1)
        # Past duration → ends.
        self.assertFalse(scene.process_frame(scene.duration_s + 1.0))

    def test_finished_source_ends_scene(self):
        scene, _mode, src = self._scene()
        scene.setup()
        scene.start_time = 0.0
        src._finished = True
        self.assertFalse(scene.process_frame(0.1))

    def test_competes_for_audio_lock_delegates_to_audio_source(self):
        scene, _mode, _src = self._scene()
        self.assertFalse(scene.competes_for_audio_lock())
        scene.audio_source.wants_audio_lock = True
        self.assertTrue(scene.competes_for_audio_lock())

    def test_teardown_stops_audio_and_source(self):
        scene, _mode, src = self._scene()
        scene.setup()
        scene.teardown()
        self.assertTrue(src.teardown_called)

    def test_resets_display_source_reasserts_display_after_audio(self):
        # A SID audio source reverts the VIC to text mode (run_prg), so the
        # display set up in Scene.setup (BEFORE the audio source) must be
        # re-asserted AFTER it — else a bitmap mode renders $0400 as PETSCII.
        class _CountingMode(_FakeMode):
            def __init__(self):
                super().__init__()
                self.setups = 0

            def setup(self, api):
                self.setups += 1

        class _ResetAudio(NullAudioSource):
            resets_display = True

        mode = _CountingMode()
        api = SimpleNamespace(invalidate_cache=lambda: None)
        scene = SourceScene(
            cast(C64Backend, api),
            None,
            cast(DisplayMode, mode),
            _CountingSource(),
            _ResetAudio(),
            "x",
        )
        scene.setup()
        self.assertEqual(mode.setups, 2)  # Scene.setup + re-assert after the player

    def test_non_resetting_source_sets_up_display_once(self):
        class _CountingMode(_FakeMode):
            def __init__(self):
                super().__init__()
                self.setups = 0

            def setup(self, api):
                self.setups += 1

        mode = _CountingMode()
        scene = SourceScene(
            cast(C64Backend, SimpleNamespace()),
            None,
            cast(DisplayMode, mode),
            _CountingSource(),
            NullAudioSource(),  # resets_display = False
            "x",
        )
        scene.setup()
        self.assertEqual(mode.setups, 1)  # no re-assert for a non-SID source

    def test_modulation_threaded_from_audio_source_to_frame_source(self):
        # The audio source's features() snapshot must reach the frame source's
        # read() — this is the music→visuals wiring.
        from c64cast.modulation import MusicModulation

        snap = MusicModulation(0.5, 1.0, 2.0, 120.0, (1.0, 0.0, 0.0), (True, False, False))

        class _ReactiveAudio(NullAudioSource):
            def features(self):
                return snap

        scene, _mode, src = self._scene(audio_source=_ReactiveAudio())
        scene.setup()
        scene.start_time = 0.0
        scene.process_frame(0.0)
        self.assertIs(src.last_modulation, snap)

    def test_audio_source_setup_failure_aborts_scene(self):
        # A failing audio source (e.g. a SID source whose tune run_sid_player
        # refuses) must abort the scene: setup() flips is_done, and
        # process_frame() must honor it — the generative source's `finished`
        # is always False, so without the is_done guard the playlist's
        # `is_done = not still_active` would clobber the abort and play silent
        # video for the full duration.
        class _BoomAudio:
            wants_audio_lock = True

            def setup(self):
                raise RuntimeError("boom")

            def teardown(self):
                pass

            def position_seconds(self):
                return None

            def features(self):
                return None

        scene, _mode, _src = self._scene(audio_source=_BoomAudio())
        with self.assertLogs("c64cast.scenes", level="ERROR"):
            scene.setup()
        self.assertTrue(scene.is_done)
        scene.start_time = 0.0
        self.assertFalse(scene.process_frame(0.0))


class _RecordingEffect(FrameEffect):
    name = "recording"

    def __init__(self):
        self.applied = 0
        self.reset_count = 0
        self.marker = np.full((2, 2, 3), 123, np.uint8)

    def apply(self, frame, t, modulation=None):
        self.applied += 1
        return self.marker

    def reset(self):
        self.reset_count += 1


class EffectHookTest(unittest.TestCase):
    def test_effect_applied_before_display(self):
        mode = _FakeMode()
        eff = _RecordingEffect()
        scene = cast(Scene, SimpleNamespace(name="x", effect=eff, overlays=[]))
        frame = np.zeros((2, 2, 3), np.uint8)
        _render_with_overlays(
            cast(DisplayMode, mode), cast(C64Backend, SimpleNamespace()), frame, [], 0.0, scene
        )
        self.assertEqual(eff.applied, 1)
        # The display received the effect's output, not the raw frame.
        np.testing.assert_array_equal(mode.rendered[0], eff.marker)

    def test_no_effect_passes_raw_frame(self):
        mode = _FakeMode()
        scene = cast(Scene, SimpleNamespace(name="x", effect=None, overlays=[]))
        frame = np.full((2, 2, 3), 7, np.uint8)
        _render_with_overlays(
            cast(DisplayMode, mode), cast(C64Backend, SimpleNamespace()), frame, [], 0.0, scene
        )
        np.testing.assert_array_equal(mode.rendered[0], frame)

    def test_setup_resets_effect(self):
        mode = _FakeMode()
        eff = _RecordingEffect()
        scene = SourceScene(
            cast(C64Backend, SimpleNamespace()),
            None,
            cast(DisplayMode, mode),
            _CountingSource(),
            NullAudioSource(),
            "x",
        )
        scene.effect = eff
        scene.setup()
        self.assertEqual(eff.reset_count, 1)


class _DummyAPI:
    # `profile` is a pure capability read (not device I/O), legitimately read at
    # build time to resolve [video].double_buffer — so it's a real attribute.
    # __getattr__ still guards against any actual device call at build time.
    profile = HardwareProfile(name="Dummy", family="fake")

    def __getattr__(self, name):
        raise AssertionError(f"api.{name} should not be called at build time")


class ConfigGenerativeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_build_generative_with_effect(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", effect="trails")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.effect, TrailsEffect)
        # Default audio_source = "mic", but no streamer (audio disabled) → null.
        self.assertIsInstance(scene.audio_source, NullAudioSource)

    def test_audio_source_none_is_null(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="none")
        # Even with a live streamer, "none" stays silent.
        streamer = cast(AudioStreamer, _FakeStreamer())
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, NullAudioSource)

    def test_audio_source_mic_uses_streamer_when_enabled(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="mic")
        streamer = cast(AudioStreamer, _FakeStreamer())
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, MicAudioSource)
        self.assertIs(scene.audio, streamer)

    def test_audio_source_mic_falls_back_to_null_without_streamer(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="mic")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, NullAudioSource)

    def test_ensemble_suppresses_mic_source(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="mic")
        streamer = cast(AudioStreamer, _FakeStreamer())
        scene = build_scene(
            s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None, is_ensemble=True
        )
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, NullAudioSource)
        self.assertIsNone(scene.audio)

    def test_invalid_audio_source_rejected(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="bogus")
        with self.assertRaisesRegex(ValueError, "audio_source"):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_generative_petscii_orthogonal(self):
        s = SceneCfg(type="generative", source="tunnel", display="petscii")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        self.assertEqual(type(scene.display_mode).__name__, "PETSCIIDisplayMode")
        self.assertIsNone(scene.effect)

    def test_build_generative_hiphotic(self):
        s = SceneCfg(type="generative", source="hiphotic", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.HiphoticSource)

    def test_build_generative_metaballs(self):
        s = SceneCfg(type="generative", source="metaballs", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.MetaballsSource)

    def test_build_generative_rotozoomer(self):
        s = SceneCfg(type="generative", source="rotozoomer", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.RotozoomerSource)

    def test_build_generative_with_blur_effect(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", effect="blur")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.effect, BlurEffect)

    def test_unknown_source_rejected(self):
        s = SceneCfg(type="generative", source="bogus", display="mhires")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_blank_display_rejected(self):
        s = SceneCfg(type="generative", source="plasma", display="blank")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_effect_on_non_frame_scene_rejected(self):
        s = SceneCfg(type="blank", display="blank", effect="trails")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_unknown_effect_rejected(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", effect="bogus")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)


if __name__ == "__main__":
    unittest.main()
