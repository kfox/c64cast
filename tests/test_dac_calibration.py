"""Tests for per-system DAC calibration: identity-key resolution (profile
override / live device identity / offline fallback), schema-v2 persistence +
per-socket entry selection, the socket-isolation config PUTs, the
system-aware "auto"/"calibrated" resolver, and the slot-ring measurement
primitive — ring construction, level extraction from a simulated capture, and
the ladder fold. No real hardware; the capture is synthesised."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide so the test focus stays on behavior rather than type wrapping
# (same convention as test_waveform.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import itertools
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
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


RING = 0x2000  # audio.RING_BUFFER_SIZE, without importing the audio stack
NMI_TRUE = 1022727 / 128  # what the CIA latch actually gives, vs the 8000 asked for


def _simulate(
    codes: list[int],
    *,
    secs: float = 4.5,
    fc: float = 12.0,
    noise: float = 1e-3,
    drop_frac: float = 0.0,
    phase: float = 0.31,
    seed: int = 7,
    sr: int = dc.CAP_SR,
) -> tuple[np.ndarray, np.ndarray]:
    """A synthetic Cam Link capture of the slot ring `codes` would produce, and
    the true levels it encodes.

    Models everything the extraction has to survive: an NMI clock that is not a
    rational multiple of the capture rate, the AC-coupled capture path, noise, a
    capture that starts at an arbitrary ring phase, and (optionally)
    avfoundation dropping samples so the timebase is compressed. `sr` is the
    capture rate — not every capture device does 48 kHz."""
    rng = np.random.default_rng(seed)
    true = np.zeros(256)
    mode = rng.uniform(-1.0, 1.0, 16)
    mode[0] = 1.0
    for c in range(256):
        true[c] = (c & 0x0F) / 15.0 * mode[c >> 4] * 0.45  # vol 0 == silence

    ring = np.frombuffer(dc.build_slot_ring(codes, RING), dtype=np.uint8)
    n = int(secs * sr)
    idx = np.floor(np.arange(n) * (NMI_TRUE / sr) + phase * RING).astype(np.int64)
    v = true[ring[idx % RING]]
    if drop_frac:
        v = v[rng.random(v.size) > drop_frac]
    a = np.exp(-2 * np.pi * fc / sr)  # one-pole high-pass = AC coupling
    y = np.empty_like(v)
    acc, prev = 0.0, v[0]
    for i in range(v.size):
        acc = a * (acc + v[i] - prev)
        prev = v[i]
        y[i] = acc
    return y + rng.normal(0, noise, y.size), true[codes] - true[dc.REF_ZERO]


def _signed_levels(lmax: float = 0.5) -> list[tuple[int, float]]:
    """A consistent set of measured signed levels: codes with volume nibble 0
    output silence (master volume 0), the rest spread negative→positive."""
    levels = {c: 0.0 if (c & 0x0F) == 0 else (c - 128) / 256.0 for c in range(256)}
    levels[dc.ANCHOR_CODE] = lmax
    return [(c, levels[c]) for c in range(256)]


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
        raw = [(c, (c - 128) / 300.0) for c in range(256)]
        res = dc.CalibrationResult(list(range(256)), {}, "6581", raw)
        path = dc.save_calibration(cfg, key, {"default": res}, {})
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertEqual(len(entry["raw_signed_levels"]), 256)
        self.assertEqual(entry["raw_signed_levels"][1], [1, round(-127 / 300.0, 8)])

    def test_raw_levels_omitted_when_absent_and_file_still_loads(self):
        # raw_signed_levels is additive under the same schema: a result carrying none
        # writes the pre-existing key set, and readers only need `sidtable`.
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        path = dc.save_calibration(cfg, key, {"default": _result(0)}, {})
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertNotIn("raw_signed_levels", entry)
        self.assertEqual(dc.load_calibrated_table(cfg), bytes(256))

    def test_load_ignores_raw_levels(self):
        # A file written by a newer run stays loadable by the table reader.
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        raw = [(c, 0.0) for c in range(256)]
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


class SlotRingLayoutTest(unittest.TestCase):
    def test_ring_is_sync_gap_then_code_ref_pairs(self):
        ring = np.frombuffer(dc.build_slot_ring([0x0F, 0x37], RING), dtype=np.uint8)
        self.assertEqual(ring.size, RING)
        slots = ring.reshape(-1, dc.SLOT_SAMPLES)
        # Every slot holds one constant code — that is what makes a plateau.
        self.assertTrue((slots == slots[:, :1]).all())
        seq = slots[:, 0]
        self.assertTrue((seq[: dc.SYNC_SLOTS] == dc.REF_ZERO).all())
        self.assertEqual(seq[dc.SYNC_SLOTS], 0x0F)
        self.assertEqual(seq[dc.SYNC_SLOTS + 1], dc.REF_ZERO)
        self.assertEqual(seq[dc.SYNC_SLOTS + 2], 0x37)
        self.assertTrue((seq[dc.SYNC_SLOTS + 4 :] == dc.REF_ZERO).all())

    def test_too_many_codes_is_refused(self):
        with self.assertRaises(ValueError):
            dc.build_slot_ring(range(dc.codes_per_ring(RING) + 1), RING)

    def test_batches_cover_every_code_exactly_once(self):
        batches = dc.plan_code_batches(dc.codes_per_ring(RING) - 1)
        self.assertEqual(sorted(c for b in batches for c in b), list(range(256)))
        self.assertTrue(all(len(b) <= dc.codes_per_ring(RING) - 1 for b in batches))

    def test_batches_stride_so_no_ring_holds_a_long_same_nibble_run(self):
        """Slicing 0-110 / 111-221 / 222-255 would put all sixteen codes sharing
        an upper nibble in consecutive slots. On a chip that is silent across
        such a band those slots carry no edges, and a long edgeless run is
        exactly what the sync-gap detector looks for. Striding caps the run at
        16/rings, well under the ~12 consecutive silent codes it would take to
        fake a gap."""
        batches = dc.plan_code_batches(dc.codes_per_ring(RING) - 1)
        for batch in batches:
            runs = [len(list(g)) for _, g in itertools.groupby(c >> 4 for c in batch)]
            self.assertLessEqual(max(runs), -(-16 // len(batches)))

    def test_rounds_give_every_code_evenly_spaced_positions(self):
        rounds = dc.plan_capture_rounds(dc.codes_per_ring(RING) - 1, rounds=3)
        self.assertEqual(len(rounds), 3)
        for r in rounds:
            self.assertEqual(sorted(c for b in r for c in b), list(range(256)))
        positions = [[b.index(0x37) for b in r if 0x37 in b][0] for r in rounds]
        self.assertEqual(len(set(positions)), 3)


class SlotRingExtractionTest(unittest.TestCase):
    """The extraction is where a calibration goes stably wrong: an open-loop
    slot grid reads mid-plateau on a drifting baseline and returns levels that
    repeat perfectly and mean nothing. These drive it from a synthesised
    capture with a known answer."""

    def test_recovers_known_levels_through_ac_coupling(self):
        codes = [dc.ANCHOR_CODE, *range(40)]
        levels, want = _simulate(codes)
        got = dc.extract_slot_levels(levels, len(codes), RING)
        scale = got.levels[0] / want[0]
        err = np.abs(got.levels / scale - want).max() / np.abs(want).max()
        self.assertLess(err, 0.01)
        self.assertGreaterEqual(got.diagnostics["passes"], 3)

    def test_recovers_the_same_levels_from_a_96k_capture(self):
        """Not every capture device does 48 kHz — the cheap HDMI→USB dongles are
        commonly 96 kHz-only. Every timing constant in the extraction comes from
        the `sr` it is handed, so the rate the device forces on us costs nothing
        as long as it is threaded through instead of assumed."""
        codes = [dc.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes, sr=96000)
        got = dc.extract_slot_levels(cap, len(codes), RING, sr=96000)
        scale = got.levels[0] / want[0]
        err = np.abs(got.levels / scale - want).max() / np.abs(want).max()
        self.assertLess(err, 0.01)
        self.assertAlmostEqual(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE, delta=2.0)

    def test_recovers_the_true_nmi_rate_not_the_nominal_one(self):
        """A slot is 192.24 capture samples, not 192: the NMI runs at
        1022727/128 = 7990.05 Hz, not the 8000 Hz it is asked for. Tracking that
        is the difference between a correct grid and a slowly walking one."""
        codes = [dc.ANCHOR_CODE, *range(40)]
        cap, _ = _simulate(codes)
        got = dc.extract_slot_levels(cap, len(codes), RING)
        self.assertAlmostEqual(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE, delta=2.0)
        self.assertAlmostEqual(got.diagnostics["ac_coupling_hz"], 12.0, delta=1.0)

    def test_survives_a_stretched_capture_timebase(self):
        """avfoundation drops samples under load, compressing the timebase. The
        grid tracks edge by edge rather than stepping a nominal pitch, so a
        heavily stretched capture still reads the right levels — and says so in
        the implied NMI rate."""
        codes = [dc.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes, drop_frac=0.12)
        got = dc.extract_slot_levels(cap, len(codes), RING)
        scale = got.levels[0] / want[0]
        self.assertLess(np.abs(got.levels / scale - want).max() / np.abs(want).max(), 0.02)
        self.assertGreater(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE * 1.05)

    def test_pass_spread_flags_a_capture_the_grid_could_not_hold(self):
        # Every pass measures the same levels, so disagreement between them is
        # the one symptom that separates a mistracked capture from a real curve.
        codes = [dc.ANCHOR_CODE, *range(40)]
        cap, _ = _simulate(codes)
        self.assertLess(
            dc.extract_slot_levels(cap, len(codes), RING).diagnostics["pass_spread_frac"], 0.01
        )

    def test_silent_capture_raises_rather_than_inventing_levels(self):
        with self.assertRaises(dc.MeasurementError):
            dc.extract_slot_levels(np.zeros(4 * dc.CAP_SR), 40, RING)


class RingCaptureGateTest(unittest.TestCase):
    """A recording of the *wrong input* still parses. Reported from the field on
    a Windows rig whose capture auto-picked the on-board microphone: ring 1 read
    "2 passes, L($0F)=-0.00001, pass spread 100.08%" — numbers, from room noise —
    and the run then died on ring 2 with a raw traceback, 30 s in. Whether a
    recording is of the ring is decided per ring, before its levels go anywhere."""

    def test_a_real_capture_passes_the_gate(self):
        codes = [dc.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes)
        got = dc.read_ring_capture(cap, len(codes), RING)
        scale = got.levels[0] / want[0]
        self.assertLess(np.abs(got.levels / scale - want).max() / np.abs(want).max(), 0.01)

    def test_silence_is_refused_before_anything_is_extracted(self):
        with self.assertRaises(dc.MeasurementError) as ctx:
            dc.read_ring_capture(np.zeros(4 * dc.CAP_SR), 40, RING)
        self.assertIn("silence", str(ctx.exception))

    def test_a_capture_that_is_mostly_noise_is_refused(self):
        """The reported failure verbatim: enough noise on top of the ring that
        only one sync marker survives. It used to escape as a traceback."""
        cap, _ = _simulate([dc.ANCHOR_CODE, *range(40)], noise=0.1)
        with self.assertRaises(dc.MeasurementError):
            dc.read_ring_capture(cap, 41, RING)

    def test_levels_the_passes_disagree_about_are_refused(self):
        """The subtler half: the extraction found a grid and returned levels,
        but the passes contradict each other, so the levels are noise. Hardware
        reads 0.01-0.2% here, so anything near 100% must not reach the table."""
        cap, _ = _simulate([dc.ANCHOR_CODE, *range(40)])
        bad = dc.SlotLevels(
            levels=np.full(41, 1e-5),
            per_pass=np.zeros((2, 41)),
            diagnostics={"pass_spread_frac": 1.0008, "passes": 2},
        )
        with patch.object(dc, "extract_slot_levels", return_value=bad):
            with self.assertRaises(dc.MeasurementError) as ctx:
                dc.read_ring_capture(cap, 41, RING)
        self.assertIn("100.1%", str(ctx.exception))

    def test_the_failure_names_the_device_and_the_alternatives(self):
        """Every reason above reads like a bug in the measurement; it is almost
        always the rig. The message the user sees has to say which input was
        recorded from, and which ones they could pick instead."""
        fake = _FakeSD([_dev("Microphone (2- Realtek(R) Audio", 2), _dev("Cam Link 4K", 2)])
        with patch.dict("sys.modules", {"sounddevice": fake}):
            msg = dc._capture_fault_message(0, "it recorded silence", 1e-5)
        self.assertIn("Realtek", msg)
        self.assertIn("microphone", msg)
        self.assertIn("Cam Link 4K", msg)
        self.assertIn("--audio-device", msg)


class MergeMeasurementsTest(unittest.TestCase):
    def test_rings_are_rescaled_onto_the_common_anchor(self):
        # Two rings whose capture gain differs by 2x must still merge to one
        # consistent set of levels — the anchor code is what ties them together.
        a = dc.SlotLevels(np.array([1.0, 0.5, 0.25]), np.zeros((2, 3)), {})
        b = dc.SlotLevels(np.array([2.0, 1.0, -1.0]), np.zeros((2, 3)), {})
        raw, metrics = dc.merge_measurements([([1, 2], a), ([3, 4], b)])
        self.assertEqual([c for c, _ in raw], [1, 2, 3, 4])
        vals = [v for _, v in raw]
        # Half of ring a's anchor and half of ring b's must land on one level.
        self.assertAlmostEqual(vals[0], vals[2])
        self.assertEqual(metrics["rings"], 2)

    def test_repeated_codes_are_averaged_and_their_spread_reported(self):
        a = dc.SlotLevels(np.array([1.0, 0.4]), np.zeros((2, 2)), {})
        b = dc.SlotLevels(np.array([1.0, 0.6]), np.zeros((2, 2)), {})
        raw, metrics = dc.merge_measurements([([7], a), ([7], b)])
        self.assertAlmostEqual(raw[0][1], 0.5)
        self.assertAlmostEqual(metrics["context_spread_frac"], 0.2, places=4)


class BuildSidtableTest(unittest.TestCase):
    def test_reconstruct_from_synthetic_signed_curve(self):
        sidtable, metrics = dc.build_sidtable_from_levels(_signed_levels())
        assert sidtable is not None
        self.assertEqual(len(sidtable), 256)
        self.assertTrue(all(0 <= v <= 255 for v in sidtable))
        self.assertIn("signed_span", metrics)
        lo, hi = metrics["signed_span"]
        self.assertLess(lo, hi)
        self.assertGreater(metrics["ladder_bits"], 4.0)


class Volume0SelfTestTest(unittest.TestCase):
    """Codes $h0 set the master volume nibble to 0, so their output level is
    $00's whatever the upper nibble does — L($h0) must measure zero. That holds
    with no model assumptions, which makes it the one check that can tell a
    sound measurement from one whose numbers are not output levels at all."""

    def test_consistent_measurement_passes_and_yields_a_table(self):
        sidtable, metrics = dc.build_sidtable_from_levels(_signed_levels())
        self.assertIsNotNone(sidtable)
        self.assertAlmostEqual(metrics["volume0_selftest_worst"], 0.0, places=6)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_inconsistent_measurement_is_rejected_with_no_table(self):
        # Master-volume-0 codes coming back with an upper-nibble-dependent level
        # is impossible for any real set of levels.
        raw = [(c, v + 0.02 * (c >> 4) if (c & 0x0F) == 0 else v) for c, v in _signed_levels()]
        sidtable, metrics = dc.build_sidtable_from_levels(raw)
        self.assertIsNone(sidtable)
        self.assertGreater(metrics["volume0_selftest_worst"], dc.SELFTEST_TOLERANCE)
        # Still fully diagnosable: the metrics survive the rejection.
        self.assertIn("signed_span", metrics)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_rejection_writes_no_sidtable_and_reads_back_as_no_calibration(self):
        cfg = _u64_cfg()
        key = dc.resolve_calibration_key(cfg)
        raw = [(c, v + 0.3 if (c & 0x0F) == 0 else v) for c, v in _signed_levels()]
        sidtable, metrics = dc.build_sidtable_from_levels(raw)
        self.assertIsNone(sidtable)
        path = dc.save_calibration(
            cfg, key, {"default": dc.CalibrationResult(sidtable, metrics, None, raw)}, {}
        )
        doc = json.loads(path.read_text())
        entry = doc["sids"]["default"]
        self.assertNotIn("sidtable", entry)
        # The raw levels are kept so the failure can be investigated offline.
        self.assertEqual(len(entry["raw_signed_levels"]), 256)
        self.assertIsNone(dc.load_calibrated_table(cfg))


class LadderMetricsTest(unittest.TestCase):
    def test_quality_is_independent_of_capture_gain(self):
        """The metrics this replaced counted level steps exceeding the capture
        noise floor, so a quieter rig scored more "effective bits" on identical
        hardware — it once rated a chip degraded to ~4 bits above a working one.
        Scaling the whole capture must not change the ladder's quality figures."""
        _, m1 = dc.build_sidtable_from_levels(_signed_levels())
        _, m2 = dc.build_sidtable_from_levels([(c, v * 10.0) for c, v in _signed_levels()])
        for k in ("ladder_bits", "worst_gap_frac", "ladder_rms_err_frac"):
            self.assertAlmostEqual(m1[k], m2[k], places=3, msg=k)

    def test_worst_gap_position_is_reported_relative_to_silence(self):
        sidtable, metrics = dc.build_sidtable_from_levels(_signed_levels())
        self.assertIsNotNone(sidtable)
        # 0 = the gap straddles silence (crossover distortion), ±0.5 = it sits
        # out at an extreme, where the same gap is benign.
        self.assertGreaterEqual(metrics["worst_gap_from_zero_frac"], -0.5)
        self.assertLessEqual(metrics["worst_gap_from_zero_frac"], 0.5)
        self.assertGreaterEqual(metrics["crossover_gap_frac"], 0.0)


def _dev(name, max_in, default_sr=48000.0):
    return {"name": name, "max_input_channels": max_in, "default_samplerate": default_sr}


class _FakeSD:
    """Minimal sounddevice stand-in: a device table plus a settings check that
    accepts only the (channels, rate) combinations each device really supports."""

    def __init__(self, devices, accept=None, default_input=0):
        self._devices = devices
        # sd.default.device is (input, output); -1 means "none".
        self.default = SimpleNamespace(device=(default_input, -1))
        # {device_index: {(channels, rate), …}}; default = anything up to max_in
        # at any rate.
        self._accept = accept
        self.checked: list[tuple[int, int, int]] = []

    def query_devices(self, dev=None):
        return self._devices if dev is None else self._devices[dev]

    def check_input_settings(self, device: int, channels: int, samplerate: int, dtype: str):
        self.checked.append((device, channels, samplerate))
        if self._accept is not None:
            ok = (channels, samplerate) in self._accept[device]
        else:
            ok = 1 <= channels <= self._devices[device]["max_input_channels"]
        if not ok:
            raise RuntimeError("Invalid number of channels [PaErrorCode -9998]")


class ResolveCaptureFormatTest(unittest.TestCase):
    """A capture device is not necessarily a Cam Link. Opening a mono-only input
    with a hardcoded channels=2, or a 96 kHz-only HDMI dongle at 48 kHz, is how
    a calibration run used to die with a raw `PortAudioError`."""

    def _run(self, fake, dev=0):
        with patch.dict("sys.modules", {"sounddevice": fake}):
            return dc.resolve_capture_format(dev)

    def test_prefers_stereo_at_the_nominal_rate(self):
        fake = _FakeSD([_dev("Cam Link 4K", 2)])
        self.assertEqual(self._run(fake), (2, dc.CAP_SR))

    def test_falls_back_to_mono_on_a_mono_only_device(self):
        fake = _FakeSD([_dev("Mono Capture", 1)])
        self.assertEqual(self._run(fake), (1, dc.CAP_SR))
        # 2 is never even probed on a device that reports a single channel.
        self.assertEqual(fake.checked, [(0, 1, dc.CAP_SR)])

    def test_falls_back_when_a_device_lies_about_its_channel_count(self):
        """max_input_channels is what the driver advertises; PortAudio can still
        refuse that count at the requested rate. Probe, don't trust."""
        fake = _FakeSD([_dev("Fussy Capture", 2)], accept={0: {(1, 48000)}})
        self.assertEqual(self._run(fake), (1, 48000))
        self.assertEqual(fake.checked, [(0, 2, 48000), (0, 1, 48000)])

    def test_96k_only_dongle_is_measured_at_96k(self):
        """The cheap MacroSilicon HDMI→USB capture sticks are commonly 96 kHz-only.
        extract_slot_levels takes its rate as a parameter, so this measures fine."""
        fake = _FakeSD([_dev("USB Digital Audio", 2, 96000.0)], accept={0: {(2, 96000)}})
        self.assertEqual(self._run(fake), (2, 96000))

    def test_rate_is_preferred_over_channel_count(self):
        """A 48 kHz mono capture beats a 96 kHz stereo one — the channel fold is
        free, the rate change is the compromise."""
        fake = _FakeSD([_dev("Odd Capture", 2)], accept={0: {(1, 48000), (2, 96000)}})
        self.assertEqual(self._run(fake), (1, 48000))

    def test_native_rate_is_tried_before_the_static_fallbacks(self):
        fake = _FakeSD([_dev("Odd Rate", 1, 22050.0)], accept={0: {(1, 22050)}})
        self.assertEqual(self._run(fake), (1, 22050))

    def test_multichannel_device_captures_the_first_pair(self):
        fake = _FakeSD([_dev("8-in Interface", 8)])
        self.assertEqual(self._run(fake), (2, dc.CAP_SR))

    def test_output_only_device_raises_actionable_error(self):
        fake = _FakeSD([_dev("Speakers", 0), _dev("Cam Link 4K", 2)])
        with self.assertRaises(dc.CaptureUnavailableError) as ctx:
            self._run(fake, dev=0)
        # The message must name a device the user can actually pass instead.
        self.assertIn("Cam Link 4K", str(ctx.exception))
        self.assertIn("--audio-device", str(ctx.exception))

    def test_no_workable_format_raises_capture_unavailable(self):
        fake = _FakeSD([_dev("Hostile Capture", 2)], accept={0: set()})
        with self.assertRaises(dc.CaptureUnavailableError):
            self._run(fake)


class FindCaptureDeviceTest(unittest.TestCase):
    """Only "cam link" used to be recognized, so every other rig fell through to
    the system default input — on Windows the on-board microphone, which records
    room noise for the whole run and measures like a dead chip."""

    def _run(self, fake, preferred=None):
        with patch.dict("sys.modules", {"sounddevice": fake}):
            return dc.find_capture_device(preferred)

    def test_an_explicit_device_is_used_as_given(self):
        fake = _FakeSD([_dev("Microphone (Realtek)", 2), _dev("Cam Link 4K", 2)])
        self.assertEqual(self._run(fake, preferred=0), 0)

    def test_an_hdmi_stick_is_picked_over_the_default_microphone(self):
        fake = _FakeSD(
            [_dev("Microphone (2- Realtek(R) Audio", 2), _dev("USB3.0 HD Video Capture", 2)],
            default_input=0,
        )
        self.assertEqual(self._run(fake), 1)

    def test_a_cam_link_wins_over_another_capture_input(self):
        fake = _FakeSD([_dev("USB Video", 2), _dev("Cam Link 4K", 2)])
        self.assertEqual(self._run(fake), 1)

    def test_an_output_only_capture_device_is_skipped(self):
        fake = _FakeSD([_dev("HDMI Output", 0), _dev("HDMI Capture", 2)])
        self.assertEqual(self._run(fake), 1)

    def test_falls_back_to_the_system_default_when_nothing_is_recognized(self):
        fake = _FakeSD([_dev("Speakers", 0), _dev("Line In", 2)], default_input=1)
        self.assertEqual(self._run(fake), 1)
        # …and that fallback is exactly what run_calibration warns about.
        self.assertFalse(dc.looks_like_capture_input("Line In"))
        self.assertTrue(dc.looks_like_capture_input("Cam Link 4K"))


if __name__ == "__main__":
    unittest.main()
