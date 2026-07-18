"""Phase 1 of the MIDI live-tune feature: OSD, live-tunable display-mode params
(scalars + discrete choices), the mode.<name> holder in the MIDI/WLED param
seam, and the LiveTuneTracker → config save-back.

No hardware: display modes construct pure numpy state, and the MIDI holder logic
is exercised through a fake playlist/scene/mode.
"""

import os
import sys
import time
import unittest
from dataclasses import fields, replace
from typing import cast

from c64cast import config as cfgmod
from c64cast import scenes
from c64cast.config import _PALETTE_MODE_CHOICES, ColorCfg, Config, SceneCfg

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI  # noqa: E402

from c64cast.dither import DITHER_METHODS
from c64cast.midi_control import MidiControlListener
from c64cast.modes import (
    HiresDisplayMode,
    MCMDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
)
from c64cast.palette import (
    CELL_STRATEGIES,
    COLOR_MATCH_MODES,
    ColorFit,
    ColorFitAccumulator,
)
from c64cast.transport import LiveTuneTracker, atomic_write_text


def _color_choices(field_name: str) -> tuple[str, ...]:
    """The `choices` metadata of a ColorCfg field."""
    for f in fields(ColorCfg):
        if f.name == field_name:
            return tuple(f.metadata["choices"])
    raise KeyError(field_name)


# ---------------------------------------------------------------- OSD ----------
class OsdStateTests(unittest.TestCase):
    def test_post_then_current_then_expiry(self):
        osd = scenes.OsdState()
        self.assertIsNone(osd.current())  # nothing posted yet
        osd.post("dither_strength 0.70", duration_s=10.0)
        self.assertEqual(osd.current(), "dither_strength 0.70")

    def test_expiry_clears(self):
        osd = scenes.OsdState()
        osd.post("hello", duration_s=0.01)
        time.sleep(0.03)
        self.assertIsNone(osd.current())

    def test_post_supersedes(self):
        osd = scenes.OsdState()
        osd.post("first", duration_s=10.0)
        osd.post("second", duration_s=10.0)
        self.assertEqual(osd.current(), "second")

    def test_disabled_is_silent(self):
        osd = scenes.OsdState(enabled=False)
        osd.post("nope", duration_s=10.0)  # no-op when disabled
        self.assertIsNone(osd.current())

    def test_annotate_osd_returns_copy_same_shape(self):
        import numpy as np

        img = np.zeros((200, 320, 3), dtype=np.uint8)
        out = scenes._annotate_osd(img, "auto_fit_strength 0.50", "bottom")
        self.assertEqual(out.shape, img.shape)
        self.assertIsNot(out, img)
        self.assertTrue((img == 0).all())  # original untouched
        self.assertTrue((out != 0).any())  # text was drawn

    def test_annotate_osd_top_vs_bottom_differ(self):
        import numpy as np

        img = np.zeros((200, 320, 3), dtype=np.uint8)
        top = scenes._annotate_osd(img, "x 1", "top")
        bot = scenes._annotate_osd(img, "x 1", "bottom")
        # Text lands in different halves of the frame.
        self.assertTrue((top[:100].astype(int).sum()) > (top[100:].astype(int).sum()))
        self.assertTrue((bot[100:].astype(int).sum()) > (bot[:100].astype(int).sum()))


# ------------------------------------------------ LIVE_CHOICES drift -----------
class LiveChoicesDriftTests(unittest.TestCase):
    """Every discrete live-tune choice tuple must equal the config-metadata
    choices for the field it maps to (minus the resolve-time "auto"), so the
    live surface can't drift from the config surface — the single source of
    truth. See DisplayMode.LIVE_CHOICES."""

    def test_dither_method_matches_color_dither(self):
        # [color].dither metadata is ("auto",) + DITHER_METHODS.
        self.assertEqual(_color_choices("dither")[1:], DITHER_METHODS)
        for cls in (MCMDisplayMode, HiresDisplayMode, MultiHiresDisplayMode):
            self.assertEqual(cls.LIVE_CHOICES["dither_method"], DITHER_METHODS)

    def test_color_match_matches_metadata(self):
        self.assertEqual(_color_choices("color_match")[1:], COLOR_MATCH_MODES)
        for cls in (
            MCMDisplayMode,
            HiresDisplayMode,
            MultiHiresDisplayMode,
            PETSCIIDisplayMode,
        ):
            self.assertEqual(cls.LIVE_CHOICES["color_match"], COLOR_MATCH_MODES)

    def test_cell_strategy_matches_metadata(self):
        self.assertEqual(_color_choices("cell_strategy")[1:], CELL_STRATEGIES)
        self.assertEqual(MultiHiresDisplayMode.LIVE_CHOICES["cell_strategy"], CELL_STRATEGIES)

    def test_palette_mode_matches_metadata(self):
        for cls in (MCMDisplayMode, MultiHiresDisplayMode):
            self.assertEqual(cls.LIVE_CHOICES["palette_mode"], _PALETTE_MODE_CHOICES)


# --------------------------------------------------- mode setters --------------
class ModeSetterTests(unittest.TestCase):
    def test_dither_strength_property(self):
        m = MCMDisplayMode()
        name = "dither_strength"  # exercise the LIVE_PARAMS setattr path (as _apply_param does)
        setattr(m, name, 1.3)
        self.assertAlmostEqual(m._dither_strength, 1.3)
        self.assertAlmostEqual(m.dither_strength, 1.3)

    def test_motion_smoothing_rederives(self):
        m = MultiHiresDisplayMode(motion_smoothing=1.0)
        a1 = m._ema_alpha
        m.motion_smoothing = 0.0
        self.assertEqual(m._motion_smoothing, 0.0)
        self.assertEqual(m._ema_alpha, 1.0)  # s=0 → new frame fully replaces
        self.assertEqual(m._quant_hysteresis, 0.0)
        self.assertNotEqual(m._ema_alpha, a1)

    def test_color_match_rederives_penalty_and_pairwise(self):
        m = MultiHiresDisplayMode(perceptual=False, motion_smoothing=0.5)
        ps0, pair0, hy0 = m._penalty_scale, m._pal_pairwise.copy(), m._quant_hysteresis
        m.set_color_match("perceptual")
        self.assertTrue(m._perceptual)
        self.assertNotEqual(m._penalty_scale, ps0)
        self.assertFalse((m._pal_pairwise == pair0).all())
        self.assertNotEqual(m._quant_hysteresis, hy0)  # rescaled by new penalty
        # Back to rgb restores.
        m.set_color_match("rgb")
        self.assertFalse(m._perceptual)
        self.assertEqual(m._penalty_scale, ps0)

    def test_set_dither_method_and_cell_strategy(self):
        m = MultiHiresDisplayMode()
        m.set_dither_method("blue_noise")
        self.assertEqual(m._dither_method, "blue_noise")
        m.set_cell_strategy("error-min")
        self.assertEqual(m._cell_strategy, "error-min")
        with self.assertRaises(ValueError):
            m.set_cell_strategy("not-a-strategy")

    def test_get_live_choice(self):
        m = MultiHiresDisplayMode(perceptual=True, cell_strategy="contrast")
        self.assertEqual(m.get_live_choice("color_match"), "perceptual")
        self.assertEqual(m.get_live_choice("cell_strategy"), "contrast")
        self.assertEqual(m.get_live_choice("palette_mode"), m.palette_mode)
        self.assertIsNone(m.get_live_choice("nonexistent"))

    def test_set_live_choice_cycle(self):
        m = MultiHiresDisplayMode()
        # dither_method dispatches to set_dither_method (no api needed).
        label = m.set_live_choice(None, "dither_method", DITHER_METHODS[0])  # type: ignore[arg-type]
        self.assertEqual(m._dither_method, DITHER_METHODS[0])
        self.assertEqual(label, f"dither_method={DITHER_METHODS[0]}")

    def test_auto_fit_lerp_matches_accumulator(self):
        """The mode-side lerp of a full-strength fit must equal the value the
        ColorFitAccumulator would have baked at that strength — the refactor is
        behaviour-preserving at every strength, not just the default."""
        import numpy as np

        rng = np.random.default_rng(0)
        img = rng.integers(0, 255, size=(64, 96, 3), dtype=np.uint8)
        for st in (0.0, 0.25, 0.5, 1.0):
            baked = ColorFitAccumulator(strength=st)
            baked.add(img)
            baked_fit = baked.result()
            full = ColorFitAccumulator(strength=1.0)
            full.add(img)
            full_fit = full.result()
            if full_fit is None:
                self.assertIsNone(baked_fit)
                continue
            assert full_fit is not None
            lerped = full_fit.lerped(st)
            if baked_fit is None:
                self.assertTrue(lerped.is_identity())
            else:
                self.assertAlmostEqual(lerped.black, baked_fit.black, places=4)
                self.assertAlmostEqual(lerped.white, baked_fit.white, places=4)
                self.assertAlmostEqual(lerped.sat_mult, baked_fit.sat_mult, places=4)

    def test_fit_for_apply_uses_strength(self):
        m = MCMDisplayMode()
        m.set_color_fit(ColorFit(black=40.0, white=200.0, sat_mult=1.5))
        m.auto_fit_strength = 0.0
        zeroed = m._fit_for_apply()
        assert zeroed is not None
        self.assertTrue(zeroed.is_identity())
        m.auto_fit_strength = 1.0
        f = m._fit_for_apply()
        assert f is not None
        self.assertAlmostEqual(f.black, 40.0)
        self.assertAlmostEqual(f.white, 200.0)

    def test_hires_has_no_auto_fit(self):
        self.assertNotIn("auto_fit_strength", HiresDisplayMode.LIVE_PARAMS)
        self.assertNotIn("palette_mode", HiresDisplayMode.LIVE_CHOICES)


# -------------------------------------- MIDI mode.<name> holder ----------------
class _FakeMode:
    LIVE_PARAMS = {"dither_strength": (0.0, 2.0)}
    LIVE_CHOICES = {"dither_method": DITHER_METHODS}

    def __init__(self):
        self._dither_method = DITHER_METHODS[0]

    @property
    def dither_strength(self):
        return self._dither_strength

    @dither_strength.setter
    def dither_strength(self, v):
        self._dither_strength = float(v)

    def get_live_choice(self, name):
        return self._dither_method if name == "dither_method" else None

    def set_live_choice(self, api, name, value):
        self._dither_method = value
        return f"{name}={value}"


class _FakeScene:
    def __init__(self, mode):
        self.display_mode = mode
        self.api = object()
        self.osd = scenes.OsdState()


class _FakePlaylist:
    def __init__(self, scene):
        self.current = scene
        self.name = "s"
        self.osd_posts: list[str] = []
        self.live_tracker = LiveTuneTracker()

    def post_osd(self, text, duration_s=2.5):
        self.osd_posts.append(text)


class MidiModeHolderTests(unittest.TestCase):
    def _listener(self, pl):
        return MidiControlListener({"s": pl}, cc_map=[])

    def test_mode_scalar_sweep(self):
        mode = _FakeMode()
        pl = _FakePlaylist(_FakeScene(mode))
        lis = self._listener(pl)
        lis._apply_param(pl, "mode.dither_strength", 127, "cc")  # type: ignore[arg-type]
        self.assertAlmostEqual(mode._dither_strength, 2.0)  # full CC → hi
        self.assertTrue(pl.osd_posts)  # OSD posted
        self.assertTrue(pl.live_tracker.has_changes())

    def test_mode_choice_cc_bucket_select(self):
        mode = _FakeMode()
        pl = _FakePlaylist(_FakeScene(mode))
        lis = self._listener(pl)
        lis._apply_param(pl, "mode.dither_method", 127, "cc")  # type: ignore[arg-type]
        self.assertEqual(mode._dither_method, DITHER_METHODS[-1])

    def test_mode_choice_note_cycles(self):
        mode = _FakeMode()
        start = mode._dither_method
        pl = _FakePlaylist(_FakeScene(mode))
        lis = self._listener(pl)
        lis._apply_param(pl, "mode.dither_method", 100, "note")  # type: ignore[arg-type]
        self.assertEqual(mode._dither_method, DITHER_METHODS[1])
        self.assertNotEqual(mode._dither_method, start)

    def test_unknown_mode_param_is_noop(self):
        mode = _FakeMode()
        pl = _FakePlaylist(_FakeScene(mode))
        lis = self._listener(pl)
        lis._apply_param(pl, "mode.nonexistent", 64, "cc")  # type: ignore[arg-type]
        self.assertFalse(pl.osd_posts)
        self.assertFalse(pl.live_tracker.has_changes())


# ---------------------------------------------- LiveTuneTracker ----------------
class LiveTuneTrackerTests(unittest.TestCase):
    def test_record_and_describe(self):
        t = LiveTuneTracker()
        t.record("mode.dither_strength", 0.5, 0.7)
        self.assertTrue(t.has_changes())
        self.assertEqual(t.describe(), ["mode.dither_strength: 0.5 -> 0.7"])

    def test_retune_keeps_original_old(self):
        t = LiveTuneTracker()
        t.record("mode.dither_strength", 0.5, 0.7)
        t.record("mode.dither_strength", 0.7, 0.9)
        self.assertEqual(t.describe(), ["mode.dither_strength: 0.5 -> 0.9"])

    def test_back_to_start_drops_entry(self):
        t = LiveTuneTracker()
        t.record("mode.dither_strength", 0.5, 0.7)
        t.record("mode.dither_strength", 0.7, 0.5)  # back where it started
        self.assertFalse(t.has_changes())

    def test_apply_to_config_color_section(self):
        cfg = Config()
        t = LiveTuneTracker()
        t.record("mode.dither_strength", 0.5, 0.9)
        t.record("mode.dither_method", "none", "blue_noise")  # maps to [color].dither
        t.record("mode.color_match", "auto", "perceptual")
        applied = t.apply(cfg)
        self.assertAlmostEqual(cfg.color.dither_strength, 0.9)
        self.assertEqual(cfg.color.dither, "blue_noise")
        self.assertEqual(cfg.color.color_match, "perceptual")
        self.assertEqual(len(applied), 3)

    def test_palette_mode_not_persisted(self):
        # palette_mode is per-scene, not [color]; live-only in Phase 1.
        t = LiveTuneTracker()
        t.record("mode.palette_mode", "percell", "vivid")
        self.assertEqual(t.apply(Config()), [])

    def test_toml_snippet(self):
        t = LiveTuneTracker()
        t.record("mode.dither_strength", 0.5, 0.9)
        snippet = t.toml_snippet()
        self.assertIn("[color]", snippet)
        self.assertIn("dither_strength = 0.9", snippet)

    def test_empty_snippet(self):
        self.assertEqual(LiveTuneTracker().toml_snippet(), "")


class BuildSceneOsdStampTests(unittest.TestCase):
    """config.build_scene stamps [midi_control].osd onto each scene's OsdState."""

    def _build(self, osd_value: str) -> scenes.Scene:
        cfg = Config()
        cfg.midi_control = replace(cfg.midi_control, osd=osd_value)
        s = SceneCfg(type="blank")
        api = cast("cfgmod.C64Backend", FakeAPI())  # type: ignore[attr-defined]
        return cfgmod.build_scene(s, cfg, api, None, None)

    def test_bottom_default(self):
        scene = self._build("bottom")
        self.assertTrue(scene.osd.enabled)
        self.assertEqual(scene.osd.position, "bottom")

    def test_top(self):
        scene = self._build("top")
        self.assertTrue(scene.osd.enabled)
        self.assertEqual(scene.osd.position, "top")

    def test_off_disables(self):
        scene = self._build("off")
        self.assertFalse(scene.osd.enabled)


class BuildSceneLoopAudioStampTests(unittest.TestCase):
    """config.build_scene passes [midi_control].loop_audio to VideoScene's
    ctor (Phase 4 audio-resync policy) — mirrors BuildSceneOsdStampTests."""

    def _build_video(self, loop_audio: str) -> scenes.Scene:
        import tempfile

        fd, vid = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(os.unlink, vid)
        cfg = Config()
        cfg.midi_control = replace(cfg.midi_control, loop_audio=loop_audio)
        s = SceneCfg(type="video", display="mhires", file=vid)
        api = cast("cfgmod.C64Backend", FakeAPI())  # type: ignore[attr-defined]
        # A sentinel audio streamer is enough — setup() is never called here
        # (matches the fps/ensemble build_scene tests).
        audio = cast("cfgmod.AudioStreamer", object())  # type: ignore[attr-defined]
        return cfgmod.build_scene(s, cfg, api, audio, None)

    def test_on_round_trips(self):
        scene = self._build_video("on")
        self.assertEqual(scene._loop_audio, "on")  # type: ignore[attr-defined]

    def test_mute_round_trips(self):
        scene = self._build_video("mute")
        self.assertEqual(scene._loop_audio, "mute")  # type: ignore[attr-defined]


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_roundtrip(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "f.json")
            atomic_write_text(p, '{"a": 1}')
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"a": 1}')
            # No stray temp files left behind.
            self.assertEqual(os.listdir(os.path.dirname(p)), ["f.json"])


if __name__ == "__main__":
    unittest.main()
