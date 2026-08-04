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


def _bind(scene, cls, *methods) -> None:
    """Attach `cls`'s helper methods to a SimpleNamespace stand-in, so the
    scene method under test can call its own helpers. These tests drive scene
    methods unbound against a namespace rather than building a real scene (which
    would need a MIDI port, a .sid file and a display)."""
    for name in methods:
        setattr(scene, name, getattr(cls, name).__get__(scene))


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

    def test_beyond_four_clamps_to_the_four_source_ceiling(self):
        # The U64 has only 4 pan controls, so there is no 5+ spread to give.
        for n in range(5, 9):
            self.assertEqual(sp.default_pan_spread(n), sp.default_pan_spread(4))

    def test_every_spread_is_in_range_and_right_length(self):
        for n in range(1, sp.MAX_PANNED_SOURCES + 1):
            spread = sp.default_pan_spread(n)
            self.assertEqual(len(spread), n)
            for value in spread:
                self.assertGreaterEqual(value, sp.PAN_MIN)
                self.assertLessEqual(value, sp.PAN_MAX)

    def test_zero_or_negative_is_empty(self):
        self.assertEqual(sp.default_pan_spread(0), ())
        self.assertEqual(sp.default_pan_spread(-1), ())

    def test_more_chips_than_sources_collapses_to_center(self):
        # A 3-SID tune on a machine with no socketed SID has only the 2 UltiSID
        # cores. Spreading them [-3, 3] would throw two chips hard left against
        # one hard right; mono is the honest default.
        self.assertEqual(sp.default_pan_spread(2, 3), (0, 0))

    def test_one_chip_per_source_still_spreads(self):
        self.assertEqual(sp.default_pan_spread(2, 2), (-3, 3))
        self.assertEqual(sp.default_pan_spread(3, 3), (0, -3, 3))


class ResolvePanningTest(unittest.TestCase):
    def test_empty_config_uses_the_default_spread(self):
        self.assertEqual(sp.resolve_panning([], 3), (0, -3, 3))
        self.assertEqual(sp.resolve_panning(None, 2), (-3, 3))

    def test_config_overrides_the_default(self):
        self.assertEqual(sp.resolve_panning(["Left 4", "Right 4"], 2), (-4, 4))

    def test_short_config_centers_the_remaining_chips(self):
        self.assertEqual(sp.resolve_panning([-1], 3), (-1, 0, 0))

    def test_config_still_spreads_when_sources_are_doubled_up(self):
        # The all-center collapse is a *default*, not a cap on what a user can
        # ask for.
        self.assertEqual(sp.resolve_panning(None, 2, 3), (0, 0))
        self.assertEqual(sp.resolve_panning([-5, 5], 2, 3), (-5, 5))

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
        # socket2 is the only pannable source, so it is the FIRST distinct one
        # and takes pans[0] — entries are per source, not per chip.
        plan = sp.plan_sid_panning((None, "socket2", "", "nonsense"), (0, 3, 1, 2))
        self.assertEqual(plan, {PAN_S2: "Center"})

    def test_chips_sharing_one_split_core_share_its_single_pan(self):
        # One source ⇒ one entry consumed; the core has one pan control.
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


class DistinctSourcesTest(unittest.TestCase):
    """sid_panning entries index distinct SOURCES, not chips — the U64 has one
    pan control per source, so that is the only thing a pan can address."""

    def test_first_claim_order(self):
        self.assertEqual(
            sp.distinct_sources(("socket1", "socket2", "ultisid1")),
            ("socket1", "socket2", "ultisid1"),
        )

    def test_repeats_collapse(self):
        self.assertEqual(
            sp.distinct_sources(("socket1", "ultisid1", "ultisid1", "ultisid2")),
            ("socket1", "ultisid1", "ultisid2"),
        )

    def test_unrouted_entries_are_dropped(self):
        self.assertEqual(sp.distinct_sources((None, "socket2", "", "bogus")), ("socket2",))

    def test_no_sockets_leaves_only_two_positions(self):
        # The U64-without-socketed-SIDs case: both cores, nothing else.
        sources = ("ultisid1", "ultisid1", "ultisid2", "ultisid2")
        self.assertEqual(sp.distinct_sources(sources), ("ultisid1", "ultisid2"))

    def test_ceiling_is_four(self):
        crowded = ("socket1", "socket2", "ultisid1", "ultisid1", "ultisid2", "ultisid2")
        self.assertLessEqual(len(sp.distinct_sources(crowded)), sp.MAX_PANNED_SOURCES)

    def test_kth_entry_pans_the_kth_source(self):
        sources = ("socket1", "ultisid1", "ultisid1", "ultisid2")
        self.assertEqual(
            sp.source_pans(sources, (-5, 0, 5)),
            {"socket1": -5, "ultisid1": 0, "ultisid2": 5},
        )


class ChipPanValuesTest(unittest.TestCase):
    """Per-chip effective pan — what the scope orders its columns by."""

    def test_one_chip_per_source(self):
        self.assertEqual(
            sp.chip_pan_values(("socket1", "socket2", "ultisid1"), (0, -3, 3)), (0, -3, 3)
        )

    def test_chips_sharing_a_source_report_its_shared_pan(self):
        sources = ("socket1", "ultisid1", "ultisid1")
        self.assertEqual(sp.chip_pan_values(sources, (-3, 3)), (-3, 3, 3))

    def test_unrouted_chip_reports_center(self):
        self.assertEqual(sp.chip_pan_values((None, "socket1"), (4,)), (0, 4))


class WindowOrderTest(unittest.TestCase):
    """Scope columns run left-to-right across the stereo field, so the display
    matches what you hear (see the user-facing spread rationale in sid.md)."""

    def test_identity_when_all_equal(self):
        self.assertEqual(sp.window_order_for_pans((0, 0, 0)), (0, 1, 2))

    def test_single_chip_is_identity(self):
        self.assertEqual(sp.window_order_for_pans((0,)), (0,))

    def test_three_sid_default_puts_primary_chip_in_the_center_column(self):
        # Default [0, -3, 3]: chip 0 is the primary and sits dead center, so it
        # must render in the MIDDLE column, flanked by chips 1 and 2.
        order = sp.window_order_for_pans(sp.default_pan_spread(3))
        self.assertEqual(order, (1, 0, 2))

    def test_four_sid_default_puts_chips_0_and_1_closest_to_center(self):
        # Default [-2, 2, -5, 5]: chips 0/1 are the important pair and sit
        # nearest center, so they occupy the two middle columns.
        order = sp.window_order_for_pans(sp.default_pan_spread(4))
        self.assertEqual(order, (2, 0, 1, 3))

    def test_two_sid_default_is_identity(self):
        self.assertEqual(sp.window_order_for_pans(sp.default_pan_spread(2)), (0, 1))

    def test_ties_keep_chip_order(self):
        self.assertEqual(sp.window_order_for_pans((3, -3, 3, -3)), (1, 3, 0, 2))

    def test_order_is_always_a_permutation(self):
        for pans in ((0,), (0, -3, 3), (-2, 2, -5, 5), (1, 1, -1), (5, -5)):
            self.assertEqual(sorted(sp.window_order_for_pans(pans)), list(range(len(pans))))


class ApplyPanningTest(unittest.TestCase):
    """The live path: read once, write only what differs, hand back originals
    plus the column order."""

    def test_writes_only_the_sources_whose_pan_changes(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center"})
        result = sp.apply_panning(api, ("socket1", "socket2"), [0, 3])

        self.assertEqual(api.config_puts, [(CAT, "Pan Socket 2", "Right 3")])
        self.assertEqual(result.originals, {PAN_S2: "Center"})

    def test_already_correct_writes_nothing_and_restores_nothing(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center"})
        result = sp.apply_panning(api, ("socket1",), [])

        self.assertEqual(api.config_puts, [])
        self.assertEqual(result.originals, {})

    def test_originals_capture_the_pre_change_values(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Right 2", "Pan UltiSID 1": "Left 1"})
        result = sp.apply_panning(api, ("socket1", "ultisid1"), [-3, 3])

        self.assertEqual(result.originals, {PAN_S1: "Right 2", PAN_U1: "Left 1"})
        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Left 3", "Pan UltiSID 1": "Right 3"},
        )

    def test_default_spread_applied_when_unconfigured(self):
        api = _ultimate_fake(
            mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center", "Pan UltiSID 1": "Center"}
        )
        sp.apply_panning(api, ("socket1", "socket2", "ultisid1"), [])

        self.assertEqual(
            dict(api.config_store[CAT]),
            {"Pan Socket 1": "Center", "Pan Socket 2": "Left 3", "Pan UltiSID 1": "Right 3"},
        )

    def test_returns_the_column_order(self):
        api = _ultimate_fake(
            mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center", "Pan UltiSID 1": "Center"}
        )
        result = sp.apply_panning(api, ("socket1", "socket2", "ultisid1"), [])
        self.assertEqual(result.window_order, (1, 0, 2))

    def test_backend_without_config_api_is_a_no_op_with_identity_columns(self):
        api = _ultimate_fake(supports_config=False, mixer={"Pan Socket 1": "Center"})
        result = sp.apply_panning(api, ("socket1", "socket2"), [3, -3])

        self.assertEqual(api.config_puts, [])
        self.assertEqual(result.originals, {})
        self.assertEqual(result.window_order, (0, 1), "no panning ⇒ columns stay in chip order")

    def test_no_routed_sources_is_a_no_op(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center"})
        result = sp.apply_panning(api, (None, None), [0, 3])
        self.assertEqual(result.originals, {})
        self.assertEqual(api.config_puts, [])

    def test_mixer_read_failure_degrades_to_no_change(self):
        class BrokenAPI(FakeAPI):
            def get_config_category(self, category, *, timeout=3.0):
                raise RuntimeError("boom")

        api = BrokenAPI()
        api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=True)
        result = sp.apply_panning(api, ("socket1",), [3])
        self.assertEqual(result.originals, {})
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
        sp.apply_panning(api, ("socket1", "socket2", "ultisid1", "ultisid2"), [])

        self.assertEqual(
            dict(api.config_store[CAT]),
            {
                "Pan Socket 1": "Left 2",
                "Pan Socket 2": "Right 2",
                "Pan UltiSID 1": "Left 5",
                "Pan UltiSID 2": "Right 5",
            },
        )


class LimitedSourceWarningTest(unittest.TestCase):
    """The U64 can offer fewer pan positions than a tune has chips — notably
    with no socketed SIDs, where only the 2 UltiSID cores are pannable."""

    def _api(self):
        return _ultimate_fake(mixer={"Pan UltiSID 1": "Center", "Pan UltiSID 2": "Center"})

    def test_warns_when_chips_outnumber_pannable_sources(self):
        with self.assertLogs("c64cast.sid_panning", level="WARNING") as cm:
            sp.apply_panning(self._api(), ("ultisid1", "ultisid1", "ultisid2"), [])
        self.assertTrue(any("3 SID chips but only 2" in m for m in cm.output), cm.output)

    def test_warning_names_the_no_socket_cause(self):
        # "in use", not "present": model-aware routing skips a populated socket
        # whose chip is the wrong model, which is how a machine with two 6581s
        # ends up with only the two cores pannable.
        with self.assertLogs("c64cast.sid_panning", level="WARNING") as cm:
            sp.apply_panning(self._api(), ("ultisid1", "ultisid1", "ultisid2"), [])
        self.assertTrue(any("no socketed SID in use" in m for m in cm.output), cm.output)

    def test_warns_when_config_has_more_entries_than_sources(self):
        with self.assertLogs("c64cast.sid_panning", level="WARNING") as cm:
            sp.apply_panning(self._api(), ("ultisid1", "ultisid2"), [-5, 5, 3, -3])
        self.assertTrue(any("extra entries are ignored" in m for m in cm.output), cm.output)

    def test_no_warning_when_every_chip_has_its_own_source(self):
        api = _ultimate_fake(mixer={"Pan Socket 1": "Center", "Pan Socket 2": "Center"})
        with self.assertNoLogs("c64cast.sid_panning", level="WARNING"):
            sp.apply_panning(api, ("socket1", "socket2"), [])


class ScenePanningFoldTest(unittest.TestCase):
    """The scene glue: whatever panning changed must land in the scene's
    saved-config dict, which teardown PUTs back — otherwise the user's mixer
    stays where the tune left it. The scope's column order comes along too."""

    def _waveform_self(self, api, *, n_sids, addresses, panning=(), saved=None):
        from types import SimpleNamespace

        from c64cast.waveform import WaveformScene

        scene = SimpleNamespace(
            api=api,
            _n_sids=n_sids,
            _sid_addresses=addresses,
            _sid_panning=list(panning),
            _sid_volume=[],
            _saved_sid_config=saved,
            window_order=None,
        )
        scene.set_window_chip_order = lambda order: setattr(scene, "window_order", tuple(order))
        _bind(scene, WaveformScene, "_sid_sources", "_fold_into_restore")
        return scene

    def _centered_socket_api(self):
        api = _ultimate_fake(
            mixer={
                "Pan Socket 1": "Center",
                "Pan Socket 2": "Center",
                "Pan UltiSID 1": "Center",
            }
        )
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
            {"Pan Socket 1": "Left 3", "Pan Socket 2": "Right 3", "Pan UltiSID 1": "Center"},
        )
        self.assertEqual(scene._saved_sid_config, {PAN_S1: "Center", PAN_S2: "Center"})

    def test_waveform_sets_the_scope_column_order(self):
        from c64cast.asid_sidmap import SidMap
        from c64cast.waveform import WaveformScene

        api = self._centered_socket_api()
        scene = self._waveform_self(api, n_sids=3, addresses=(0xD400, 0xD420, 0xD440))
        sid_map = SidMap(
            addresses=(0xD400, 0xD420, 0xD440),
            requested=3,
            sources=("socket1", "socket2", "ultisid1"),
        )

        WaveformScene._apply_sid_panning(scene, sid_map)

        # Default [0, -3, 3] ⇒ the centered primary chip renders in the middle.
        self.assertEqual(scene.window_order, (1, 0, 2))

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

        self.assertEqual(api.config_store[CAT]["Pan Socket 1"], "Left 5")
        self.assertEqual(api.config_store[CAT]["Pan Socket 2"], "Right 5")

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
            _sid_volume=[],
            _saved_config=None,
            window_order=None,
        )
        scene.set_window_chip_order = lambda order: setattr(scene, "window_order", tuple(order))
        _bind(scene, AsidScene, "_sid_sources", "_fold_into_restore")
        sid_map = SidMap(addresses=(0xD400, 0xD420), requested=2, sources=("socket1", "socket2"))

        AsidScene._apply_sid_mixer(scene, sid_map)

        self.assertEqual(api.config_store[CAT]["Pan Socket 1"], "Left 3")
        self.assertEqual(api.config_store[CAT]["Pan Socket 2"], "Right 3")
        self.assertEqual(scene._saved_config, {PAN_S1: "Center", PAN_S2: "Center"})


if __name__ == "__main__":
    unittest.main()
