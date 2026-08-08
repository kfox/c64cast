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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from _fakes import FakeAPI

from c64cast.app.config import Config
from c64cast.audio import dac_calibration as dc
from c64cast.audio import dac_calibration_store as dcs
from c64cast.audio import dac_capture_device as dcap
from c64cast.audio import dac_curve_resolve as dcr
from c64cast.audio import dac_slot_ring as dsr
from c64cast.audio.dac_curves import MAHONEY_ULTISID
from c64cast.hw.backend import HardwareProfile
from c64cast.sid.asid_sidmap import CAT_ADDRESSING, CAT_SOCKETS


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


def _result(fill: int) -> dcs.CalibrationResult:
    return dcs.CalibrationResult(sidtable=[fill & 0xFF] * 256, metrics={"ladder_bits": 6.5})


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
    sr: int = dsr.CAP_SR,
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

    ring = np.frombuffer(dsr.build_slot_ring(codes, RING), dtype=np.uint8)
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
    return y + rng.normal(0, noise, y.size), true[codes] - true[dsr.REF_ZERO]


def _signed_levels(lmax: float = 0.5) -> list[tuple[int, float]]:
    """A consistent set of measured signed levels: codes with volume nibble 0
    output silence (master volume 0), the rest spread negative→positive."""
    levels = {c: 0.0 if (c & 0x0F) == 0 else (c - 128) / 256.0 for c in range(256)}
    levels[dsr.ANCHOR_CODE] = lmax
    return [(c, levels[c]) for c in range(256)]


class ResolveKeyTest(unittest.TestCase):
    def test_ultimate_offline_key_uses_host(self):
        self.assertEqual(
            dcs.resolve_calibration_key(_u64_cfg("192.168.2.64")), "ultimate-192.168.2.64"
        )

    def test_ultimate_live_key_uses_unique_id(self):
        cfg = _u64_cfg()
        api = _ultimate_fake()
        api.device_info = {"product": "C64 Ultimate", "unique_id": "5D327C"}
        self.assertEqual(dcs.resolve_calibration_key(cfg, api), "ultimate-5D327C")

    def test_ultimate_live_lookup_failure_falls_back_to_host(self):
        cfg = _u64_cfg("192.168.2.64")
        api = _ultimate_fake()  # device_info left None -> get_device_info() raises
        self.assertEqual(dcs.resolve_calibration_key(cfg, api), "ultimate-192.168.2.64")

    def test_tr_serial_key_offline_sanitizes_device(self):
        key = dcs.resolve_calibration_key(_tr_serial_cfg("/dev/cu.usbmodem1234"))
        self.assertEqual(key, "tr-serial-_dev_cu.usbmodem1234")

    def test_tr_serial_key_uses_live_usb_serial_number(self):
        cfg = _tr_serial_cfg("/dev/cu.usbmodem1234")
        api = FakeAPI()
        with patch("c64cast.hw.teensyrom_dma.usb_serial_number", return_value="TR12345"):
            key = dcs.resolve_calibration_key(cfg, api)
        self.assertEqual(key, "tr-TR12345")

    def test_tr_serial_key_falls_back_when_no_usb_serial(self):
        cfg = _tr_serial_cfg("/dev/cu.usbmodem1234")
        api = FakeAPI()
        with patch("c64cast.hw.teensyrom_dma.usb_serial_number", return_value=None):
            key = dcs.resolve_calibration_key(cfg, api)
        self.assertEqual(key, "tr-serial-_dev_cu.usbmodem1234")

    def test_tr_tcp_key(self):
        cfg = Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "tcp"
        cfg.teensyrom.host = "teensy.lan"
        cfg.teensyrom.tcp_port = 2112
        self.assertEqual(dcs.resolve_calibration_key(cfg), "tr-tcp-teensy.lan-2112")

    def test_distinct_hosts_distinct_keys(self):
        self.assertNotEqual(
            dcs.resolve_calibration_key(_u64_cfg("a.lan")),
            dcs.resolve_calibration_key(_u64_cfg("b.lan")),
        )

    def test_profile_override_wins_over_everything(self):
        cfg = _u64_cfg("192.168.2.64")
        cfg.audio.dac_calibration_profile = "My Breadbin!"
        api = _ultimate_fake()
        api.device_info = {"unique_id": "5D327C"}
        self.assertEqual(dcs.resolve_calibration_key(cfg, api), "profile-My_Breadbin_")

    def test_profile_override_applies_to_teensyrom_too(self):
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "breadbin"
        self.assertEqual(dcs.resolve_calibration_key(cfg), "profile-breadbin")

    def test_a_bare_name_matching_an_existing_file_is_not_re_prefixed(self):
        """The auto-keyed files --calibrate-dac writes are named for the device
        ("ultimate-<unique-id>"), not "profile-…". Naming one of those — the
        obvious thing to type, since it is what is on disk — resolved to
        "profile-ultimate-<unique-id>" and matched nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"C64CAST_DATA_DIR": tmp}):
                d = Path(tmp) / "calibration" / "dac"
                d.mkdir(parents=True)
                (d / "ultimate-DEV123.json").write_text("{}")
                cfg = _tr_serial_cfg()
                cfg.audio.dac_calibration_profile = "ultimate-DEV123"
                self.assertEqual(dcs.resolve_calibration_key(cfg), "ultimate-DEV123")
                self.assertEqual(dcs.calibration_path(cfg), d / "ultimate-DEV123.json")

    def test_a_bare_name_still_prefixes_when_no_such_file_exists(self):
        """A run calibrating *under* a new profile name must keep writing
        profile-<name>.json, or every fresh profile would file itself under the
        unprefixed spelling."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"C64CAST_DATA_DIR": tmp}):
                cfg = _tr_serial_cfg()
                cfg.audio.dac_calibration_profile = "breadbin"
                self.assertEqual(dcs.resolve_calibration_key(cfg), "profile-breadbin")

    def test_a_prefixed_file_wins_over_an_unprefixed_one(self):
        """Both spellings on disk is ambiguous; the profile spelling is the one
        the flag has always meant, so it keeps precedence."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"C64CAST_DATA_DIR": tmp}):
                d = Path(tmp) / "calibration" / "dac"
                d.mkdir(parents=True)
                (d / "profile-x.json").write_text("{}")
                (d / "x.json").write_text("{}")
                cfg = _tr_serial_cfg()
                cfg.audio.dac_calibration_profile = "x"
                self.assertEqual(dcs.resolve_calibration_key(cfg), "profile-x")

    def test_profile_naming_a_file_is_used_as_a_path(self):
        # A path is the only way to point one backend's run at a calibration
        # filed under another's device identity (a TR+ in a U64's cart port
        # driving the SID the Ultimate already measured). Sanitizing it into a
        # key instead folds every separator to '_' and matches no file.
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "/data/c64cast/calibration/dac/ultimate-5D327C.json"
        self.assertEqual(
            dcs.calibration_path(cfg),
            Path("/data/c64cast/calibration/dac/ultimate-5D327C.json"),
        )
        self.assertEqual(dcs.resolve_calibration_key(cfg), "ultimate-5D327C")

    def test_profile_path_expands_user(self):
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "~/cal/breadbin.json"
        self.assertEqual(dcs.calibration_path(cfg), Path.home() / "cal/breadbin.json")

    def test_bare_name_is_never_treated_as_a_path(self):
        # A name with no separator and no .json suffix stays a key, dots and all.
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "my.rig"
        self.assertIsNone(dcs.profile_path_override(cfg))
        self.assertEqual(dcs.resolve_calibration_key(cfg), "profile-my.rig")


class DataDirIsolated(unittest.TestCase):
    """Base for every test that persists a calibration.

    Inherited rather than copied into each class because the one class that
    lacked it wrote a real 15 KB calibration into the developer's own
    ``~/.local/share/c64cast`` on every run — silently, since a passing test
    says nothing about where it wrote. Anything reaching `save_calibration`
    belongs here."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Redirect the whole data root at the env layer (paths.calibration_dir()
        # is resolved from $C64CAST_DATA_DIR); no module global to patch.
        self._env = patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class PersistenceTest(DataDirIsolated):
    def test_save_load_default_entry_round_trip(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        path = dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(0)}, {}))
        self.assertTrue(path.exists())
        got = dcs.load_calibrated_table(cfg)
        self.assertEqual(got, bytes(256))

    def test_raw_levels_persisted_when_present(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        raw = [(c, (c - 128) / 300.0) for c in range(256)]
        res = dcs.CalibrationResult(list(range(256)), {}, "6581", raw)
        path = dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": res}, {}))
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertEqual(len(entry["raw_signed_levels"]), 256)
        self.assertEqual(entry["raw_signed_levels"][1], [1, round(-127 / 300.0, 8)])

    def test_raw_levels_omitted_when_absent_and_file_still_loads(self):
        # raw_signed_levels is additive under the same schema: a result carrying none
        # writes the pre-existing key set, and readers only need `sidtable`.
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        path = dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(0)}, {}))
        entry = json.loads(path.read_text())["sids"]["default"]
        self.assertNotIn("raw_signed_levels", entry)
        self.assertEqual(dcs.load_calibrated_table(cfg), bytes(256))

    def test_default_entry_says_the_sid_was_never_identified(self):
        # Only the Ultimate exposes the socket map, so every other link files its
        # measurement under "default" with detected=None — right on a one-SID
        # machine, a blend of two chips on a machine with a second one or with
        # mirroring on. Neither the file nor this side can tell them apart, so
        # the assumption has to be stated where the table is chosen.
        cfg = _tr_serial_cfg()
        be = FakeAPI()  # profile.supports_config False, like the real TR
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                dcs.resolve_calibration_key(cfg, be), {"default": _result(0)}, {}
            ),
        )
        with self.assertLogs("c64cast.audio.dac_calibration_store", level="INFO") as logs:
            self.assertEqual(dcs.load_calibrated_table(cfg, be=be), bytes(256))
        self.assertIn("assumes one SID", "\n".join(logs.output))

    def test_default_entry_from_a_link_that_can_identify_stays_quiet(self):
        # A backend with the socket map either resolved the identity or chose not
        # to write per-socket entries — knowingly, either way. Saying it there
        # would fire on every Ultimate run whose file predates per-socket entries.
        cfg = _u64_cfg()
        be = FakeAPI()
        be.profile = HardwareProfile(name="Fake", family="fake", supports_config=True)
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                dcs.resolve_calibration_key(cfg, be), {"default": _result(0)}, {}
            ),
        )
        with self.assertNoLogs("c64cast.audio.dac_calibration_store", level="INFO"):
            self.assertEqual(dcs.load_calibrated_table(cfg, be=be), bytes(256))

    def test_load_ignores_raw_levels(self):
        # A file written by a newer run stays loadable by the table reader.
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        raw = [(c, 0.0) for c in range(256)]
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                key, {"default": dcs.CalibrationResult(list(range(256)), {}, None, raw)}, {}
            ),
        )
        self.assertEqual(dcs.load_calibrated_table(cfg), bytes(range(256)))

    def test_save_honours_a_path_profile_and_loads_back(self):
        # --calibrate-dac and playback must agree on where the file lives, so a
        # path profile has to steer the write as well as the read.
        cfg = _u64_cfg()
        dest = Path(self._tmp.name) / "elsewhere" / "breadbin.json"
        cfg.audio.dac_calibration_profile = str(dest)
        path = dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(dcs.resolve_calibration_key(cfg), {"default": _result(0)}, {}),
        )
        self.assertEqual(path, dest)
        self.assertEqual(dcs.load_calibrated_table(cfg), bytes(256))

    def test_load_missing_returns_none(self):
        self.assertIsNone(dcs.load_calibrated_table(_u64_cfg("nope.lan")))

    def test_load_wrong_length_returns_none(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        bad = dcs.CalibrationResult(sidtable=list(range(10)), metrics={})
        dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": bad}, {}))
        self.assertIsNone(dcs.load_calibrated_table(cfg))

    def test_load_corrupt_file_returns_none(self):
        cfg = _u64_cfg()
        dcs.calibration_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        dcs.calibration_path(cfg).write_text("{ not json")
        self.assertIsNone(dcs.load_calibrated_table(cfg))

    def test_load_old_schema_returns_none(self):
        # Clean cutover: an old schema=1 single-sidtable file is never read
        # under the new (also-renamed) key scheme; guard the shape too.
        cfg = _u64_cfg()
        dcs.calibration_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        dcs.calibration_path(cfg).write_text(
            '{"schema": 1, "key": "u64-192.168.2.64", "sidtable": ' + str(list(range(256))) + "}"
        )
        self.assertIsNone(dcs.load_calibrated_table(cfg))

    def test_multi_socket_selection_uses_live_active_socket(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                key, {"1": _result(1), "2": _result(2)}, {"unique_id": "5D327C"}
            ),
        )
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
        got = dcs.load_calibrated_table(cfg, be=api)
        self.assertEqual(got, bytes([2] * 256))

    def test_multi_socket_selection_none_when_ultisid_owns_d400(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(
            cfg, dcs.CalibrationDocument(key, {"1": _result(1), "2": _result(2)}, {})
        )
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
        self.assertIsNone(dcs.load_calibrated_table(cfg, be=api))

    def test_default_entry_used_even_with_live_api_when_no_socket_keys(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(7)}, {}))
        api = _ultimate_fake()
        got = dcs.load_calibrated_table(cfg, be=api)
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


class ResolveCurveTest(DataDirIsolated):
    def test_auto_ultimate_no_cal_uses_baked_mahoney(self):
        label, table = dcr.resolve_dac_curve_for_backend(_u64_cfg())
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)

    def test_auto_teensyrom_no_cal_uses_linear(self):
        label, table = dcr.resolve_dac_curve_for_backend(_tr_serial_cfg())
        self.assertEqual(label, "linear")
        self.assertIsNone(table)

    def test_auto_prefers_calibration_when_present(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(0)}, {}))
        label, table = dcr.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(256))

    def test_auto_yields_to_digi_boost(self):
        cfg = _u64_cfg()
        cfg.audio.digi_boost = True
        label, table = dcr.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(label, "linear")
        self.assertIsNone(table)

    def test_calibrated_missing_raises(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "calibrated"
        with self.assertRaises(ValueError):
            dcr.resolve_dac_curve_for_backend(cfg)

    def test_calibrated_present_returns_table(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "calibrated"
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(0)}, {}))
        label, table = dcr.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes(256))

    def test_explicit_linear_and_mahoney_pass_through(self):
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "linear"
        self.assertEqual(dcr.resolve_dac_curve_for_backend(cfg), ("linear", None))
        cfg.audio.dac_curve = "mahoney_ultisid"
        label, table = dcr.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)


class MissingCalibrationLogTest(DataDirIsolated):
    """A live "auto" resolution that finds no calibration logs an actionable
    line (this replaced the old --doctor repo-location migration nudge). It
    stays silent for an offline resolution (be=None) — --doctor reports that
    case separately and can't even confirm the identity key."""

    def test_ultimate_live_no_cal_logs_info(self):
        cfg = _u64_cfg()
        with self.assertLogs("c64cast.audio.dac_curve_resolve", level="INFO") as cm:
            label, table = dcr.resolve_dac_curve_for_backend(cfg, be=_ultimate_fake())
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)
        joined = "\n".join(cm.output)
        self.assertIn("no per-unit DAC calibration", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_teensyrom_live_no_cal_logs_warning(self):
        cfg = _tr_serial_cfg()
        with patch("c64cast.hw.teensyrom_dma.usb_serial_number", return_value=None):
            with self.assertLogs("c64cast.audio.dac_curve_resolve", level="WARNING") as cm:
                label, table = dcr.resolve_dac_curve_for_backend(cfg, be=FakeAPI())
        self.assertEqual(label, "linear")
        self.assertIsNone(table)
        joined = "\n".join(cm.output)
        self.assertIn("no DAC calibration found", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_offline_no_cal_is_silent(self):
        # be=None → no log (assertNoLogs raises if anything is emitted).
        with self.assertNoLogs("c64cast.audio.dac_curve_resolve", level="INFO"):
            dcr.resolve_dac_curve_for_backend(_u64_cfg())
        with self.assertNoLogs("c64cast.audio.dac_curve_resolve", level="INFO"):
            dcr.resolve_dac_curve_for_backend(_tr_serial_cfg())

    def test_live_calibration_present_is_silent(self):
        # A hit doesn't warn.
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(cfg, dcs.CalibrationDocument(key, {"default": _result(0)}, {}))
        with self.assertNoLogs("c64cast.audio.dac_curve_resolve", level="INFO"):
            label, _ = dcr.resolve_dac_curve_for_backend(cfg, be=_ultimate_fake())
        self.assertTrue(label.startswith("calibrated:"))


def _socket_at_d400(socket: int) -> FakeAPI:
    """An Ultimate whose populated physical `socket` answers $D400 — what the
    NMI DAC handler's `STA $D418` reaches. Mirrors the rig that exposed the
    bug: both sockets populated, auto-mirroring on, an UltiSID core nominally
    at the same address (the socket wins, so the real chip is audible)."""
    other = 2 if socket == 1 else 1
    api = _ultimate_fake()
    api.config_store[CAT_ADDRESSING] = {
        f"SID Socket {socket} Address": "$D400",
        f"SID Socket {other} Address": "$D420",
        "UltiSID 1 Address": "$D400",
        "Auto Address Mirroring": "Enabled",
    }
    api.config_store[CAT_SOCKETS] = {
        "SID Socket 1": "Enabled",
        "SID Socket 2": "Enabled",
        "SID Detected Socket 1": "6581",
        "SID Detected Socket 2": "6581",
    }
    return api


class AutoCurveD400OwnershipTest(DataDirIsolated):
    """Resolution must never hand the baked emulated-UltiSID table to a
    physical chip: measured at ~29% RMS level error cross-chip, which is worse
    than the 4-bit linear path it is supposed to improve on."""

    def test_physical_socket_at_d400_without_calibration_falls_back_to_linear(self):
        cfg = _u64_cfg()
        with self.assertLogs("c64cast.audio.dac_curve_resolve", level="WARNING") as cm:
            label, table = dcr.resolve_dac_curve_for_backend(cfg, be=_socket_at_d400(1))
        self.assertEqual(label, "linear")
        self.assertIsNone(table)
        joined = "\n".join(cm.output)
        self.assertIn("socket 1", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_socket_2_at_d400_is_named_in_the_warning(self):
        cfg = _u64_cfg()
        with self.assertLogs("c64cast.audio.dac_curve_resolve", level="WARNING") as cm:
            label, _ = dcr.resolve_dac_curve_for_backend(cfg, be=_socket_at_d400(2))
        self.assertEqual(label, "linear")
        self.assertIn("socket 2", "\n".join(cm.output))

    def test_ultisid_at_d400_still_gets_the_baked_table(self):
        # Nothing physical answers $D400, so the baked table is the *matched*
        # one and stays the right default.
        cfg = _u64_cfg()
        api = _ultimate_fake()
        api.config_store[CAT_ADDRESSING] = {
            "SID Socket 1 Address": "$D420",
            "UltiSID 1 Address": "$D400",
        }
        api.config_store[CAT_SOCKETS] = {
            "SID Socket 1": "Enabled",
            "SID Detected Socket 1": "6581",
        }
        label, table = dcr.resolve_dac_curve_for_backend(cfg, be=api)
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)

    def test_empty_socket_mapped_at_d400_still_gets_the_baked_table(self):
        # Mapped but no chip detected — nothing physical is there to mismatch.
        cfg = _u64_cfg()
        api = _ultimate_fake()
        api.config_store[CAT_ADDRESSING] = {"SID Socket 1 Address": "$D400"}
        api.config_store[CAT_SOCKETS] = {
            "SID Socket 1": "Enabled",
            "SID Detected Socket 1": "None",
        }
        label, _ = dcr.resolve_dac_curve_for_backend(cfg, be=api)
        self.assertEqual(label, "mahoney_ultisid")

    def test_calibration_for_that_socket_still_wins(self):
        # The guard is a fallback, not a veto: a table measured on the chip
        # that owns $D400 is exactly what should be used.
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        dcs.save_calibration(
            cfg, dcs.CalibrationDocument(key, {"1": _result(1), "2": _result(2)}, {})
        )
        label, table = dcr.resolve_dac_curve_for_backend(cfg, be=_socket_at_d400(1))
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes([1] * 256))

    def test_offline_resolution_is_unchanged_and_silent(self):
        # be=None can't read who owns $D400; --doctor reports that separately.
        with self.assertNoLogs("c64cast.audio.dac_curve_resolve", level="INFO"):
            label, table = dcr.resolve_dac_curve_for_backend(_u64_cfg())
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)

    def test_explicit_mahoney_is_not_second_guessed(self):
        # The guard only shapes "auto". A user who named the curve meant it.
        cfg = _u64_cfg()
        cfg.audio.dac_curve = "mahoney_ultisid"
        label, table = dcr.resolve_dac_curve_for_backend(cfg, be=_socket_at_d400(1))
        self.assertEqual(label, "mahoney_ultisid")
        self.assertEqual(table, MAHONEY_ULTISID)


class CrossBackendSocketSelectionTest(DataDirIsolated):
    """A multi-socket calibration measured on the Ultimate and replayed over a
    link with no SID config query — the cross-backend reuse
    ``dac_calibration_profile`` is documented to support (one machine, two
    links: a TeensyROM+ cartridge plugged into a U64)."""

    def _tr(self) -> tuple[Config, FakeAPI]:
        cfg = _tr_serial_cfg()
        cfg.audio.dac_calibration_profile = "shared"
        return cfg, FakeAPI()  # profile.supports_config False, like the real TR

    def _save(self, cfg, be, entries, d400=None) -> Path:
        return dcs.save_calibration(
            cfg, dcs.CalibrationDocument(dcs.resolve_calibration_key(cfg, be), entries, {}, d400)
        )

    def test_a_two_socket_file_is_not_discarded_by_a_link_that_cannot_ask(self):
        # The regression: "can't read who owns $D400" was treated as the same
        # answer as "an UltiSID owns it", so a good file resolved to nothing and
        # playback silently dropped to the 4-bit linear DAC.
        cfg, be = self._tr()
        self._save(cfg, be, {"1": _result(1), "2": _result(2)})
        with self.assertLogs("c64cast.audio.dac_calibration_store", level="WARNING"):
            label, table = dcr.resolve_dac_curve_for_backend(cfg, be=be)
        self.assertTrue(label.startswith("calibrated:"))
        self.assertEqual(table, bytes([1] * 256))

    def test_the_assumed_socket_is_stated_and_says_how_to_make_it_certain(self):
        cfg, be = self._tr()
        self._save(cfg, be, {"1": _result(1), "2": _result(2)})
        with self.assertLogs("c64cast.audio.dac_calibration_store", level="WARNING") as cm:
            dcs.load_calibrated_table(cfg, be=be)
        joined = "\n".join(cm.output)
        self.assertIn("socket 1", joined)
        self.assertIn("--calibrate-dac", joined)

    def test_the_recorded_owner_wins_over_the_assumption(self):
        cfg, be = self._tr()
        self._save(cfg, be, {"1": _result(1), "2": _result(2)}, d400=2)
        with self.assertNoLogs("c64cast.audio.dac_calibration_store", level="WARNING"):
            table = dcs.load_calibrated_table(cfg, be=be)
        self.assertEqual(table, bytes([2] * 256))

    def test_a_file_holding_no_table_for_the_recorded_owner_applies_none(self):
        # Socket 2 answers $D400 but only socket 1 was tabled: the one entry
        # present is the *other* chip, so no table here is the right one.
        cfg, be = self._tr()
        self._save(cfg, be, {"1": _result(1)}, d400=2)
        self.assertIsNone(dcs.load_calibrated_table(cfg, be=be))

    def test_a_single_socket_file_still_loads_without_an_assumption(self):
        cfg, be = self._tr()
        self._save(cfg, be, {"1": _result(1)})
        with self.assertNoLogs("c64cast.audio.dac_calibration_store", level="WARNING"):
            self.assertEqual(dcs.load_calibrated_table(cfg, be=be), bytes([1] * 256))

    def test_the_ultimate_still_refuses_a_physical_table_when_an_ultisid_owns_d400(self):
        # The live answer stays authoritative, including its None. Guarding this
        # because the "unknown" path added beside it must not become a way for a
        # physical-chip table to reach an emulated core.
        cfg = _u64_cfg()
        api = _ultimate_fake()
        api.config_store[CAT_ADDRESSING] = {
            "SID Socket 1 Address": "$D420",
            "UltiSID 1 Address": "$D400",
        }
        api.config_store[CAT_SOCKETS] = {
            "SID Socket 1": "Enabled",
            "SID Detected Socket 1": "6581",
        }
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(dcs.resolve_calibration_key(cfg, api), {"1": _result(1)}, {}),
        )
        self.assertIsNone(dcs.load_calibrated_table(cfg, be=api))

    def test_offline_selection_is_unchanged(self):
        # be=None can't confirm the identity key either; --doctor reports that
        # separately, so an offline miss must stay a miss rather than acquiring
        # an assumption of its own.
        cfg = _u64_cfg()
        dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                dcs.resolve_calibration_key(cfg), {"1": _result(1), "2": _result(2)}, {}
            ),
        )
        self.assertIsNone(dcs.load_calibrated_table(cfg))

    def test_the_owner_is_recorded_in_the_file(self):
        cfg = _u64_cfg()
        path = self._save(cfg, None, {"1": _result(1), "2": _result(2)}, d400=2)
        self.assertEqual(json.loads(path.read_text())["d400_socket"], 2)

    def test_a_run_that_could_not_read_the_owner_writes_no_claim(self):
        # Absent, not null: an older file and a link that can't ask are the same
        # state, and both have to read back as "unknown".
        cfg = _u64_cfg()
        path = self._save(cfg, None, {"1": _result(1)})
        self.assertNotIn("d400_socket", json.loads(path.read_text()))


class SlotRingLayoutTest(unittest.TestCase):
    def test_ring_is_sync_gap_then_code_ref_pairs(self):
        ring = np.frombuffer(dsr.build_slot_ring([0x0F, 0x37], RING), dtype=np.uint8)
        self.assertEqual(ring.size, RING)
        slots = ring.reshape(-1, dsr.SLOT_SAMPLES)
        # Every slot holds one constant code — that is what makes a plateau.
        self.assertTrue((slots == slots[:, :1]).all())
        seq = slots[:, 0]
        self.assertTrue((seq[: dsr.SYNC_SLOTS] == dsr.REF_ZERO).all())
        self.assertEqual(seq[dsr.SYNC_SLOTS], 0x0F)
        self.assertEqual(seq[dsr.SYNC_SLOTS + 1], dsr.REF_ZERO)
        self.assertEqual(seq[dsr.SYNC_SLOTS + 2], 0x37)
        self.assertTrue((seq[dsr.SYNC_SLOTS + 4 :] == dsr.REF_ZERO).all())

    def test_too_many_codes_is_refused(self):
        with self.assertRaises(ValueError):
            dsr.build_slot_ring(range(dsr.codes_per_ring(RING) + 1), RING)

    def test_batches_cover_every_code_exactly_once(self):
        batches = dsr.plan_code_batches(dsr.codes_per_ring(RING) - 1)
        self.assertEqual(sorted(c for b in batches for c in b), list(range(256)))
        self.assertTrue(all(len(b) <= dsr.codes_per_ring(RING) - 1 for b in batches))

    def test_batches_stride_so_no_ring_holds_a_long_same_nibble_run(self):
        """Slicing 0-110 / 111-221 / 222-255 would put all sixteen codes sharing
        an upper nibble in consecutive slots. On a chip that is silent across
        such a band those slots carry no edges, and a long edgeless run is
        exactly what the sync-gap detector looks for. Striding caps the run at
        16/rings, well under the ~12 consecutive silent codes it would take to
        fake a gap."""
        batches = dsr.plan_code_batches(dsr.codes_per_ring(RING) - 1)
        for batch in batches:
            runs = [len(list(g)) for _, g in itertools.groupby(c >> 4 for c in batch)]
            self.assertLessEqual(max(runs), -(-16 // len(batches)))

    def test_rounds_give_every_code_evenly_spaced_positions(self):
        rounds = dsr.plan_capture_rounds(dsr.codes_per_ring(RING) - 1, rounds=3)
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
        codes = [dsr.ANCHOR_CODE, *range(40)]
        levels, want = _simulate(codes)
        got = dsr.extract_slot_levels(levels, len(codes), RING)
        scale = got.levels[0] / want[0]
        err = np.abs(got.levels / scale - want).max() / np.abs(want).max()
        self.assertLess(err, 0.01)
        self.assertGreaterEqual(got.diagnostics["passes"], 3)

    def test_recovers_the_same_levels_from_a_96k_capture(self):
        """Not every capture device does 48 kHz — the cheap HDMI→USB dongles are
        commonly 96 kHz-only. Every timing constant in the extraction comes from
        the `sr` it is handed, so the rate the device forces on us costs nothing
        as long as it is threaded through instead of assumed."""
        codes = [dsr.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes, sr=96000)
        got = dsr.extract_slot_levels(cap, len(codes), RING, sr=96000)
        scale = got.levels[0] / want[0]
        err = np.abs(got.levels / scale - want).max() / np.abs(want).max()
        self.assertLess(err, 0.01)
        self.assertAlmostEqual(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE, delta=2.0)

    def test_recovers_the_true_nmi_rate_not_the_nominal_one(self):
        """A slot is 192.24 capture samples, not 192: the NMI runs at
        1022727/128 = 7990.05 Hz, not the 8000 Hz it is asked for. Tracking that
        is the difference between a correct grid and a slowly walking one."""
        codes = [dsr.ANCHOR_CODE, *range(40)]
        cap, _ = _simulate(codes)
        got = dsr.extract_slot_levels(cap, len(codes), RING)
        self.assertAlmostEqual(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE, delta=2.0)
        self.assertAlmostEqual(got.diagnostics["ac_coupling_hz"], 12.0, delta=1.0)

    def test_survives_a_stretched_capture_timebase(self):
        """avfoundation drops samples under load, compressing the timebase. The
        grid tracks edge by edge rather than stepping a nominal pitch, so a
        heavily stretched capture still reads the right levels — and says so in
        the implied NMI rate."""
        codes = [dsr.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes, drop_frac=0.12)
        got = dsr.extract_slot_levels(cap, len(codes), RING)
        scale = got.levels[0] / want[0]
        self.assertLess(np.abs(got.levels / scale - want).max() / np.abs(want).max(), 0.02)
        self.assertGreater(got.diagnostics["nmi_rate_implied_hz"], NMI_TRUE * 1.05)

    def test_pass_spread_flags_a_capture_the_grid_could_not_hold(self):
        # Every pass measures the same levels, so disagreement between them is
        # the one symptom that separates a mistracked capture from a real curve.
        codes = [dsr.ANCHOR_CODE, *range(40)]
        cap, _ = _simulate(codes)
        self.assertLess(
            dsr.extract_slot_levels(cap, len(codes), RING).diagnostics["pass_spread_frac"], 0.01
        )

    def test_silent_capture_raises_rather_than_inventing_levels(self):
        with self.assertRaises(dsr.MeasurementError):
            dsr.extract_slot_levels(np.zeros(4 * dsr.CAP_SR), 40, RING)


class RingCaptureGateTest(unittest.TestCase):
    """A recording of the *wrong input* still parses. Reported from the field on
    a Windows rig whose capture auto-picked the on-board microphone: ring 1 read
    "2 passes, L($0F)=-0.00001, pass spread 100.08%" — numbers, from room noise —
    and the run then died on ring 2 with a raw traceback, 30 s in. Whether a
    recording is of the ring is decided per ring, before its levels go anywhere."""

    def test_a_real_capture_passes_the_gate(self):
        codes = [dsr.ANCHOR_CODE, *range(40)]
        cap, want = _simulate(codes)
        got = dsr.read_ring_capture(cap, len(codes), RING)
        scale = got.levels[0] / want[0]
        self.assertLess(np.abs(got.levels / scale - want).max() / np.abs(want).max(), 0.01)

    def test_silence_is_refused_before_anything_is_extracted(self):
        with self.assertRaises(dsr.MeasurementError) as ctx:
            dsr.read_ring_capture(np.zeros(4 * dsr.CAP_SR), 40, RING)
        self.assertIn("silence", str(ctx.exception))

    def test_a_capture_that_is_mostly_noise_is_refused(self):
        """The reported failure verbatim: enough noise on top of the ring that
        only one sync marker survives. It used to escape as a traceback."""
        cap, _ = _simulate([dsr.ANCHOR_CODE, *range(40)], noise=0.1)
        with self.assertRaises(dsr.MeasurementError):
            dsr.read_ring_capture(cap, 41, RING)

    def test_levels_the_passes_disagree_about_are_refused(self):
        """The subtler half: the extraction found a grid and returned levels,
        but the passes contradict each other, so the levels are noise. Hardware
        reads 0.01-0.2% here, so anything near 100% must not reach the table."""
        cap, _ = _simulate([dsr.ANCHOR_CODE, *range(40)])
        bad = dsr.SlotLevels(
            levels=np.full(41, 1e-5),
            per_pass=np.zeros((2, 41)),
            diagnostics={"pass_spread_frac": 1.0008, "pass_spread_p95_frac": 1.0008, "passes": 2},
        )
        with patch.object(dsr, "extract_slot_levels", return_value=bad):
            with self.assertRaises(dsr.MeasurementError) as ctx:
                dsr.read_ring_capture(cap, 41, RING)
        self.assertIn("100.1%", str(ctx.exception))

    def test_a_ring_that_does_not_replay_the_same_levels_is_refused(self):
        """The band the gate used to miss. Two orders of magnitude below "you
        recorded the room", one above healthy: the capture really is the ring,
        the grid tracks, the levels are plausible — and they move between passes,
        so the ladder fitted to them is wrong. A link that read 1.85% here wrote
        a table agreeing with the same chip's on 95 of 256 entries."""
        cap, _ = _simulate([dsr.ANCHOR_CODE, *range(40)])
        wobbly = dsr.SlotLevels(
            levels=np.linspace(-0.5, 0.5, 41),
            per_pass=np.zeros((3, 41)),
            diagnostics={"pass_spread_frac": 0.0185, "pass_spread_p95_frac": 0.0185, "passes": 3},
        )
        with patch.object(dsr, "extract_slot_levels", return_value=wobbly):
            with self.assertRaises(dsr.UnsteadyRingError) as ctx:
                dsr.read_ring_capture(cap, 41, RING)
        msg = str(ctx.exception)
        self.assertIn("1.85%", msg)
        # Distinct from the noise message: this one has to say the ring is real
        # but unsteady, or it reads as "your capture device is wrong" and sends
        # the user to re-cable a rig that is already correct.
        self.assertIn("not replaying the same levels", msg)

    def test_the_unsteady_advice_does_not_send_the_user_to_the_cabling(self):
        """The number this failure is built from reads like the mistracked-capture
        one, so the advice has to invert: the input is right and the ring is
        playing. Pointing at the input instead would have someone re-cable a rig
        that is already correct."""
        msg = dc._unsteady_ring_message(
            "its ring passes disagree by 1.85%",
            {
                "pass_spread_p95_frac": 0.0185,
                "pass_residual_frac": 0.016,
                "pass_gain_span_frac": 0.01,
            },
            None,
        )
        self.assertIn("input is right", msg)
        self.assertNotIn("--audio-device", msg)
        # The one class of cause the tool cannot clear for the user: over a link
        # with no config API it mutes nothing, so anything else up in the
        # machine's mixer lands in the measurement.
        self.assertIn("nothing is muted for you", msg)

    def test_a_drifting_level_is_not_blamed_on_the_mixer(self):
        """Both failure modes reach the same pass_spread_frac, and the advice for
        one is useless for the other: muting another source cannot fix a capture
        whose level was still settling. When rescaling each pass collapses the
        disagreement, the ring replayed faithfully and only the level moved."""
        msg = dc._unsteady_ring_message(
            "its ring passes disagree by 1.30%",
            {
                "pass_spread_p95_frac": 0.013,
                "pass_residual_frac": 0.0004,
                "pass_gain_span_frac": 0.07,
                "pass_gains": [0.978, 1.000, 1.022],
            },
            None,
        )
        self.assertIn("level change, not a different ring", msg)
        self.assertIn("had not settled", msg)
        self.assertNotIn("nothing is muted for you", msg)

    def test_the_two_unsteady_kinds_are_separated_by_the_residual(self):
        """The discriminator itself, on the numbers that motivated it: a per-pass
        gain absorbs a drift and leaves the control's residual behind, but cannot
        absorb laps that genuinely differ."""
        levels = np.linspace(-0.5, 0.5, 41)
        drift = levels * np.array([0.97, 1.0, 1.03])[:, None]
        gains, resid = dsr._pass_gain_decomposition(drift, drift.mean(axis=0), 0.5)
        self.assertAlmostEqual(float(np.max(gains) - np.min(gains)), 0.06, places=3)
        self.assertLess(resid, 1e-9)  # a pure gain change leaves nothing behind

        rng = np.random.default_rng(1)
        noisy = levels + rng.normal(0, 0.01, (3, 41))
        _, resid_noisy = dsr._pass_gain_decomposition(noisy, noisy.mean(axis=0), 0.5)
        self.assertGreater(resid_noisy, 0.005)

    def test_passes_that_agree_exactly_are_not_classified_as_drift(self):
        """A spread of zero leaves nothing to classify. The two call sites used
        to implement the discriminator separately and only one carried this
        guard; the shared predicate keeps it for both."""
        self.assertFalse(
            dsr.is_level_drift({"pass_spread_p95_frac": 0.0, "pass_residual_frac": 0.0})
        )

    def test_a_run_of_marginal_rings_is_called_out_though_each_ring_passed(self):
        """Rings under the trust gate still add up to a table worth re-measuring:
        one run whose rings sat at 0.2-0.44% produced a table disagreeing with the
        same chip measured cleanly by 18% RMS, where two clean runs agree to 0.12%.
        No single ring failed anything, so nothing said so."""
        count, note = dc._marginal_run_summary([0.0003, 0.0031, 0.0044, 0.0002, 0.0025], "SID")
        self.assertEqual(count, 3)
        assert note is not None
        self.assertIn("3/5 rings", note)
        self.assertIn("0.44%", note)
        # It must not read as a failure — the table is written either way.
        self.assertIn("still written", note)

    def test_a_clean_run_says_nothing(self):
        """The summary only earns its place by being absent on a healthy run."""
        self.assertEqual(dc._marginal_run_summary([0.0003, 0.0011, 0.0002], "SID"), (0, None))

    def test_one_glitched_slot_does_not_fail_an_otherwise_perfect_ring(self):
        """The regression. Individual slots glitch: on every refused capture
        examined, 1-6 codes out of ~86 read far off on exactly one pass while the
        rest agreed to 0.004%. Gating on the max over codes turned that single
        transient into a failed run, on both links."""
        levels = np.linspace(-0.5, 0.5, 41)
        pp = np.tile(levels, (3, 1))
        pp[2, 16] += 0.25  # one slot, one pass — a glitch, not an unsteady ring
        scale = float(np.max(np.abs(np.median(pp, axis=0))))
        p95 = float(np.percentile(pp.std(axis=0), 95)) / scale
        worst = float(np.max(pp.std(axis=0))) / scale
        self.assertGreater(worst, dsr.RING_TRUST_MAX_SPREAD)  # the old gate refused it
        self.assertLess(p95, dsr.RING_TRUST_MAX_SPREAD)  # the robust one does not

    def test_a_glitched_slot_is_discarded_rather_than_averaged_in(self):
        """A mean folds the outlier into that code's level and the error survives
        into the ladder, which is what a wrong entry sounds like. With three
        passes the median discards it outright."""
        pp = np.tile(np.linspace(-0.5, 0.5, 41), (3, 1))
        pp[2, 16] += 0.25
        self.assertAlmostEqual(float(np.median(pp, axis=0)[16]), float(pp[0, 16]), places=9)
        self.assertGreater(abs(float(pp.mean(axis=0)[16]) - float(pp[0, 16])), 0.08)

    def test_a_ring_where_every_code_moves_is_still_refused(self):
        """The robust statistic must not be a way through for a ring that really
        is not replaying: that moves every code, so it moves the 95th percentile
        too."""
        rng = np.random.default_rng(5)
        levels = np.linspace(-0.5, 0.5, 41)
        pp = levels + rng.normal(0, 0.02, (3, 41))
        scale = float(np.max(np.abs(np.median(pp, axis=0))))
        self.assertGreater(
            float(np.percentile(pp.std(axis=0), 95)) / scale, dsr.RING_TRUST_MAX_SPREAD
        )

    def test_the_refused_capture_is_saved_for_diagnosis(self):
        """A refused capture is the only evidence for the refusal, and repeating
        it costs a hardware run that may not reproduce the fault. Twice a
        calibration has been rejected on a number that could not be gone back to.
        The codes and rate travel with the waveform because extraction needs them."""
        cap = np.linspace(-1.0, 1.0, 512)
        fmt = dcap.CaptureFormat(channels=2, samplerate=48000)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"C64CAST_DATA_DIR": tmp}):
                path = dc._save_unusable_capture(
                    cap, [dsr.ANCHOR_CODE, 1, 2], fmt, "tr-abc", {"pass_spread_frac": 0.013}
                )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.exists())
            with np.load(path) as z:
                np.testing.assert_allclose(z["capture"], cap, atol=1e-6)
                self.assertEqual(z["codes"].tolist(), [dsr.ANCHOR_CODE, 1, 2])
                self.assertEqual(int(z["samplerate"]), 48000)
                self.assertEqual(json.loads(str(z["diagnostics"]))["pass_spread_frac"], 0.013)

    def test_saving_the_capture_never_masks_the_real_failure(self):
        """It is a diagnosis aid. A full disk or a read-only data dir must still
        surface the measurement failure, not replace it with an OSError."""
        fmt = dcap.CaptureFormat(channels=2, samplerate=48000)
        with patch.object(dc.paths, "unusable_capture_dir", side_effect=OSError("read-only")):
            self.assertIsNone(dc._save_unusable_capture(np.zeros(8), [1], fmt, "k", {}))

    def test_the_healthy_band_still_passes_untouched(self):
        """The gate moved by 20x, so the case it must not start refusing is the
        ordinary one: hardware reads 0.01-0.2%."""
        cap, _ = _simulate([dsr.ANCHOR_CODE, *range(40)])
        fine = dsr.SlotLevels(
            levels=np.linspace(-0.5, 0.5, 41),
            per_pass=np.zeros((3, 41)),
            diagnostics={"pass_spread_frac": 0.002, "pass_spread_p95_frac": 0.002, "passes": 3},
        )
        with patch.object(dsr, "extract_slot_levels", return_value=fine):
            self.assertIs(dsr.read_ring_capture(cap, 41, RING), fine)

    def test_the_failure_names_the_device_and_the_alternatives(self):
        """Every reason above reads like a bug in the measurement; it is almost
        always the rig. The message the user sees has to say which input was
        recorded from, and which ones they could pick instead."""
        fake = _FakeSD([_dev("Microphone (2- Realtek(R) Audio", 2), _dev("Cam Link 4K", 2)])
        with patch.dict("sys.modules", {"sounddevice": fake}):
            msg = dcap.capture_fault_message(0, "it recorded silence", 1e-5)
        self.assertIn("Realtek", msg)
        self.assertIn("microphone", msg)
        self.assertIn("Cam Link 4K", msg)
        self.assertIn("--audio-device", msg)


class MergeMeasurementsTest(unittest.TestCase):
    def test_rings_are_rescaled_onto_the_common_anchor(self):
        # Two rings whose capture gain differs by 2x must still merge to one
        # consistent set of levels — the anchor code is what ties them together.
        a = dsr.SlotLevels(np.array([1.0, 0.5, 0.25]), np.zeros((2, 3)), {})
        b = dsr.SlotLevels(np.array([2.0, 1.0, -1.0]), np.zeros((2, 3)), {})
        raw, metrics = dsr.merge_measurements([([1, 2], a), ([3, 4], b)])
        self.assertEqual([c for c, _ in raw], [1, 2, 3, 4])
        vals = [v for _, v in raw]
        # Half of ring a's anchor and half of ring b's must land on one level.
        self.assertAlmostEqual(vals[0], vals[2])
        self.assertEqual(metrics["rings"], 2)

    def test_repeated_codes_are_averaged_and_their_spread_reported(self):
        a = dsr.SlotLevels(np.array([1.0, 0.4]), np.zeros((2, 2)), {})
        b = dsr.SlotLevels(np.array([1.0, 0.6]), np.zeros((2, 2)), {})
        raw, metrics = dsr.merge_measurements([([7], a), ([7], b)])
        self.assertAlmostEqual(raw[0][1], 0.5)
        self.assertAlmostEqual(metrics["context_spread_frac"], 0.2, places=4)


class BuildSidtableTest(unittest.TestCase):
    def test_reconstruct_from_synthetic_signed_curve(self):
        sidtable, metrics = dsr.build_sidtable_from_levels(_signed_levels())
        assert sidtable is not None
        self.assertEqual(len(sidtable), 256)
        self.assertTrue(all(0 <= v <= 255 for v in sidtable))
        self.assertIn("signed_span", metrics)
        lo, hi = metrics["signed_span"]
        self.assertLess(lo, hi)
        self.assertGreater(metrics["ladder_bits"], 4.0)


class Volume0SelfTestTest(DataDirIsolated):
    """Codes $h0 set the master volume nibble to 0, so their output level is
    $00's whatever the upper nibble does — L($h0) must measure zero. That holds
    with no model assumptions, which makes it the one check that can tell a
    sound measurement from one whose numbers are not output levels at all."""

    def test_consistent_measurement_passes_and_yields_a_table(self):
        sidtable, metrics = dsr.build_sidtable_from_levels(_signed_levels())
        self.assertIsNotNone(sidtable)
        self.assertAlmostEqual(metrics["volume0_selftest_worst"], 0.0, places=6)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_inconsistent_measurement_is_rejected_with_no_table(self):
        # Master-volume-0 codes coming back with an upper-nibble-dependent level
        # is impossible for any real set of levels.
        raw = [(c, v + 0.02 * (c >> 4) if (c & 0x0F) == 0 else v) for c, v in _signed_levels()]
        sidtable, metrics = dsr.build_sidtable_from_levels(raw)
        self.assertIsNone(sidtable)
        self.assertGreater(metrics["volume0_selftest_worst"], dsr.SELFTEST_TOLERANCE)
        # Still fully diagnosable: the metrics survive the rejection.
        self.assertIn("signed_span", metrics)
        self.assertEqual(len(metrics["volume0_selftest"]), 16)

    def test_rejection_writes_no_sidtable_and_reads_back_as_no_calibration(self):
        cfg = _u64_cfg()
        key = dcs.resolve_calibration_key(cfg)
        raw = [(c, v + 0.3 if (c & 0x0F) == 0 else v) for c, v in _signed_levels()]
        sidtable, metrics = dsr.build_sidtable_from_levels(raw)
        self.assertIsNone(sidtable)
        path = dcs.save_calibration(
            cfg,
            dcs.CalibrationDocument(
                key, {"default": dcs.CalibrationResult(sidtable, metrics, None, raw)}, {}
            ),
        )
        doc = json.loads(path.read_text())
        entry = doc["sids"]["default"]
        self.assertNotIn("sidtable", entry)
        # The raw levels are kept so the failure can be investigated offline.
        self.assertEqual(len(entry["raw_signed_levels"]), 256)
        self.assertIsNone(dcs.load_calibrated_table(cfg))


class LadderMetricsTest(unittest.TestCase):
    def test_quality_is_independent_of_capture_gain(self):
        """The metrics this replaced counted level steps exceeding the capture
        noise floor, so a quieter rig scored more "effective bits" on identical
        hardware — it once rated a chip degraded to ~4 bits above a working one.
        Scaling the whole capture must not change the ladder's quality figures."""
        _, m1 = dsr.build_sidtable_from_levels(_signed_levels())
        _, m2 = dsr.build_sidtable_from_levels([(c, v * 10.0) for c, v in _signed_levels()])
        for k in ("ladder_bits", "worst_gap_frac", "ladder_rms_err_frac"):
            self.assertAlmostEqual(m1[k], m2[k], places=3, msg=k)

    def test_worst_gap_position_is_reported_relative_to_silence(self):
        sidtable, metrics = dsr.build_sidtable_from_levels(_signed_levels())
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
            return dcap.resolve_capture_format(dev)

    def test_prefers_stereo_at_the_nominal_rate(self):
        fake = _FakeSD([_dev("Cam Link 4K", 2)])
        self.assertEqual(self._run(fake), (2, dsr.CAP_SR))

    def test_falls_back_to_mono_on_a_mono_only_device(self):
        fake = _FakeSD([_dev("Mono Capture", 1)])
        self.assertEqual(self._run(fake), (1, dsr.CAP_SR))
        # 2 is never even probed on a device that reports a single channel.
        self.assertEqual(fake.checked, [(0, 1, dsr.CAP_SR)])

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
        self.assertEqual(self._run(fake), (2, dsr.CAP_SR))

    def test_output_only_device_raises_actionable_error(self):
        fake = _FakeSD([_dev("Speakers", 0), _dev("Cam Link 4K", 2)])
        with self.assertRaises(dcap.CaptureUnavailableError) as ctx:
            self._run(fake, dev=0)
        # The message must name a device the user can actually pass instead.
        self.assertIn("Cam Link 4K", str(ctx.exception))
        self.assertIn("--audio-device", str(ctx.exception))

    def test_no_workable_format_raises_capture_unavailable(self):
        fake = _FakeSD([_dev("Hostile Capture", 2)], accept={0: set()})
        with self.assertRaises(dcap.CaptureUnavailableError):
            self._run(fake)


class FindCaptureDeviceTest(unittest.TestCase):
    """Only "cam link" used to be recognized, so every other rig fell through to
    the system default input — on Windows the on-board microphone, which records
    room noise for the whole run and measures like a dead chip."""

    def _run(self, fake, preferred=None):
        with patch.dict("sys.modules", {"sounddevice": fake}):
            return dcap.find_capture_device(preferred)

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
        self.assertFalse(dcap.looks_like_capture_input("Line In"))
        self.assertTrue(dcap.looks_like_capture_input("Cam Link 4K"))


if __name__ == "__main__":
    unittest.main()
