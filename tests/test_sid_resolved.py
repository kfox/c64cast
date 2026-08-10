"""Tests for the resolved-audio log line (c64cast/sid/sid_resolved.py): the pure
renderer's verdicts on each way a chip can end up wrong, and the best-effort
read-back path (FakeAPI — no real hardware)."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide (same convention as test_sid_autoconfig.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast.sid import sid_resolved as sr
from c64cast.sid.asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    CAT_ULTISID,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID1_FILTER,
)
from c64cast.sid.sid_panning import CAT_MIXER


def _state(
    *,
    addr_map: dict[int, str] | None = None,
    socket_models: tuple[str | None, str | None] = ("6581", None),
    ultisid_curves: dict[str, str] | None = None,
    mixer: dict[str, str] | None = None,
) -> sr.SidHardwareState:
    """A hardware state with one 6581 socket, both cores on an 8580 curve, and
    everything unity-gain and centered — so each test varies only what it is
    about."""
    return sr.SidHardwareState(
        addr_map={0xD400: "socket1"} if addr_map is None else addr_map,
        socket_models=socket_models,
        ultisid_curves=ultisid_curves or {"ultisid1": "8580 Lo", "ultisid2": "8580 Lo"},
        mixer=mixer
        or {
            "Vol Socket 1": " 0 dB",
            "Vol Socket 2": "OFF",
            "Vol UltiSid 1": " 0 dB",
            "Vol UltiSid 2": "OFF",
            "Pan Socket 1": "Center",
            "Pan UltiSID 1": "Center",
        },
    )


class DescribeResolvedAudioTest(unittest.TestCase):
    """What the line says, and when it counts as clean (INFO) vs not (WARNING)."""

    def test_matching_chip_is_clean_and_names_source_model_level_and_pan(self):
        resolved = sr.describe_resolved_audio(_state(), (0xD400,), ("6581",))
        self.assertTrue(resolved.clean)
        self.assertEqual(resolved.summary, "$D400 → socket1 (6581) @ 0 dB Center")

    def test_wrong_model_is_flagged(self):
        # The exact failure this line exists to catch: autoconfig logs "→
        # ultisid1 (8580 Lo)" while the 6581 in socket 1 is what sounds.
        resolved = sr.describe_resolved_audio(_state(), (0xD400,), ("8580",))
        self.assertFalse(resolved.clean)
        self.assertIn("tune wants 8580", resolved.summary)

    def test_ultisid_curve_variant_still_satisfies_the_bare_model(self):
        resolved = sr.describe_resolved_audio(
            _state(addr_map={0xD400: "ultisid1"}), (0xD400,), ("8580",)
        )
        self.assertTrue(resolved.clean)
        self.assertIn("ultisid1 (8580 Lo)", resolved.summary)

    def test_muted_source_is_flagged_inaudible(self):
        resolved = sr.describe_resolved_audio(
            _state(mixer={"Vol Socket 1": "OFF"}), (0xD400,), ("6581",)
        )
        self.assertFalse(resolved.clean)
        self.assertIn("INAUDIBLE", resolved.summary)

    def test_unmapped_address_is_flagged(self):
        resolved = sr.describe_resolved_audio(_state(addr_map={}), (0xD400,), ("6581",))
        self.assertFalse(resolved.clean)
        self.assertIn("nothing mapped", resolved.summary)

    def test_no_model_requirement_only_checks_audibility(self):
        for required in (None, "?", "6581+8580"):
            with self.subTest(required=required):
                resolved = sr.describe_resolved_audio(_state(), (0xD400,), (required,))
                self.assertTrue(resolved.clean)

    def test_missing_required_models_report_without_a_match_check(self):
        # ASID carries no PSID header, so it passes addresses only.
        resolved = sr.describe_resolved_audio(_state(), (0xD400,))
        self.assertTrue(resolved.clean)
        self.assertIn("socket1", resolved.summary)

    def test_audible_source_the_tune_does_not_use_is_reported(self):
        # A core left up at another address bleeds in as a detuned double,
        # which sounds like a bad tune rather than a config mistake.
        resolved = sr.describe_resolved_audio(
            _state(addr_map={0xD400: "socket1", 0xD500: "ultisid1"}), (0xD400,), ("6581",)
        )
        self.assertTrue(resolved.clean)  # the tune's own chip is fine
        self.assertIn("also audible: ultisid1 (8580 Lo) at $D500", resolved.summary)

    def test_muted_bystander_is_not_reported(self):
        resolved = sr.describe_resolved_audio(
            _state(addr_map={0xD400: "socket1", 0xD500: "ultisid2"}), (0xD400,), ("6581",)
        )
        self.assertNotIn("also audible", resolved.summary)

    def test_unreported_level_counts_as_audible(self):
        # Claiming silence we never measured sends someone hunting a mixer
        # problem that isn't there.
        resolved = sr.describe_resolved_audio(_state(mixer={}), (0xD400,), ("6581",))
        self.assertTrue(resolved.clean)

    def test_multi_chip_renders_one_fragment_per_chip(self):
        resolved = sr.describe_resolved_audio(
            _state(addr_map={0xD400: "socket1", 0xD420: "ultisid1"}),
            (0xD400, 0xD420),
            ("6581", "8580"),
        )
        self.assertTrue(resolved.clean)
        self.assertEqual(resolved.summary.count(" → "), 2)
        self.assertNotIn("also audible", resolved.summary)


class ReadSidHardwareStateTest(unittest.TestCase):
    """The live read-back: what it assembles, and what it does when it can't."""

    def _api(self) -> FakeAPI:
        api = FakeAPI.ultimate()
        api.config_store[CAT_ADDRESSING] = {
            ITEM_SOCKET1_ADDR: "$D400",
            ITEM_ULTISID1_ADDR: "$D500",
        }
        api.config_store[CAT_SOCKETS] = {
            ITEM_SOCKET1_EN: "Enabled",
            ITEM_SOCKET1_TYPE: "6581",
        }
        api.config_store[CAT_ULTISID] = {ITEM_ULTISID1_FILTER: "8580 Lo"}
        api.config_store[CAT_MIXER] = {"Vol Socket 1": " 0 dB", "Vol UltiSid 1": "OFF"}
        return api

    def test_assembles_routing_models_curves_and_mixer(self):
        state = sr.read_sid_hardware_state(self._api())
        assert state is not None
        self.assertEqual(state.addr_map, {0xD400: "socket1", 0xD500: "ultisid1"})
        self.assertEqual(state.socket_models, ("6581", None))
        self.assertEqual(state.ultisid_curves["ultisid1"], "8580 Lo")
        self.assertFalse(state.audible("ultisid1"))

    def test_backend_without_a_config_api_reads_nothing(self):
        self.assertIsNone(sr.read_sid_hardware_state(FakeAPI.ultimate(supports_config=False)))


class LogResolvedAudioTest(unittest.TestCase):
    """Log level carries the verdict, and nothing here may crash a scene."""

    def _api_at(self, socket_type: str) -> FakeAPI:
        api = FakeAPI.ultimate()
        api.config_store[CAT_ADDRESSING] = {ITEM_SOCKET1_ADDR: "$D400"}
        api.config_store[CAT_SOCKETS] = {ITEM_SOCKET1_EN: "Enabled", ITEM_SOCKET1_TYPE: socket_type}
        api.config_store[CAT_MIXER] = {"Vol Socket 1": " 0 dB"}
        return api

    def test_clean_result_logs_at_info(self):
        with self.assertLogs("c64cast.sid.sid_resolved", level="INFO") as cm:
            sr.log_resolved_audio(self._api_at("6581"), (0xD400,), ("6581",))
        self.assertEqual(cm.records[0].levelname, "INFO")

    def test_wrong_model_logs_at_warning(self):
        with self.assertLogs("c64cast.sid.sid_resolved", level="INFO") as cm:
            sr.log_resolved_audio(self._api_at("6581"), (0xD400,), ("8580",))
        self.assertEqual(cm.records[0].levelname, "WARNING")

    def test_no_addresses_logs_nothing(self):
        with self.assertNoLogs("c64cast.sid.sid_resolved", level="INFO"):
            sr.log_resolved_audio(self._api_at("6581"), ())

    def test_read_failure_logs_nothing_and_does_not_raise(self):
        api = FakeAPI.ultimate()
        api.get_config_category = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertNoLogs("c64cast.sid.sid_resolved", level="INFO"):
            sr.log_resolved_audio(api, (0xD400,), ("6581",))


if __name__ == "__main__":
    unittest.main()
