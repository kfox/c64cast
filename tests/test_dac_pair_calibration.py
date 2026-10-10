"""--calibrate-dac's two-SID pair phase (dac_calibration._measure_pair) and
the pair record the calibration file carries."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import numpy as np
from _fakes import FakeAPI

from c64cast.app.config import Config
from c64cast.audio import dac_calibration as dc
from c64cast.audio import dac_calibration_store as dcs
from c64cast.audio import dac_pair as dp
from c64cast.audio import dac_slot_ring as dsr
from c64cast.audio.dac_capture_device import CaptureFormat
from c64cast.sid.asid_sidmap import CAT_ADDRESSING, CAT_SOCKETS
from c64cast.sid.sid_panning import CAT_MIXER
from c64cast.sid.sid_volume import VOL_OFF, VOL_UNITY

FINE = 0xD420
# The true levels the fakes play back: a coarse Mahoney-ish ladder with volume
# 0 silent, and a fine chip at 1/16 of the coarse $0F per full volume step.
COARSE_TRUE = np.array([(c & 0x0F) / 15.0 * (1.0 - 1.6 * (c >> 4) / 15) * 0.4 for c in range(256)])
FINE_TRUE = np.arange(16) / 15.0 * COARSE_TRUE[dsr.ANCHOR_CODE] / 16


def _u64() -> FakeAPI:
    api = FakeAPI.ultimate()
    api.config_store[CAT_ADDRESSING] = {
        "SID Socket 1 Address": "$D400",
        "SID Socket 2 Address": "$D420",
        "UltiSID 1 Address": "$D400",
        "UltiSID 2 Address": "$D420",
    }
    api.config_store[CAT_SOCKETS] = {
        "SID Socket 1": "Enabled",
        "SID Socket 2": "Enabled",
        "SID Detected Socket 1": "6581",
        "SID Detected Socket 2": "6581",
    }
    api.config_store[CAT_MIXER] = {
        "Vol Socket 1": "-3 dB",
        "Vol Socket 2": VOL_UNITY,
        "Vol UltiSid 1": VOL_UNITY,
        "Vol UltiSid 2": VOL_OFF,
    }
    return api


def _ctx(api: FakeAPI) -> dc._RunContext:
    return dc._RunContext(
        be=cast(Any, api),
        key="test-key",
        device=0,
        fmt=CaptureFormat(channels=2, samplerate=48000),
        secs=4.5,
        settle=0.4,
        log_fn=lambda _m: None,
    )


def _played(api: FakeAPI, codes: list[int]) -> dsr.SlotLevels:
    """What a capture of the slot ring `codes` reads through the pair routine:
    each index looked up in the tables the run last uploaded, both chips summed,
    relative to index 0."""
    ct = api.mem_files[f"{dp.COARSE_TABLE_ADDR:04X}"]
    ft = api.mem_files[f"{dp.FINE_TABLE_ADDR:04X}"]

    def level(i: int) -> float:
        return float(COARSE_TRUE[ct[i]] + FINE_TRUE[ft[i]])

    levels = np.array([level(i) - level(0) for i in codes])
    return dsr.SlotLevels(levels, levels[None, :], {"pass_spread_p95_frac": 0.0001})


class FineLadderTest(unittest.TestCase):
    def test_reads_each_fine_code_against_the_coarse_anchor(self):
        api = FakeAPI()
        with patch.object(dc, "_capture_ring", side_effect=lambda _c, codes: _played(api, codes)):
            fine = dc._measure_fine_ladder(_ctx(api))
        np.testing.assert_allclose(fine, FINE_TRUE / COARSE_TRUE[dsr.ANCHOR_CODE])

    def test_rotates_the_slot_order_between_rings(self):
        api = FakeAPI()
        seen: list[list[int]] = []

        def capture(_ctx, codes):
            seen.append(codes)
            return _played(api, codes)

        with patch.object(dc, "_capture_ring", side_effect=capture):
            dc._measure_fine_ladder(_ctx(api))
        self.assertEqual(len(seen), dc._FINE_RINGS)
        self.assertTrue(all(codes[0] == 1 for codes in seen), "the anchor leads every ring")
        self.assertEqual(len({tuple(c) for c in seen}), dc._FINE_RINGS)
        self.assertLessEqual(max(len(c) for c in seen), dsr.codes_per_ring(0x2000))


class CheckFineLevelsTest(unittest.TestCase):
    def test_accepts_a_rising_ladder_in_range(self):
        dc._check_fine_levels(np.arange(16) * 0.004, 1.0)

    def test_refuses_a_silent_or_a_coarse_fine_chip(self):
        for top in (0.0001, 0.9):
            with self.subTest(top=top), self.assertRaises(dsr.MeasurementError):
                dc._check_fine_levels(np.linspace(0, top, 16), 1.0)

    def test_refuses_a_ladder_that_falls(self):
        fine = np.arange(16) * 0.004
        fine[8] = 0.0
        with self.assertRaises(dsr.MeasurementError):
            dc._check_fine_levels(fine, 1.0)


class PairMixerTest(unittest.TestCase):
    def test_coarse_at_unity_fine_at_the_pair_level_the_rest_off(self):
        api = _u64()
        present = set(api.config_store[CAT_MIXER])
        self.assertEqual(dc._pair_mixer(cast(Any, api), FINE, present), ("socket1", "socket2"))
        mixer = api.config_store[CAT_MIXER]
        self.assertEqual(mixer["Vol Socket 1"], VOL_UNITY)
        self.assertEqual(mixer["Vol Socket 2"], f"{dp.FINE_GAIN_DB} dB")
        self.assertEqual(mixer["Vol UltiSid 1"], VOL_OFF)
        self.assertEqual(mixer["Vol UltiSid 2"], VOL_OFF)

    def test_refuses_an_address_nothing_answers(self):
        api = _u64()
        with self.assertRaisesRegex(dsr.MeasurementError, r"\$D500"):
            dc._pair_mixer(cast(Any, api), 0xD500, set(api.config_store[CAT_MIXER]))


class MeasurePairTest(unittest.TestCase):
    def _run(self, api: FakeAPI, *, sid_config: bool = True) -> dict[str, Any]:
        raw = [(c, float(COARSE_TRUE[c])) for c in range(256)]
        sidtable, metrics = dsr.build_sidtable_from_levels(raw)
        st = MagicMock()
        with (
            patch.object(dc, "_measure_one", return_value=(sidtable, metrics, raw)),
            patch.object(dc, "_capture_ring", side_effect=lambda _c, codes: _played(api, codes)),
            patch.object(dc.time, "sleep"),
        ):
            record = dc._measure_pair(_ctx(api), st, FINE, sid_config)
        st._enable_mahoney_env.assert_any_call(FINE)
        return record

    def test_the_record_holds_a_pair_that_beats_one_chip(self):
        api = _u64()
        record = self._run(api)
        self.assertEqual(record["fine_base"], "$D420")
        self.assertEqual((record["coarse_source"], record["fine_source"]), ("socket1", "socket2"))
        self.assertEqual(record["fine_gain_db"], dp.FINE_GAIN_DB)
        m = record["metrics"]
        self.assertGreater(m["ladder_bits"], m["single_chip_ladder_bits"])
        dp.DacPair(FINE, bytes(record["coarse_table"]), bytes(record["fine_table"]))

    def test_installs_the_pair_routine_with_the_timer_stopped(self):
        api = _u64()
        self._run(api)
        ops = [op for op in api.ops if op[0] in ("write_regs", "write_memory_file")]
        routine_at = next(
            i for i, op in enumerate(ops) if op[0] == "write_memory_file" and op[1] == "C020"
        )
        self.assertEqual(ops[routine_at][2], dp.pair_nmi_routine(FINE))
        stops = [i for i, op in enumerate(ops) if op[:2] == ("write_regs", "DD0D")]
        self.assertTrue(any(i < routine_at for i in stops), "the timer must stop first")

    def test_puts_the_mixer_back(self):
        api = _u64()
        before = dict(api.config_store[CAT_MIXER])
        self._run(api)
        self.assertEqual(api.config_store[CAT_MIXER], before)

    def test_a_link_without_the_sid_config_surface_measures_as_it_finds_it(self):
        api = FakeAPI()
        record = self._run(api, sid_config=False)
        self.assertIsNone(record["fine_source"])
        self.assertIsNone(record["fine_gain_db"])
        self.assertEqual(api.config_puts, [])

    def test_a_first_sid_that_fails_its_self_test_folds_no_pair(self):
        api = _u64()
        with (
            patch.object(dc, "_measure_one", return_value=(None, {}, [])),
            patch.object(dc.time, "sleep"),
            self.assertRaises(dsr.MeasurementError),
        ):
            dc._measure_pair(_ctx(api), MagicMock(), FINE, True)


class RunCalibrationPairTest(unittest.TestCase):
    def _run(self, second_sid: int | None) -> tuple[MagicMock, MagicMock]:
        api = FakeAPI()
        save = MagicMock(return_value=Path("cal.json"))
        pair = MagicMock(return_value={"fine_base": "$D420"})
        with (
            patch.object(dc, "_require_sounddevice"),
            patch.object(dc, "_bring_up_dac_env"),
            patch.object(dc, "_identify_d400_chip", return_value=None),
            patch.object(dc, "_paint_status_line"),
            patch.object(dc, "_open_capture", return_value=(0, CaptureFormat(2, 48000))),
            patch.object(dc, "_measure_one", return_value=([0] * 256, {}, [])),
            patch.object(dc, "_measure_pair", pair),
            patch.object(dc, "_silence_and_reset"),
            patch.object(dc, "save_calibration", save),
            patch.object(dc, "_report_run"),
            patch.object(dc, "resolve_calibration_key", return_value="test-key"),
            patch.object(dc, "_device_provenance", return_value={}),
        ):
            dc.run_calibration(cast(Any, api), Config(), second_sid=second_sid)
        return save, pair

    def test_a_second_sid_adds_the_pair_record(self):
        save, pair = self._run(FINE)
        self.assertEqual(pair.call_args.args[2], FINE)
        self.assertEqual(save.call_args.args[1].pair, {"fine_base": "$D420"})

    def test_without_one_there_is_no_pair_phase(self):
        save, pair = self._run(None)
        pair.assert_not_called()
        self.assertIsNone(save.call_args.args[1].pair)


class PairRecordStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _save(self, pair: dict[str, Any] | None) -> Path:
        cfg = Config()
        doc = dcs.CalibrationDocument(
            key="test-key",
            entries={"default": dcs.CalibrationResult([0] * 256, {})},
            device={},
            pair=pair,
        )
        return dcs.save_calibration(cfg, doc)

    def test_round_trips(self):
        pair = {"fine_base": "$D420", "coarse_table": [1] * 256, "fine_table": [2] * 256}
        self.assertEqual(dcs.load_pair_record(self._save(pair)), pair)

    def test_a_file_with_no_pair_has_none(self):
        path = self._save(None)
        self.assertNotIn("pair", json.loads(path.read_text()))
        self.assertIsNone(dcs.load_pair_record(path))

    def test_a_malformed_pair_is_none(self):
        for bad in (
            {"fine_base": "$D420", "coarse_table": [1] * 255, "fine_table": [2] * 256},
            {"fine_base": "$D420", "coarse_table": [256] * 256, "fine_table": [2] * 256},
            {"coarse_table": [1] * 256, "fine_table": [2] * 256},
        ):
            with self.subTest(bad=list(bad)):
                self.assertIsNone(dcs.load_pair_record(self._save(bad)))

    def test_a_missing_file_is_none(self):
        self.assertIsNone(dcs.load_pair_record(Path(self._tmp.name) / "absent.json"))


if __name__ == "__main__":
    unittest.main()
