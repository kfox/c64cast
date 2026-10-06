"""DAC calibration on an ARMSID: the calibrating run records the model the
chip was measured in, and a run playing through that table puts the chip back
into it (c64cast/audio/dac_curve_resolve.py, dac_calibration.py)."""

# FakeAPI duck-types C64Backend rather than subclassing it.
# pyright: reportArgumentType=false
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from test_armsid import ArmsidAPI, _NoSettle

from c64cast.app.config import Config
from c64cast.audio import dac_calibration, dac_curve_resolve
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
        return dac_curve_resolve.provision_calibrated_chip_model(cfg, api, "calibrated:cal")

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
        cfg = _cfg_with_calibration("ARMSID 6581")
        self.assertIsNone(
            dac_curve_resolve.provision_calibrated_chip_model(cfg, api, "mahoney_ultisid")
        )
        self.assertEqual(api.left.model, "8580")

    def test_a_chip_that_is_no_longer_an_armsid_is_warned_about(self):
        api = ArmsidAPI(left="8580")
        api.read_memory = lambda *a, **k: None  # type: ignore[method-assign]
        with (
            self.assertLogs("c64cast.audio.dac_curve_resolve", "WARNING"),
            self.assertLogs("c64cast.sid.armsid", "INFO"),
        ):
            self.assertIsNone(self._provision(api, _cfg_with_calibration("ARMSID 6581")))


class RecordModelTest(_NoSettle):
    def test_the_measured_socket_is_labeled_with_its_model(self):
        api = ArmsidAPI(left="6581")
        self.assertEqual(
            dac_calibration._populated_sockets(api, lambda _msg: None), [(1, "ARM2SID 6581")]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
