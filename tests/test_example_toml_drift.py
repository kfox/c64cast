"""Drift guards for the hand-curated example configs (#4).

The metadata in config.py is the single source of truth that feeds
`--describe` and the JSON schema (both generated, so they can't drift). The
example TOMLs are hand-written, so they CAN drift — this module keeps them
honest with three checks:

  1. Forward strictness: every key used in any shipped config is a real
     dataclass field / overlay parameter (catches typos + keys left behind
     when a field is renamed or removed). Pure stdlib — no jsonschema needed.
  2. Section-field coverage: c64cast.example.toml (the kitchen-sink
     reference) documents every config-section field, minus a small,
     explicitly-justified exempt set.
  3. Type coverage: the packaged examples ship a demo for every scene type and
     every overlay type (so a newly added type can't land undocumented).

The configs are enumerated through `paths`, the same resolver `--config
example:NAME` uses, so these checks cover exactly what ships.
"""

from __future__ import annotations

import dataclasses
import tomllib
import unittest

from c64cast.app import config as cfgmod
from c64cast.app import introspect, paths

_EXAMPLE = paths.resolve_example("c64cast.example")

_SECTION_DC = {
    "hardware": cfgmod.HardwareCfg,
    "teensyrom": cfgmod.TeensyromCfg,
    "ultimate64": cfgmod.Ultimate64Cfg,
    "video": cfgmod.VideoCfg,
    "audio": cfgmod.AudioCfg,
    "vision": cfgmod.VisionCfg,
    "interstitial": cfgmod.InterstitialCfg,
    "playlist": cfgmod.PlaylistCfg,
    "debug": cfgmod.DebugCfg,
    "preview": cfgmod.PreviewCfg,
    "recording": cfgmod.RecordingCfg,
    "color": cfgmod.ColorCfg,
    "dsp": cfgmod.DSPCfg,
    "audio_features": cfgmod.AudioFeaturesCfg,
    "control": cfgmod.ControlPlaneCfg,
    "web": cfgmod.WebCfg,
    "midi_control": cfgmod.MidiControlCfg,
    "performance": cfgmod.PerformanceCfg,
    "menu": cfgmod.MenuCfg,
}

# Section fields intentionally NOT shown as live keys in the reference TOML:
#   dma_password    — a secret; documented in prose + supplied via env var.
#   songlengths_file — points at an HVSC DB the user supplies; path is local.
#   log_file        — headless-run convenience; not part of the showcase.
#   frame_numbers   — a video-flicker diagnostic aid; not part of the showcase.
#   hue_corrections — a list-of-tables; the built-in purple-rescue default is
#                     the showcase. Shown commented in the [color] block (an
#                     uncommented band would double-apply the default boost).
#   pre_emphasis    — default is None (source-aware auto: mic 0.7 / line 0.6),
#                     not representable as a TOML value. Shown commented in
#                     [dsp]; an uncommented number would force all sources.
#   dac_calibration_profile — default is None (auto-derive from device
#                     identity); a name is only needed for a roaming
#                     TeensyROM+. Shown commented in [audio].
_COVERAGE_EXEMPT = {
    ("ultimate64", "dma_password"),
    ("playlist", "songlengths_file"),
    ("debug", "log_file"),
    ("debug", "frame_numbers"),
    ("color", "hue_corrections"),
    ("dsp", "pre_emphasis"),
    ("audio", "dac_calibration_profile"),
    # An unset charset_path means "use the character ROM c64cast dumped off
    # your own C64", which is what we want every user on — so the reference
    # documents it as a commented-out override rather than a live key that
    # would point everyone at a file only the author has.
    ("preview", "charset_path"),
    # cc_map_is_default is a non-persisted internal tracking flag (compare=False,
    # metadata.internal) — it's set from whether a cc_map key was authored, never
    # written to TOML. See MidiControlCfg.
    ("midi_control", "cc_map_is_default"),
}


def _all_configs():
    return paths.example_config_paths()


def _name(path):
    return paths.example_name(path)


def _load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


class ForwardStrictnessTest(unittest.TestCase):
    """No key in any shipped config may be unknown."""

    def test_section_keys_are_real(self):
        for path in _all_configs():
            data = _load(path)
            for section, dc in _SECTION_DC.items():
                if section not in data:
                    continue
                valid = {f.name for f in dataclasses.fields(dc)}
                unknown = set(data[section]) - valid
                self.assertFalse(
                    unknown,
                    f"{_name(path)} [{section}] unknown keys: {sorted(unknown)}",
                )

    def test_scene_keys_are_real(self):
        valid = {f.name for f in dataclasses.fields(cfgmod.SceneCfg)}
        for path in _all_configs():
            for s in _load(path).get("scenes", []):
                unknown = set(s) - valid
                self.assertFalse(
                    unknown,
                    f"{_name(path)} [[scenes]] unknown keys: {sorted(unknown)}",
                )

    def test_clip_keys_are_real(self):
        # [[performance.clips]] tables carry SceneCfg scene-spec fields plus the
        # launch/pad keys; _validate_clips is the authority (unknown keys, bad
        # slot/launch/quantize/pad). Every shipped config's grid must pass it.
        for path in _all_configs():
            clips = _load(path).get("performance", {}).get("clips", [])
            if not clips:
                continue
            try:
                cfgmod._validate_clips(clips)
            except ValueError as e:
                self.fail(f"{_name(path)} [[performance.clips]] invalid: {e}")

    def test_overlay_keys_are_real(self):
        params = {od.name: {p.name for p in od.params} for od in introspect.overlay_docs()}
        for path in _all_configs():
            for s in _load(path).get("scenes", []):
                for ov in s.get("overlays", []):
                    ot = ov.get("type")
                    self.assertIn(ot, params, f"{_name(path)} unknown overlay type {ot!r}")
                    unknown = set(ov) - {"type"} - params[ot]
                    self.assertFalse(
                        unknown,
                        f"{_name(path)} overlay {ot!r} unknown keys: {sorted(unknown)}",
                    )


class SectionCoverageTest(unittest.TestCase):
    def test_reference_documents_every_section_field(self):
        data = _load(_EXAMPLE)
        missing = []
        for section, dc in _SECTION_DC.items():
            present = set(data.get(section, {}))
            for f in dataclasses.fields(dc):
                if (section, f.name) in _COVERAGE_EXEMPT:
                    continue
                if f.name not in present:
                    missing.append(f"{section}.{f.name}")
        self.assertFalse(
            missing,
            "c64cast.example.toml is missing these fields (document them, or "
            f"add to _COVERAGE_EXEMPT with a reason): {sorted(missing)}",
        )


class TypeCoverageTest(unittest.TestCase):
    def _examples_union(self):
        scene_types, overlay_types = set(), set()
        for path in _all_configs():
            for s in _load(path).get("scenes", []):
                scene_types.add(s.get("type", "webcam"))
                for ov in s.get("overlays", []):
                    overlay_types.add(ov.get("type"))
        return scene_types, overlay_types

    def test_every_scene_type_has_a_demo(self):
        scene_types, _ = self._examples_union()
        missing = set(introspect.scene_type_names()) - scene_types
        self.assertFalse(missing, f"no packaged example demo for scene types: {sorted(missing)}")

    def test_every_overlay_has_a_demo(self):
        _, overlay_types = self._examples_union()
        missing = set(introspect.overlay_names()) - overlay_types
        self.assertFalse(missing, f"no packaged example demo for overlays: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
