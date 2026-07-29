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
import tempfile
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


class LegacyPresetsDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _make_orphans(self) -> Path:
        legacy = Path(self._tmp.name) / "repo"
        (legacy / "presets").mkdir(parents=True)
        (legacy / "presets" / "wled-x.json").write_text("{}")
        return legacy

    def test_returns_legacy_dir_when_orphaned_and_canonical_absent(self):
        legacy = self._make_orphans()
        data = str(Path(self._tmp.name) / "data")  # canonical does not exist
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": data}):
            with mock.patch("c64cast.paths.legacy_data_root", return_value=legacy):
                self.assertEqual(paths.legacy_presets_dir(), legacy / "presets")

    def test_none_when_canonical_exists(self):
        legacy = self._make_orphans()
        data = Path(self._tmp.name) / "data"
        (data / "presets").mkdir(parents=True)  # already migrated
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": str(data)}):
            with mock.patch("c64cast.paths.legacy_data_root", return_value=legacy):
                self.assertIsNone(paths.legacy_presets_dir())

    def test_none_when_no_json_files(self):
        legacy = Path(self._tmp.name) / "repo"
        (legacy / "presets").mkdir(parents=True)  # dir exists but empty
        data = str(Path(self._tmp.name) / "data")
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": data}):
            with mock.patch("c64cast.paths.legacy_data_root", return_value=legacy):
                self.assertIsNone(paths.legacy_presets_dir())

    def test_none_for_installed_package(self):
        with mock.patch("c64cast.paths.legacy_data_root", return_value=None):
            self.assertIsNone(paths.legacy_presets_dir())


class PackagedResourcesTest(unittest.TestCase):
    """The shipped example configs + JSON schema, and the `example:` resolver."""

    def test_examples_and_schema_are_real_files_under_the_package(self):
        pkg = Path(paths.__file__).resolve().parent
        self.assertEqual(paths.examples_dir().resolve(), pkg / "examples")
        self.assertEqual(
            paths.packaged_schema_path().resolve(), pkg / "data" / "c64cast.schema.json"
        )
        self.assertTrue(paths.packaged_schema_path().is_file())

    def test_example_config_paths_finds_the_demos_and_the_subdirectory_ones(self):
        names = [paths.example_name(p) for p in paths.example_config_paths()]
        self.assertIn("hello", names)
        self.assertIn("c64cast.example", names)
        self.assertIn("ensemble/master", names)
        # Single-file demos are listed before the sub-directory ones.
        self.assertLess(names.index("hello"), names.index("ensemble/master"))
        self.assertEqual(len(names), len(set(names)), "duplicate example names")

    def test_every_packaged_example_carries_the_schema_directive(self):
        # A relative `#:schema` that doesn't resolve from the file's own
        # directory silently kills editor autocomplete for that demo.
        for path in paths.example_config_paths():
            with self.subTest(example=paths.example_name(path)):
                first = path.read_text(encoding="utf-8").splitlines()[0]
                self.assertTrue(first.startswith("#:schema "), first)
                target = (path.parent / first.removeprefix("#:schema ").strip()).resolve()
                self.assertEqual(target, paths.packaged_schema_path().resolve())

    def test_resolve_example_accepts_a_bare_name_and_a_toml_suffix(self):
        expected = paths.examples_dir() / "hello.toml"
        self.assertEqual(paths.resolve_example("hello"), expected)
        self.assertEqual(paths.resolve_example("hello.toml"), expected)

    def test_resolve_example_reaches_a_subdirectory_demo(self):
        self.assertEqual(
            paths.resolve_example("ensemble/master"),
            paths.examples_dir() / "ensemble" / "master.toml",
        )

    def test_unknown_name_raises_with_a_close_match(self):
        with self.assertRaises(ValueError) as ctx:
            paths.resolve_example("helo")
        self.assertIn("hello", str(ctx.exception))

    def test_traversal_out_of_the_examples_dir_is_refused(self):
        with self.assertRaises(ValueError):
            paths.resolve_example("../../pyproject")

    def test_resolve_config_spec_passes_through_everything_else(self):
        self.assertIsNone(paths.resolve_config_spec(None))
        self.assertEqual(paths.resolve_config_spec("my.toml"), "my.toml")
        self.assertEqual(paths.resolve_config_spec("/abs/example.toml"), "/abs/example.toml")

    def test_resolve_config_spec_expands_the_example_prefix(self):
        self.assertEqual(
            paths.resolve_config_spec("example:hello"), str(paths.examples_dir() / "hello.toml")
        )


if __name__ == "__main__":
    unittest.main()
