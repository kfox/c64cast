"""flicker_tear_ab writes configs c64cast accepts, with blending where it means it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _fakes import MachineSettingsIsolation
from test_diags_nmi_latch import _load

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory

_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


class PhaseConfigTest(unittest.TestCase):
    def _resolved_tolerance(self, *, flicker: bool) -> str:
        tool = _load("flicker_tear_ab")
        out = Path(tempfile.mkdtemp())
        cfg_path = out / "phase.toml"
        tool.write_config(cfg_path, out / "ab.mkv", flicker=flicker)
        cfg = cfgmod.load(str(cfg_path))
        for validate in scene_factory.PER_SYSTEM_VALIDATORS:
            validate(cfg)
        (scene,) = cfg.scenes
        assert scene.display is not None
        return scene_factory.resolve_flicker_tolerance(
            cfgmod.scene_color(cfg, scene).flicker_tolerance, scene.display
        )

    def test_the_flicker_phase_arms_blending(self) -> None:
        self.assertNotEqual(self._resolved_tolerance(flicker=True), "off")

    def test_the_plain_phase_leaves_it_off(self) -> None:
        self.assertEqual(self._resolved_tolerance(flicker=False), "off")


if __name__ == "__main__":
    unittest.main()
