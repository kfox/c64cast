"""Tests for per-system DAC calibration: system key derivation, table
persistence round-trip, the system-aware "auto"/"calibrated" resolver, and the
signed sidtable reconstruction. No real hardware (the capture path is not
exercised here)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c64cast import dac_calibration as dc
from c64cast.config import Config
from c64cast.dac_curves import MAHONEY_ULTISID


def _u64_cfg(host: str = "192.168.2.64") -> Config:
    cfg = Config()
    cfg.hardware.backend = "ultimate"
    cfg.ultimate64.url = f"http://{host}"
    cfg.audio.enabled = True
    cfg.audio.dac_curve = "auto"
    cfg.audio.digi_boost = False
    return cfg


def _tr_serial_cfg(dev: str | None = "/dev/cu.usbmodem1234") -> Config:
    cfg = Config()
    cfg.hardware.backend = "teensyrom"
    cfg.teensyrom.transport = "serial"
    cfg.teensyrom.serial_port = dev
    cfg.audio.enabled = True
    cfg.audio.dac_curve = "auto"
    return cfg


class SystemKeyTest(unittest.TestCase):
    def test_ultimate_key_uses_host(self):
        self.assertEqual(dc.system_calibration_key(_u64_cfg("192.168.2.64")), "u64-192.168.2.64")

    def test_tr_serial_key_sanitizes_device(self):
        key = dc.system_calibration_key(_tr_serial_cfg("/dev/cu.usbmodem1234"))
        self.assertEqual(key, "tr-serial-_dev_cu.usbmodem1234")

    def test_tr_tcp_key(self):
        cfg = Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "tcp"
        cfg.teensyrom.host = "teensy.lan"
        cfg.teensyrom.tcp_port = 2112
        self.assertEqual(dc.system_calibration_key(cfg), "tr-tcp-teensy.lan-2112")

    def test_distinct_hosts_distinct_keys(self):
        self.assertNotEqual(
            dc.system_calibration_key(_u64_cfg("a.lan")),
            dc.system_calibration_key(_u64_cfg("b.lan")),
        )


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dc.CALIBRATION_DIR
        dc.CALIBRATION_DIR = Path(self._tmp.name) / "dac"

    def tearDown(self):
        dc.CALIBRATION_DIR = self._orig
        self._tmp.cleanup()

    def test_save_load_round_trip(self):
        cfg = _u64_cfg()
        table = list(range(256))
        path = dc.save_calibrated_table(cfg, table, {"effective_bits": 6.5})
        self.assertTrue(path.exists())
        got = dc.load_calibrated_table(cfg)
        self.assertEqual(got, bytes(range(256)))

    def test_load_missing_returns_none(self):
        self.assertIsNone(dc.load_calibrated_table(_u64_cfg("nope.lan")))

    def test_load_wrong_length_returns_none(self):
        cfg = _u64_cfg()
        dc.save_calibrated_table(cfg, list(range(10)), {})
        self.assertIsNone(dc.load_calibrated_table(cfg))

    def test_load_corrupt_file_returns_none(self):
        cfg = _u64_cfg()
        dc.CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        dc.calibration_path(cfg).write_text("{ not json")
        self.assertIsNone(dc.load_calibrated_table(cfg))


class ResolveCurveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dc.CALIBRATION_DIR
        dc.CALIBRATION_DIR = Path(self._tmp.name) / "dac"

    def tearDown(self):
        dc.CALIBRATION_DIR = self._orig
        self._tmp.cleanup()

    def test_auto_ultimate_no_cal_uses_baked_mahoney(self):
        label, table = dc.resolve_dac_curve_for_backend(_u64_cfg())
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)

    def test_auto_teensyrom_no_cal_uses_linear(self):
        label, table = dc.resolve_dac_curve_for_backend(_tr_serial_cfg())
        self.assertEqual(label, "linear")
        self.assertIsNone(table)

    def test_auto_prefers_calibration_when_present(self):
        cfg = _u64_cfg()
        dc.save_calibrated_table(cfg, list(range(256)), {})
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(range(256)))

    def test_auto_yields_to_digi_boost(self):
        cfg = _u64_cfg()
        cfg.audio.digi_boost = True
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(label, "linear")
        self.assertIsNone(table)

    def test_calibrated_missing_raises(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "calibrated"
        with self.assertRaises(ValueError):
            dc.resolve_dac_curve_for_backend(cfg)

    def test_calibrated_present_returns_table(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "calibrated"
        dc.save_calibrated_table(cfg, list(range(256)), {})
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(range(256)))

    def test_explicit_linear_and_mahoney_pass_through(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "linear"
        self.assertEqual(dc.resolve_dac_curve_for_backend(cfg), ("linear", None))
        cfg.audio.dac_curve = "mahoney_ultisid"
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)


class BuildSidtableTest(unittest.TestCase):
    def test_reconstruct_from_synthetic_signed_curve(self):
        # A synthetic bipolar transfer curve: L(c) known, p=|L|, q=|L-Lmax|.
        # Codes with volume nibble 0 output ~silence (master volume 0) — that's
        # the measured noise floor; the rest spread negative→positive.
        lmax = 0.5
        levels = {c: 0.0 if (c & 0x0F) == 0 else (c - 128) / 256.0 for c in range(256)}
        levels[0x0F] = lmax
        signed_raw = [(c, abs(levels[c]), abs(levels[c] - lmax)) for c in range(256)]
        sidtable, metrics = dc.build_sidtable_from_signed(signed_raw)
        self.assertEqual(len(sidtable), 256)
        self.assertTrue(all(0 <= v <= 255 for v in sidtable))
        self.assertGreater(metrics["distinct_levels"], 16)
        self.assertIn("signed_span", metrics)
        lo, hi = metrics["signed_span"]
        self.assertLess(lo, hi)


if __name__ == "__main__":
    unittest.main()
