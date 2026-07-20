"""Phase 5 of the MIDI live-tune feature: the --midi-setup learn wizard,
controller profiles, the profile-merge precedence, introspect.live_targets(),
and the osd.position live action.

No hardware and no real MIDI port: the wizard's pure helpers are driven by
scripted (kind, number, value, pressed) tuples, and the profile store / merge
resolver take an injected tempdir.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from c64cast import config as cfgmod
from c64cast import introspect, midi_setup
from c64cast import midi_control as mc
from c64cast.config import _DEFAULT_MIDI_CC_MAP
from c64cast.playlist import Playlist
from c64cast.transport import (
    ControllerProfileStore,
    controller_profile_path,
    make_controller_profile_store,
    slugify_port,
)


def _defaults() -> list[dict]:
    return [dict(d) for d in _DEFAULT_MIDI_CC_MAP]


# --------------------------------------------------- ControllerProfileStore ----
class ControllerProfileStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ctl.json"
            store = ControllerProfileStore(p)
            maps = [{"type": "note", "number": 36, "action": "skip"}]
            store.save("KeyLab mkII 49", maps)
            again = ControllerProfileStore(p)
            self.assertEqual(again.port(), "KeyLab mkII 49")
            self.assertEqual(again.mappings(), maps)

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = ControllerProfileStore(Path(d) / "nope.json")
            self.assertEqual(store.port(), "")
            self.assertEqual(store.mappings(), [])

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{ this is not json", encoding="utf-8")
            store = ControllerProfileStore(p)
            self.assertEqual(store.port(), "")
            self.assertEqual(store.mappings(), [])

    def test_malformed_mappings_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(
                json.dumps({"schema": 1, "port": "X", "mappings": [1, "no", {"ok": True}]}),
                encoding="utf-8",
            )
            self.assertEqual(ControllerProfileStore(p).mappings(), [{"ok": True}])

    def test_slug_stability(self):
        self.assertEqual(slugify_port("KeyLab mkII 49:MIDI 1"), "keylab-mkii-49-midi-1")
        self.assertEqual(slugify_port("!!!"), "controller")  # empty slug fallback
        # path derives from slug and is deterministic
        self.assertEqual(
            controller_profile_path("KeyLab").name,
            make_controller_profile_store("KeyLab").path.name,
        )

    def test_feedback_block_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "grid.json"
            store = ControllerProfileStore(p)
            fb = {"channel": 0, "active": 5, "port": "Launchpad OUT"}
            store.save("Launchpad", [{"type": "note", "number": 36, "action": "skip"}], feedback=fb)
            self.assertEqual(ControllerProfileStore(p).feedback(), fb)

    def test_feedback_absent_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "noled.json"
            ControllerProfileStore(p).save("X", [])  # no feedback arg
            self.assertEqual(ControllerProfileStore(p).feedback(), {})


# ------------------------------------------------- LED feedback (Phase 4) -------
class ProfileFeedbackLoaderTests(unittest.TestCase):
    """midi_control._load_profile_feedback resolves a profile's feedback block
    the same way the mapping loader resolves mappings."""

    def _dir_with_feedback(self, d: str, port: str, fb: dict) -> Path:
        base = Path(d)
        ControllerProfileStore(base / "ctl.json").save(port, [], feedback=fb)
        return base

    def test_auto_resolves_matching_profile(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_feedback(d, "MyPort", {"active": 9, "port": "MyPort OUT"})
            fb = mc._load_profile_feedback("auto", "USB MyPort 1", base)
            self.assertEqual(fb.get("active"), 9)
            # FeedbackMap picks it up (velocity override applied, defaults for the rest).
            fm = mc.FeedbackMap.from_dict(fb)
            self.assertEqual(fm.active, 9)
            self.assertEqual(fm.loaded, mc.FeedbackMap().loaded)

    def test_off_ignores_profile(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_feedback(d, "MyPort", {"active": 9})
            self.assertEqual(mc._load_profile_feedback("off", "USB MyPort 1", base), {})

    def test_named_profile_loads(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ControllerProfileStore(base / "grid.json").save("W", [], feedback={"fx_on": 7})
            self.assertEqual(mc._load_profile_feedback("grid", "Unrelated", base).get("fx_on"), 7)


# ------------------------------------------------------ merge precedence -------
class MergePrecedenceTests(unittest.TestCase):
    """The subtle bit: defaults < profile < explicit cc_map, while cc_map=[]
    still disables the shipped defaults. Table-driven over resolve_effective_cc_map."""

    def _dir_with_profile(self, d: str, port: str, mappings: list[dict]) -> Path:
        base = Path(d)
        ControllerProfileStore(base / "ctl.json").save(port, mappings)
        return base

    def _resolve(self, base_cc_map, is_default, profile_val, opened, base) -> dict:
        eff = mc.resolve_effective_cc_map(
            base_cc_map, is_default, profile_val, opened, profiles_dir=base
        )
        return mc._parse_cc_map(eff)

    def test_defaults_only_no_profile(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)  # empty dir, no profile
            m = self._resolve(_defaults(), True, "auto", "AnyPort", base)
            self.assertEqual(m[("note", 36)].action, "skip")  # shipped default intact

    def test_is_default_plus_profile_reclaims(self):
        # A profile reclaims note 36 (a shipped default) because defaults+profile
        # → later (profile) wins.
        prof = [{"type": "note", "number": 36, "action": "jump", "scene": 7}]
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_profile(d, "MyPort", prof)
            m = self._resolve(_defaults(), True, "auto", "USB MyPort 1", base)
            self.assertEqual(m[("note", 36)].action, "jump")
            self.assertEqual(m[("note", 36)].scene, 7)
            # a non-reclaimed default still present
            self.assertEqual(m[("note", 37)].action, "cycle_style")

    def test_explicit_cc_map_wins_over_profile(self):
        explicit = [{"type": "note", "number": 36, "action": "skip"}]
        prof = [{"type": "note", "number": 36, "action": "jump", "scene": 7}]
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_profile(d, "MyPort", prof)
            m = self._resolve(explicit, False, "auto", "USB MyPort 1", base)
            self.assertEqual(m[("note", 36)].action, "skip")  # explicit wins
            # shipped defaults are NOT re-injected under an explicit cc_map
            self.assertNotIn(("note", 37), m)

    def test_empty_cc_map_disables_defaults_profile_still_applies(self):
        prof = [{"type": "cc", "number": 20, "action": "param", "target": "mode.dither_strength"}]
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_profile(d, "MyPort", prof)
            m = self._resolve([], False, "auto", "USB MyPort 1", base)
            self.assertNotIn(("note", 36), m)  # defaults stay disabled
            self.assertIn(("cc", 20), m)  # profile applies

    def test_off_ignores_profile(self):
        prof = [{"type": "note", "number": 36, "action": "jump", "scene": 7}]
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_profile(d, "MyPort", prof)
            m = self._resolve(_defaults(), True, "off", "USB MyPort 1", base)
            self.assertEqual(m[("note", 36)].action, "skip")  # default, profile ignored

    def test_named_profile_loads_by_filename(self):
        prof = [{"type": "note", "number": 50, "action": "skip"}]
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ControllerProfileStore(base / "myctl.json").save("Whatever", prof)
            m = self._resolve(_defaults(), True, "myctl", "UnrelatedPort", base)
            self.assertEqual(m[("note", 50)].action, "skip")

    def test_auto_no_port_yet_is_empty_profile(self):
        prof = [{"type": "note", "number": 36, "action": "jump", "scene": 7}]
        with tempfile.TemporaryDirectory() as d:
            base = self._dir_with_profile(d, "MyPort", prof)
            m = self._resolve(_defaults(), True, "auto", None, base)
            self.assertEqual(m[("note", 36)].action, "skip")  # no match → defaults


# --------------------------------------------------- wizard pure helpers -------
class WizardHelperTests(unittest.TestCase):
    def test_detect_encoder_relative(self):
        self.assertTrue(midi_setup.detect_encoder([1, 2, 1, 3, 2]))
        self.assertTrue(midi_setup.detect_encoder([127, 126, 125, 127]))

    def test_detect_encoder_absolute(self):
        self.assertFalse(midi_setup.detect_encoder([0, 20, 40, 60, 90, 127]))
        self.assertFalse(midi_setup.detect_encoder([1, 2]))  # too few

    def test_dominant_control_picks_most_frequent_pressed(self):
        evs = [
            ("cc", 13, 0, True),
            ("cc", 13, 5, True),
            ("cc", 13, 9, True),
            ("note", 36, 100, True),
        ]
        self.assertEqual(midi_setup.dominant_control(evs), ("cc", 13))

    def test_dominant_control_ignores_releases(self):
        evs = [("note", 40, 0, False), ("note", 40, 0, False)]
        self.assertIsNone(midi_setup.dominant_control(evs))

    def test_values_for(self):
        evs = [("cc", 13, 3, True), ("cc", 14, 9, True), ("cc", 13, 5, True)]
        self.assertEqual(midi_setup.values_for(evs, "cc", 13), [3, 5])

    def test_mmc_entry_from_sysex_classification(self):
        # A transport button that emits MMC classifies as ("mmc", cmd) and builds
        # a type:"mmc" entry — auto-recognized, no separate step.
        msg = types.SimpleNamespace(type="sysex", data=(0x7F, 0x7F, 0x06, 0x02))
        c = mc.classify_message(msg)
        self.assertEqual(c, ("mmc", 0x02, 127, True))
        assert c is not None
        entry = midi_setup.build_transport_entry("transport.play_pause", c[0], c[1])
        self.assertEqual(entry, {"type": "mmc", "number": 2, "action": "transport.play_pause"})

    def test_dedupe_last_wins(self):
        maps = [
            {"type": "note", "number": 60, "action": "skip"},
            {"type": "note", "number": 60, "action": "cycle_style"},
        ]
        out = midi_setup.dedupe_mappings(maps)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "cycle_style")

    def test_build_feedback_block_defaults_and_overrides(self):
        # Defaults come from FeedbackMap; a valid override wins, a bad one is
        # dropped; the port is included when given.
        block = midi_setup.build_feedback_block(
            port="Launchpad OUT", overrides={"active": 5, "loaded": 999, "bogus": 1}
        )
        default = mc.FeedbackMap().to_dict()
        self.assertEqual(block["active"], 5)
        self.assertEqual(block["loaded"], default["loaded"])  # out-of-range dropped
        self.assertNotIn("bogus", block)
        self.assertEqual(block["port"], "Launchpad OUT")
        # Round-trips through the runtime reader without loss.
        self.assertEqual(mc.FeedbackMap.from_dict(block).active, 5)

    def test_build_feedback_block_no_port(self):
        block = midi_setup.build_feedback_block()
        self.assertNotIn("port", block)

    def test_learned_entries_validate(self):
        # Everything the wizard can build must pass the config validator, so a
        # saved profile can't produce an invalid cc_map at load time.
        maps = [
            midi_setup.build_transport_entry("transport.stop", "note", 60),
            midi_setup.build_transport_entry("osd.position", "cc", 80),
            midi_setup.build_param_entry(13, "mode.dither_strength"),
            midi_setup.build_jog_entry(20),
            midi_setup.build_jump_entry("pc", 3, 5),
        ]
        cfg = cfgmod.MidiControlCfg(enabled=True, cc_map=maps)
        cfgmod.validate_midi_control_cfg(cfg)  # must not raise
        mc._parse_cc_map(maps)  # runtime parser must accept them too


# ------------------------------------------------- introspect.live_targets -----
class LiveTargetsDriftTests(unittest.TestCase):
    """live_targets() must expose exactly the LIVE_PARAMS/LIVE_CHOICES declared
    across the effect/generator/mode/scope registries — same spirit as the
    LIVE_CHOICES ↔ [color] metadata pin in test_live_tune.py."""

    def _declared(self) -> set[str]:
        from c64cast import effects, generators, voice_scope
        from c64cast import modes as modesmod

        declared: set[str] = set()

        def add(holder, cls):
            for pname in getattr(cls, "LIVE_PARAMS", {}) or {}:
                declared.add(f"{holder}.{pname}")
            for pname in getattr(cls, "LIVE_CHOICES", {}) or {}:
                declared.add(f"{holder}.{pname}")

        def walk(cls):
            for sub in cls.__subclasses__():
                if getattr(sub, "name", ""):
                    add("mode", sub)
                walk(sub)

        walk(modesmod.DisplayMode)
        for cls in effects.REGISTRY.values():
            add("effect", cls)
        for cls in generators.REGISTRY.values():
            add("source", cls)
        add("scene", voice_scope.VoiceScopeRenderer)
        return declared

    def test_live_targets_matches_registries(self):
        exposed = {t.target for t in introspect.live_targets()}
        self.assertEqual(exposed, self._declared())

    def test_scalar_vs_choice_kind(self):
        for t in introspect.live_targets():
            if t.kind == "scalar":
                self.assertIsNotNone(t.lo)
                self.assertIsNotNone(t.hi)
                self.assertFalse(t.choices)
            else:
                self.assertEqual(t.kind, "choice")
                self.assertTrue(t.choices)

    def test_groups_present(self):
        groups = {t.group for t in introspect.live_targets()}
        self.assertEqual(groups, {"Color pipeline", "Effect", "Generator", "Scope"})


# ------------------------------------------------------- osd.position ----------
class _OsdScene:
    def __init__(self):
        from c64cast import scenes

        self.osd = scenes.OsdState()


class PlaylistCycleOsdTests(unittest.TestCase):
    """The real Playlist.cycle_osd logic, driven on a minimal stand-in self
    (it only touches self.current)."""

    def _call(self, scene, *, double_tap):
        ns = types.SimpleNamespace(current=scene)
        Playlist.cycle_osd(ns, double_tap=double_tap)  # type: ignore[arg-type]

    def test_tap_toggles_corner(self):
        s = _OsdScene()
        self.assertEqual(s.osd.position, "bottom")
        self._call(s, double_tap=False)
        self.assertEqual(s.osd.position, "top")
        self._call(s, double_tap=False)
        self.assertEqual(s.osd.position, "bottom")

    def test_double_tap_disables(self):
        s = _OsdScene()
        self._call(s, double_tap=True)
        self.assertFalse(s.osd.enabled)

    def test_tap_while_disabled_reenables(self):
        s = _OsdScene()
        s.osd.enabled = False
        self._call(s, double_tap=False)
        self.assertTrue(s.osd.enabled)

    def test_none_scene_is_noop(self):
        ns = types.SimpleNamespace(current=None)
        Playlist.cycle_osd(ns, double_tap=False)  # type: ignore[arg-type]  # must not raise


class _OsdPlaylist:
    """Records cycle_osd(double_tap=...) calls for the listener double-tap test."""

    def __init__(self):
        self.name = "s"
        self.calls: list[bool] = []
        self.current = _OsdScene()

    def cycle_osd(self, *, double_tap):
        self.calls.append(double_tap)


class OsdPositionDispatchTests(unittest.TestCase):
    def _dispatch_osd(self, listener, pl, at):
        mapping = mc._CCMapping(kind="cc", number=80, action="osd.position")
        with mock.patch.object(mc.time, "monotonic", return_value=at):
            listener._apply(pl, mapping, 127, True)

    def test_double_tap_timing(self):
        pl = _OsdPlaylist()
        lis = mc.MidiControlListener({"s": pl}, cc_map=[])  # type: ignore[dict-item]
        self._dispatch_osd(lis, pl, at=100.0)  # first tap → not double
        self._dispatch_osd(lis, pl, at=100.1)  # within window → double
        self._dispatch_osd(lis, pl, at=101.0)  # after window → not double
        self.assertEqual(pl.calls, [False, True, False])


# ---------------------------------------------------- config round-trip --------
class ConfigFieldTests(unittest.TestCase):
    def test_controller_profile_round_trips(self):
        from c64cast import config_serialize

        cfg = cfgmod.Config()
        cfg.midi_control.controller_profile = "my-keylab"
        cfg.midi_control.enabled = True
        text = config_serialize.dumps(cfg)
        self.assertIn("my-keylab", text)
        reloaded = _load_from_string(text)
        self.assertEqual(reloaded, cfg)

    def test_cc_map_is_default_not_serialized(self):
        from c64cast import config_serialize

        cfg = cfgmod.Config()
        cfg.midi_control.cc_map_is_default = False  # internal — never emitted
        self.assertNotIn("cc_map_is_default", config_serialize.dumps(cfg))

    def test_validate_accepts_new_action_and_profile(self):
        cfg = cfgmod.MidiControlCfg(
            enabled=True,
            controller_profile="off",
            cc_map=[{"type": "cc", "number": 80, "action": "osd.position"}],
        )
        cfgmod.validate_midi_control_cfg(cfg)  # must not raise

    def test_validate_rejects_empty_profile(self):
        cfg = cfgmod.MidiControlCfg(enabled=True, controller_profile="")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate_midi_control_cfg(cfg)


def _load_from_string(text: str) -> cfgmod.Config:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        with mock.patch.dict("os.environ", {"C64CAST_SETTINGS": path + ".nonexistent"}):
            return cfgmod.load(path)
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
