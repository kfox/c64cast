"""Tests for ensemble (multi-system) config loading.

Covers `load_master()` routing logic and `EnsembleCfg` / `SystemEntryCfg`
dataclass shape. The override cascade implemented by
`apply_master_defaults` is exercised in a later commit; this file just
asserts the stub passes per-system configs through unchanged."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from _fakes import MachineSettingsIsolation

from c64cast.app import config as cfgmod

# Every load_master()/resolve_recording_path() call reads the machine-settings
# file, so the module points $C64CAST_SETTINGS at a missing path; the tests that
# want a machine layer write their own and re-patch over this.
_iso = MachineSettingsIsolation()


def setUpModule():
    _iso.start()


def tearDownModule():
    _iso.stop()


def _write(path: str, body: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class LoadMasterRoutingTest(unittest.TestCase):
    def test_no_ensemble_returns_single_config(self):
        toml = """
            [ultimate64]
            url = "http://single.lan"
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            result = cfgmod.load_master(path)
        self.assertFalse(result.is_ensemble)
        self.assertEqual(len(result.cfgs), 1)
        self.assertEqual(result.names, ["system"])
        self.assertEqual(result.cfgs[0].ultimate64.url, "http://single.lan")
        self.assertIsNone(result.cfgs[0].ensemble)

    def test_missing_default_path_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                result = cfgmod.load_master(None)
            finally:
                os.chdir(cwd)
        self.assertFalse(result.is_ensemble)
        self.assertEqual(len(result.cfgs), 1)
        self.assertEqual(result.names, ["system"])

    def test_ensemble_returns_list(self):
        master = """
            [ensemble]
            systems = [
                { name = "left",  config = "left.toml"  },
                { name = "right", config = "right.toml" },
            ]
        """
        left = '[ultimate64]\nurl = "http://left.lan"\n'
        right = '[ultimate64]\nurl = "http://right.lan"\n'
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "left.toml"), left)
            _write(os.path.join(tmp, "right.toml"), right)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                result = cfgmod.load_master(master_path)
        self.assertTrue(result.is_ensemble)
        self.assertEqual(result.names, ["left", "right"])
        self.assertEqual(len(result.cfgs), 2)
        self.assertEqual(result.cfgs[0].ultimate64.url, "http://left.lan")
        self.assertEqual(result.cfgs[1].ultimate64.url, "http://right.lan")
        # Per-system Configs never carry ensemble metadata themselves.
        self.assertIsNone(result.cfgs[0].ensemble)
        self.assertIsNone(result.cfgs[1].ensemble)

    def test_ensemble_resolves_paths_relative_to_master(self):
        master = """
            [ensemble]
            systems = [
                { name = "sub", config = "nested/sub.toml" },
            ]
        """
        sub = '[ultimate64]\nurl = "http://sub.lan"\n'
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            os.makedirs(os.path.join(tmp, "nested"))
            _write(os.path.join(tmp, "nested", "sub.toml"), sub)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                result = cfgmod.load_master(master_path)
        self.assertTrue(result.is_ensemble)
        self.assertEqual(result.cfgs[0].ultimate64.url, "http://sub.lan")

    def test_ensemble_warns_on_master_level_scenes(self):
        master = """
            [ensemble]
            systems = [ { name = "only", config = "only.toml" } ]
            [[scenes]]
            type = "blank"
        """
        only = '[ultimate64]\nurl = "http://only.lan"\n'
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), only)
            with self.assertLogs("c64cast.app.config", level="WARNING") as cm:
                result = cfgmod.load_master(master_path)
        self.assertTrue(any("[[scenes]]" in line for line in cm.output))
        # Master-level scenes don't bleed into the per-system Config.
        self.assertEqual(result.cfgs[0].scenes, [])

    def test_master_control_carries_from_master_toml(self):
        # The control plane is wired from the master TOML in ensemble mode;
        # per-system [control] sections are ignored. master_control surfaces
        # whatever the master set.
        master = """
            [ensemble]
            systems = [ { name = "only", config = "only.toml" } ]
            [control]
            enabled = true
            port = 9876
        """
        only = '[ultimate64]\nurl = "http://only.lan"\n'
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), only)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                result = cfgmod.load_master(master_path)
        self.assertTrue(result.master_control.enabled)
        self.assertEqual(result.master_control.port, 9876)


class ApplyMasterDefaultsTest(unittest.TestCase):
    """Override cascade: master fields fill in per-system fields the user
    left at the dataclass default, but never overwrite explicit values."""

    def test_master_default_fills_unset_per_system_field(self):
        defaults = cfgmod.Config()
        defaults.interstitial.duration_s = 7.5
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.interstitial.duration_s, 7.5)

    def test_per_system_explicit_value_wins_over_master(self):
        defaults = cfgmod.Config()
        defaults.interstitial.duration_s = 7.5
        sys_cfg = cfgmod.Config()
        sys_cfg.interstitial.duration_s = 4.2
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.interstitial.duration_s, 4.2)

    def test_url_never_cascades(self):
        # Even if master sets ultimate64.url, per-system at the dataclass
        # default does NOT inherit — every U64 must declare its own URL.
        defaults = cfgmod.Config()
        defaults.ultimate64.url = "http://shared.lan"
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.ultimate64.url, cfgmod.Ultimate64Cfg().url)

    def test_dma_port_does_cascade(self):
        defaults = cfgmod.Config()
        defaults.ultimate64.dma_port = 1234
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.ultimate64.dma_port, 1234)

    def test_video_section_does_not_cascade(self):
        # [video] is per-system only (hardware-specific device index).
        defaults = cfgmod.Config()
        defaults.video.device = 5
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.video.device, cfgmod.VideoCfg().device)

    def test_recording_path_does_not_cascade(self):
        # A cascaded path would point every system's cv2.VideoWriter at one
        # file; `enabled` still cascades, so "record the wall" stays one key.
        defaults = cfgmod.Config()
        defaults.recording.enabled = True
        defaults.recording.path = "wall.mp4"
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertTrue(sys_cfg.recording.enabled)
        self.assertEqual(sys_cfg.recording.path, cfgmod.RecordingCfg().path)

    def test_control_section_does_not_cascade(self):
        # [control] is wired from the master directly (one control plane
        # for the whole ensemble); per-system [control] would be confusing.
        defaults = cfgmod.Config()
        defaults.control.enabled = True
        defaults.control.port = 9999
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.control.enabled, cfgmod.ControlPlaneCfg().enabled)
        self.assertEqual(sys_cfg.control.port, cfgmod.ControlPlaneCfg().port)

    def test_cascade_through_load_master(self):
        # End-to-end through load_master: master sets interstitial duration,
        # per-system file doesn't, per-system Config picks it up.
        master = """
            [ensemble]
            systems = [ { name = "only", config = "only.toml" } ]
            [interstitial]
            duration_s = 11.0
            [audio]
            enabled = true
        """
        only = '[ultimate64]\nurl = "http://only.lan"\n'
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), only)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                result = cfgmod.load_master(master_path)
        self.assertEqual(result.cfgs[0].interstitial.duration_s, 11.0)
        self.assertTrue(result.cfgs[0].audio.enabled)

    def test_per_system_override_through_load_master(self):
        master = """
            [ensemble]
            systems = [ { name = "only", config = "only.toml" } ]
            [interstitial]
            duration_s = 11.0
        """
        only = """
            [ultimate64]
            url = "http://only.lan"
            [interstitial]
            duration_s = 2.5
        """
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), only)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                result = cfgmod.load_master(master_path)
        self.assertEqual(result.cfgs[0].interstitial.duration_s, 2.5)

    def test_a_cascaded_mutable_value_is_not_shared_between_systems(self):
        # A bare setattr handed every inheriting system the same list object as
        # the master and as each other, so one system mutating it in place
        # mutated every system's — invisibly at the config layer.
        defaults = cfgmod.Config()
        defaults.ultimate64.sid_panning = [-3, 3]
        left, right = cfgmod.Config(), cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, left)
        cfgmod.apply_master_defaults(defaults, right)
        self.assertEqual(left.ultimate64.sid_panning, [-3, 3])
        self.assertIsNot(left.ultimate64.sid_panning, defaults.ultimate64.sid_panning)
        self.assertIsNot(left.ultimate64.sid_panning, right.ultimate64.sid_panning)
        left.ultimate64.sid_panning.append(0)
        self.assertEqual(right.ultimate64.sid_panning, [-3, 3])
        self.assertEqual(defaults.ultimate64.sid_panning, [-3, 3])

    def test_a_cascaded_list_of_tables_is_deep_copied(self):
        defaults = cfgmod.Config()
        defaults.color.hue_corrections = [{"name": "band", "hue_lo_deg": 10}]
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        sys_cfg.color.hue_corrections[0]["hue_lo_deg"] = 99
        self.assertEqual(defaults.color.hue_corrections[0]["hue_lo_deg"], 10)


class SectionClassificationTest(unittest.TestCase):
    """A section's cascade behavior is spelled out in exactly one place.

    It used to be a don't-list inside a comment next to a hand-written apply
    tuple in load_master, and the two drifted: [hardware] and [teensyrom] were
    listed as cascading while the master file's copies of them never reached
    `defaults` at all, so the cascade could only ever copy the machine layer."""

    def test_every_scalar_section_is_classified_exactly_once(self):
        cascading = {name for name, _ in cfgmod._CASCADE_SECTIONS}
        never = set(cfgmod._NEVER_CASCADE_SECTIONS)
        self.assertEqual(cascading & never, set())
        self.assertEqual(cascading | never, {*cfgmod._TOML_SCALAR_SECTIONS, "color"})

    def test_the_process_wide_set_is_a_subset_of_the_never_cascade_set(self):
        self.assertTrue(set(cfgmod._NEVER_CASCADE_SECTIONS) >= cfgmod._MASTER_PROCESS_WIDE_SECTIONS)

    def test_every_cascading_section_is_reachable_from_a_master_toml(self):
        # The check that would have caught the drift: a section listed as
        # cascading has to actually receive the master file's values.
        for name, skips in cfgmod._CASCADE_SECTIONS:
            cascadable = [
                f
                for f in dataclasses.fields(getattr(cfgmod.Config(), name))
                if f.name not in skips and not f.metadata.get("internal")
            ]
            self.assertTrue(cascadable, name)
            defaults = cfgmod.Config()
            _apply = getattr(defaults, name)
            probe = _probe_value(getattr(_apply, cascadable[0].name))
            if probe is None:
                continue
            cfgmod._apply_toml_sections(
                defaults, {name: {cascadable[0].name: probe}}, source="master.toml"
            )
            sys_cfg = cfgmod.Config()
            cfgmod.apply_master_defaults(defaults, sys_cfg)
            self.assertEqual(getattr(getattr(sys_cfg, name), cascadable[0].name), probe, name)


def _probe_value(current: object) -> object | None:
    """A legal-but-different value for `current`, or None when this field's
    type has no obvious one (so the caller skips it)."""
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 3
    if isinstance(current, float):
        return current + 3.0
    return None


class MasterSectionCoverageTest(unittest.TestCase):
    """The master TOML's own sections go through the same apply loop as any
    other file. A hand-written tuple used to stand in for it and had dropped
    six sections, which produced neither an applied value nor an unknown-key
    record — no warning, no doctor row."""

    def _load(self, master_body: str, per_system: str = '[ultimate64]\nurl = "http://only.lan"\n'):
        master = '[ensemble]\nsystems = [ { name = "only", config = "only.toml" } ]\n' + master_body
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), per_system)
            with self.assertLogs("c64cast.app.config", level="INFO"):
                return cfgmod.load_master(master_path)

    def test_master_hardware_backend_reaches_every_system(self):
        # The load-bearing case: [hardware] is in _CASCADE_SECTIONS, so the
        # cascade dutifully ran — over a defaults.hardware nothing populated.
        result = self._load('[hardware]\nbackend = "teensyrom"\n')
        self.assertEqual(result.cfgs[0].hardware.backend, "teensyrom")

    def test_master_teensyrom_transport_cascades_but_the_port_does_not(self):
        result = self._load(
            '[hardware]\nbackend = "teensyrom"\n[teensyrom]\ntransport = "tcp"\nbaud = 500000\n',
            per_system='[teensyrom]\nhost = "tr.lan"\n',
        )
        self.assertEqual(result.cfgs[0].teensyrom.transport, "tcp")
        self.assertEqual(result.cfgs[0].teensyrom.baud, 500000)
        self.assertEqual(result.cfgs[0].teensyrom.host, "tr.lan")

    def test_master_dsp_and_audio_features_reach_every_system(self):
        result = self._load("[dsp]\nenabled = false\n[audio_features]\nbands = 8\n")
        self.assertFalse(result.cfgs[0].dsp.enabled)
        self.assertEqual(result.cfgs[0].audio_features.bands, 8)

    def test_master_wled_reaches_every_system(self):
        result = self._load('[wled]\nname = "wall"\n')
        self.assertEqual(result.cfgs[0].wled.name, "wall")

    def test_an_unknown_master_section_is_collected_not_discarded(self):
        result = self._load('[hardwear]\nbackend = "teensyrom"\n')
        self.assertIn(("", "hardwear"), [(r.section, r.key) for r in result.unknown_keys])

    def test_a_master_section_that_reaches_nothing_is_called_out(self):
        # [video] never cascades and LoadResult does not expose it, so a master
        # [video] block is dead — which used to be entirely silent.
        master = (
            '[ensemble]\nsystems = [ { name = "only", config = "only.toml" } ]\n'
            "[video]\nsetup_progress_bar = false\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), '[ultimate64]\nurl = "http://only.lan"\n')
            with self.assertLogs("c64cast.app.config", level="WARNING") as logs:
                cfgmod.load_master(master_path)
        self.assertTrue(any("[video]" in m for m in logs.output))

    def test_the_master_meets_the_full_validator_battery(self):
        # [ultimate64] cascades with only `url` skipped, so an unvalidated
        # master pan list was copied into every system and failed mid-run when
        # the mixer was configured — which _validate_sid_panning exists to stop.
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_panning = [99]\n")
        self.assertIn("sid_panning", str(ctx.exception))

    def test_a_master_authored_cc_map_still_clears_the_default_flag(self):
        result = self._load("[midi_control]\ncc_map = []\n")
        self.assertFalse(result.master_midi_control.cc_map_is_default)

    def test_master_hue_corrections_replace_rather_than_extend(self):
        result = self._load(
            '[color]\n[[color.hue_corrections]]\nname = "master"\nhue_lo_deg = 10\n'
        )
        self.assertEqual([hc["name"] for hc in result.cfgs[0].color.hue_corrections], ["master"])


class EnsembleEnvCredentialTest(unittest.TestCase):
    """The control plane an ensemble binds is LoadResult.master_control, an
    object no merge_cli call ever touches — so the env tokens landed on N
    per-system Configs nothing reads while the plane came up on whatever the
    shared master TOML declared, the opposite of the field's own promise."""

    def _load(self, master_body: str):
        master = '[ensemble]\nsystems = [ { name = "only", config = "only.toml" } ]\n' + master_body
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), '[ultimate64]\nurl = "http://only.lan"\n')
            with self.assertLogs("c64cast.app.config", level="INFO"):
                return cfgmod.load_master(master_path)

    def test_the_env_token_beats_the_committed_master_token(self):
        with mock.patch.dict(os.environ, {"C64CAST_CONTROL_TOKEN": "rotated"}):
            result = self._load('[control]\nenabled = true\ntoken = "placeholder"\n')
        self.assertEqual(result.master_control.token, "rotated")

    def test_the_env_viewer_token_reaches_the_master_too(self):
        with mock.patch.dict(os.environ, {"C64CAST_CONTROL_VIEWER_TOKEN": "watch"}):
            result = self._load('[control]\nenabled = true\ntoken = "full"\n')
        self.assertEqual(result.master_control.viewer_token, "watch")

    def test_no_env_leaves_the_master_token_alone(self):
        drop = {"C64CAST_CONTROL_TOKEN", "C64CAST_CONTROL_VIEWER_TOKEN"}
        kept = {k: v for k, v in os.environ.items() if k not in drop}
        with mock.patch.dict(os.environ, kept, clear=True):
            result = self._load('[control]\nenabled = true\ntoken = "from-master"\n')
        self.assertEqual(result.master_control.token, "from-master")


class AudioContentionMirrorTest(unittest.TestCase):
    """_scene_contends_for_audio claims to mirror
    Scene.competes_for_audio_lock(), and a generative scene with
    audio_source = "sid" builds a SidFileAudioSource whose
    `wants_audio_lock` is True — so the mirror has to know about it or
    _warn_audio_only_ensemble goes silent on the footgun it exists to catch."""

    def test_a_generative_sid_scene_contends(self):
        s = cfgmod.SceneCfg(type="generative", audio_source="sid", file="tune.sid")
        self.assertTrue(cfgmod._scene_contends_for_audio(s))

    def test_a_generative_scene_with_any_other_source_does_not(self):
        for source in ("none", "mic", "listen", "file"):
            s = cfgmod.SceneCfg(type="generative", audio_source=source)
            self.assertFalse(cfgmod._scene_contends_for_audio(s), source)

    def test_an_all_generative_sid_playlist_gets_the_warning(self):
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="generative", audio_source="sid", file="tune.sid")]
        with self.assertLogs("c64cast.app.config", level="WARNING") as logs:
            cfgmod._warn_audio_only_ensemble([cfg], ["left"])
        self.assertIn("ensemble audio slot", logs.output[0])


class EnsembleMachineSettingsTest(unittest.TestCase):
    """Machine settings apply in ensemble mode with the precedence
    machine < master < per-system (per the plan). $C64CAST_SETTINGS points at
    a tmp file so the real ~/.config file is never read."""

    def _run(self, *, settings: str, master: str, per_system: str):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "settings.toml")
            _write(settings_path, settings)
            master_path = os.path.join(tmp, "master.toml")
            _write(master_path, master)
            _write(os.path.join(tmp, "only.toml"), per_system)
            with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": settings_path}):
                with self.assertLogs("c64cast.app.config", level="INFO"):
                    return cfgmod.load_master(master_path)

    _MASTER_ONLY = """
        [ensemble]
        systems = [ { name = "only", config = "only.toml" } ]
    """

    def test_machine_only_field_reaches_per_system(self):
        # A field set only in machine settings, absent from master + per-system,
        # survives onto the per-system Config.
        result = self._run(
            settings="[audio]\nsample_rate = 8000\n",
            master=self._MASTER_ONLY,
            per_system='[ultimate64]\nurl = "http://only.lan"\n',
        )
        self.assertEqual(result.cfgs[0].audio.sample_rate, 8000)

    def test_master_overrides_machine(self):
        # machine < master: the master TOML wins over a machine default.
        result = self._run(
            settings="[interstitial]\nduration_s = 3.0\n",
            master=self._MASTER_ONLY + "\n[interstitial]\nduration_s = 11.0\n",
            per_system='[ultimate64]\nurl = "http://only.lan"\n',
        )
        self.assertEqual(result.cfgs[0].interstitial.duration_s, 11.0)

    def test_per_system_overrides_master_and_machine(self):
        # per-system wins over both master and machine.
        result = self._run(
            settings="[interstitial]\nduration_s = 3.0\n",
            master=self._MASTER_ONLY + "\n[interstitial]\nduration_s = 11.0\n",
            per_system='[ultimate64]\nurl = "http://only.lan"\n[interstitial]\nduration_s = 2.5\n',
        )
        self.assertEqual(result.cfgs[0].interstitial.duration_s, 2.5)


class EnsembleSectionParseTest(unittest.TestCase):
    def test_empty_systems_rejected(self):
        master = """
            [ensemble]
            systems = []
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.toml")
            _write(path, master)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load_master(path)
        self.assertIn("non-empty `systems`", str(cm.exception))

    def test_missing_name_rejected(self):
        master = """
            [ensemble]
            systems = [ { config = "x.toml" } ]
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.toml")
            _write(path, master)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load_master(path)
        self.assertIn("`name`", str(cm.exception))

    def test_missing_config_rejected(self):
        master = """
            [ensemble]
            systems = [ { name = "left" } ]
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.toml")
            _write(path, master)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load_master(path)
        self.assertIn("`config`", str(cm.exception))

    def test_duplicate_names_rejected(self):
        master = """
            [ensemble]
            systems = [
                { name = "dup", config = "a.toml" },
                { name = "dup", config = "b.toml" },
            ]
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.toml")
            _write(path, master)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load_master(path)
        self.assertIn("duplicate", str(cm.exception))


class SceneOrchestrateFlagTest(unittest.TestCase):
    """`orchestrate = true` on a scene requires a `name` because that's
    the cross-system match key followers use to find their local override."""

    def test_orchestrate_without_name_rejected(self):
        toml = """
            [[scenes]]
            type = "blank"
            orchestrate = true
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load(path)
        self.assertIn("orchestrate = true", str(cm.exception))
        self.assertIn("name", str(cm.exception))

    def test_orchestrate_with_name_accepted(self):
        toml = """
            [[scenes]]
            type = "blank"
            name = "morning-greeting"
            orchestrate = true
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            cfg = cfgmod.load(path)
        self.assertEqual(len(cfg.scenes), 1)
        self.assertTrue(cfg.scenes[0].orchestrate)
        self.assertEqual(cfg.scenes[0].name, "morning-greeting")

    def test_orchestrate_default_is_false(self):
        toml = """
            [[scenes]]
            type = "blank"
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            cfg = cfgmod.load(path)
        self.assertFalse(cfg.scenes[0].orchestrate)


class SceneFollowerOnlyFlagTest(unittest.TestCase):
    """`follower_only = true` marks a scene that lives in cfg.scenes for
    follower-override lookup but is skipped by the normal playlist
    rotation. Like `orchestrate`, it requires `name`; the two are mutually
    exclusive (one initiates broadcasts, the other receives them)."""

    def test_follower_only_without_name_rejected(self):
        toml = """
            [[scenes]]
            type = "blank"
            follower_only = true
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load(path)
        self.assertIn("follower_only = true", str(cm.exception))
        self.assertIn("name", str(cm.exception))

    def test_follower_only_with_orchestrate_rejected(self):
        toml = """
            [[scenes]]
            type = "blank"
            name = "morning-hello"
            follower_only = true
            orchestrate = true
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            with self.assertRaises(cfgmod.ConfigError) as cm:
                cfgmod.load(path)
        self.assertIn("follower_only", str(cm.exception))
        self.assertIn("orchestrate", str(cm.exception))

    def test_follower_only_with_name_accepted(self):
        toml = """
            [[scenes]]
            type = "blank"
            name = "morning-hello"
            follower_only = true
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            cfg = cfgmod.load(path)
        self.assertTrue(cfg.scenes[0].follower_only)
        self.assertEqual(cfg.scenes[0].name, "morning-hello")

    def test_follower_only_default_is_false(self):
        toml = """
            [[scenes]]
            type = "blank"
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single.toml")
            _write(path, toml)
            cfg = cfgmod.load(path)
        self.assertFalse(cfg.scenes[0].follower_only)


class ResolveRecordingPathTest(unittest.TestCase):
    """Each ensemble system needs its own output file: cv2.VideoWriter has
    no notion of sharing one, so N writers on a path leave one truncated
    stream and no error."""

    def test_ensemble_default_gets_system_name(self):
        cfg = cfgmod.RecordingCfg()
        self.assertEqual(
            cfgmod.resolve_recording_path(cfg, "left", is_ensemble=True),
            "recording-left.mp4",
        )

    def test_ensemble_systems_do_not_collide(self):
        cfg = cfgmod.RecordingCfg()
        resolved = {
            cfgmod.resolve_recording_path(cfg, n, is_ensemble=True)
            for n in ("left", "middle", "right")
        }
        self.assertEqual(len(resolved), 3)

    def test_single_system_path_is_untouched(self):
        cfg = cfgmod.RecordingCfg()
        self.assertEqual(
            cfgmod.resolve_recording_path(cfg, "system", is_ensemble=False),
            "recording.mp4",
        )

    def test_explicit_path_is_honored_verbatim(self):
        # Naming the file outranks a scheme for naming it — including the
        # extension, which the user may have chosen to match `fourcc`.
        cfg = cfgmod.RecordingCfg(path="~/vids/wall.mkv")
        self.assertEqual(
            cfgmod.resolve_recording_path(cfg, "left", is_ensemble=True),
            "~/vids/wall.mkv",
        )

    def test_a_machine_settings_path_still_counts_as_unset(self):
        # "Explicit" is measured against the machine-overlaid baseline, the
        # same reference apply_master_defaults uses. Measuring against the
        # dataclass default made a settings.toml `path` look explicit for
        # every system, skip the per-system stem and point N cv2.VideoWriters
        # at one file — through the one layer every other layering decision in
        # the module treats as unset.
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.toml")
            _write(settings, '[recording]\nenabled = true\npath = "show.mp4"\n')
            with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": settings}):
                with self.assertLogs("c64cast.app.config", level="INFO"):
                    cfg = cfgmod.load(None).recording
                resolved = {
                    cfgmod.resolve_recording_path(cfg, n, is_ensemble=True)
                    for n in ("left", "right")
                }
        self.assertEqual(resolved, {"show-left.mp4", "show-right.mp4"})

    def test_a_caller_can_pass_the_baseline_it_already_built(self):
        blank = cfgmod.RecordingCfg(path="show.mp4")
        cfg = cfgmod.RecordingCfg(path="show.mp4")
        self.assertEqual(
            cfgmod.resolve_recording_path(cfg, "left", is_ensemble=True, baseline=blank),
            "show-left.mp4",
        )


if __name__ == "__main__":
    unittest.main()
