"""Tests for the `--save-settings` CLI command (c64cast.app.cli.run_save_settings
via cli.main).

Drives the real argparse entry point with $C64CAST_SETTINGS pointed at a tmp
file so nothing touches the real ~/.config location. Covers: a sparse write
from -u/-d, merging onto an existing file, the round-trip back through
config.load, the nothing-to-save exit code, and the invariant that the DMA
password is never serialized.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from _fakes import quiet_logging

from c64cast.app import config as cfgmod
from c64cast.app.cli import build_parser, main
from c64cast.app.cli_commands import run_save_settings


class SaveSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._settings = os.path.join(self._tmp.name, "settings.toml")

    def _main(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            with quiet_logging(), redirect_stdout(buf):
                rc = main(argv)
        return rc, buf.getvalue()

    def test_saves_url_and_device_sparse(self):
        rc, out = self._main(["-u", "u64://box.lan", "-d", "2", "--save-settings"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(self._settings))
        with open(self._settings) as f:
            text = f.read()
        self.assertIn("[ultimate64]", text)
        self.assertIn("box.lan", text)
        self.assertIn("[video]", text)
        self.assertIn("device = 2", text)
        # Sparse: sections the flags didn't touch are absent.
        self.assertNotIn("[playlist]", text)
        self.assertNotIn("[interstitial]", text)
        # The path + contents are echoed to stdout.
        self.assertIn(self._settings, out)

    def test_round_trips_through_load(self):
        rc, _ = self._main(["-u", "u64://box.lan", "--sid-model", "8580", "--save-settings"])
        self.assertEqual(rc, 0)
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            cfg = cfgmod.load(None)
        self.assertEqual(cfg.ultimate64.url, "http://box.lan")
        self.assertEqual(cfg.ultimate64.sid_model, "8580")

    def test_merges_onto_existing(self):
        rc1, _ = self._main(["-u", "u64://box.lan", "--save-settings"])
        self.assertEqual(rc1, 0)
        rc2, _ = self._main(["-d", "3", "--save-settings"])
        self.assertEqual(rc2, 0)
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            cfg = cfgmod.load(None)
        # Both the first write (url) and the merge (device) survive.
        self.assertEqual(cfg.ultimate64.url, "http://box.lan")
        self.assertEqual(cfg.video.device, 3)

    def test_audio_device_name_saved(self):
        rc, _ = self._main(["-D", "Cam Link", "--save-settings"])
        self.assertEqual(rc, 0)
        with open(self._settings) as f:
            text = f.read()
        self.assertIn("[audio]", text)
        self.assertIn('device = "Cam Link"', text)
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            cfg = cfgmod.load(None)
        self.assertEqual(cfg.audio.device, "Cam Link")

    def test_nothing_to_save_exits_2(self):
        rc, _ = self._main(["--save-settings"])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self._settings))

    def test_dma_password_never_written(self):
        # Even with the env password set, it must not land in the file.
        buf = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"C64CAST_SETTINGS": self._settings, "C64CAST_DMA_PASSWORD": "topsecret"},
        ):
            with quiet_logging(), redirect_stdout(buf):
                rc = main(["-u", "u64://box.lan", "--save-settings"])
        self.assertEqual(rc, 0)
        with open(self._settings) as f:
            text = f.read()
        self.assertNotIn("topsecret", text)
        self.assertNotIn("dma_password", text)

    def test_system_saved(self):
        rc, _ = self._main(["-s", "PAL", "--save-settings"])
        self.assertEqual(rc, 0)
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            cfg = cfgmod.load(None)
        self.assertEqual(cfg.ultimate64.system, "PAL")

    def test_bad_url_exits_2_not_a_traceback(self):
        # ConnectionURIError (a ValueError) used to escape main() here as an
        # uncaught traceback instead of the exit-2 usage error connect.py's
        # docstring promises — --save-settings is dispatched before
        # _resolve_configs' try/except, and had no guard of its own.
        rc, _ = self._main(["-u", "not-a-connection-target", "--save-settings"])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self._settings))

    def test_url_with_userinfo_rejected_not_persisted(self):
        # parse_connection_uri now refuses embedded credentials outright, so
        # none of this ever reaches settings.toml or stdout.
        rc, out = self._main(["-u", "u64://admin:s3cret@192.168.2.64", "--save-settings"])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self._settings))
        self.assertNotIn("s3cret", out)


class SaveSettingsDmaPasswordWarningTest(unittest.TestCase):
    """Drives run_save_settings directly (not cli.main()) so assertLogs can
    capture its warning without cli.main()'s configure_logging() installing a
    real terminal handler alongside it (the two must not nest — see
    CLAUDE.md's test conventions)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._settings = os.path.join(self._tmp.name, "settings.toml")
        with open(self._settings, "w", encoding="utf-8") as f:
            f.write('[ultimate64]\ndma_password = "topsecret"\n')

    def test_existing_dma_password_triggers_warning_not_silent_drop(self):
        # A hand-written dma_password in settings.toml can't survive a save
        # (config_serialize suppresses it on write) — the loss must be
        # visible, not silent.
        args = build_parser().parse_args(["-d", "2", "--save-settings"])
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            with self.assertLogs("c64cast", level="WARNING") as cm:
                with redirect_stdout(buf):
                    rc = run_save_settings(args)
        self.assertEqual(rc, 0)
        self.assertTrue(any("dma_password" in r for r in cm.output))
        with open(self._settings) as f:
            text = f.read()
        self.assertNotIn("topsecret", text)


if __name__ == "__main__":
    unittest.main()
