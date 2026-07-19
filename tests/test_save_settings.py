"""Tests for the `--save-settings` CLI command (c64cast.cli.run_save_settings
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

from c64cast import config as cfgmod
from c64cast.cli import main


class SaveSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._settings = os.path.join(self._tmp.name, "settings.toml")

    def _main(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._settings}):
            with redirect_stdout(buf):
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
            with redirect_stdout(buf):
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


if __name__ == "__main__":
    unittest.main()
