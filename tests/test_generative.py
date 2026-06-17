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
from c64cast.backend import C64Backend
from c64cast.config import AudioCfg, Config, SceneCfg, build_scene, validate_scene_cfg
from c64cast.effects import FrameEffect, TrailsEffect, build_effect
from c64cast.frame_source import BaseFrameSource, FrameSource
from c64cast.generators import build_generator, generator_names
from c64cast.modes import DisplayMode
from c64cast.scenes import Scene, SourceScene, _render_with_overlays


class GeneratorTest(unittest.TestCase):
    def test_registry_nonempty_and_named(self):
        names = generator_names()
        self.assertIn("plasma", names)
        self.assertIn("tunnel", names)

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
        # the historical pure-time output for both generators (the offline
        # renderer + drift tests rely on this).
        for name in ("plasma", "tunnel"):
            g = build_generator(name)
            np.testing.assert_array_equal(g.render(0.7), g.render(0.7, None))
            np.testing.assert_array_equal(g.read(0.7), g.render(0.7, None))

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


class EffectTest(unittest.TestCase):
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

    def apply(self, frame, t):
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
