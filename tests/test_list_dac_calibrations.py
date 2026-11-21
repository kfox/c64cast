"""Tests for `--list-dac-calibrations`: every stored calibration is listed by a
name that `--dac-calibration-profile` resolves back to the same file."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from _fakes import quiet_logging

from c64cast.app import paths
from c64cast.app.cli import main
from c64cast.app.config import Config
from c64cast.audio import dac_calibration_store as dcs


def _result(detected: str | None, *, playable: bool = True) -> dcs.CalibrationResult:
    return dcs.CalibrationResult(
        sidtable=[0x1F] * 256 if playable else None, metrics={}, detected=detected
    )


def _list() -> tuple[int, str]:
    buf = io.StringIO()
    with quiet_logging(), redirect_stdout(buf):
        rc = main(["--list-dac-calibrations"])
    return rc, buf.getvalue()


class ListDacCalibrationsTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"C64CAST_DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)

    def _save(self, backend: str, key: str, entries: dict[str, dcs.CalibrationResult]) -> Path:
        cfg = Config()
        cfg.hardware.backend = backend
        return dcs.save_calibration(cfg, dcs.CalibrationDocument(key, entries, {}))

    def _write_raw(self, stem: str, record: object) -> Path:
        path = paths.calibration_dir() / f"{stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return path

    def test_nothing_stored_says_so_and_exits_zero(self):
        rc, out = _list()
        self.assertEqual(rc, 0)
        self.assertIn("No DAC calibrations stored", out)
        self.assertIn(str(paths.calibration_dir()), out)

    def test_profile_is_listed_by_its_bare_name_with_backend_and_chip(self):
        self._save("teensyrom", "profile-breadbin", {"default": _result("6581")})
        created = json.loads((paths.calibration_dir() / "profile-breadbin.json").read_text())[
            "created"
        ]
        _, out = _list()
        (row,) = [line for line in out.splitlines() if "breadbin" in line]
        self.assertEqual(row.split()[:3], ["breadbin", "teensyrom", created])
        self.assertIn("default: 6581", row)

    def test_device_keyed_file_lists_each_socket_and_flags_a_rejected_one(self):
        self._save(
            "ultimate",
            "ultimate-5D327C",
            {"1": _result("6581"), "2": _result("8580", playable=False)},
        )
        _, out = _list()
        (row,) = [line for line in out.splitlines() if "ultimate-5D327C" in line]
        self.assertIn("socket 1: 6581, socket 2: 8580 (no usable table)", row)

    def test_entry_with_no_detected_chip_names_only_its_socket(self):
        self._save("ultimate", "ultimate-X", {"1": _result(None)})
        _, out = _list()
        (row,) = [line for line in out.splitlines() if "ultimate-X" in line]
        self.assertTrue(row.rstrip().endswith("socket 1"), row)

    def test_file_from_an_older_schema_is_listed_as_unplayable(self):
        self._write_raw("old-rig", {"schema": 1, "sidtable": [0] * 256})
        _, out = _list()
        (row,) = [line for line in out.splitlines() if "old-rig" in line]
        self.assertIn("not a calibration this version plays", row)

    def test_hand_edited_file_with_malformed_fields_still_lists(self):
        self._write_raw(
            "edited",
            {"schema": 2, "backend": 7, "sids": {"1": "junk", "2": {"detected": "8580"}}},
        )
        rc, out = _list()
        self.assertEqual(rc, 0)
        (row,) = [line for line in out.splitlines() if "edited" in line]
        self.assertEqual(row.split()[1], "?")
        self.assertTrue(row.rstrip().endswith("socket 2: 8580 (no usable table)"), row)


class ListedNameResolvesBackTest(unittest.TestCase):
    """The listed name, passed to `--dac-calibration-profile`, selects the file
    it was listed for — including where a bare name would select another."""

    def setUp(self):
        env = patch.dict(os.environ, {"C64CAST_DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)

    def _stored(self, *stems: str) -> dict[str, dcs.StoredCalibration]:
        cal_dir = paths.calibration_dir()
        cal_dir.mkdir(parents=True, exist_ok=True)
        for stem in stems:
            (cal_dir / f"{stem}.json").write_text("{}")
        return {c.path.stem: c for c in dcs.stored_calibrations()}

    def _resolves_to(self, name: str) -> Path:
        cfg = Config()
        cfg.audio.dac_calibration_profile = name
        return dcs.calibration_path(cfg)

    def test_every_listed_name_selects_its_own_file(self):
        stored = self._stored(
            "profile-breadbin",
            "tr-ABC123",
            "ultimate-5D327C",
            "shared",
            "profile-shared",
            "my rig",
            "rig.json",
            "profile-den.json",
        )
        for stem, c in stored.items():
            with self.subTest(stem=stem):
                self.assertEqual(self._resolves_to(c.name), c.path)

    def test_names_are_the_short_spelling_where_one_resolves(self):
        stored = self._stored("profile-breadbin", "tr-ABC123")
        self.assertEqual(stored["profile-breadbin"].name, "breadbin")
        self.assertEqual(stored["tr-ABC123"].name, "tr-ABC123")

    def test_a_file_a_profile_shadows_is_named_by_its_path(self):
        stored = self._stored("shared", "profile-shared")
        self.assertEqual(stored["profile-shared"].name, "shared")
        self.assertEqual(stored["shared"].name, str(stored["shared"].path))


if __name__ == "__main__":
    unittest.main()
