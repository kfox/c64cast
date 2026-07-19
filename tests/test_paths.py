"""Tests for c64cast.paths — the canonical settings + data-dir resolver.

Covers the env overrides ($C64CAST_SETTINGS / $C64CAST_DATA_DIR), the XDG /
POSIX defaults, the derived subdirectories, and legacy-repo detection. Pure
stdlib — no hardware, no package state.

Note on the Windows branch: since Python 3.12, ``pathlib.Path(...)`` picks
``WindowsPath`` vs ``PosixPath`` from ``os.name`` *at call time* and refuses to
instantiate the foreign one, so patching ``os.name="nt"`` on a POSIX host makes
even ``Path("/x")`` raise. The Windows-default assertions therefore run only on
a real Windows host (``skipUnless``); the POSIX-default + env-override paths
carry the coverage everywhere else.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from c64cast import paths

_ON_WINDOWS = os.name == "nt"
_ON_POSIX = os.name == "posix"


def _clean_env(*drop: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in drop}


class SettingsPathTest(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": "/custom/s.toml"}):
            self.assertEqual(paths.settings_path(), Path("/custom/s.toml"))

    @unittest.skipUnless(_ON_POSIX, "POSIX-only default path")
    def test_empty_env_override_falls_through(self):
        # An empty value is treated as unset (XDG semantics).
        env = _clean_env("C64CAST_SETTINGS")
        env["C64CAST_SETTINGS"] = ""
        env["XDG_CONFIG_HOME"] = "/xdg/cfg"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.settings_path(), Path("/xdg/cfg/c64cast/settings.toml"))

    @unittest.skipUnless(_ON_POSIX, "POSIX-only default path")
    def test_xdg_config_home_default(self):
        env = _clean_env("C64CAST_SETTINGS")
        env["XDG_CONFIG_HOME"] = "/xdg/cfg"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.settings_path(), Path("/xdg/cfg/c64cast/settings.toml"))

    @unittest.skipUnless(_ON_POSIX, "POSIX-only default path")
    def test_posix_home_fallback(self):
        env = _clean_env("C64CAST_SETTINGS", "XDG_CONFIG_HOME")
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(Path, "home", return_value=Path("/home/u")):
                self.assertEqual(
                    paths.settings_path(), Path("/home/u/.config/c64cast/settings.toml")
                )

    @unittest.skipUnless(_ON_WINDOWS, "Windows path construction only works on Windows")
    def test_windows_appdata(self):  # pragma: no cover - Windows only
        env = _clean_env("C64CAST_SETTINGS")
        env["APPDATA"] = r"C:\Users\u\AppData\Roaming"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                paths.settings_path(),
                Path(r"C:\Users\u\AppData\Roaming") / "c64cast" / "settings.toml",
            )


class DataRootTest(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": "/custom/data"}):
            self.assertEqual(paths.data_root(), Path("/custom/data"))

    @unittest.skipUnless(_ON_POSIX, "POSIX-only default path")
    def test_xdg_data_home_default(self):
        env = _clean_env("C64CAST_DATA_DIR")
        env["XDG_DATA_HOME"] = "/xdg/data"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.data_root(), Path("/xdg/data/c64cast"))

    @unittest.skipUnless(_ON_POSIX, "POSIX-only default path")
    def test_posix_home_fallback(self):
        env = _clean_env("C64CAST_DATA_DIR", "XDG_DATA_HOME")
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(Path, "home", return_value=Path("/home/u")):
                self.assertEqual(paths.data_root(), Path("/home/u/.local/share/c64cast"))

    @unittest.skipUnless(_ON_WINDOWS, "Windows path construction only works on Windows")
    def test_windows_localappdata(self):  # pragma: no cover - Windows only
        env = _clean_env("C64CAST_DATA_DIR")
        env["LOCALAPPDATA"] = r"C:\Users\u\AppData\Local"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.data_root(), Path(r"C:\Users\u\AppData\Local") / "c64cast")

    def test_derived_subdirs_are_under_data_root(self):
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": "/d"}):
            self.assertEqual(paths.calibration_dir(), Path("/d/calibration/dac"))
            self.assertEqual(paths.presets_dir(), Path("/d/presets"))
            self.assertEqual(paths.loop_presets_dir(), Path("/d/presets/loops"))


class LegacyDataRootTest(unittest.TestCase):
    def test_returns_repo_root_when_pyproject_present(self):
        # This test runs from a source checkout, so pyproject.toml is present.
        legacy = paths.legacy_data_root()
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertTrue((legacy / "pyproject.toml").is_file())

    def test_returns_none_without_pyproject(self):
        # Simulate an installed package: the package parent has no pyproject.
        fake_pkg_file = Path("/opt/site-packages/c64cast/paths.py")
        with mock.patch.object(paths, "__file__", str(fake_pkg_file)):
            self.assertIsNone(paths.legacy_data_root())


if __name__ == "__main__":
    unittest.main()
