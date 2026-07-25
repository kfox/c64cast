"""Tests for per-system DAC calibration: identity-key resolution (profile
override / live device identity / offline fallback), schema-v2 persistence +
per-socket entry selection, the socket-isolation config PUTs, the
system-aware "auto"/"calibrated" resolver, and the signed sidtable
reconstruction. No real hardware (the capture path is not exercised here)."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide so the test focus stays on behavior rather than type wrapping
# (same convention as test_waveform.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from _fakes import FakeAPI

from c64cast import dac_calibration as dc
from c64cast.asid_sidmap import CAT_ADDRESSING, CAT_SOCKETS
from c64cast.backend import HardwareProfile
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


def _ultimate_fake() -> FakeAPI:
    api = FakeAPI()
    api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=True)
    return api


def _result(fill: int) -> dc.CalibrationResult:
    return dc.CalibrationResult(sidtable=[fill & 0xFF] * 256, metrics={"ladder_bits": 6.5})


def _consistent_raw(lmax: float = 0.5) -> list[tuple[int, float, float]]:
    """A two-reference measurement that IS a metric: p and q are real distances
    to a single set of signed levels, so the volume-0 self-test passes exactly.
    Codes with volume nibble 0 output silence (master volume 0); the rest spread
    negative→positive."""
    levels = {c: 0.0 if (c & 0x0F) == 0 else (c - 128) / 256.0 for c in range(256)}
    levels[0x0F] = lmax
    return [(c, abs(levels[c]), abs(levels[c] - lmax)) for c in range(256)]


class ResolveKeyTest(unittest.TestCase):
    def test_ultimate_offline_key_uses_host(self):
        self.assertEqual(
            dc.resolve_calibration_key(_u64_cfg("192.168.2.64")), "ultimate-192.168.2.64"
        )

    def test_ultimate_live_key_uses_unique_id(self):
        cfg = _u64_cfg()
        api = _ultimate_fake()
        api.device_info = {"product": "C64 Ultimate", "unique_id": "5D327C"}
        self.assertEqual(dc.resolve_calibration_key(cfg, api), "ultimate-5D327C")

    def test_ultimate_live_lookup_failure_falls_back_to_host(self):
        cfg = _u64_cfg("192.168.2.64")
        api = _ultimate_fake()  # device_info left None -> get_device_info() raises
        self.assertEqual(dc.resolve_calibration_key(cfg, api), "ultimate-192.168.2.64")

    def test_tr_serial_key_offline_sanitizes_device(self):
        key = dc.resolve_calibration_key(_tr_serial_cfg("/dev/cu.usbmodem1234"))
        self.assertEqual(key, "tr-serial-_dev_cu.usbmodem1234")

    def test_tr_serial_key_uses_live_usb_serial_number(self):
        cfg = _tr_serial_cfg("/dev/cu.usbmodem1234")
        api = FakeAPI()
        with patch("c64cast.teensyrom_dma.usb_serial_number", return_value="TR12345"):
            key = dc.resolve_calibration_key(cfg, api)
        self.assertEqual(key, "tr-TR12345")

    def test_tr_serial_key_falls_back_when_no_usb_serial(self):
        cfg = _tr_serial_cfg("/dev/cu.usbmodem1234")
        api = FakeAPI()
        with patch("c64cast.teensyrom_dma.usb_serial_number", return_value=None):
            key = dc.resolve_calibration_key(cfg, api)
        self.assertEqual(key, "tr-serial-_dev_cu.usbmodem1234")

    def test_tr_tcp_key(self):
        cfg = Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "tcp"
        cfg.teensyrom.host = "teensy.lan"
        cfg.teensyrom.tcp_port = 2112
        self.assertEqual(dc.resolve_calibration_key(cfg), "tr-tcp-teensy.lan-2112")

    def test_distinct_hosts_distinct_keys(self):
        self.assertNotEqual(
            dc.resolve_calibration_key(_u64_cfg("a.lan")),
            dc.resolve_calibration_key(_u64_cfg("b.lan")),
        )

    def test_profile_override_wins_over_everything(self):
        cfg = _u64_cfg("192.168.2.64")
        cfg.audio.dac_calibration_profile = "My Breadbin!"
        api = _ultimate_fake()
        api.device_info = {"unique_id": "5D327C"}
        self.assertEqual(dc.resolve_calibration_key(cfg, api), "profile-My_Breadbin_")

    def test_profile_override_applies_to_teensyrom_too(self):
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "breadbin"
        self.assertEqual(dc.resolve_calibration_key(cfg), "profile-breadbin")


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Redirect the whole data root at the env layer (paths.calibration_dir()
        # is resolved from $C64CAST_DATA_DIR); no module global to patch.
        self._env = patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_save_load_default_entry_round_trip(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        path = dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        self.assertTrue(path.exists())
        got = dc.load_calibrated_table(cfg)
        self.assertEqual(got, bytes(256))

    def test_raw_levels_persisted_when_present(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        raw = [(c, c / 300.0, abs(c - 15) / 300.0) for c in range(256)]
        res = dc.CalibrationResult(list(range(256)), {}, "6581", raw)
        path = dc.save_calibration(cfg, key, {"default": res}, {})
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertEqual(len(entry["raw_levels"]), 256)
        self.assertEqual(entry["raw_levels"][1], [1, round(1 / 300.0, 8), round(14 / 300.0, 8)])

    def test_raw_levels_omitted_when_absent_and_file_still_loads(self):
        # raw_levels is additive under the same schema: a result carrying none
        # writes the pre-existing key set, and readers only need `sidtable`.
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        path = dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertNotIn("raw_levels", entry)
        self.assertEqual(dc.load_calibrated_table(cfg), bytes(256))

    def test_load_ignores_raw_levels(self):
        # A file written by a newer run stays loadable by the table reader.
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        raw = [(c, 0.0, 0.0) for c in range(256)]
        dc.save_calibration(
            cfg, key, {"default": dc.CalibrationResult(list(range(256)), {}, None, raw)}, {}
        )
        self.assertEqual(dc.load_calibrated_table(cfg), bytes(range(256)))

    def test_load_missing_returns_none(self):
        self.assertIsNone(dc.load_calibrated_table(_u64_cfg("nope.lan")))

    def test_load_wrong_length_returns_none(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        bad = dc.CalibrationResult(sidtable=list(range(10)), metrics={})
        dc.save_calibration(cfg, key, {"default": bad}, {})
        self.assertIsNone(dc.load_calibrated_table(cfg))

    def test_load_corrupt_file_returns_none(self):
        cfg = _u64_cfg()
        dc.calibration_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        dc.calibration_path(cfg).write_text("{ not json")
        self.assertIsNone(dc.load_calibrated_table(cfg))

    def test_load_old_schema_returns_none(self):
        # Clean cutover: an old schema=1 single-sidtable file is never read
        # under the new (also-renamed) key scheme; guard the shape too.
        cfg = _u64_cfg()
        dc.calibration_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        dc.calibration_path(cfg).write_text(
            '{"schema": 1, "key": "u64-192.168.2.64", "sidtable": ' + str(list(range(256))) + "}"
        )
        self.assertIsNone(dc.load_calibrated_table(cfg))

    def test_multi_socket_selection_uses_live_active_socket(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"1": _result(1), "2": _result(2)}, {"unique_id": "5D327C"})
        api = _ultimate_fake()
        api.config_store[CAT_ADDRESSING] = {
            "SID Socket 1 Address": "$D420",
            "SID Socket 2 Address": "$D400",
        }
        api.config_store[CAT_SOCKETS] = {
            "SID Socket 1": "Enabled",
            "SID Socket 2": "Enabled",
            "SID Detected Socket 1": "6581",
            "SID Detected Socket 2": "6581",
        }
        got = dc.load_calibrated_table(cfg, be=api)
        self.assertEqual(got, bytes([2] * 256))

    def test_multi_socket_selection_none_when_ultisid_owns_d400(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"1": _result(1), "2": _result(2)}, {})
        api = _ultimate_fake()
        api.config_store[CAT_ADDRESSING] = {
            "SID Socket 1 Address": "$D420",
            "SID Socket 2 Address": "$D440",
        }
        api.config_store[CAT_SOCKETS] = {
            "SID Socket 1": "Enabled",
            "SID Socket 2": "Enabled",
            "SID Detected Socket 1": "6581",
            "SID Detected Socket 2": "6581",
        }
        self.assertIsNone(dc.load_calibrated_table(cfg, be=api))

    def test_default_entry_used_even_with_live_api_when_no_socket_keys(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"default": _result(7)}, {})
        api = _ultimate_fake()
        got = dc.load_calibrated_table(cfg, be=api)
        self.assertEqual(got, bytes([7] * 256))


class IsolateSocketTest(unittest.TestCase):
    def test_isolate_socket_1(self):
        api = _ultimate_fake()
        dc._isolate_socket(api, 1)
        self.assertEqual(
            api.config_puts,
            [
                (CAT_ADDRESSING, "SID Socket 1 Address", "$D400"),
                (CAT_SOCKETS, "SID Socket 1", "Enabled"),
                (CAT_SOCKETS, "SID Socket 2", "Disabled"),
                (CAT_ADDRESSING, "UltiSID 1 Address", "Unmapped"),
                (CAT_ADDRESSING, "UltiSID 2 Address", "Unmapped"),
                (CAT_ADDRESSING, "Auto Address Mirroring", "Disabled"),
            ],
        )

    def test_isolate_socket_2(self):
        api = _ultimate_fake()
        dc._isolate_socket(api, 2)
        self.assertEqual(
            api.config_puts,
            [
                (CAT_ADDRESSING, "SID Socket 2 Address", "$D400"),
                (CAT_SOCKETS, "SID Socket 2", "Enabled"),
                (CAT_SOCKETS, "SID Socket 1", "Disabled"),
                (CAT_ADDRESSING, "UltiSID 1 Address", "Unmapped"),
                (CAT_ADDRESSING, "UltiSID 2 Address", "Unmapped"),
                (CAT_ADDRESSING, "Auto Address Mirroring", "Disabled"),
            ],
        )


class ResolveCurveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
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
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(256))

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
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(256))

    def test_explicit_linear_and_mahoney_pass_through(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "linear"
        self.assertEqual(dc.resolve_dac_curve_for_backend(cfg), ("linear", None))
        cfg.audio.dac_curve = "mahoney_ultisid"
        label, table = dc.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)


class MissingCalibrationLogTest(unittest.TestCase):
    """A live "auto" resolution that finds no calibration logs an actionable
    line (this replaced the old --doctor repo-location migration nudge). It
    stays silent for an offline resolution (be=None) — --doctor reports that
    case separately and can't even confirm the identity key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_ultimate_live_no_cal_logs_info(self):
        cfg = _u64_cfg()
        with self.assertLogs("c64cast.dac_calibration", level="INFO") as cm:
            label, table = dc.resolve_dac_curve_for_backend(cfg, be=_ultimate_fake())
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)
        joined = "\n".join(cm.output)
        self.assertIn("no per-unit DAC calibration", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_teensyrom_live_no_cal_logs_warning(self):
        cfg = _tr_serial_cfg()
        with patch("c64cast.teensyrom_dma.usb_serial_number", return_value=None):
            with self.assertLogs("c64cast.dac_calibration", level="WARNING") as cm:
                label, table = dc.resolve_dac_curve_for_backend(cfg, be=FakeAPI())
        self.assertEqual(label, "linear")
        self.assertIsNone(table)
        joined = "\n".join(cm.output)
        self.assertIn("no DAC calibration found", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_offline_no_cal_is_silent(self):
        # be=None → no log (assertNoLogs raises if anything is emitted).
        with self.assertNoLogs("c64cast.dac_calibration", level="INFO"):
            dc.resolve_dac_curve_for_backend(_u64_cfg())
        with self.assertNoLogs("c64cast.dac_calibration", level="INFO"):
            dc.resolve_dac_curve_for_backend(_tr_serial_cfg())

    def test_live_calibration_present_is_silent(self):
        # A hit doesn't warn.
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        with self.assertNoLogs("c64cast.dac_calibration", level="INFO"):
            label, _ = dc.resolve_dac_curve_for_backend(cfg, be=_ultimate_fake())
        self.assertTrue(label.startswith("calibrated:"))


class BuildSidtableTest(unittest.TestCase):
    def test_reconstruct_from_synthetic_signed_curve(self):
        sidtable, metrics = dc.build_sidtable_from_signed(_consistent_raw())
        assert sidtable is not None
        self.assertEqual(len(sidtable), 256)
        self.assertTrue(all(0 <= v <= 255 for v in sidtable))
        self.assertIn("signed_span", metrics)
        lo, hi = metrics["signed_span"]
        self.assertLess(lo, hi)
        self.assertGreater(metrics["ladder_bits"], 4.0)


class Volume0SelfTestTest(unittest.TestCase):
    """Codes $h0 set the master volume to 0, so their level equals $00's and
    q($h0) must equal lmax exactly. That holds with no model assumptions, which
    makes it the one check that can tell a sound measurement from a measurement
    whose p/q are not distances at all — the failure the two-reference scheme
    is otherwise silent about."""

    def test_consistent_measurement_passes_and_yields_a_table(self):
        sidtable, metrics = dc.build_sidtable_from_signed(_consistent_raw())
        self.assertIsNotNone(sidtable)
        self.assertAlmostEqual(metrics["volume0_selftest_worst"], 0.0, places=6)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_inconsistent_measurement_is_rejected_with_no_table(self):
        # Reproduces the measured failure: q comes back short for codes whose
        # upper nibble is non-zero, by an amount that depends on the nibble. The
        # volume-0 codes then report a distance to $0F that is not lmax, which
        # is impossible for any real set of levels.
        raw = [(c, p, q * (1.0 - 0.03 * (c >> 4))) for c, p, q in _consistent_raw()]
        sidtable, metrics = dc.build_sidtable_from_signed(raw)
        self.assertIsNone(sidtable)
        self.assertGreater(metrics["volume0_selftest_worst"], dc.SELFTEST_TOLERANCE)
        # Still fully diagnosable: the metrics survive the rejection.
        self.assertIn("signed_span", metrics)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_rejection_writes_no_sidtable_and_reads_back_as_no_calibration(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        raw = [(c, p, q * 0.5) for c, p, q in _consistent_raw()]
        sidtable, metrics = dc.build_sidtable_from_signed(raw)
        self.assertIsNone(sidtable)
        path = dc.save_calibration(
            cfg, key, {"default": dc.CalibrationResult(sidtable, metrics, None, raw)}, {}
        )
        doc = json.loads(path.read_text())
        entry = doc["sids"]["default"]
        self.assertNotIn("sidtable", entry)
        # The raw levels are kept so the failure can be investigated without
        # repeating the ~6 min/socket measurement.
        self.assertEqual(len(entry["raw_levels"]), 256)
        self.assertIsNone(dc.load_calibrated_table(cfg))


class LadderMetricsTest(unittest.TestCase):
    def test_quality_is_independent_of_capture_noise_floor(self):
        """The metrics this replaced counted level steps exceeding the capture
        noise floor, so a quieter rig scored more "effective bits" on identical
        hardware — it once rated a chip degraded to ~4 bits above a working one.
        Scaling the whole capture must not change the ladder's quality figures."""
        raw = _consistent_raw()
        loud = [(c, p * 10.0, q * 10.0) for c, p, q in raw]
        _, m1 = dc.build_sidtable_from_signed(raw)
        _, m2 = dc.build_sidtable_from_signed(loud)
        for k in ("ladder_bits", "worst_gap_frac", "ladder_rms_err_frac"):
            self.assertAlmostEqual(m1[k], m2[k], places=3, msg=k)
        # …while the noise floor itself, being a property of the rig, does move.
        self.assertAlmostEqual(m2["capture_noise_floor"], m1["capture_noise_floor"] * 10, places=5)

    def test_worst_gap_position_is_reported_relative_to_silence(self):
        sidtable, metrics = dc.build_sidtable_from_signed(_consistent_raw())
        self.assertIsNotNone(sidtable)
        # 0 = the gap straddles silence (crossover distortion), ±0.5 = it sits
        # out at an extreme, where the same gap is benign.
        self.assertGreaterEqual(metrics["worst_gap_from_zero_frac"], -0.5)
        self.assertLessEqual(metrics["worst_gap_from_zero_frac"], 0.5)
        self.assertGreaterEqual(metrics["crossover_gap_frac"], 0.0)


if __name__ == "__main__":
    unittest.main()
