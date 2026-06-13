"""Tests for ensemble (multi-system) config loading.

Covers `load_master()` routing logic and `EnsembleCfg` / `SystemEntryCfg`
dataclass shape. The override cascade implemented by
`apply_master_defaults` is exercised in a later commit; this file just
asserts the stub passes per-system configs through unchanged."""
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest

from c64cast import config as cfgmod


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
            with self.assertLogs("c64cast.config", level="INFO"):
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
            with self.assertLogs("c64cast.config", level="INFO"):
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
            with self.assertLogs("c64cast.config", level="WARNING") as cm:
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
            with self.assertLogs("c64cast.config", level="INFO"):
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
        self.assertEqual(sys_cfg.ultimate64.url,
                         cfgmod.Ultimate64Cfg().url)

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

    def test_control_section_does_not_cascade(self):
        # [control] is wired from the master directly (one control plane
        # for the whole ensemble); per-system [control] would be confusing.
        defaults = cfgmod.Config()
        defaults.control.enabled = True
        defaults.control.port = 9999
        sys_cfg = cfgmod.Config()
        cfgmod.apply_master_defaults(defaults, sys_cfg)
        self.assertEqual(sys_cfg.control.enabled,
                         cfgmod.ControlPlaneCfg().enabled)
        self.assertEqual(sys_cfg.control.port,
                         cfgmod.ControlPlaneCfg().port)

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
            with self.assertLogs("c64cast.config", level="INFO"):
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
            with self.assertLogs("c64cast.config", level="INFO"):
                result = cfgmod.load_master(master_path)
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


if __name__ == "__main__":
    unittest.main()
