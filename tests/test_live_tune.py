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
from typing import Any, cast

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.app.config import _PALETTE_MODE_CHOICES, ColorCfg, Config, SceneCfg
from c64cast.scenes import scenes

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI  # noqa: E402

from c64cast.control import live_tune as lt
from c64cast.control.midi_control import MidiControlListener
from c64cast.control.transport import LiveTuneTracker, atomic_write_text
from c64cast.video.dither import DITHER_METHODS
from c64cast.video.modes import (
    HiresDisplayMode,
    MCMDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
)
from c64cast.video.palette import (
    CELL_STRATEGIES,
    COLOR_MATCH_MODES,
    ColorFit,
    ColorFitAccumulator,
)


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
        behavior-preserving at every strength, not just the default."""
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


class LiveChoiceReadbackTests(unittest.TestCase):
    """Every declared LIVE_CHOICE must read back as one of its own choices.

    A mode declares a live choice by adding an entry to `LIVE_CHOICES`, and
    `get_live_choice` used to need a matching case — `cell_pick` was declared
    without one and read back `None`. Nothing caught it because the only reader
    was the MIDI cycle path, which treats `None` as "start at the first one".
    A surface that *shows* the value renders an empty picker instead."""

    def _modes(self):
        return [
            HiresDisplayMode(),
            MCMDisplayMode(),
            MultiHiresDisplayMode(),
            PETSCIIDisplayMode(),
        ]

    def test_every_declared_choice_reads_back_a_declared_value(self):
        for mode in self._modes():
            for name, choices in type(mode).LIVE_CHOICES.items():
                with self.subTest(mode=type(mode).__name__, choice=name):
                    self.assertIn(mode.get_live_choice(name), choices)

    def test_an_undeclared_name_is_none(self):
        self.assertIsNone(HiresDisplayMode().get_live_choice("nonesuch"))


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


# ------------------------------------------ live_tune, the shared module -------
class _FakeEffect:
    LIVE_PARAMS = {"amount": (0.0, 4.0)}

    def __init__(self):
        self.amount = 1.0


class _FakeSource:
    LIVE_PARAMS = {"speed": (0.0, 2.0)}

    def __init__(self):
        self.speed = 0.5


class _RichScene:
    """A scene with one of everything a holder prefix can name."""

    LIVE_PARAMS = {"gain": (0.25, 3.0)}

    def __init__(self, mode):
        self.display_mode = mode
        self.api = object()
        self.gain = 1.0
        self.source = _FakeSource()
        self.effects = [_FakeEffect(), _FakeEffect()]


def _rich() -> tuple[Any, _FakeMode, _RichScene]:
    """A playlist standing in for the real one. The fake exposes exactly what
    `live_tune` touches — `current`, `post_osd`, `live_tracker` — which is most
    of what makes the module worth having as one."""
    mode = _FakeMode()
    scene = _RichScene(mode)
    return _FakePlaylist(scene), mode, scene


class LiveTuneResolveTests(unittest.TestCase):
    """One lookup, shared by the MIDI surface, the WLED bridge and the web
    console — the thing that used to be three hand-mirrored copies."""

    def test_every_holder_prefix(self):
        _pl, mode, scene = _rich()
        self.assertIs(lt.resolve_holder(scene, "scene"), scene)
        self.assertIs(lt.resolve_holder(scene, "mode"), mode)
        self.assertIs(lt.resolve_holder(scene, "source"), scene.source)
        self.assertIs(lt.resolve_holder(scene, "fx1"), scene.effects[1])
        self.assertIs(lt.resolve_holder(scene, "effect[0]"), scene.effects[0])

    def test_holders_that_are_not_there(self):
        _pl, _mode, scene = _rich()
        self.assertIsNone(lt.resolve_holder(None, "mode"))
        self.assertIsNone(lt.resolve_holder(scene, "fx7"))  # past the chain
        self.assertIsNone(lt.resolve_holder(scene, "nonesuch"))

    def test_a_holder_without_the_declaration_does_not_resolve(self):
        # The class attributes are the whole definition of what is tunable: a
        # holder that exists but declares nothing is as unresolved as no holder.
        _pl, _mode, scene = _rich()
        self.assertIsNone(lt.resolve(scene, "source.nonesuch"))
        self.assertIsNone(lt.resolve(scene, "source"))

    def test_scalar_and_choice_carry_what_the_class_declared(self):
        _pl, _mode, scene = _rich()
        scalar = lt.resolve(scene, "mode.dither_strength")
        assert scalar is not None
        self.assertEqual((scalar.kind, scalar.lo, scalar.hi), ("scalar", 0.0, 2.0))
        choice = lt.resolve(scene, "mode.dither_method")
        assert choice is not None
        self.assertEqual(choice.kind, "choice")
        self.assertEqual(choice.choices, tuple(DITHER_METHODS))

    def test_resolve_first_is_the_wled_slider_rule(self):
        _pl, _mode, scene = _rich()
        found = lt.resolve_first(scene, ("nope.nope", "source.speed", "scene.gain"))
        assert found is not None
        self.assertEqual(found.name, "speed")
        self.assertIsNone(lt.resolve_first(scene, ("nope.nope",)))

    def test_read_and_norm(self):
        _pl, _mode, scene = _rich()
        self.assertEqual(lt.read(scene, "source.speed"), 0.5)
        self.assertIsNone(lt.read(scene, "source.nonesuch"))
        found = lt.resolve(scene, "source.speed")
        assert found is not None
        self.assertAlmostEqual(lt.norm_of(found, 0.5), 0.25)


class LiveTuneApplyTests(unittest.TestCase):
    def test_a_position_scales_into_the_declared_range(self):
        pl, _mode, scene = _rich()
        self.assertTrue(lt.apply(pl, "source.speed", lt.Move(position=127, full_scale=127.0)))
        self.assertAlmostEqual(scene.source.speed, 2.0)
        lt.apply(pl, "source.speed", lt.Move(position=128, full_scale=255.0))
        self.assertAlmostEqual(scene.source.speed, 2.0 * 128 / 255, places=4)

    def test_a_real_value_is_taken_as_it_is_and_clamped(self):
        pl, _mode, scene = _rich()
        lt.apply(pl, "source.speed", lt.Move(value=1.25))
        self.assertAlmostEqual(scene.source.speed, 1.25)
        lt.apply(pl, "source.speed", lt.Move(value=99.0))
        self.assertAlmostEqual(scene.source.speed, 2.0)

    def test_a_choice_by_name_by_position_and_by_cycle(self):
        pl, mode, _scene = _rich()
        lt.apply(pl, "mode.dither_method", lt.Move(value=DITHER_METHODS[2]))
        self.assertEqual(mode._dither_method, DITHER_METHODS[2])
        lt.apply(pl, "mode.dither_method", lt.Move(cycle=True))
        self.assertEqual(mode._dither_method, DITHER_METHODS[3 % len(DITHER_METHODS)])
        lt.apply(pl, "mode.dither_method", lt.Move(position=0, full_scale=127.0))
        self.assertEqual(mode._dither_method, DITHER_METHODS[0])

    def test_a_choice_the_list_does_not_contain_is_refused(self):
        pl, mode, _scene = _rich()
        start = mode._dither_method
        self.assertFalse(lt.apply(pl, "mode.dither_method", lt.Move(value="marching-ants")))
        self.assertEqual(mode._dither_method, start)

    def test_the_osd_line_is_the_surfaces_decision(self):
        # A controller wants the C64 to confirm the change; the web console must
        # not put a performer's edits on an audience-facing screen.
        pl, _mode, _scene = _rich()
        lt.apply(pl, "source.speed", lt.Move(value=1.0))
        self.assertEqual(len(pl.osd_posts), 1)
        lt.apply(pl, "source.speed", lt.Move(value=1.5, osd=False))
        self.assertEqual(len(pl.osd_posts), 1)

    def test_only_mode_changes_reach_the_save_back(self):
        # mode.* is the live face of a [color] config field; a generator's speed
        # is runtime state, and tracking it would write knob positions into a
        # show file.
        pl, _mode, _scene = _rich()
        lt.apply(pl, "source.speed", lt.Move(value=1.0))
        lt.apply(pl, "scene.gain", lt.Move(value=2.0))
        lt.apply(pl, "fx0.amount", lt.Move(value=2.0))
        self.assertFalse(pl.live_tracker.has_changes())
        lt.apply(pl, "mode.dither_strength", lt.Move(value=1.5))
        self.assertEqual(pl.live_tracker.describe(), ["mode.dither_strength: None -> 1.5"])

    def test_a_target_that_does_not_resolve_writes_nothing(self):
        pl, _mode, _scene = _rich()
        self.assertFalse(lt.apply(pl, "mode.nonexistent", lt.Move(value=1.0)))
        self.assertFalse(lt.apply(pl, "fx9.amount", lt.Move(value=1.0)))
        self.assertFalse(lt.apply_first(pl, ("a.b", "c.d"), lt.Move(value=1.0)))
        self.assertFalse(pl.osd_posts)

    def test_a_move_that_says_nothing_a_target_can_use(self):
        pl, _mode, scene = _rich()
        self.assertFalse(lt.apply(pl, "source.speed", lt.Move(value="fast")))
        self.assertFalse(lt.apply(pl, "source.speed", lt.Move(cycle=True)))
        self.assertAlmostEqual(scene.source.speed, 0.5)


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
        return scene_factory.build_scene(s, cfg, api, None, None)

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
        return scene_factory.build_scene(s, cfg, api, audio, None)

    def test_on_round_trips(self):
        scene = self._build_video("on")
        assert isinstance(scene, scenes.VideoScene)
        self.assertEqual(scene.transport.loop_audio, "on")

    def test_mute_round_trips(self):
        scene = self._build_video("mute")
        assert isinstance(scene, scenes.VideoScene)
        self.assertEqual(scene.transport.loop_audio, "mute")


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
