"""Tests for the composable-scene building blocks: generative frame sources,
pixel effects, the FrameSource/AudioSource protocols, SourceScene, and the
config wiring for `type = "generative"` + per-scene `effect`."""

from __future__ import annotations

import time
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest import mock

import numpy as np

from c64cast.app.config import AudioCfg, Config, SceneCfg
from c64cast.app.scene_factory import build_scene, validate_scene_cfg
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_source import MicAudioSource, NullAudioSource
from c64cast.hw.backend import C64Backend, HardwareProfile
from c64cast.scenes import generators
from c64cast.scenes.effects import (
    BlurEffect,
    FrameEffect,
    PulseEffect,
    RgbShiftEffect,
    TrailsEffect,
    build_effect,
)
from c64cast.scenes.frame_source import BaseFrameSource, FrameSource
from c64cast.scenes.generators import build_generator, generator_names
from c64cast.scenes.scenes import Scene, SourceScene, _render_with_overlays
from c64cast.video.modes import DisplayMode
from c64cast.video.video import ensure_pyav


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
        self.assertIn("lissajous", names)
        self.assertIn("dna", names)
        self.assertIn("drift", names)
        self.assertIn("colored_bursts", names)
        self.assertIn("dotswarm", names)
        self.assertIn("game_of_life", names)
        self.assertIn("soap", names)
        self.assertIn("fireworks", names)

    def test_live_params_declared_with_valid_ranges(self):
        # midi_control.py scales a CC into each declared (min, max) range and
        # setattr()s it directly, so a malformed range corrupts a live param sweep.
        expected = {
            "plasma": {"speed", "scale"},
            "tunnel": {"speed", "scale"},
            "fire": {"scroll_speed", "intensity"},
            "mandelbrot": {"zoom_speed", "cycle_speed"},
            "moire2": {"ring_freq", "drift_speed"},
            "halo": {"drift_speed", "pulse_speed"},
            "epicycle": {"speed"},
            "hopalong": {"shape", "drift_speed"},
            "rorschach": {"grow_speed"},
            "hiphotic": {"speed", "scale"},
            "metaballs": {"speed"},
            "rotozoomer": {"speed", "scale"},
            "lissajous": {"speed", "scale"},
            "dna": {"speed", "scale"},
            "drift": {"speed", "scale"},
            "colored_bursts": {"speed", "scale"},
            "dotswarm": {"speed", "scale"},
            "game_of_life": {"speed"},
            "soap": {"speed", "scale"},
            "fireworks": {"speed", "scale"},
        }
        for name in generator_names():
            g = build_generator(name)
            live_params = g.LIVE_PARAMS
            self.assertEqual(set(live_params), expected.get(name, set()), name)
            for param, (lo, hi) in live_params.items():
                self.assertLess(lo, hi, f"{name}.{param}")
                self.assertTrue(hasattr(g, param), f"{name}.{param} not a real attribute")

    def test_live_params_settable_via_generic_setattr(self):
        # The exact mechanism midi_control.py uses: setattr(obj, name, val), no
        # per-class wiring. Constructed directly so pyright sees `speed` declared.
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
        # The determinism guard: render(t, None) must be byte-for-byte the pure-time
        # output for every generator — the offline renderer + drift tests rely on it.
        for name in generator_names():
            g = build_generator(name)
            np.testing.assert_array_equal(g.render(0.7), g.render(0.7, None))
            np.testing.assert_array_equal(g.read(0.7), g.render(0.7, None))
            self.assertFalse(np.array_equal(g.render(0.0), g.render(1.0)))  # animates

    def test_fire_flares_with_level_and_onset(self):
        # A transient + loudness push the heat field toward the white-hot end of
        # COLORMAP_HOT, so the reactive frame is strictly brighter than resting fire.
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("fire")
        rest = g.render(0.5)  # pure path
        flare = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(0.5, flare).sum()), int(rest.sum()))

    def test_fire_intensity_raises_heat(self):
        # Higher intensity scales the whole heat field up, so more of the frame
        # reaches the white-hot end; lower dims it. Default 1.0 is the baseline.
        base = generators.FireSource().render(0.5)
        hot = generators.FireSource(intensity=2.0).render(0.5)
        cool = generators.FireSource(intensity=0.3).render(0.5)
        self.assertGreater(int(hot.sum()), int(base.sum()))
        self.assertLess(int(cool.sum()), int(base.sum()))

    def test_tunnel_scale_changes_ring_density(self):
        # `scale` multiplies the depth coefficient, changing the concentric-ring
        # density. Default 1.0 reproduces the historical output.
        base = generators.TunnelSource().render(0.5)
        dense = generators.TunnelSource(scale=4.0).render(0.5)
        self.assertEqual(base.shape, dense.shape)
        self.assertFalse(np.array_equal(base, dense))

    def test_modulation_changes_output(self):
        from c64cast.scenes.modulation import MusicModulation

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
        # with onset=0.
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("plasma")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_beat_phase_advances_hue(self):
        # A larger accumulated beat_phase shifts the hue (tempo-driven cycling).
        from c64cast.scenes.modulation import MusicModulation

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
        # The starting (scale=1) view frames the whole set, so some pixels never
        # escape and render pure black regardless of the cycling hue.
        g = build_generator("mandelbrot")
        frame = g.render(0.0)
        self.assertTrue((frame.sum(axis=-1) == 0).any())

    def test_mandelbrot_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("mandelbrot")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_moire2_frame_shape_and_reacts_to_voice_freq(self):
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("halo")
        quiet = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        loud = MusicModulation(1.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        # Bigger halos ⇒ more lit pixels overall.
        self.assertGreater(int(g.render(1.0, loud).sum()), int(g.render(1.0, quiet).sum()))

    def test_epicycle_frame_shape_and_voice_freq_changes_shape(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("epicycle")
        f0 = g.render(3.0)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(3.0))
        base = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        driven = MusicModulation(0.3, 0.0, 0.0, 120.0, (300.0, 150.0, 0.0), (True, True, False))
        self.assertFalse(np.array_equal(g.render(3.0, base), g.render(3.0, driven)))

    def test_epicycle_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

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
        # More of the walk is revealed partway into the grow cycle ⇒ more lit pixels.
        self.assertGreater(int(g.render(5.0).sum()), int(f0.sum()))

    def test_rorschach_mirror_symmetric(self):
        g = build_generator("rorschach")
        frame = g.render(5.0)
        mask = frame.sum(axis=-1) > 0
        # Mirrored across the vertical center column.
        np.testing.assert_array_equal(mask, mask[:, ::-1])

    def test_rorschach_onset_jumps_reveal(self):
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

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
        # `scale` feeds the affine zoom factor directly.
        base = generators.RotozoomerSource().render(0.5)
        zoomed = generators.RotozoomerSource(scale=3.0).render(0.5)
        self.assertEqual(base.shape, zoomed.shape)
        self.assertFalse(np.array_equal(base, zoomed))

    def test_rotozoomer_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("rotozoomer")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_lissajous_frame_shape_and_determinism(self):
        g = build_generator("lissajous")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_lissajous_scale_changes_shape(self):
        base = generators.LissajousSource().render(0.5)
        reshaped = generators.LissajousSource(scale=3.0).render(0.5)
        self.assertEqual(base.shape, reshaped.shape)
        self.assertFalse(np.array_equal(base, reshaped))

    def test_lissajous_reacts_to_beat_phase(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("lissajous")
        m0 = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        m1 = MusicModulation(0.3, 0.0, 2.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertFalse(np.array_equal(g.render(1.0, m0), g.render(1.0, m1)))

    def test_dna_frame_shape_and_determinism(self):
        g = build_generator("dna")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_dna_scale_changes_frame(self):
        base = generators.DnaSource().render(0.5)
        reshaped = generators.DnaSource(scale=3.0).render(0.5)
        self.assertEqual(base.shape, reshaped.shape)
        self.assertFalse(np.array_equal(base, reshaped))

    def test_dna_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("dna")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_drift_frame_shape_and_determinism(self):
        g = build_generator("drift")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_drift_scale_changes_frame(self):
        base = generators.DriftSource().render(0.5)
        reshaped = generators.DriftSource(scale=2.0).render(0.5)
        self.assertEqual(base.shape, reshaped.shape)
        self.assertFalse(np.array_equal(base, reshaped))

    def test_drift_level_grows_radius(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("drift")
        rest = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        loud = MusicModulation(1.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertFalse(np.array_equal(g.render(1.0, rest), g.render(1.0, loud)))

    def test_colored_bursts_frame_shape_and_determinism(self):
        g = build_generator("colored_bursts")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_colored_bursts_scale_changes_frame(self):
        base = generators.ColoredBurstsSource().render(0.5)
        reshaped = generators.ColoredBurstsSource(scale=3.0).render(0.5)
        self.assertEqual(base.shape, reshaped.shape)
        self.assertFalse(np.array_equal(base, reshaped))

    def test_colored_bursts_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("colored_bursts")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_dotswarm_frame_shape_and_determinism(self):
        g = build_generator("dotswarm")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_dotswarm_scale_changes_frame(self):
        base = generators.DotSwarmSource().render(0.5)
        reshaped = generators.DotSwarmSource(scale=2.0).render(0.5)
        self.assertEqual(base.shape, reshaped.shape)
        self.assertFalse(np.array_equal(base, reshaped))

    def test_dotswarm_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("dotswarm")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_game_of_life_frame_shape_and_determinism(self):
        g = build_generator("game_of_life")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_game_of_life_direct_jump_matches_gradual_replay(self):
        # A fresh instance rendering t=5.0 directly must equal one that got there in
        # steps: the (epoch, generation) cache changes cost, never the answer.
        direct = build_generator("game_of_life").render(5.0)
        gradual = build_generator("game_of_life")
        for t in (0.5, 1.3, 2.7, 4.0, 5.0):
            out = gradual.render(t)
        np.testing.assert_array_equal(direct, out)

    def test_game_of_life_epoch_reseeds(self):
        # Past one full epoch the board reseeds from a fresh random soup.
        g = generators.GameOfLifeSource()
        epoch_s = g._epoch_s  # noqa: SLF001 — reading the instance's own constant
        f0 = g.render(0.5)
        f1 = g.render(epoch_s + 0.5)
        self.assertFalse(np.array_equal(f0, f1))

    def test_game_of_life_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("game_of_life")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_soap_frame_shape_and_stable_at_fixed_t(self):
        g = build_generator("soap")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        # Repeated/non-advancing t must not re-step the simulation.
        np.testing.assert_array_equal(f0, g.render(0.5))
        self.assertFalse(np.array_equal(f0, g.render(3.0)))

    def test_soap_scale_changes_frame(self):
        base = generators.SoapSource().render(1.0)
        wide = generators.SoapSource(scale=3.0).render(1.0)
        self.assertEqual(base.shape, wide.shape)
        self.assertFalse(np.array_equal(base, wide))

    def test_soap_reset_clears_state(self):
        g = generators.SoapSource()
        g.render(2.0)
        g.reset()
        # After reset the buffer is back to the seed pattern, so rendering at the
        # same t reproduces the first frame.
        fresh = generators.SoapSource().render(0.0)
        np.testing.assert_array_equal(g.render(0.0), fresh)

    def test_soap_onset_flashes_brightness(self):
        from c64cast.scenes.modulation import MusicModulation

        g = build_generator("soap")
        rest = MusicModulation(0.3, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        self.assertGreater(int(g.render(1.0, hit).sum()), int(g.render(1.0, rest).sum()))

    def test_fireworks_frame_shape_and_stable_at_fixed_t(self):
        g = build_generator("fireworks")
        f0 = g.render(0.5)
        self.assertEqual(f0.shape, (generators.GEN_HEIGHT, generators.GEN_WIDTH, 3))
        self.assertEqual(f0.dtype, np.uint8)
        np.testing.assert_array_equal(f0, g.render(0.5))

    def test_fireworks_evolves_over_time(self):
        g = build_generator("fireworks")
        frames = [g.render(t) for t in (0.1, 0.5, 1.0, 2.0, 4.0, 8.0, 12.0)]
        # Over enough sim time a shell must launch/explode/fade.
        self.assertTrue(any(not np.array_equal(frames[0], f) for f in frames[1:]))

    def test_fireworks_reset_clears_particles(self):
        g = generators.FireworksSource()
        for t in (0.5, 3.0, 6.0):
            g.render(t)
        g.reset()
        self.assertFalse(g._p_alive.any())  # noqa: SLF001
        self.assertFalse(g._shell_alive.any())  # noqa: SLF001

    def test_fireworks_onset_triggers_immediate_burst(self):
        from c64cast.scenes.modulation import MusicModulation

        g = generators.FireworksSource()
        g.render(0.1)  # let the accumulator/RNG advance past the first tick
        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        out = g.render(0.2, hit)
        self.assertTrue(g._p_alive.any())  # noqa: SLF001
        self.assertGreater(int(out.sum()), 0)

    def test_fireworks_scale_changes_burst_spread(self):
        # `scale` multiplies burst particle speed. Force an explosion at t=0 on both
        # instances, then run a few ticks of physics before comparing spread: at the
        # burst instant every particle still sits exactly at the burst center.
        from c64cast.scenes.modulation import MusicModulation

        hit = MusicModulation(0.3, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        tight = generators.FireworksSource(scale=0.3)
        wide = generators.FireworksSource(scale=3.0)
        tight.render(0.0, hit)
        tight.render(0.2)
        wide.render(0.0, hit)
        wide.render(0.2)
        tight_spread = float(np.std(tight._p_x[tight._p_alive]))  # noqa: SLF001
        wide_spread = float(np.std(wide._p_x[wide._p_alive]))  # noqa: SLF001
        self.assertGreater(wide_spread, tight_spread)


class EffectTest(unittest.TestCase):
    def test_live_params_declared_with_valid_ranges(self):
        # As above, for effects. pulse/rgb_shift expose `intensity` (the reaction-
        # depth knob), inert-looking only because they do nothing without modulation.
        expected = {
            "trails": {"decay"},
            "pulse": {"intensity"},
            "rgb_shift": {"intensity"},
            "blur": {"intensity"},
            "strobe": {"duty", "rate"},
            "invert": {"mix"},
            "mirror": set(),  # choice-only (LIVE_CHOICES axis), no scalars
            "posterize": {"levels"},
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
        # A transient + loudness raise the effective decay, so more of a prior bright
        # frame survives into the next — a longer tail than the baseline.
        from c64cast.scenes.modulation import MusicModulation

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
        from c64cast.scenes.modulation import MusicModulation

        eff = build_effect("pulse")
        f = np.zeros((8, 8, 3), np.uint8)
        f[3:5, 3:5] = 255  # non-uniform so a zoom is detectable
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        out = eff.apply(f, 0.0, hit)
        self.assertEqual(out.shape, f.shape)
        self.assertFalse(np.array_equal(out, f))

    def test_pulse_silent_modulation_is_noop(self):
        # A modulation present but with no transient/loudness ⇒ scale 1.0 ⇒ no-op.
        from c64cast.scenes.modulation import MusicModulation

        eff = build_effect("pulse")
        f = np.random.default_rng(4).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        silent = MusicModulation(0.0, 0.0, 0.0, 0.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, silent), f)

    def test_pulse_intensity_zero_is_identity_under_modulation(self):
        # intensity=0 scales the reaction away ⇒ identity even with a full transient.
        from c64cast.scenes.modulation import MusicModulation

        eff = PulseEffect(intensity=0.0)
        f = np.random.default_rng(6).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        hit = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, hit), f)

    def test_pulse_intensity_scales_reaction(self):
        # A higher intensity zooms harder, so the frame diverges further from source.
        from c64cast.scenes.modulation import MusicModulation

        f = np.zeros((16, 16, 3), np.uint8)
        f[6:10, 6:10] = 255
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        base = PulseEffect()  # intensity 1.0
        hot = PulseEffect(intensity=2.5)
        base_diff = int(np.abs(base.apply(f, 0.0, hit).astype(int) - f).sum())
        hot_diff = int(np.abs(hot.apply(f, 0.0, hit).astype(int) - f).sum())
        self.assertGreater(hot_diff, base_diff)

    def test_effect_intensity_default_is_baseline(self):
        # The default intensity=1.0 makes the multiply a bit-exact identity.
        self.assertEqual(PulseEffect().intensity, 1.0)
        self.assertEqual(RgbShiftEffect().intensity, 1.0)

    def test_rgb_shift_identity_without_modulation(self):
        eff = build_effect("rgb_shift")
        f = np.random.default_rng(2).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        np.testing.assert_array_equal(eff.apply(f, 0.0), f)

    def test_rgb_shift_silent_modulation_is_noop(self):
        # Present-but-silent modulation rounds the shift to 0 ⇒ no-op.
        from c64cast.scenes.modulation import MusicModulation

        eff = build_effect("rgb_shift")
        f = np.random.default_rng(5).integers(0, 256, (8, 8, 3)).astype(np.uint8)
        silent = MusicModulation(0.0, 0.0, 0.0, 0.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, silent), f)

    def test_rgb_shift_intensity_zero_is_identity_under_modulation(self):
        # intensity=0 zeros the separation ⇒ identity even with a full transient.
        from c64cast.scenes.modulation import MusicModulation

        eff = RgbShiftEffect(intensity=0.0)
        f = np.random.default_rng(7).integers(0, 256, (8, 16, 3)).astype(np.uint8)
        hit = MusicModulation(0.9, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        np.testing.assert_array_equal(eff.apply(f, 0.0, hit), f)

    def test_rgb_shift_separates_channels_on_onset(self):
        # A transient slews blue + red apart horizontally; green stays put.
        from c64cast.scenes.modulation import MusicModulation

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
        # Same base intensity, more onset ⇒ more blur (base + reactive kick).
        from c64cast.scenes.modulation import MusicModulation

        f = np.zeros((16, 16, 3), np.uint8)
        f[7:9, 7:9] = 255
        rest = MusicModulation(0.0, 0.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        hit = MusicModulation(0.0, 1.0, 0.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        eff = BlurEffect(intensity=0.5)
        rest_out = eff.apply(f, 0.0, rest)
        hit_out = eff.apply(f, 0.0, hit)
        # More blur spreads the bright point's energy, so its peak value drops more.
        self.assertLess(int(hit_out.max()), int(rest_out.max()))

    def test_render_with_overlays_threads_modulation_to_effect(self):
        # The render path must hand the per-frame modulation snapshot to the effect.
        from c64cast.scenes.modulation import MusicModulation

        snap = MusicModulation(0.4, 0.9, 1.0, 120.0, (0.0, 0.0, 0.0), (False, False, False))
        seen: dict[str, object] = {}

        class _RecordingEffect(FrameEffect):
            name = "rec"

            def apply(self, frame, t, modulation=None):
                seen["mod"] = modulation
                seen["t"] = t
                return frame

        mode = _FakeMode()
        scene = SimpleNamespace(
            name="s", effects=[_RecordingEffect()], overlays=[], clock_modulation=None
        )
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
        scene = SimpleNamespace(
            name="s", effects=[_RecordingEffect()], overlays=[], clock_modulation=None
        )
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
        # The real AudioStreamer's pre-DSP analysis hook + rate, which a
        # reactive MicAudioSource installs into (see audio_features.py).
        self.sample_rate = 12000
        self.analysis_sink = None
        self.use_reu_pump = True

    def start_mic(self, device, sensitivity, noise_gate, *, skip_irq_vector_hook=False):
        self.started = {
            "device": device,
            "sens": sensitivity,
            "gate": noise_gate,
            "skip": skip_irq_vector_hook,
        }

    def start_listen(self, device, sensitivity, *, sample_rate=None):
        self.started = {
            "device": device,
            "sens": sensitivity,
            "sample_rate": sample_rate,
            "listen": True,
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
        mode = SimpleNamespace(audio_reu_pump_active=True, use_reu_staged=True)
        mic = MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, cfg),
            display_mode=cast(DisplayMode, mode),
            reactive=False,
        )
        self.assertFalse(mic.wants_audio_lock)
        self.assertIsNone(mic.features())  # non-reactive → no feature stream
        mic.setup()
        assert streamer.started is not None
        self.assertEqual(streamer.started["device"], -1)
        self.assertTrue(streamer.started["skip"])  # mirrors REU-pump coordination
        mic.teardown()
        self.assertTrue(streamer.stopped)

    def _mic_over(self, streamer, mode) -> MicAudioSource:
        return MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.02)),
            display_mode=cast(DisplayMode, mode),
            reactive=False,
        )

    def test_mic_pump_refuses_a_host_rec_staged_mode(self):
        # The REU-staged char push and the REU mic pump both drive the REC.
        streamer = _FakeStreamer()
        mic = self._mic_over(streamer, SimpleNamespace(drives_rec_from_host=True))
        with self.assertRaises(ValueError):
            mic.setup()
        self.assertIsNone(streamer.started)

    def test_mic_without_pump_accepts_a_host_rec_staged_mode(self):
        streamer = _FakeStreamer()
        streamer.use_reu_pump = False
        mic = self._mic_over(streamer, SimpleNamespace(drives_rec_from_host=True))
        mic.setup()
        assert streamer.started is not None
        self.assertFalse(streamer.started["skip"])

    def test_listen_only_accepts_a_host_rec_staged_mode(self):
        # Listen-only never starts a pump, so the REC conflict cannot arise.
        streamer = _FakeStreamer()
        mic = MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.02)),
            display_mode=cast(DisplayMode, SimpleNamespace(drives_rec_from_host=True)),
            reactive=False,
            listen_only=True,
        )
        mic.setup()
        assert streamer.started is not None
        self.assertTrue(streamer.started["listen"])

    def _mic(self, streamer, *, reactive: bool) -> MicAudioSource:
        return MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.02)),
            display_mode=cast(DisplayMode, SimpleNamespace(audio_reu_pump_active=False)),
            reactive=reactive,
        )

    def test_non_reactive_mic_installs_no_analysis_sink(self):
        streamer = _FakeStreamer()
        mic = self._mic(streamer, reactive=False)
        mic.setup()
        try:
            self.assertIsNone(streamer.analysis_sink)
            self.assertIsNone(mic.features())
        finally:
            mic.teardown()

    def test_reactive_mic_registers_and_clears_the_analysis_sink(self):
        streamer = _FakeStreamer()
        mic = self._mic(streamer, reactive=True)
        mic.setup()
        try:
            # The sink must be live BEFORE capture starts, so the first
            # callbacks already reach the analyzer.
            self.assertIsNotNone(streamer.analysis_sink)
            self.assertIsNotNone(streamer.started)
        finally:
            mic.teardown()
        self.assertIsNone(streamer.analysis_sink)
        self.assertIsNone(mic.features())  # stream torn down
        self.assertTrue(streamer.stopped)

    def test_reactive_mic_features_reflect_pushed_audio(self):
        streamer = _FakeStreamer()
        mic = self._mic(streamer, reactive=True)
        mic.setup()
        try:
            sink = streamer.analysis_sink
            assert sink is not None
            # Drive the sink the way a mic callback would.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                t = np.arange(2048, dtype=np.float32) / streamer.sample_rate
                sink((0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32))
                feat = mic.features()
                if feat is not None and feat.level > 0.0:
                    break
                time.sleep(0.02)
            feat = mic.features()
            assert feat is not None
            self.assertGreater(feat.level, 0.0)
            self.assertEqual(len(feat.bands), 8)  # [audio_features].bands default
        finally:
            mic.teardown()

    def _listen(self, streamer) -> MicAudioSource:
        return MicAudioSource(
            cast(AudioStreamer, streamer),
            cast(AudioCfg, SimpleNamespace(device=-1, mic_sensitivity=1.0, noise_gate=0.02)),
            display_mode=cast(DisplayMode, SimpleNamespace(audio_reu_pump_active=False)),
            reactive=True,
            listen_only=True,
        )

    def test_listen_only_captures_without_the_dac(self):
        # Listen-only must open the analysis-only capture path (start_listen),
        # NOT start_mic — no audio reaches the 4-bit DAC.
        streamer = _FakeStreamer()
        mic = self._listen(streamer)
        mic.setup()
        try:
            assert streamer.started is not None
            self.assertTrue(streamer.started.get("listen"))
            self.assertIsNotNone(streamer.analysis_sink)
        finally:
            mic.teardown()
        self.assertIsNone(streamer.analysis_sink)
        self.assertTrue(streamer.stopped)

    def test_listen_analyzer_uses_full_bandwidth_rate(self):
        # Listen captures and analyzes at listen_sample_rate (44.1 kHz default),
        # while mic stays at the DAC rate, matching what the C64 plays.
        from c64cast.app.config import AudioFeaturesCfg

        listen_streamer = _FakeStreamer()
        listen = self._listen(listen_streamer)
        listen.setup()
        try:
            assert listen_streamer.started is not None
            self.assertEqual(
                listen_streamer.started["sample_rate"], AudioFeaturesCfg().listen_sample_rate
            )
            self.assertEqual(
                listen._features._analyzer.sample_rate,  # type: ignore[union-attr]
                float(AudioFeaturesCfg().listen_sample_rate),
            )
        finally:
            listen.teardown()

        mic_streamer = _FakeStreamer()
        mic = self._mic(mic_streamer, reactive=True)
        mic.setup()
        try:
            self.assertEqual(
                mic._features._analyzer.sample_rate,  # type: ignore[union-attr]
                float(mic_streamer.sample_rate),
            )
        finally:
            mic.teardown()


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
        self.addCleanup(scene.teardown)
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

    def test_finished_audio_source_ends_scene(self):
        # An audio file at its end ends the scene even when duration_s is
        # unbounded, which is what a file-sized scene now is.
        class _EndedAudio(NullAudioSource):
            finished = True

        scene, _mode, _src = self._scene(audio_source=_EndedAudio())
        scene.duration_s = float("inf")
        scene.setup()
        scene.start_time = 0.0
        self.assertFalse(scene.process_frame(0.1))

    def test_duration_follows_each_setups_pick(self):
        # A pool re-picks at every setup(), so the scene's size has to come
        # from that pick rather than the one made at build time.
        class _PickedAudio(NullAudioSource):
            duration_s = 0.0

        audio = _PickedAudio()
        scene, _mode, _src = self._scene(audio_source=audio)
        scene.duration_s = 30.0
        scene.duration_follows_audio = True
        audio.duration_s = 240.0
        scene.setup()
        self.assertEqual(scene.duration_s, float("inf"))
        # A pick with no length (a live stream) has no end to wait for, so it
        # gets the scene-type default back.
        audio.duration_s = 0.0
        scene.setup()
        self.assertEqual(scene.duration_s, 30.0)

    def test_a_duration_set_live_outlasts_the_next_setup(self):
        # The live menu's DURATION sets scene.duration_s between plays. A
        # follow-the-audio re-size at the next setup() undid it.
        class _PickedAudio(NullAudioSource):
            duration_s = 0.0

        audio = _PickedAudio()
        scene, _mode, _src = self._scene(audio_source=audio)
        scene.duration_s = 30.0
        scene.duration_follows_audio = True
        scene.setup()
        scene.duration_s = 60.0
        scene.setup()
        self.assertEqual(scene.duration_s, 60.0)
        audio.duration_s = 240.0
        scene.setup()
        self.assertEqual(scene.duration_s, 60.0)

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
        # The audio source's features() snapshot must reach the frame source's read().
        from c64cast.scenes.modulation import MusicModulation

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
        # A failing audio source must abort the scene: setup() flips is_done and
        # process_frame() honors it. The generative source's `finished` is always
        # False, so without the guard the playlist plays silent video for the duration.
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
        with self.assertLogs("c64cast.scenes.scenes", level="ERROR"):
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
        scene = cast(
            Scene,
            SimpleNamespace(name="x", effects=[eff], overlays=[], clock_modulation=None),
        )
        frame = np.zeros((2, 2, 3), np.uint8)
        _render_with_overlays(
            cast(DisplayMode, mode), cast(C64Backend, SimpleNamespace()), frame, [], 0.0, scene
        )
        self.assertEqual(eff.applied, 1)
        np.testing.assert_array_equal(mode.rendered[0], eff.marker)

    def test_no_effect_passes_raw_frame(self):
        mode = _FakeMode()
        scene = cast(
            Scene, SimpleNamespace(name="x", effects=[], overlays=[], clock_modulation=None)
        )
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
    # `profile` is a pure capability read, needed at build time to resolve
    # [video].double_buffer; __getattr__ still guards any real device call.
    profile = HardwareProfile(name="Dummy", family="fake")

    def __getattr__(self, name):
        raise AssertionError(f"api.{name} should not be called at build time")


class _FileSink:
    """The scene-facing slice of a sink that `AudioFileSource._decode_loop`
    touches. `played` is `position_seconds`: a figure, or a callable for a
    clock that moves; None reports everything pushed as already played."""

    is_sampler = False
    sample_rate = 8000
    effective_rate = 8000.0
    analysis_sink = None
    content_lag_seconds = 0.0

    def __init__(self, played: float | Callable[[], float] | None = None):
        self.pushed = 0
        self._played = played
        # Pushes from this one on are refused, as a DAC drops a blob its full
        # queue would not take and a sampler that gave up takes nothing.
        self.refuse_from: int | None = None
        self._calls = 0

    def push_samples(self, arr):
        self._calls += 1
        if self.refuse_from is not None and self._calls >= self.refuse_from:
            return 0
        self.pushed += int(arr.size)
        return int(arr.size)

    def end_input(self):
        pass

    def position_seconds(self):
        if self._played is None:
            return self.pushed / self.effective_rate
        return self._played() if callable(self._played) else self._played


class _SamplerSink(_FileSink):
    """A `_FileSink` that plays as a sampler: its re-anchors put the sound
    `content_lag_seconds` behind its clock, and `reanchor_lag_seconds()`
    reports the same lag, as a real sampler's does once the read head has
    crossed every re-anchor's hold."""

    is_sampler = True

    def reanchor_lag_seconds(self, position: float | None = None) -> float:
        return self.content_lag_seconds


class _SamplerLink:
    """The write surface `UltimateAudioSampler` drives, recording nothing."""

    def reu_write(self, offset: int, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def write_regs(self, base_addr: str, *values: int) -> None:
        pass

    def write_memory(self, address: str, data_hex: str) -> None:
        pass


class _NoWriter:
    """Stands in for the sampler's writer thread: the clock is the subject,
    not the ring writes."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_running(self) -> bool:
        return False


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class AudioFileSourceEndTest(unittest.TestCase):
    """The scene ends when the audio does. The container header's duration is
    a claim the file makes about itself: a truncated download or a doctored
    Xing frame count says minutes or years while the decoder runs dry in
    seconds, and a scene sized by it played silence for the difference."""

    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = f"{tmp.name}/tune.wav"
        ConfigGenerativeTest._make_wav(self.wav, seconds=0.4)
        self.now = [1000.0]
        from c64cast.audio import audio_source

        patcher = mock.patch.object(
            audio_source, "time", SimpleNamespace(monotonic=lambda: self.now[0])
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _source(self, sink):
        from c64cast.audio.audio_source import AudioFileSource

        return AudioFileSource(cast(AudioStreamer, sink), self.wav, reactive=False)

    def test_not_finished_before_decoding_ends(self):
        self.assertFalse(self._source(_FileSink()).finished)

    def test_a_large_frame_reaches_the_sink_in_pieces_the_history_covers(self):
        # The sampler's queue counts pushes, so the push size is what bounds
        # how far ahead of the sound it holds: a whole 65535-sample FLAC frame
        # per push let 256 of them hold minutes, past the analyzer's history.
        from c64cast.audio import sampler

        sink = _FileSink()
        sink.sample_rate, sink.effective_rate = 44100, 44100.0
        sizes: list[int] = []
        push = sink.push_samples

        def record(arr):
            sizes.append(int(arr.size))
            return push(arr)

        sink.push_samples = record  # type: ignore[method-assign]
        src = self._source(sink)
        frame = SimpleNamespace(to_ndarray=lambda: np.zeros((1, 65535), dtype=np.int16))
        self.assertEqual(src._push_frame(frame), 65535)
        self.assertEqual(sum(sizes), 65535)
        smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=44100)
        held_s = smp._q.maxsize * max(sizes) / 44100 + sampler.DEFAULT_LEAD_SECONDS
        self.assertLess(held_s, src._FEATURE_HISTORY_S)

    def test_the_dac_keeps_the_history_its_lag_can_reach_and_not_30_s(self):
        # The DAC holds at most its queue's soft cap, a push over it, the
        # worker's chunks in hand and the ring unplayed: about 2 s at 12 kHz.
        from c64cast.audio.audio_handlers import MAX_QUEUED_SAMPLES, RING_BUFFER_SIZE

        sink = _FileSink()
        sink.sample_rate, sink.effective_rate = 12000, 12032.0
        history = self._source(sink)._feature_history_samples(12032.0)
        self.assertGreater(history, MAX_QUEUED_SAMPLES + RING_BUFFER_SIZE)
        self.assertLess(history / 12032.0, 6.0)

    def test_the_decoder_resamples_to_the_rate_the_sink_plays_at(self):
        # 44.1 kHz asked, 44 kHz achieved: 0.4 s is 17600 samples at the
        # achieved rate, or the clip plays 0.2 % long against its picture.
        sink = _FileSink()
        sink.sample_rate = 44100
        sink.effective_rate = 44000.0
        self._source(sink)._decode_loop()
        self.assertEqual(sink.pushed, 17600)

    def test_position_seconds_is_the_sinks_clock(self):
        self.assertEqual(self._source(_FileSink(played=1.25)).position_seconds(), 1.25)

    def test_a_file_with_no_audio_stream_is_skipped(self):
        container = SimpleNamespace(
            streams=SimpleNamespace(audio=[]), duration=1_000_000, close=lambda: None
        )
        src = self._source(_FileSink())
        with (
            mock.patch("c64cast.video.video.av_open", return_value=container),
            self.assertLogs("c64cast.audio.audio_source", "WARNING") as cm,
            self.assertRaises(ValueError),
        ):
            src._pick_and_probe()
        self.assertTrue(any("no audio stream" in m for m in cm.output), cm.output)

    def test_a_second_setup_decodes_again(self):
        # Teardown leaves the stop event set; setup has to clear it, or every
        # later activation of the scene decodes nothing and plays silence.
        class _Startable(_FileSink):
            def start_for_external_source(self) -> None:
                pass

            def stop(self) -> None:
                pass

        sink = _Startable()
        src = self._source(sink)
        src._stop.set()
        with self.assertLogs("c64cast.audio.audio_source", "INFO"):
            src.setup()
            assert src._thread is not None
            src._thread.join(timeout=5.0)
        self.addCleanup(src.teardown)
        self.assertGreater(sink.pushed, 0)

    def test_finishes_at_end_of_track_once_played_out(self):
        sink = _FileSink()
        src = self._source(sink)
        src._decode_loop()
        self.assertGreater(sink.pushed, 0)
        self.assertTrue(src.finished)

    def test_the_resampler_tail_is_pushed(self):
        # 0.4 s at 8 kHz resampled to 44 kHz is 17600 samples. The resampler
        # holds its filter's last few milliseconds until flushed at EOF.
        sink = _FileSink()
        sink.effective_rate = 44000.0
        sink.sample_rate = 44000
        src = self._source(sink)
        src._decode_loop()
        self.assertEqual(sink.pushed, 17600)
        # The tail counts toward the length the scene waits out, too.
        assert src._end is not None
        self.assertAlmostEqual(src._end[0], 0.4, places=6)

    def test_the_length_is_on_the_sinks_clock(self):
        # The DAC's clock divides by the rate its CIA latch achieves, not the
        # one requested (12 kHz NTSC plays at 12032.1 Hz). A length divided by
        # the request sits 0.27% past anything that clock reaches, and the
        # scene ran out the deadline.
        sink = _FileSink()
        sink.effective_rate = 8032.5
        src = self._source(sink)
        src._decode_loop()
        self.assertTrue(src.finished)

    def test_waits_for_what_the_sink_has_not_played(self):
        start = self.now[0]
        src = self._source(_FileSink(played=lambda: self.now[0] - start))
        src._decode_loop()
        self.assertFalse(src.finished)
        self.now[0] += 0.39
        self.assertFalse(src.finished)
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_waits_out_a_sampler_reanchor(self):
        # A sampler re-anchors late audio past its read head, and its clock
        # does not follow, so the last sample is heard that long after the
        # clock reaches the length.
        start = self.now[0]
        sink = _SamplerSink(played=lambda: self.now[0] - start)
        sink.content_lag_seconds = 0.2
        src = self._source(sink)
        src._decode_loop()
        self.now[0] += 0.59
        self.assertFalse(src.finished, "ended before the re-anchored tail played")
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_the_bound_waits_out_a_reanchor_too(self):
        # Re-anchors add up over a slow stretch of a long stream, and the
        # queue can still hold seconds of audio when decoding ends. A bound
        # that left the lag out ended the scene with the tail still playing
        # once the lag passed the grace, including lag gained after the end.
        start = self.now[0]
        sink = _SamplerSink(played=lambda: self.now[0] - start)
        src = self._source(sink)
        src._decode_loop()
        # A writer that re-anchors the queued tail does so after decoding.
        sink.content_lag_seconds = 6.0
        self.now[0] += 6.39
        self.assertFalse(src.finished, "ended before the re-anchored tail played")
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_a_lagging_wait_is_bounded_from_the_lagged_end(self):
        # A clock past the length but short of length + lag still has the
        # rest of the lag to play, and no more: the bound is the grace past
        # that, not past the whole lag again.
        sink = _SamplerSink(played=3.0)
        sink.content_lag_seconds = 4.0
        src = self._source(sink)
        src._decode_loop()
        self.now[0] += 1.4 + src._DRAIN_GRACE_S - 0.01
        self.assertFalse(src.finished)
        self.now[0] += 0.02
        # A lag within the cap is waited out whole, so the cap did not decide
        # the end and its warning stays quiet.
        with self.assertNoLogs("c64cast.audio.audio_source", "WARNING"):
            self.assertTrue(src.finished)

    def test_a_lag_that_keeps_growing_cannot_hold_the_scene_open(self):
        # A producer that never catches up is re-anchored again and again,
        # and each re-anchor adds to the lag. A bound that counted the whole
        # lag moved away as fast as the clock approached it, so the scene
        # never ended; the lag counts up to a cap, and passing it is logged.
        sink = _SamplerSink(played=-1e9)
        src = self._source(sink)
        src._decode_loop()
        start = self.now[0]
        cap = src._MAX_COUNTED_LAG_S
        ceiling = start + 0.4 + src._DRAIN_GRACE_S + cap
        # The lag runs ahead of the clock: 2 s gained for each second played.
        sink.content_lag_seconds = 2.0 * (ceiling - 0.01 - start)
        self.now[0] = ceiling - 0.01
        self.assertFalse(src.finished)
        self.now[0] = ceiling + 0.01
        sink.content_lag_seconds = 2.0 * (self.now[0] - start)
        with self.assertLogs("c64cast.audio.audio_source", "WARNING") as logs:
            self.assertTrue(src.finished)
            self.assertTrue(src.finished)
        self.assertEqual(len(logs.records), 1, "the cap is logged once, not per poll")
        self.assertIn(f"{sink.content_lag_seconds:.1f} s", logs.output[0])

    def test_lag_gained_before_decoding_ended_is_waited_out_whole(self):
        # A long slow stream re-anchors over minutes of decoding, so its lag
        # can pass the cap before the decoder reaches EOF. That lag is tail
        # still queued, not growth: capped with the rest, it ended the scene
        # at once with 2 s of the track still to play. Here the lag is 25 s
        # at decoding's end and the clock reaches length + lag 2 s later.
        start = self.now[0]
        sink = _SamplerSink(played=lambda: 23.4 + (self.now[0] - start))
        sink.content_lag_seconds = 25.0
        src = self._source(sink)
        src._decode_loop()
        with self.assertNoLogs("c64cast.audio.audio_source", "WARNING"):
            self.now[0] = start + 1.99
            self.assertFalse(src.finished, "ended with the queued tail unplayed")
            self.now[0] = start + 2.01
            self.assertTrue(src.finished)

    def test_the_cap_counts_growth_on_top_of_the_lag_at_decodings_end(self):
        # The lag at decoding's end is waited out whole, and growth past it
        # still gets the full cap on top: a cap that overlapped the lag
        # already there ended the scene 10 s early, and a warning that named
        # the whole lag as growth overstated what the cap cut.
        sink = _SamplerSink(played=-1e9)
        sink.content_lag_seconds = 25.0
        src = self._source(sink)
        src._decode_loop()
        start = self.now[0]
        cap = src._MAX_COUNTED_LAG_S
        ceiling = start + 0.4 + src._DRAIN_GRACE_S + 25.0 + cap
        # Re-anchors keep coming after decoding ended: 2 s gained per second.
        self.now[0] = ceiling - 0.01
        sink.content_lag_seconds = 25.0 + 2.0 * (self.now[0] - start)
        self.assertFalse(src.finished, "the cap overlapped the lag at decoding's end")
        self.now[0] = ceiling + 0.01
        growth = 2.0 * (self.now[0] - start)
        sink.content_lag_seconds = 25.0 + growth
        with self.assertLogs("c64cast.audio.audio_source", "WARNING") as logs:
            self.assertTrue(src.finished)
        self.assertIn(f"{growth:.1f} s of it gained", logs.output[0])

    def test_waits_for_a_sink_that_starts_playing_after_decoding_ends(self):
        # The sampler gates its ring, and starts its clock, only after the
        # prebuffer and ring prefill, by when a short file is decoded whole.
        # The wait runs on the sink's clock, not from the end of decoding.
        gate: list[float | None] = [None]

        def played():
            return 0.0 if gate[0] is None else self.now[0] - gate[0]

        src = self._source(_FileSink(played=played))
        src._decode_loop()
        self.now[0] += 2.0
        gate[0] = self.now[0]
        self.now[0] += 0.39
        self.assertFalse(src.finished)
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_the_wait_is_bounded(self):
        # A sink clock that never reaches the end must not hold the scene
        # open past the audio's length and the grace.
        src = self._source(_FileSink(played=-1e9))
        src._decode_loop()
        self.now[0] += 0.4 + src._DRAIN_GRACE_S - 0.01
        self.assertFalse(src.finished)
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_audio_the_sink_refused_is_not_waited_for(self):
        # A sink that stops taking samples mid-file (a sampler whose writer
        # gave up on the link, a DAC blob dropped at the put timeout) never
        # plays them, so its clock stops short of the file's length; counted,
        # they held the scene to the deadline on silence.
        sink = _FileSink()
        sink.refuse_from = 2
        src = self._source(sink)
        src._decode_loop()
        self.assertGreater(sink.pushed, 0)
        self.assertLess(sink.pushed, 3200, "the sink refused nothing")
        self.assertTrue(src.finished)

    def test_a_dac_ends_with_the_last_sample_it_enqueued(self):
        # Measured on hardware: a 6 s WAV on the DAC ran its scene for
        # 10.4-11.4 s. The DAC drops a blob its queue held full past
        # QUEUE_PUT_TIMEOUT_S, so its clock, which counts what it enqueued,
        # stopped short of the length the decoder had handed over, and the
        # scene sat out the deadline on silence. No worker drains the queue
        # here, so every blob past the cap is dropped at once.
        from _fakes import FakeAPI

        from c64cast.audio import audio as audio_mod
        from c64cast.audio.audio_source import AudioFileSource

        dac = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC")
        dac._max_queued_samples = 1600
        src = AudioFileSource(dac, self.wav, reactive=False)
        dac.running = True
        with mock.patch.object(audio_mod, "QUEUE_PUT_TIMEOUT_S", 0.0):
            src._decode_loop()
        self.assertLess(dac._pushed_count, 3200, "the DAC dropped nothing")
        # Everything it enqueued lands and plays: the queue is empty and the
        # unplayed lead behind the last landed sample is gone.
        dac._queued_samples = 0
        dac.servo.ring_lead = 0.0
        self.assertTrue(src.finished, "the scene waits for audio the DAC dropped")

    def test_a_sampler_ends_with_the_last_sample_it_plays(self):
        # Measured on hardware: a 6 s WAV on the sampler ended its scene
        # after 2.7-3.7 s while ring writes were still going. The whole file
        # fits the sampler's chunk-counted queue, so the decoder reached EOF
        # before the ring was gated, read its clock as 0, and scheduled the
        # end from the decode, capped at 5 s.
        from c64cast.audio import sampler
        from c64cast.audio.audio_source import AudioFileSource

        ConfigGenerativeTest._make_wav(self.wav, seconds=6.0, rate=44100)
        clock = SimpleNamespace(monotonic=lambda: self.now[0])
        with (
            mock.patch.object(sampler, "time", clock),
            mock.patch.object(sampler, "PollThread", _NoWriter),
        ):
            smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=44100)
            src = AudioFileSource(smp, self.wav, reactive=False)
            smp.arm()
            src._decode_loop()
            self.assertGreater(smp._q.qsize(), 0, "the decoder did not run ahead of the ring")
            self.now[0] += 1.0  # the ring prefill and prebuffer
            smp.start()
            self.addCleanup(smp.stop)
            gate = self.now[0]
            self.now[0] = gate + 5.9
            self.assertFalse(src.finished, "ended before the last sample played")
            self.now[0] = gate + 6.05
            self.assertTrue(src.finished)

    def test_a_reanchored_sampler_ends_with_the_last_sample_it_plays(self):
        # A re-anchor plays every later sample that much past its slot, so
        # the last one is heard that much after the clock reaches the length.
        from c64cast.audio import sampler
        from c64cast.audio.audio_source import AudioFileSource

        ConfigGenerativeTest._make_wav(self.wav, seconds=6.0, rate=44100)
        clock = SimpleNamespace(monotonic=lambda: self.now[0])
        with (
            mock.patch.object(sampler, "time", clock),
            mock.patch.object(sampler, "PollThread", _NoWriter),
        ):
            smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=44100)
            src = AudioFileSource(smp, self.wav, reactive=False)
            smp.arm()
            src._decode_loop()
            self.now[0] += 1.0
            smp.start()
            self.addCleanup(smp.stop)
            smp._reanchor_lag = (int(0.5 * smp.effective_rate) * smp.bps, (), 0)
            gate = self.now[0]
            self.now[0] = gate + 6.4
            self.assertFalse(src.finished, "ended before the re-anchored tail played")
            self.now[0] = gate + 6.55
            self.assertTrue(src.finished)

    def test_a_reanchor_hold_reads_the_lag_at_the_position_it_comes_off(self):
        # Inside a re-anchor's hold the heard sample stands still while the
        # clock runs. With the lag read at the head as of a later clock read
        # than the position, the heard sample stepped back by the time between.
        from c64cast.audio import sampler
        from c64cast.audio.audio_source import AudioFileSource

        ConfigGenerativeTest._make_wav(self.wav, seconds=6.0, rate=44100)
        ticks = [self.now[0]]

        def monotonic() -> float:
            ticks[0] += 0.005  # every clock read is 5 ms after the last
            return ticks[0]

        with (
            mock.patch.object(sampler, "time", SimpleNamespace(monotonic=monotonic)),
            mock.patch.object(sampler, "PollThread", _NoWriter),
        ):
            smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=44100)
            src = AudioFileSource(smp, self.wav, reactive=False)
            smp.arm()
            src._decode_loop()
            smp.start()
            self.addCleanup(smp.stop)
            held = 1000  # samples: the hold below runs far past the head
            smp._reanchor_lag = (10**9 - held * smp.bps, ((0, 10**9),), 0)
            rate = smp.effective_rate
            for _ in range(3):
                self.assertAlmostEqual(src._heard_seconds(), held / rate, delta=1.5 / rate)

    def test_the_wait_bound_counts_a_reanchored_sampler_s_unheard_tail(self):
        # Decoding ends with the sampler's clock at the length but its last
        # 0.3 s re-anchored past it, so 0.3 s is still unheard; the bound
        # waits that out on top of the grace even if the clock never moves.
        sink = _SamplerSink(played=0.4)
        sink.content_lag_seconds = 0.3
        src = self._source(sink)
        src._decode_loop()
        self.now[0] += 0.3 + src._DRAIN_GRACE_S - 0.01
        self.assertFalse(src.finished)
        self.now[0] += 0.02
        self.assertTrue(src.finished)

    def test_a_decode_that_cannot_open_finishes(self):
        import os

        src = self._source(_FileSink())
        os.remove(self.wav)
        with self.assertLogs("c64cast.audio.audio_source", level="ERROR"):
            src._decode_loop()
        self.assertTrue(src.finished)

    def test_a_stopped_decode_does_not_finish(self):
        # Teardown stops the decoder, and the sink it stops can raise into the
        # push in flight; neither is the track ending.
        sink = _FileSink()
        src = self._source(sink)

        def torn_down(arr):
            src._stop.set()
            raise RuntimeError("sink stopped")

        sink.push_samples = torn_down
        src._decode_loop()
        self.assertFalse(src.finished)


class _ConsumerLink:
    """A DAC link whose NMI read pointer R advances at the consumer's rate from
    the moment the NMI starts, so the servo and the streamer's clock see a
    consumer that plays the ring."""

    def __init__(self, api, rate: float):
        self._api = api
        self._rate = rate
        self.started_at: float | None = None

    def read_memory(self, address, length, timeout=1.0):
        from c64cast.audio import audio_handlers as h

        if address != h.READ_PTR_LO_ADDR or length != 2:
            return self._api.read_memory(address, length, timeout)
        played = 0 if self.started_at is None else time.monotonic() - self.started_at
        r = h.RING_BUFFER_ADDR + int(played * self._rate) % h.RING_BUFFER_SIZE
        return bytes([r & 0xFF, r >> 8])


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class AudioFileShortClipTest(unittest.TestCase):
    """A clip shorter than the sink's prebuffer ends with its audio. Both sinks
    wait for a prebuffer before they play: the DAC starts its NMI after
    PREBUFFER_CHUNKS chunks (about 0.5 s at 12 kHz), the sampler gates its ring
    after `prebuffer_seconds` or a 2 s timeout. A clip shorter than that never
    filled it, so the DAC never started the NMI and the scene ran to the
    length + 5 s deadline in silence, and the sampler held setup() for the 2 s
    timeout before gating. Real time: the subject is the sinks' own threads."""

    CLIP_S = 0.3
    # The worst case either sink should take past the clip: one idle collect on
    # the DAC, the poll on the sampler, plus headroom for a loaded runner. The
    # defect overran by 2 s (sampler) and 5 s (DAC).
    SLACK_S = 1.0

    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = f"{tmp.name}/clip.wav"
        ConfigGenerativeTest._make_wav(self.wav, seconds=self.CLIP_S)

    def _run_scene(self, sink) -> float:
        """Set the source up and return the seconds until it finishes."""
        from c64cast.audio.audio_source import AudioFileSource

        src = AudioFileSource(sink, self.wav, reactive=False)
        t0 = time.monotonic()
        try:
            src.setup()
            while not src.finished and time.monotonic() - t0 < self.CLIP_S + 6.0:
                time.sleep(0.01)
            return time.monotonic() - t0
        finally:
            src.teardown()

    def test_a_dac_plays_a_clip_shorter_than_its_prebuffer(self):
        from _fakes import FakeAPI, quiet_logging

        api = FakeAPI()
        dac = AudioStreamer(cast(C64Backend, api), 8000, "NTSC")
        link = _ConsumerLink(FakeAPI(), dac.effective_rate)
        api.read_memory = link.read_memory  # type: ignore[method-assign]
        start_nmi = dac.nmi.start

        def started(*args, **kwargs):
            link.started_at = time.monotonic()
            return start_nmi(*args, **kwargs)

        with quiet_logging(), mock.patch.object(dac.nmi, "start", side_effect=started):
            took = self._run_scene(dac)
        self.assertIsNotNone(link.started_at, "the NMI never started, so the clip never played")
        self.assertLess(took, self.CLIP_S + self.SLACK_S, "the scene ran out the deadline")

    def test_end_input_wakes_a_dac_collect(self):
        # A priming collect waits a chunk period for samples; once the producer
        # has ended, none are coming, so the wait only delays the start.
        from _fakes import FakeAPI

        dac = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC")
        dac.running = True
        dac.end_input()
        t0 = time.monotonic()
        n, _, _ = dac._collect_until(
            bytearray(dac.chunk_size), 0, b"", t0 + 5.0, generation=dac._worker_generation
        )
        self.assertEqual(n, 0)
        self.assertLess(time.monotonic() - t0, 1.0, "the collect waited out its deadline")

    def test_a_stale_end_input_blob_does_not_cut_the_next_producers_collect(self):
        # An end_input() that raced its teardown's drain leaves its wake-up in
        # the queue for the next producer, whose worker cleared _input_ended.
        from _fakes import FakeAPI

        dac = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC")
        dac.running = True
        dac.q.put_nowait(b"")
        dac.q.put_nowait(b"\x01" * 16)
        n, _, _ = dac._collect_until(
            bytearray(16), 0, b"", time.monotonic() + 1.0, generation=dac._worker_generation
        )
        self.assertEqual(n, 16, "a stale wake-up ended the next producer's collect")

    def test_a_new_dac_worker_clears_the_last_producers_end(self):
        # end_input() marks one producer's end. The next activation's worker
        # starts without it, or the wake-up the last producer left in the
        # queue reads as this producer's end and cuts its first collect.
        from _fakes import FakeAPI

        dac = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC")
        dac.end_input()
        with mock.patch.object(dac, "_worker"):
            dac._start_worker().join(timeout=5.0)
        dac.running = True
        dac.q.put_nowait(b"\x01" * 16)
        n, _, _ = dac._collect_until(
            bytearray(16), 0, b"", time.monotonic() + 1.0, generation=dac._worker_generation
        )
        self.assertEqual(n, 16, "the last producer's end cut the next producer's collect")

    def test_a_dac_worker_idles_after_a_producer_that_pushed_nothing(self):
        # A decode that failed before its first push still ends the input. With
        # nothing landed there is no prebuffer to pad out, and a priming collect
        # on a zero deadline turned the idle branch into a busy spin.
        from _fakes import FakeAPI, quiet_logging

        dac = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC")
        collects = 0
        collect = dac._collect_until

        def counted(*args, **kwargs):
            nonlocal collects
            collects += 1
            return collect(*args, **kwargs)

        with quiet_logging(), mock.patch.object(dac, "_collect_until", side_effect=counted):
            dac.start_for_external_source()
            try:
                dac.end_input()
                time.sleep(0.3)
            finally:
                dac.stop()
        # An idle collect blocks a chunk period (128 ms at 8 kHz): a few
        # passes, not hundreds of thousands.
        self.assertLess(collects, 20, "the worker spun on an ended, empty input")

    def test_a_sampler_plays_a_clip_shorter_than_its_prebuffer(self):
        from _fakes import quiet_logging

        from c64cast.audio import sampler

        with mock.patch.object(sampler, "PollThread", _NoWriter):
            smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=8000)
            with quiet_logging():
                took = self._run_scene(smp)
        self.assertLess(took, self.CLIP_S + self.SLACK_S, "setup sat out the prebuffer timeout")


def _make_click_wav(path: str, *, seconds: float, period: float, rate: int = 44100) -> list[float]:
    """A click track: a 30 ms noise burst every `period` seconds, silence
    between. Returns the click times in seconds."""
    import wave

    rng = np.random.default_rng(7)
    n = int(rate * seconds)
    x = np.zeros(n, dtype=np.float64)
    clicks = [period * (k + 1) for k in range(int(seconds / period) - 1)]
    burst = int(0.03 * rate)
    for t in clicks:
        i = int(t * rate)
        x[i : i + burst] = rng.uniform(-0.8, 0.8, burst)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return clicks


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class AudioFileSourceFeatureSyncTest(unittest.TestCase):
    """A reactive file scene pulses with the click the listener hears, not the
    one the decoder has just reached. Both sinks keep a queue and a ring of
    decoded audio ahead of playback (≈1.4 s + the ring on the DAC, the whole
    of a short file on the sampler), and analyzing the newest pushed window
    put every onset that far ahead of its sound."""

    PERIOD = 0.5
    TOLERANCE_S = 0.12  # the 1024-sample window plus a few 60 Hz ticks

    def setUp(self):
        import tempfile

        from c64cast.audio import audio_features, audio_source

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = f"{tmp.name}/clicks.wav"
        self.clicks = _make_click_wav(self.wav, seconds=4.0, period=self.PERIOD)
        self.now = [1000.0]
        clock = SimpleNamespace(monotonic=lambda: self.now[0])
        for patcher in (
            mock.patch.object(audio_source, "time", clock),
            mock.patch.object(audio_features, "time", clock),
            mock.patch.object(audio_features, "PollThread", _NoWriter),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _source(self, sink):
        from c64cast.app.config import AudioFeaturesCfg
        from c64cast.audio.audio_source import AudioFileSource

        src = AudioFileSource(sink, self.wav, reactive=True, features_cfg=AudioFeaturesCfg())
        src._start_features()
        self.addCleanup(src.teardown)
        assert src._features is not None
        return src

    def _assert_onsets_on_the_clicks(self, onsets: list[float]) -> None:
        heard = [c for c in self.clicks if c < 3.0]
        self.assertGreaterEqual(len(onsets), len(heard), f"onsets at {onsets}")
        for t in onsets:
            self.assertTrue(
                any(0.0 <= t - c <= self.TOLERANCE_S for c in self.clicks),
                f"onset at played {t:.3f} s is on no click {self.clicks}",
            )

    def _sampler_onsets(self, *, reanchor_lag_s: float = 0.0) -> list[float]:
        """Onset times, as heard, over a real sampler on the fake clock. A
        nonzero `reanchor_lag_s` stands for re-anchors the writer made before
        the gate: every sample then plays that much past its slot."""
        from c64cast.audio import sampler

        with (
            mock.patch.object(sampler, "time", SimpleNamespace(monotonic=lambda: self.now[0])),
            mock.patch.object(sampler, "PollThread", _NoWriter),
        ):
            smp = sampler.UltimateAudioSampler(cast(C64Backend, _SamplerLink()), sample_rate=44100)
            src = self._source(smp)
            smp.arm()
            src._decode_loop()  # a 4 s file fits the queue: decoded whole up front
            self.now[0] += 1.0
            smp.start()
            smp._reanchor_lag = (int(reanchor_lag_s * smp.effective_rate) * smp.bps, (), 0)
            gate = self.now[0]
            onsets = []
            assert src._features is not None
            for k in range(int((3.0 + reanchor_lag_s) * 60)):
                self.now[0] = gate + k / 60.0
                src._features._process_tick()
                m = src._features.features()
                if m is not None and m.onset == 1.0:
                    onsets.append(smp.position_seconds() - reanchor_lag_s)
        return onsets

    def test_sampler_onsets_land_on_the_heard_clicks(self):
        self._assert_onsets_on_the_clicks(self._sampler_onsets())

    def test_sampler_onsets_follow_the_sound_a_reanchor_delayed(self):
        # A producer that fell behind is re-anchored past the read head, and
        # the sound then lags the sampler's wall clock by the shift: the
        # analyzer has to read that much behind the clock too.
        self._assert_onsets_on_the_clicks(self._sampler_onsets(reanchor_lag_s=0.25))

    def test_dac_a_blob_dropped_on_backpressure_never_reaches_the_tap(self):
        # The tap is read at the streamer's played count, which a blob the
        # queue refused never enters; tapped anyway, it would put every later
        # window that far behind the sound.
        from _fakes import new_streamer

        from c64cast.audio import audio as audio_mod
        from c64cast.audio.audio_features import AnalysisTap

        streamer = new_streamer(sample_rate=12000)
        tap = AnalysisTap(size=1 << 16)
        streamer.analysis_sink = tap.push
        streamer.running = True
        streamer.push_samples(np.full(streamer._max_queued_samples, 1000, dtype=np.int16))
        with mock.patch.object(audio_mod, "QUEUE_PUT_TIMEOUT_S", 0.0):
            streamer.push_samples(np.full(512, 2000, dtype=np.int16))
        self.assertEqual(streamer._pushed_count, streamer._max_queued_samples)
        self.assertEqual(tap.pushed, streamer._pushed_count)

    def test_dac_onsets_land_on_the_heard_clicks(self):
        # The real streamer's push and clock; its worker is modeled: the ring
        # holds `ring` samples ahead of the read head and the queue refills to
        # its cap after every tick, the steady state of a file decoder.
        import av
        from _fakes import new_streamer

        streamer = new_streamer(sample_rate=12000)
        src = self._source(streamer)
        rate = streamer.effective_rate
        ring = 4096
        streamer.running = True
        streamer.servo.ring_lead = float(ring)
        container = av.open(self.wav)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=int(round(rate)))
        pcm = np.concatenate(
            [
                r.to_ndarray().reshape(-1)
                for f in container.decode(audio=0)
                for r in resampler.resample(f)
            ]
        ).astype(np.int16)
        container.close()
        fed = 0
        deepest = 0
        onsets = []
        assert src._features is not None
        for k in range(int(3.0 * 60)):
            played = k / 60.0 * rate
            with streamer._count_lock:
                streamer._queued_samples = max(0, streamer._pushed_count - int(played) - ring)
            while not streamer.q.empty():
                streamer.q.get_nowait()
            while fed < pcm.size and streamer._queued_samples + 512 <= streamer._max_queued_samples:
                streamer.push_samples(pcm[fed : fed + 512])
                fed += 512
            deepest = max(deepest, streamer._queued_samples)
            self.now[0] += 1.0 / 60.0
            src._features._process_tick()
            m = src._features.features()
            if m is not None and m.onset == 1.0:
                # The model's read head, as a time in the source: `played` is
                # the index into `pcm` the NMI has reached. Not the streamer's
                # clock, which is the thing under test: the analyzer indexes
                # its window off that clock, so an onset read back off it too
                # lands on a click however wrong the clock is.
                onsets.append(int(played) / int(round(rate)))
        self.assertGreater(deepest, 15000, "the model never ran the queue up")
        self._assert_onsets_on_the_clicks(onsets)


class ConfigGenerativeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_build_generative_with_effect(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", effect="trails")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.effect, TrailsEffect)
        # Default audio_source = "none" → null.
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

    def test_audio_source_listen_builds_listen_only_source(self):
        # "listen" builds a listen-only MicAudioSource on the shared streamer, but
        # the scene carries no DAC audio: no C64 sound, only reactive visuals.
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="listen")
        streamer = cast(AudioStreamer, _FakeStreamer())
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, MicAudioSource)
        self.assertTrue(scene.audio_source._listen_only)  # type: ignore[union-attr]
        self.assertIsNone(scene.audio)  # no DAC path on the scene

    def test_audio_source_listen_not_suppressed_in_ensemble(self):
        # Listen produces no sound, so it never contends for the ensemble audio
        # spotlight — unlike a mic source, it stays live under is_ensemble.
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="listen")
        streamer = cast(AudioStreamer, _FakeStreamer())
        scene = build_scene(
            s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None, is_ensemble=True
        )
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, MicAudioSource)
        self.assertTrue(scene.audio_source._listen_only)  # type: ignore[union-attr]

    def test_audio_source_listen_falls_back_to_null_without_streamer(self):
        s = SceneCfg(type="generative", source="plasma", display="mhires", audio_source="listen")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, NullAudioSource)

    @staticmethod
    def _make_wav(path: str, seconds: float = 0.4, rate: int = 8000) -> None:
        import wave

        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * int(rate * seconds))

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_audio_source_file_builds_source_sized_to_track(self):
        import tempfile

        from c64cast.audio.audio_source import AudioFileSource

        with tempfile.TemporaryDirectory() as d:
            wav = f"{d}/tune.wav"
            self._make_wav(wav, seconds=0.4)
            s = SceneCfg(
                type="generative", source="plasma", display="mcm", audio_source="file", file=wav
            )
            streamer = cast(AudioStreamer, _FakeStreamer())
            scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), streamer, None)
            assert isinstance(scene, SourceScene)
            self.assertIsInstance(scene.audio_source, AudioFileSource)
            self.assertIs(scene.audio, streamer)
            # duration_s (unset on the cfg) follows the audio: a file with a
            # length runs until the source reports `finished`, not until the
            # header's figure, which a truncated or doctored file gets wrong.
            self.assertTrue(scene.duration_follows_audio)
            self.assertEqual(scene.duration_s, float("inf"))

    @staticmethod
    def _sampler_api() -> _DummyAPI:
        """A build-time API whose profile advertises the Ultimate Audio sampler,
        so the file path resolves to it (parity with the video sampler tests)."""
        from dataclasses import replace

        api = _DummyAPI()
        api.profile = replace(api.profile, supports_sampler=True)
        return api

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_audio_source_file_routes_to_sampler_and_caps_fps_at_30(self):
        # On a sampler-capable U64, audio_source="file" decodes into the off-bus
        # UltimateAudioSampler rather than the 4-bit DAC. The bitmap fps stays at the
        # muted 30 cap, not the video path's 60: a generative source doesn't dedup, so
        # 60 mhires frames/s starves the sampler ring and crashes the C64 (HW 2026-07-24).
        import tempfile

        from c64cast.audio.sampler import UltimateAudioSampler

        with tempfile.TemporaryDirectory() as d:
            wav = f"{d}/tune.wav"
            self._make_wav(wav, seconds=0.4)
            s = SceneCfg(
                type="generative", source="plasma", display="mhires", audio_source="file", file=wav
            )
            streamer = cast(AudioStreamer, _FakeStreamer())
            scene = build_scene(
                s,
                self.cfg,
                cast(C64Backend, self._sampler_api()),
                streamer,
                None,
                sampler_available=True,
            )
            assert isinstance(scene, SourceScene)
            self.assertIsInstance(scene.audio, UltimateAudioSampler)
            self.assertIsInstance(scene.audio_source._audio, UltimateAudioSampler)  # type: ignore[attr-defined]
            self.assertEqual(scene.target_fps, 30.0)

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_audio_source_file_sampler_char_mode_uncapped(self):
        # A char display (mcm) is cheap, so a sampler-routed file scene keeps the
        # playlist default (None) — the quickcast `c64cast tune.mp3` path.
        import tempfile

        from c64cast.audio.sampler import UltimateAudioSampler

        with tempfile.TemporaryDirectory() as d:
            wav = f"{d}/tune.wav"
            self._make_wav(wav, seconds=0.4)
            s = SceneCfg(
                type="generative", source="plasma", display="mcm", audio_source="file", file=wav
            )
            streamer = cast(AudioStreamer, _FakeStreamer())
            scene = build_scene(
                s,
                self.cfg,
                cast(C64Backend, self._sampler_api()),
                streamer,
                None,
                sampler_available=True,
            )
            assert isinstance(scene, SourceScene)
            self.assertIsInstance(scene.audio_source._audio, UltimateAudioSampler)  # type: ignore[attr-defined]
            self.assertIsNone(scene.target_fps)

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_audio_source_file_dac_backend_stays_on_dac_at_20_fps(self):
        # backend="dac" forces the 4-bit DAC even on a sampler-capable U64 (and it is
        # the only path on TeensyROM), keeping its 20 fps bitmap cap.
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            wav = f"{d}/tune.wav"
            self._make_wav(wav, seconds=0.4)
            cfg = Config()
            cfg.audio.backend = "dac"
            s = SceneCfg(
                type="generative", source="plasma", display="mhires", audio_source="file", file=wav
            )
            streamer = cast(AudioStreamer, _FakeStreamer())
            scene = build_scene(
                s,
                cfg,
                cast(C64Backend, self._sampler_api()),
                streamer,
                None,
                sampler_available=True,
            )
            assert isinstance(scene, SourceScene)
            self.assertIs(scene.audio, streamer)
            self.assertIs(scene.audio_source._audio, streamer)  # type: ignore[attr-defined]
            self.assertEqual(scene.target_fps, 20.0)

    def test_audio_source_file_falls_back_to_null_without_streamer(self):
        s = SceneCfg(
            type="generative", source="plasma", display="mcm", audio_source="file", file="x.mp3"
        )
        # No streamer → silence (never opens the file).
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.audio_source, NullAudioSource)

    def test_audio_source_file_requires_file(self):
        s = SceneCfg(type="generative", source="plasma", display="mcm", audio_source="file")
        with self.assertRaisesRegex(ValueError, "file"):
            validate_scene_cfg(s, self.cfg, audio_enabled=True)

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

    def test_build_generative_lissajous(self):
        s = SceneCfg(type="generative", source="lissajous", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.LissajousSource)

    def test_build_generative_dna(self):
        s = SceneCfg(type="generative", source="dna", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.DnaSource)

    def test_build_generative_drift(self):
        s = SceneCfg(type="generative", source="drift", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.DriftSource)

    def test_build_generative_colored_bursts(self):
        s = SceneCfg(type="generative", source="colored_bursts", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.ColoredBurstsSource)

    def test_build_generative_dotswarm(self):
        s = SceneCfg(type="generative", source="dotswarm", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, generators.DotSwarmSource)

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
