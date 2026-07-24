"""Tests for SID stereo panning (c64cast/sid_panning.py): pan value/label
conversion, the default spreads, the pure plan_sid_panning mapping, and the
live diff-only apply (FakeAPI — no real hardware)."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide (same convention as test_sid_autoconfig.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast import sid_panning as sp
from c64cast.backend import HardwareProfile

CAT = sp.CAT_MIXER
PAN_S1 = (CAT, "Pan Socket 1")
PAN_S2 = (CAT, "Pan Socket 2")
PAN_U1 = (CAT, "Pan UltiSID 1")
PAN_U2 = (CAT, "Pan UltiSID 2")


def _ultimate_fake(*, supports_config: bool = True, mixer: dict[str, str] | None = None) -> FakeAPI:
    api = FakeAPI()
    api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=supports_config)
    api.config_store[sp.CAT_MIXER] = dict(mixer or {})
    return api


class PanValueConversionTest(unittest.TestCase):
    """int ↔ label, the two spellings a config may use."""

    def test_int_extremes_and_center(self):
        self.assertEqual(sp.pan_to_label(-5), "Left 5")
        self.assertEqual(sp.pan_to_label(0), "Center")
        self.assertEqual(sp.pan_to_label(5), "Right 5")

    def test_every_int_round_trips_through_its_label(self):
        for value in range(sp.PAN_MIN, sp.PAN_MAX + 1):
            self.assertEqual(sp.pan_from_label(sp.pan_to_label(value)), value)

    def test_labels_are_case_insensitive_and_trimmed(self):
        self.assertEqual(sp.pan_to_label("left 2"), "Left 2")
        self.assertEqual(sp.pan_to_label("  RIGHT 4 "), "Right 4")
        self.assertEqual(sp.pan_to_label("center"), "Center")

    def test_stringified_int_is_accepted(self):
        self.assertEqual(sp.pan_to_label("0"), "Center")
        self.assertEqual(sp.pan_to_label("-3"), "Left 3")

    def test_out_of_range_int_reports_the_range(self):
        for bad in (6, -6, 99, "-9"):
            with self.assertRaisesRegex(ValueError, "out of range"):
                sp.pan_to_label(bad)

    def test_unrecognized_label_is_rejected(self):
        for bad in ("Left 9", "bogus", "", "Middle"):
            with self.assertRaisesRegex(ValueError, "unrecognized pan label"):
                sp.pan_to_label(bad)

    def test_bool_is_rejected_despite_being_an_int(self):
        with self.assertRaisesRegex(ValueError, "invalid pan value"):
            sp.pan_to_label(True)

    def test_normalize_pan_spec_mixes_ints_and_labels(self):
        self.assertEqual(sp.normalize_pan_spec([-3, "Right 3", "Center", 5]), (-3, 3, 0, 5))
        self.assertEqual(sp.normalize_pan_spec([]), ())


class DefaultPanSpreadTest(unittest.TestCase):
    """The documented auto spreads (see docs/architecture/sid.md)."""

    def test_documented_spreads(self):
        self.assertEqual(sp.default_pan_spread(1), (0,))
        self.assertEqual(sp.default_pan_spread(2), (-3, 3))
        self.assertEqual(sp.default_pan_spread(3), (0, -3, 3))
        self.assertEqual(sp.default_pan_spread(4), (-2, 2, -5, 5))

    def test_single_sid_is_centered(self):
        self.assertEqual(sp.default_pan_spread(1), (0,))

    def test_five_plus_spreads_evenly_across_the_full_field(self):
        for n in range(5, 9):
            spread = sp.default_pan_spread(n)
            self.assertEqual(len(spread), n)
            self.assertEqual(spread[0], sp.PAN_MIN)
            self.assertEqual(spread[-1], sp.PAN_MAX)
            self.assertEqual(list(spread), sorted(spread), "even spread must be monotonic")

    def test_every_spread_is_in_range_and_right_length(self):
        for n in range(1, 9):
            spread = sp.default_pan_spread(n)
            self.assertEqual(len(spread), n)
            for value in spread:
                self.assertGreaterEqual(value, sp.PAN_MIN)
                self.assertLessEqual(value, sp.PAN_MAX)

    def test_zero_or_negative_is_empty(self):
        self.assertEqual(sp.default_pan_spread(0), ())
        self.assertEqual(sp.default_pan_spread(-1), ())


class ResolvePanningTest(unittest.TestCase):
    def test_empty_config_uses_the_default_spread(self):
        self.assertEqual(sp.resolve_panning([], 3), (0, -3, 3))
        self.assertEqual(sp.resolve_panning(None, 2), (-3, 3))

    def test_config_overrides_the_default(self):
        self.assertEqual(sp.resolve_panning(["Left 4", "Right 4"], 2), (-4, 4))

    def test_short_config_centers_the_remaining_chips(self):
        self.assertEqual(sp.resolve_panning([-1], 3), (-1, 0, 0))

    def test_long_config_is_truncated(self):
        self.assertEqual(sp.resolve_panning([-4, 4, 5, -5], 2), (-4, 4))

    def test_no_chips_yields_nothing(self):
        self.assertEqual(sp.resolve_panning([-3, 3], 0), ())


class PlanSidPanningTest(unittest.TestCase):
    """Pure source → mixer-item mapping."""

    def test_each_source_gets_its_own_pan_item(self):
        plan = sp.plan_sid_panning(("socket1", "socket2", "ultisid1", "ultisid2"), (-2, 2, -5, 5))
        self.assertEqual(
            plan,
            {
                PAN_S1: "Left 2",
                PAN_S2: "Right 2",
                PAN_U1: "Left 5",
                PAN_U2: "Right 5",
            },
        )

    def test_single_centered_sid(self):
        self.assertEqual(sp.plan_sid_panning(("socket1",), (0,)), {PAN_S1: "Center"})

    def test_unrouted_and_unknown_sources_are_skipped(self):
        plan = sp.plan_sid_panning((None, "socket2", "", "nonsense"), (0, 3, 1, 2))
        self.assertEqual(plan, {PAN_S2: "Right 3"})

    def test_chips_sharing_one_split_core_keep_the_first_pan(self):
        plan = sp.plan_sid_panning(("ultisid1", "ultisid1"), (-2, 4))
        self.assertEqual(plan, {PAN_U1: "Left 2"})

    def test_extra_sources_beyond_the_pan_list_are_ignored(self):
        plan = sp.plan_sid_panning(("socket1", "socket2"), (0,))
        self.assertEqual(plan, {PAN_S1: "Center"})

    def test_no_sources_is_an_empty_plan(self):
        self.assertEqual(sp.plan_sid_panning((), ()), {})


class SourcesForAddressesTest(unittest.TestCase):
    """Address → source, from the live config (the non-remapped case)."""

    def _api_with_addressing(self) -> FakeAPI:
        api = _ultimate_fake()
        api.config_store["SID Addressing"] = {
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "$D420",
            "UltiSID 1 Address": "$D500",
            "UltiSID 2 Address": "Unmapped",
        }
        api.config_store["SID Sockets Configuration"] = {
            "SID Socket 1": "Enabled",
            "SID Socket 2": "Enabled",
        }
        return api

    def test_maps_each_address_to_its_source(self):
        api = self._api_with_addressing()
        self.assertEqual(
            sp.sources_for_addresses(api, (0xD400, 0xD420, 0xD500)),
            ("socket1", "socket2", "ultisid1"),
        )

    def test_unanswered_address_is_none(self):
        api = self._api_with_addressing()
        self.assertEqual(sp.sources_for_addresses(api, (0xD600,)), (None,))

    def test_disabled_socket_does_not_claim_its_address(self):
        api = self._api_with_addressing()
        api.config_store["SID Sockets Configuration"]["SID Socket 2"] = "Disabled"
        self.assertEqual(sp.sources_for_addresses(api, (0xD420,)), (None,))


class PlanAndApplyPanningTest(unittest.TestCase):
    """The live path: read once, write only what differs, hand back originals."""

    def test_writes_only_the_sources_whose_pan_changes(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center"})
        originals = sp.plan_and_apply_panning(api, ("socket1", "socket2"), (0, 3))

        self.assertEqual(api.config_puts, [(CAT, "Pan Socket 2", "Right 3")])
        self.assertEqual(originals, {PAN_S2: "Center"})

    def test_already_correct_writes_nothing_and_restores_nothing(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center"})
        originals = sp.plan_and_apply_panning(api, ("socket1",), (0,))

        self.assertEqual(api.config_puts, [])
        self.assertEqual(originals, {})

    def test_originals_capture_the_pre_change_values(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Right 2", "Pan UltiSID 1": "Left 1"})
        originals = sp.plan_and_apply_panning(api, ("socket1", "ultisid1"), (-3, 3))

        self.assertEqual(originals, {PAN_S1: "Right 2", PAN_U1: "Left 1"})
        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Left 3", "Pan UltiSID 1": "Right 3"},
        )

    def test_backend_without_config_api_is_a_no_op(self):
        api = _ultimate_fake(supports_config=False, mixer={"Pan Socket 1": "Center"})
        originals = sp.plan_and_apply_panning(api, ("socket1",), (3,))

        self.assertEqual(api.config_puts, [])
        self.assertEqual(originals, {})

    def test_no_routed_sources_is_a_no_op(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center"})
        self.assertEqual(sp.plan_and_apply_panning(api, (None, None), (0, 3)), {})
        self.assertEqual(api.config_puts, [])

    def test_mixer_read_failure_degrades_to_no_change(self):
        class BrokenAPI(FakeAPI):
            def get_config_category(self, category, *, timeout=3.0):
                raise RuntimeError("boom")

        api = BrokenAPI()
        api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=True)
        self.assertEqual(sp.plan_and_apply_panning(api, ("socket1",), (3,)), {})
        self.assertEqual(api.config_puts, [])

    def test_a_full_four_sid_spread_lands_on_four_distinct_sources(self):
        api = _ultimate_fake(
            mixer={
                "Pan Socket 1": "Center",
                "Pan Socket 2": "Center",
                "Pan UltiSID 1": "Center",
                "Pan UltiSID 2": "Center",
            }
        )
        sources = ("socket1", "socket2", "ultisid1", "ultisid2")
        sp.plan_and_apply_panning(api, sources, sp.default_pan_spread(4))

        self.assertEqual(
            dict(api.config_store[CAT]),
            {
                "Pan Socket 1": "Left 2",
                "Pan Socket 2": "Right 2",
                "Pan UltiSID 1": "Left 5",
                "Pan UltiSID 2": "Right 5",
            },
        )


class ScenePanningFoldTest(unittest.TestCase):
    """The scene glue: whatever panning changed must land in the scene's
    saved-config dict, which teardown PUTs back — otherwise the user's mixer
    stays where the tune left it."""

    def _waveform_self(self, api, *, n_sids, addresses, panning=(), saved=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            api=api,
            _n_sids=n_sids,
            _sid_addresses=addresses,
            _sid_panning=list(panning),
            _saved_sid_config=saved,
        )

    def _centered_socket_api(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center"})
        api.config_store["SID Addressing"] = {
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "$D420",
        }
        api.config_store["SID Sockets Configuration"] = {
            "SID Socket 1": "Enabled",
            "SID Socket 2": "Enabled",
        }
        return api

    def test_waveform_multi_sid_pans_from_the_map_and_records_originals(self):
        from c64cast.asid_sidmap import SidMap
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        scene = self._waveform_self(api, n_sids=2, addresses=(0xD400, 0xD420))
        sid_map = SidMap(addresses=(0xD400, 0xD420), requested=2, sources=("socket1", "socket2"))

        WaveformScene._apply_sid_panning(scene, sid_map)

        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Left 3", "Pan Socket 2": "Right 3"},
        )
        self.assertEqual(scene._saved_sid_config, {PAN_S1: "Center", PAN_S2: "Center"})

    def test_waveform_single_sid_reads_the_live_source(self):
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        api.config_store[CAT]["Pan Socket 1"] = "Right 4"
        scene = self._waveform_self(api, n_sids=1, addresses=(0xD400,))

        WaveformScene._apply_sid_panning(scene, None)

        self.assertEqual(api.config_store[CAT]["Pan Socket 1"], "Center")
        self.assertEqual(scene._saved_sid_config, {PAN_S1: "Right 4"})

    def test_waveform_config_override_beats_the_auto_spread(self):
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        scene = self._waveform_self(
            api, n_sids=2, addresses=(0xD400, 0xD420), panning=["Left 5", "Right 5"]
        )

        WaveformScene._apply_sid_panning(scene, None)

        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Left 5", "Pan Socket 2": "Right 5"},
        )

    def test_waveform_merges_into_an_existing_snapshot(self):
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        existing = {("SID Addressing", "UltiSID 1 Address"): "Unmapped"}
        scene = self._waveform_self(api, n_sids=1, addresses=(0xD400,), saved=dict(existing))
        scene._sid_panning = ["Right 2"]

        WaveformScene._apply_sid_panning(scene, None)

        self.assertEqual(
            scene._saved_sid_config,
            {**existing, PAN_S1: "Center"},
            "panning originals must not clobber the address/model snapshot",
        )

    def test_waveform_no_change_leaves_the_snapshot_alone(self):
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        scene = self._waveform_self(api, n_sids=1, addresses=(0xD400,))

        WaveformScene._apply_sid_panning(scene, None)

        self.assertIsNone(scene._saved_sid_config)
        self.assertEqual(api.config_puts, [])

    def test_asid_pans_from_the_map_on_remap(self):
        from types import SimpleNamespace

        from c64cast.asid_scene import AsidScene
        from c64cast.asid_sidmap import SidMap

        api = self._centered_socket_api()
        scene = SimpleNamespace(
            api=api,
            _active_chips=2,
            _chip_addresses=[0xD400, 0xD420],
            _sid_panning=[],
            _saved_config=None,
        )
        sid_map = SidMap(addresses=(0xD400, 0xD420), requested=2, sources=("socket1", "socket2"))

        AsidScene._apply_sid_panning(scene, sid_map)

        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Left 3", "Pan Socket 2": "Right 3"},
        )
        self.assertEqual(scene._saved_config, {PAN_S1: "Center", PAN_S2: "Center"})


if __name__ == "__main__":
    unittest.main()
