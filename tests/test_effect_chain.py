"""Layerable effect chain — Live DJ/VJ Phase 3.

Covers the pieces the phase adds on top of the single-effect surface:

* the new VJ effects (`strobe`, `invert`, `mirror`, `posterize`) — behavior +
  the byte-stable identity paths;
* `FrameEffect.enabled` bypass + `mod_source` selection in the render loop;
* the `scene.effects` chain (order, per-layer bypass, failing-layer drop) and
  the `scene.effect` back-compat property;
* config build/validation of `effects` + `mod_source`;
* the `fx<N>` / `effect[<N>]` param grammar + the `fx_toggle` action.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import cast

import numpy as np

from c64cast import midi_control as mc
from c64cast.backend import HardwareProfile
from c64cast.config import (
    Config,
    SceneCfg,
    _is_valid_param_holder,
    build_scene,
    validate_scene_cfg,
)
from c64cast.effects import (
    FrameEffect,
    InvertEffect,
    MirrorEffect,
    PosterizeEffect,
    StrobeEffect,
    build_effect,
)
from c64cast.modulation import MusicModulation
from c64cast.scenes import Scene, SourceScene, _apply_effect_chain
from c64cast.tempo import ClockModulationSource, TempoClock

_SILENT_VOICES = (0.0, 0.0, 0.0)
_GATES = (False, False, False)


def _mod(level=0.0, onset=0.0, beat_phase=0.0, bpm=120.0):
    return MusicModulation(level, onset, beat_phase, bpm, _SILENT_VOICES, _GATES)


def _img(fill=100, shape=(4, 6, 3)):
    return np.full(shape, fill, np.uint8)


# --------------------------------------------------------------------------- #
# New effects
# --------------------------------------------------------------------------- #
class StrobeTest(unittest.TestCase):
    def test_identity_without_modulation(self):
        # No beat grid to read → identity (byte-stable), like pulse/rgb_shift.
        eff = StrobeEffect()
        frame = _img(200)
        np.testing.assert_array_equal(eff.apply(frame, 0.0, None), frame)

    def test_duty_one_is_always_lit(self):
        eff = StrobeEffect(duty=1.0)
        frame = _img(200)
        # Even mid-cycle, duty >= 1 never blanks.
        np.testing.assert_array_equal(eff.apply(frame, 0.0, _mod(beat_phase=0.5)), frame)

    def test_blanks_on_dark_portion_of_cycle(self):
        eff = StrobeEffect(duty=0.5, rate=1.0)
        frame = _img(200)
        lit = eff.apply(frame, 0.0, _mod(beat_phase=0.1))  # cycle 0.1 < duty
        dark = eff.apply(frame, 0.0, _mod(beat_phase=0.9))  # cycle 0.9 >= duty
        np.testing.assert_array_equal(lit, frame)
        self.assertEqual(int(dark.max()), 0)

    def test_rate_multiplies_cycles_per_beat(self):
        eff = StrobeEffect(duty=0.5, rate=2.0)
        frame = _img(200)
        # At rate 2, beat_phase 0.4 → cycle (0.8) is in the dark half.
        dark = eff.apply(frame, 0.0, _mod(beat_phase=0.4))
        self.assertEqual(int(dark.max()), 0)


class InvertTest(unittest.TestCase):
    def test_full_invert(self):
        eff = InvertEffect(mix=1.0)
        frame = _img(200)
        out = eff.apply(frame, 0.0)
        np.testing.assert_array_equal(out, np.full_like(frame, 55))  # 255 - 200

    def test_mix_zero_is_identity(self):
        eff = InvertEffect(mix=0.0)
        frame = _img(200)
        np.testing.assert_array_equal(eff.apply(frame, 0.0), frame)

    def test_partial_mix_between(self):
        eff = InvertEffect(mix=0.5)
        frame = _img(200)
        out = eff.apply(frame, 0.0)
        # Halfway between 200 and 55 ≈ 127/128.
        self.assertTrue(120 <= int(out.mean()) <= 135)

    def test_not_reactive_ignores_modulation(self):
        eff = InvertEffect(mix=1.0)
        frame = _img(200)
        a = eff.apply(frame, 0.0, None)
        b = eff.apply(frame, 0.0, _mod(onset=1.0, level=1.0))
        np.testing.assert_array_equal(a, b)


class MirrorTest(unittest.TestCase):
    def test_horizontal_fold_reflects_left_onto_right(self):
        eff = MirrorEffect(axis="horizontal")
        frame = np.zeros((2, 4, 3), np.uint8)
        frame[:, 0] = 255  # bright left edge
        out = eff.apply(frame, 0.0)
        # Right edge now mirrors the bright left edge.
        self.assertEqual(int(out[0, -1].max()), 255)

    def test_vertical_fold_reflects_top_onto_bottom(self):
        eff = MirrorEffect(axis="vertical")
        frame = np.zeros((4, 2, 3), np.uint8)
        frame[0, :] = 255
        out = eff.apply(frame, 0.0)
        self.assertEqual(int(out[-1, 0].max()), 255)

    def test_does_not_mutate_input(self):
        eff = MirrorEffect(axis="quad")
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[:, 0] = 255
        before = frame.copy()
        eff.apply(frame, 0.0)
        np.testing.assert_array_equal(frame, before)

    def test_live_choice_cycles_axis(self):
        eff = MirrorEffect()
        self.assertEqual(eff.get_live_choice("axis"), "horizontal")
        label = eff.set_live_choice(None, "axis", "quad")
        self.assertEqual(eff.axis, "quad")
        self.assertIn("quad", label or "")
        # Unknown value is rejected (no change).
        self.assertIsNone(eff.set_live_choice(None, "axis", "nope"))
        self.assertEqual(eff.axis, "quad")


class PosterizeTest(unittest.TestCase):
    def test_reduces_distinct_levels(self):
        eff = PosterizeEffect(levels=2)
        # A smooth ramp collapses to at most `levels` distinct values per channel.
        ramp = np.tile(np.arange(256, dtype=np.uint8).reshape(1, -1, 1), (1, 1, 3))
        out = eff.apply(ramp, 0.0)
        self.assertLessEqual(len(np.unique(out)), 2)

    def test_white_band_reaches_full_white(self):
        eff = PosterizeEffect(levels=4)
        out = eff.apply(_img(255), 0.0)
        self.assertEqual(int(out.max()), 255)

    def test_high_levels_is_identity(self):
        eff = PosterizeEffect(levels=2.0)
        # levels rounds to 1 or below → identity guard.
        eff.levels = 1.0
        frame = _img(123)
        np.testing.assert_array_equal(eff.apply(frame, 0.0), frame)


# --------------------------------------------------------------------------- #
# Base FrameEffect knobs
# --------------------------------------------------------------------------- #
class EffectBaseKnobsTest(unittest.TestCase):
    def test_enabled_and_mod_source_defaults(self):
        for name in ("trails", "strobe", "invert", "mirror", "posterize"):
            eff = build_effect(name)
            self.assertTrue(eff.enabled, name)
            self.assertEqual(eff.mod_source, "audio", name)


# --------------------------------------------------------------------------- #
# Render loop: chain order, bypass, mod_source, failure isolation
# --------------------------------------------------------------------------- #
class _Tagger(FrameEffect):
    """Adds a fixed constant to the frame so ordering/skipping is observable."""

    def __init__(self, delta: int, mod_source: str = "audio"):
        self.delta = delta
        self.mod_source = mod_source
        self.seen: MusicModulation | None = "unset"  # type: ignore[assignment]

    def apply(self, frame, t, modulation=None):
        self.seen = modulation
        return (frame.astype(np.int16) + self.delta).clip(0, 255).astype(np.uint8)


def _fake_scene(effects, clock_modulation=None):
    return cast(
        Scene,
        SimpleNamespace(name="s", effects=list(effects), clock_modulation=clock_modulation),
    )


class ChainRenderTest(unittest.TestCase):
    def test_layers_apply_in_order(self):
        scene = _fake_scene([_Tagger(10), _Tagger(5)])
        out = _apply_effect_chain(scene, _img(0), 0.0, None)
        self.assertEqual(int(out[0, 0, 0]), 15)

    def test_disabled_layer_is_skipped(self):
        a, b = _Tagger(10), _Tagger(5)
        b.enabled = False
        scene = _fake_scene([a, b])
        out = _apply_effect_chain(scene, _img(0), 0.0, None)
        self.assertEqual(int(out[0, 0, 0]), 10)  # only `a` ran

    def test_bypassed_layer_is_byte_identical(self):
        # A fully-bypassed chain must be exact identity (the determinism guard).
        eff = _Tagger(10)
        eff.enabled = False
        scene = _fake_scene([eff])
        frame = _img(77)
        np.testing.assert_array_equal(_apply_effect_chain(scene, frame, 0.0, None), frame)

    def test_failing_layer_dropped_and_others_survive(self):
        class _Boom(FrameEffect):
            name = "boom"

            def apply(self, frame, t, modulation=None):
                raise RuntimeError("kaboom")

        boom = _Boom()
        good = _Tagger(7)
        scene = _fake_scene([boom, good])
        out = _apply_effect_chain(scene, _img(0), 0.0, None)
        self.assertEqual(int(out[0, 0, 0]), 7)
        # The failing layer is removed from the chain, not retried next frame.
        self.assertNotIn(boom, scene.effects)
        self.assertIn(good, scene.effects)

    def test_mod_source_audio_gets_audio_snapshot(self):
        audio = _mod(level=0.3)
        eff = _Tagger(0, mod_source="audio")
        scene = _fake_scene([eff])
        _apply_effect_chain(scene, _img(0), 0.0, audio)
        self.assertIs(eff.seen, audio)

    def test_mod_source_off_gets_none(self):
        audio = _mod(level=0.3)
        eff = _Tagger(0, mod_source="off")
        scene = _fake_scene([eff])
        _apply_effect_chain(scene, _img(0), 0.0, audio)
        self.assertIsNone(eff.seen)

    def test_mod_source_clock_reads_beat_grid(self):
        clock = TempoClock(bpm=120.0, source="internal")
        clock.start(now=0.0)
        feeder = ClockModulationSource(clock)
        audio = _mod(level=0.3)
        eff = _Tagger(0, mod_source="clock")
        scene = _fake_scene([eff], clock_modulation=feeder)
        _apply_effect_chain(scene, _img(0), 0.0, audio)
        # Got a clock snapshot, not the audio one.
        self.assertIsNotNone(eff.seen)
        self.assertIsNot(eff.seen, audio)

    def test_mod_source_clock_without_feeder_is_none(self):
        eff = _Tagger(0, mod_source="clock")
        scene = _fake_scene([eff], clock_modulation=None)
        _apply_effect_chain(scene, _img(0), 0.0, _mod(level=0.3))
        self.assertIsNone(eff.seen)


# --------------------------------------------------------------------------- #
# Scene.effect back-compat property
# --------------------------------------------------------------------------- #
class EffectPropertyTest(unittest.TestCase):
    def test_property_reads_first_layer(self):
        scene = Scene.__new__(Scene)
        scene.effects = []
        self.assertIsNone(scene.effect)
        e = build_effect("trails")
        scene.effects = [e, build_effect("blur")]
        self.assertIs(scene.effect, e)

    def test_setter_replaces_chain(self):
        scene = Scene.__new__(Scene)
        scene.effects = [build_effect("blur")]
        e = build_effect("invert")
        scene.effect = e
        self.assertEqual(scene.effects, [e])
        scene.effect = None
        self.assertEqual(scene.effects, [])


# --------------------------------------------------------------------------- #
# Config build + validation
# --------------------------------------------------------------------------- #
class _DummyAPI:
    profile = HardwareProfile(name="Dummy", family="fake")

    def __getattr__(self, name):
        raise AssertionError(f"api.{name} should not be called at build time")


class ConfigEffectChainTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def _build(self, **kw):
        s = SceneCfg(type="generative", source="plasma", display="mhires", **kw)
        return build_scene(s, self.cfg, cast("object", _DummyAPI()), None, None)  # type: ignore[arg-type]

    def test_effects_chain_builds_in_order(self):
        scene = cast(SourceScene, self._build(effects=["trails", "strobe", "invert"]))
        names = [e.name for e in scene.effects]
        self.assertEqual(names, ["trails", "strobe", "invert"])

    def test_mod_source_applied_to_every_layer(self):
        scene = cast(SourceScene, self._build(effects=["trails", "strobe"], mod_source="clock"))
        self.assertTrue(all(e.mod_source == "clock" for e in scene.effects))

    def test_legacy_single_effect_becomes_one_layer_chain(self):
        scene = cast(SourceScene, self._build(effect="trails"))
        self.assertEqual([e.name for e in scene.effects], ["trails"])

    def test_effect_and_effects_mutually_exclusive(self):
        s = SceneCfg(type="generative", source="plasma", effect="trails", effects=["blur"])
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=True)

    def test_unknown_effect_in_chain_rejected(self):
        s = SceneCfg(type="generative", source="plasma", effects=["trails", "nope"])
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=True)

    def test_bad_mod_source_rejected(self):
        s = SceneCfg(type="generative", source="plasma", mod_source="banana")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=True)

    def test_effects_rejected_on_non_frame_scene(self):
        s = SceneCfg(type="waveform", effects=["trails"])
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=True)


# --------------------------------------------------------------------------- #
# midi_control: fx<N> grammar + fx_toggle
# --------------------------------------------------------------------------- #
class ParamHolderGrammarTest(unittest.TestCase):
    def test_config_holder_validator(self):
        for good in ("effect", "source", "scene", "mode", "fx0", "fx12", "effect[3]"):
            self.assertTrue(_is_valid_param_holder(good), good)
        for bad in ("fx", "effect[]", "effectx", "fxa", "bogus"):
            self.assertFalse(_is_valid_param_holder(bad), bad)

    def test_resolve_layer_by_fx_number(self):
        a, b = build_effect("trails"), build_effect("blur")
        scene = SimpleNamespace(effects=[a, b])
        self.assertIs(mc._resolve_param_holder(scene, "fx0"), a)
        self.assertIs(mc._resolve_param_holder(scene, "fx1"), b)
        self.assertIs(mc._resolve_param_holder(scene, "effect[1]"), b)

    def test_resolve_out_of_range_layer_is_none(self):
        scene = SimpleNamespace(effects=[build_effect("trails")])
        self.assertIsNone(mc._resolve_param_holder(scene, "fx5"))

    def test_resolve_plain_effect_uses_attribute(self):
        # `effect` (no index) falls through to getattr — the back-compat path
        # (the real Scene.effect property returns effects[0]).
        sentinel = object()
        scene = SimpleNamespace(effect=sentinel, effects=[])
        self.assertIs(mc._resolve_param_holder(scene, "effect"), sentinel)


class FxToggleTest(unittest.TestCase):
    def test_parse_requires_nonnegative_slot(self):
        m = mc._parse_cc_map([{"type": "note", "number": 60, "action": "fx_toggle", "slot": 0}])
        self.assertEqual(m[("note", 60)].action, "fx_toggle")
        self.assertEqual(m[("note", 60)].slot, 0)

    def test_parse_rejects_missing_slot(self):
        with self.assertRaises(ValueError):
            mc._parse_cc_map([{"type": "note", "number": 60, "action": "fx_toggle"}])

    def test_parse_rejects_negative_slot(self):
        with self.assertRaises(ValueError):
            mc._parse_cc_map([{"type": "note", "number": 60, "action": "fx_toggle", "slot": -1}])

    def test_toggle_flips_layer_enabled(self):
        eff = build_effect("strobe")
        self.assertTrue(eff.enabled)
        posts: list[str] = []
        pl = cast(
            "object",
            SimpleNamespace(current=SimpleNamespace(effects=[eff]), post_osd=posts.append),
        )
        mc.MidiControlListener._toggle_effect_layer(pl, 0)  # type: ignore[arg-type]
        self.assertFalse(eff.enabled)
        mc.MidiControlListener._toggle_effect_layer(pl, 0)  # type: ignore[arg-type]
        self.assertTrue(eff.enabled)
        self.assertTrue(posts)  # feedback posted (OSD-off runs still cheap)

    def test_toggle_out_of_range_is_noop(self):
        eff = build_effect("strobe")
        pl = cast(
            "object",
            SimpleNamespace(current=SimpleNamespace(effects=[eff]), post_osd=lambda *_: None),
        )
        mc.MidiControlListener._toggle_effect_layer(pl, 3)  # type: ignore[arg-type]
        self.assertTrue(eff.enabled)  # unchanged

    def test_toggle_no_current_scene_is_noop(self):
        pl = cast("object", SimpleNamespace(current=None, post_osd=lambda *_: None))
        mc.MidiControlListener._toggle_effect_layer(pl, 0)  # type: ignore[arg-type]


class MidiActionParityTest(unittest.TestCase):
    def test_fx_toggle_in_both_action_lists(self):
        from c64cast import config as cfgmod

        self.assertIn("fx_toggle", mc._ACTIONS)
        self.assertIn("fx_toggle", cfgmod._MIDI_ACTION_CHOICES)


if __name__ == "__main__":
    unittest.main()
