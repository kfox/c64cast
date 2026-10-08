"""DAC calibration on an ARMSID: the calibrating run records the model the
chip was measured in, and a run playing through that table puts the chip back
into it (c64cast/audio/dac_curve_resolve.py, dac_calibration.py)."""

# FakeAPI duck-types C64Backend rather than subclassing it.
# pyright: reportArgumentType=false
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import FakeAPI
from test_armsid import CAT_ARMSID1, ArmsidAPI, _NoSettle

from c64cast.app.config import Config
from c64cast.audio import dac_calibration, dac_calibration_store, dac_curve_resolve
from c64cast.audio.dac_calibration_store import D400_UNKNOWN, CalibrationResult
from c64cast.sid import armsid
from c64cast.sid.sid_hw_config import restore_sid_config


def _cfg_with_calibration(detected: str | None, socket: str = "1") -> Config:
    cfg = Config()
    cfg.hardware.backend = "ultimate"
    path = Path(tempfile.mkdtemp()) / "cal.json"
    entry: dict[str, object] = {"sidtable": list(range(256))}
    if detected is not None:
        entry["detected"] = detected
    path.write_text(json.dumps({"schema": 2, "sids": {socket: entry}, "d400_socket": 1}))
    cfg.audio.dac_calibration_profile = str(path)
    return cfg


class ProvisionModelTest(_NoSettle):
    def _provision(self, api, cfg):
        cfg.audio.dac_curve = "calibrated"
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=api)
        return dac_curve_resolve.provision_calibrated_chip_model(api, resolved)

    def test_switches_to_the_measured_model_and_restores(self):
        api = ArmsidAPI(left="8580")
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO"):
            restore = self._provision(api, _cfg_with_calibration("ARMSID 6581"))
        self.assertEqual(api.left.model, "6581")
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, "socket1"): "8580"})
        assert restore is not None
        restore_sid_config(api, restore)
        self.assertEqual(api.left.model, "8580")

    def test_already_in_the_measured_model_changes_nothing(self):
        api = ArmsidAPI(left="6581")
        self.assertIsNone(self._provision(api, _cfg_with_calibration("ARM2SID 6581")))
        self.assertEqual(api.config_puts, [])

    def test_a_fixed_chip_calibration_is_left_alone(self):
        api = ArmsidAPI(left="8580")
        self.assertIsNone(self._provision(api, _cfg_with_calibration("6581")))
        self.assertEqual(api.left.model, "8580")

    def test_a_curve_that_is_not_calibrated_is_left_alone(self):
        api = ArmsidAPI(left="8580")
        resolved = dac_curve_resolve.DacCurve("mahoney_ultisid", None, (1, "ARMSID 6581"))
        self.assertIsNone(dac_curve_resolve.provision_calibrated_chip_model(api, resolved))
        self.assertEqual(api.left.model, "8580")

    def test_a_failed_switch_does_not_raise_and_still_restores(self):
        api = ArmsidAPI(left="8580")

        def refuse(*_a, **_k):
            raise OSError("REST unreachable")

        api.put_config_item = refuse  # type: ignore[method-assign]
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING"):
            restore = self._provision(api, _cfg_with_calibration("ARMSID 6581"))
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, "socket1"): "8580"})
        self.assertEqual(api.left.model, "8580")

    def test_a_socket_entry_on_a_link_without_sid_config_is_left_alone(self):
        # The socket arm switches through the firmware's config item, which a
        # link without the SID config surface does not have.
        api = ArmsidAPI(left="8580")
        api.profile = FakeAPI().profile
        resolved = dac_curve_resolve.DacCurve("calibrated:k", bytes(256), (1, "ARMSID 6581"))
        self.assertIsNone(dac_curve_resolve.provision_calibrated_chip_model(api, resolved))
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_puts, [])

    def test_a_chip_whose_model_is_unknown_is_left_alone(self):
        api = ArmsidAPI(left="??")  # a model reply that is neither 6581 nor 8580
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING"):
            self.assertIsNone(self._provision(api, _cfg_with_calibration("ARMSID 6581")))
        self.assertEqual(api.config_puts, [])

    def test_a_failed_later_socket_map_read_still_switches_the_chosen_socket(self):
        # Resolution reads the map and picks socket 1's table. A second read of
        # the map that fails would fall back to the file's recorded d400_socket
        # and name socket 2, so the chip to switch has to come from resolution.
        api = ArmsidAPI(kind="ARMSID", left="8580")
        path = Path(tempfile.mkdtemp()) / "cal.json"
        sids = {
            "1": {"sidtable": list(range(256)), "detected": "ARMSID 6581"},
            "2": {"sidtable": [0] * 256, "detected": "ARMSID 8580"},
        }
        path.write_text(json.dumps({"schema": 2, "sids": sids, "d400_socket": 2}))
        cfg = Config()
        cfg.hardware.backend = "ultimate"
        cfg.audio.dac_curve = "calibrated"
        cfg.audio.dac_calibration_profile = str(path)
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=api)
        with (
            mock.patch.object(dac_calibration_store, "d400_owner", return_value=D400_UNKNOWN),
            self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO") as cm,
        ):
            restore = dac_curve_resolve.provision_calibrated_chip_model(api, resolved)
        self.assertEqual(api.left.model, "6581", cm.output)
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, "socket1"): "8580"})

    def test_a_chip_that_is_no_longer_an_armsid_is_warned_about(self):
        api = ArmsidAPI(left="8580")
        api.read_memory = lambda *a, **k: None  # type: ignore[method-assign]
        with (
            self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING"),
            self.assertLogs("c64cast.sid.armsid", "INFO"),
        ):
            self.assertIsNone(self._provision(api, _cfg_with_calibration("ARMSID 6581")))


class AutoPlaysArmsidTableTest(_NoSettle):
    """`auto` plays a table measured on an ARMSID like any other calibration,
    in the model it was measured in."""

    def test_auto_plays_the_table_for_every_armsid_label(self):
        for detected in ("ARM2SID 6581", "ARMSID 8580", "ARMSID", "ARMSID ?"):
            with (
                self.subTest(detected=detected),
                self.assertNoLogs("c64cast.audio.dac_curve_resolve", "WARNING"),
            ):
                resolved = dac_curve_resolve.resolve_dac_curve_for_backend(
                    _cfg_with_calibration(detected)
                )
            self.assertTrue(resolved.label.startswith("calibrated:"), resolved.label)
            self.assertEqual(resolved.table, bytes(range(256)))
            self.assertEqual(resolved.measured, (1, detected))

    def test_a_live_run_reads_the_socket_map_once(self):
        # The table and the chip provisioning switches must come from one
        # entry: a second socket-map read that failed would fall back to the
        # file's recorded mapping and could pair this table with another
        # socket's chip.
        api = ArmsidAPI(left="6581")
        reads: list[str] = []
        real = api.get_config_category

        def counting(category, *args, **kwargs):
            reads.append(category)
            return real(category, *args, **kwargs)

        api.get_config_category = counting  # type: ignore[method-assign]
        got = dac_curve_resolve.resolve_dac_curve_for_backend(
            _cfg_with_calibration("ARMSID 6581"), be=api
        )
        self.assertEqual(
            got,
            dac_curve_resolve.DacCurve(
                "calibrated:cal", bytes(range(256)), (1, "ARMSID 6581"), key="cal"
            ),
        )
        self.assertEqual(len(reads), 2, reads)

    def test_auto_switches_the_chip_into_its_measured_model_and_restores(self):
        api = ArmsidAPI(left="8580")
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(
            _cfg_with_calibration("ARMSID 6581"), be=api
        )
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO"):
            restore = dac_curve_resolve.provision_calibrated_chip_model(api, resolved)
        self.assertEqual(api.left.model, "6581")
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, "socket1"): "8580"})
        assert restore is not None
        restore_sid_config(api, restore)
        self.assertEqual(api.left.model, "8580")

    def test_calibrated_plays_the_same_table(self):
        cfg = _cfg_with_calibration("ARM2SID 6581")
        cfg.audio.dac_curve = "calibrated"
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg)
        self.assertTrue(resolved.label.startswith("calibrated:"), resolved.label)
        self.assertEqual(resolved.table, bytes(range(256)))

    def test_auto_still_plays_a_real_chips_table(self):
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(_cfg_with_calibration("6581"))
        self.assertTrue(resolved.label.startswith("calibrated:"), resolved.label)
        self.assertEqual(resolved.table, bytes(range(256)))

    def test_calibration_report_does_not_send_the_user_to_calibrated(self):
        result = CalibrationResult(
            sidtable=[0] * 256,
            metrics={
                "ladder_bits": 5.73,
                "signed_span": [-0.389, 0.252],
                "worst_gap_frac": 0.034,
                "worst_gap_from_zero_frac": 0.1,
            },
            detected="ARM2SID 6581",
        )
        lines: list[str] = []
        dac_calibration._report_run({"1": result}, Path("cal.json"), lines.append)
        self.assertFalse(any("calibrated" in line for line in lines), lines)


class RecordModelTest(_NoSettle):
    def test_the_measured_socket_is_labeled_with_its_model(self):
        api = ArmsidAPI(left="6581")
        self.assertEqual(
            dac_calibration._populated_sockets(api, lambda _msg: None), [(1, "ARM2SID 6581")]
        )


class _Calibration:
    """What one patched-out run_calibration did: the entries it would save,
    each measurement's model at the time it ran, its log lines and the
    streamer stand-in."""

    def __init__(self) -> None:
        self.entries: dict[str, CalibrationResult] = {}
        self.models: list[str] = []
        self.lines: list[str] = []
        self.streamer = mock.Mock()


def _calibrate(api, *, backend="ultimate", measure=None) -> _Calibration:
    """run_calibration with the capture, the reset and the file write patched
    out, so socket detection, the ARMSID model switch and its restore run as
    they would on the machine. `measure` replaces each measurement."""
    run = _Calibration()

    def save(_cfg, doc):
        run.entries = doc.entries
        return Path("cal.json")

    def measure_one(_ctx, _label):
        run.models.append(getattr(getattr(api, "left", None), "model", ""))
        if measure is not None:
            measure()
        return [0] * 256, {"ladder_bits": 6.5}, []

    cfg = Config()
    cfg.hardware.backend = backend
    with (
        mock.patch.object(dac_calibration, "_require_sounddevice"),
        mock.patch.object(dac_calibration, "_bring_up_dac_env", return_value=run.streamer),
        mock.patch.object(dac_calibration, "_open_capture", return_value=(0, None)),
        mock.patch.object(dac_calibration, "_measure_one", side_effect=measure_one),
        mock.patch.object(dac_calibration, "_silence_and_reset"),
        mock.patch.object(dac_calibration, "save_calibration", side_effect=save),
        mock.patch.object(dac_calibration, "_report_run"),
        mock.patch.object(dac_calibration, "resolve_calibration_key", return_value="k"),
        mock.patch.object(dac_calibration, "_device_provenance", return_value={}),
        mock.patch.object(dac_calibration.time, "sleep"),
    ):
        dac_calibration.run_calibration(api, cfg, log_fn=run.lines.append)
    return run


def _run_unisolated(api) -> dict[str, CalibrationResult]:
    """run_calibration on a link without socket detection; the entries it
    would save."""
    return _calibrate(api, backend="teensyrom").entries


def _no_socket_detection(api):
    api.profile = FakeAPI().profile  # no SID config surface, like a TeensyROM+
    return api


class CalibrateIn6581Test(_NoSettle):
    """--calibrate-dac measures an ARMSID in 6581 mode and puts the chip's own
    model back when it ends."""

    def test_a_socket_is_measured_in_6581_and_restored(self):
        api = ArmsidAPI(kind="ARM2SID", left="8580")
        run = _calibrate(api)
        self.assertEqual(run.models, ["6581"])
        self.assertEqual(run.entries["1"].detected, "ARM2SID 6581")
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_store[CAT_ARMSID1][armsid.ITEM_ARMSID_MODE], "8580")
        self.assertTrue(any("switched the ARM2SID 8580 to 6581" in m for m in run.lines))

    def test_ctrl_c_mid_measurement_still_restores_the_model(self):
        api = ArmsidAPI(kind="ARMSID", left="8580")

        def interrupt():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            _calibrate(api, measure=interrupt)
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_store[CAT_ARMSID1][armsid.ITEM_ARMSID_MODE], "8580")

    def test_a_chip_already_in_6581_is_not_touched(self):
        api = ArmsidAPI(kind="ARMSID", left="6581")
        run = _calibrate(api)
        self.assertEqual(run.entries["1"].detected, "ARMSID 6581")
        modes = [p[2] for p in api.config_puts if p[:2] == (CAT_ARMSID1, armsid.ITEM_ARMSID_MODE)]
        self.assertTrue(all(m == "6581" for m in modes), api.config_puts)
        self.assertEqual(run.models, ["6581"])
        self.assertFalse(any("switched" in m for m in run.lines), run.lines)

    def test_a_failed_switch_records_the_model_actually_measured(self):
        api = ArmsidAPI(kind="ARMSID", left="8580")
        with (
            mock.patch.object(armsid, "set_socket_model", side_effect=OSError("link down")),
            self.assertLogs("c64cast.audio.dac_calibration", "DEBUG"),
        ):
            run = _calibrate(api)
        self.assertEqual(run.models, ["8580"])
        self.assertEqual(run.entries["1"].detected, "ARMSID 8580")
        self.assertTrue(any("could not switch the ARMSID 8580" in m for m in run.lines))

    def test_d400_is_switched_through_its_registers_and_restored(self):
        api = _no_socket_detection(ArmsidAPI(kind="ARMSID", left="8580"))
        run = _calibrate(api, backend="teensyrom")
        self.assertEqual(run.models, ["6581"])
        self.assertEqual(run.entries["default"].detected, "ARMSID 6581")
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_puts, [])
        # Re-parked on the reconfigured chip before the measurement.
        run.streamer._enable_mahoney_env.assert_called_once_with()

    def test_ctrl_c_at_d400_still_restores_the_model(self):
        api = _no_socket_detection(ArmsidAPI(kind="ARMSID", left="8580"))

        def interrupt():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            _calibrate(api, backend="teensyrom", measure=interrupt)
        self.assertEqual(api.left.model, "8580")

    def test_playback_puts_the_chip_back_into_the_measured_6581(self):
        api = ArmsidAPI(kind="ARM2SID", left="8580")
        detected = _calibrate(api).entries["1"].detected
        self.assertEqual(api.left.model, "8580")
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(
            _cfg_with_calibration(detected), be=api
        )
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO"):
            restore = dac_curve_resolve.provision_calibrated_chip_model(api, resolved)
        self.assertEqual(api.left.model, "6581")
        assert restore is not None
        restore_sid_config(api, restore)
        self.assertEqual(api.left.model, "8580")


class IdentifyWithoutSocketDetectionTest(_NoSettle):
    """A run that cannot detect sockets still asks the chip at $D400 whether it
    is an ARMSID, so a run playing the table puts the chip back into its
    measured model on every link (#592)."""

    def _no_socket_detection(self, api):
        return _no_socket_detection(api)

    def test_an_armsid_at_d400_is_recorded(self):
        api = self._no_socket_detection(ArmsidAPI(kind="ARMSID", left="6581"))
        entries = _run_unisolated(api)
        self.assertEqual(list(entries), ["default"])
        self.assertEqual(entries["default"].detected, "ARMSID 6581")

    def test_an_arm2sid_at_d400_is_recorded_as_its_left_channel(self):
        api = self._no_socket_detection(ArmsidAPI(kind="ARM2SID", left="8580"))
        self.assertEqual(_run_unisolated(api)["default"].detected, "ARM2SID 6581")

    def test_an_ordinary_sid_stays_unnamed(self):
        api = self._no_socket_detection(FakeAPI())
        self.assertIsNone(_run_unisolated(api)["default"].detected)

    def test_auto_plays_the_recorded_default_entry(self):
        cfg = _cfg_with_calibration("ARMSID 6581", socket="default")
        cfg.hardware.backend = "teensyrom"
        with self.assertNoLogs("c64cast.audio.dac_curve_resolve", "WARNING"):
            resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg)
        self.assertEqual(resolved.table, bytes(range(256)))
        self.assertEqual(resolved.measured, (None, "ARMSID 6581"))

    def _provision_default(self, api, detected="ARMSID 6581"):
        cfg = _cfg_with_calibration(detected, socket="default")
        cfg.audio.dac_curve = "calibrated"
        resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=api)
        self.assertEqual(resolved.table, bytes(range(256)))
        return dac_curve_resolve.provision_calibrated_chip_model(api, resolved)

    def test_calibrated_switches_the_chip_at_d400_and_restores_it(self):
        # No socket was recorded and the link names none, so the switch goes
        # through the chip's own register protocol (#605).
        api = self._no_socket_detection(ArmsidAPI(kind="ARMSID", left="8580"))
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO"):
            restore = self._provision_default(api)
        self.assertEqual(api.left.model, "6581")
        self.assertEqual(api.config_puts, [])
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, armsid.SOURCE_D400): "8580"})
        assert restore is not None
        restore_sid_config(api, restore)
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_puts, [])

    def test_an_ultimate_switches_the_socket_mapped_at_d400(self):
        # Through the socket's config item, so the menu and the label cache
        # follow the chip rather than keeping the model it had before.
        api = ArmsidAPI(kind="ARM2SID", left="8580")
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "INFO"):
            restore = self._provision_default(api)
        self.assertEqual(api.left.model, "6581")
        self.assertEqual(api.config_store[CAT_ARMSID1][armsid.ITEM_ARMSID_MODE], "6581")
        self.assertEqual(armsid.cached_labels(api), ("ARM2SID 6581", "ARM2SID R 8580"))
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, "socket1"): "8580"})
        assert restore is not None
        restore_sid_config(api, restore)
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_store[CAT_ARMSID1][armsid.ITEM_ARMSID_MODE], "8580")
        self.assertEqual(armsid.cached_labels(api), ("ARM2SID 8580", "ARM2SID R 8580"))

    def test_a_socket_mapped_at_d400_that_no_longer_holds_an_armsid_is_left_alone(self):
        api = ArmsidAPI(left="8580")
        resolved = dac_curve_resolve.DacCurve("calibrated:k", bytes(256), (None, "ARMSID 6581"))
        with (
            mock.patch.object(
                dac_curve_resolve, "detect_socket_models", return_value=("6581", None)
            ),
            self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING") as logs,
        ):
            restore = dac_curve_resolve.provision_calibrated_chip_model(api, resolved)
        self.assertIsNone(restore)
        self.assertEqual(api.config_puts, [])
        self.assertIn("at $D400 (now socket 1)", logs.output[0])

    def test_an_ultimate_that_cannot_say_who_answers_d400_switches_nothing(self):
        # The register write would leave the socket's config item and the label
        # cache on the old model (#630's bug), so an unreadable owner on a link
        # with SID config warns instead of falling back to it.
        api = ArmsidAPI(kind="ARMSID", left="8580")
        resolved = dac_curve_resolve.DacCurve("calibrated:k", bytes(256), (None, "ARMSID 6581"))
        with (
            mock.patch.object(dac_curve_resolve, "d400_owner", return_value=D400_UNKNOWN),
            self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING") as logs,
        ):
            restore = dac_curve_resolve.provision_calibrated_chip_model(api, resolved)
        self.assertIsNone(restore)
        self.assertEqual(api.left.model, "8580")
        self.assertEqual(api.config_puts, [])
        self.assertIn("could not tell which socket answers $D400", logs.output[0])

    def test_a_chip_at_d400_already_in_the_measured_model_is_left_alone(self):
        api = self._no_socket_detection(ArmsidAPI(kind="ARMSID", left="6581"))
        self.assertIsNone(self._provision_default(api))
        self.assertEqual(api.left.model, "6581")

    def test_d400_no_longer_answering_as_an_armsid_is_warned_about(self):
        api = self._no_socket_detection(ArmsidAPI(kind="ARMSID", left="8580"))
        api.read_memory = lambda *a, **k: None  # type: ignore[method-assign]
        with self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING") as cm:
            self.assertIsNone(self._provision_default(api))
        self.assertIn("$D400", "\n".join(cm.output))
        self.assertEqual(api.left.model, "8580")

    def test_a_failed_switch_at_d400_does_not_raise_and_still_restores(self):
        api = self._no_socket_detection(ArmsidAPI(kind="ARMSID", left="8580"))
        with (
            mock.patch.object(armsid, "write_model", side_effect=OSError("link down")),
            self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING"),
        ):
            restore = self._provision_default(api)
        self.assertEqual(restore, {(armsid.CAT_SOCKET_MODEL, armsid.SOURCE_D400): "8580"})

    def test_a_labeled_default_entry_still_states_the_one_sid_assumption(self):
        # The probe names the chip answering $D400, not whether a second one is
        # mirrored there, so the blend caveat holds for a labeled entry too.
        cfg = _cfg_with_calibration("ARMSID 6581", socket="default")
        cfg.hardware.backend = "teensyrom"
        with self.assertLogs("c64cast.audio.dac_calibration_store", "INFO") as logs:
            table = dac_calibration_store.load_calibrated_table(cfg, be=FakeAPI())
        self.assertEqual(table, bytes(range(256)))
        self.assertIn("assumes one SID", "\n".join(logs.output))

    def test_auto_playing_a_labeled_default_entry_states_the_assumption(self):
        cfg = _cfg_with_calibration("ARMSID 6581", socket="default")
        cfg.hardware.backend = "teensyrom"
        with self.assertLogs("c64cast.audio.dac_calibration_store", "INFO") as logs:
            resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=FakeAPI())
        self.assertEqual(resolved.table, bytes(range(256)))
        self.assertIn("assumes one SID", "\n".join(logs.output))

    def test_auto_playing_an_unlabeled_default_entry_states_the_assumption(self):
        cfg = _cfg_with_calibration(None, socket="default")
        cfg.hardware.backend = "teensyrom"
        with self.assertLogs("c64cast.audio.dac_calibration_store", "INFO") as logs:
            resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=FakeAPI())
        self.assertEqual(resolved.table, bytes(range(256)))
        self.assertIn("assumes one SID", "\n".join(logs.output))

    def test_calibrated_playing_a_labeled_default_entry_states_the_assumption(self):
        cfg = _cfg_with_calibration("ARMSID 6581", socket="default")
        cfg.hardware.backend = "teensyrom"
        cfg.audio.dac_curve = "calibrated"
        with self.assertLogs("c64cast.audio.dac_calibration_store", "INFO") as logs:
            resolved = dac_curve_resolve.resolve_dac_curve_for_backend(cfg, be=FakeAPI())
        self.assertEqual(resolved.table, bytes(range(256)))
        self.assertIn("assumes one SID", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
