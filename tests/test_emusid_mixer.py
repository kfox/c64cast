"""emusid_mixer: the U2+ emulated-stereo-SID snoop topology and routing.

Pure planner tests pin the retarget rules — cover every tune chip using only
spare *enabled* sides, never touch a disabled side, never snap an
inexpressible address to a neighbor — and the impure entry point is exercised
against FakeAPI's config surface for gating, diffing, and restore originals.
Field names and enum labels mirror a live `Audio Output Settings` dump.
"""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide (same convention as test_sid_panning.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast.sid.emusid_mixer import (
    CAT_EMUSID,
    apply_emusid_model,
    apply_emusid_routing,
    emusid_sources_for_addresses,
    emusid_topology,
    parse_snoop_base,
    plan_emusid_model,
    plan_emusid_routing,
    read_emusid_category,
    snoop_label,
)

# A stock-looking U2+: left side snooping the primary SID address, right side
# parked on a base no ordinary tune plays.
_STOCK = {
    "SID Left": "Enabled",
    "SID Left Base": "Snoop $D400",
    "SID Right": "Enabled",
    "SID Right Base": "Snoop $D680",
    "Vol EmuSid1": " 0 dB",
    "Vol EmuSid2": " 0 dB",
}

# The same machine with both sides' model items reported — the firmware's
# `sidchip_sel` ladder is exactly these two labels, for filter curve and for
# combined waveforms alike.
_STOCK_6581 = dict(
    _STOCK,
    **{
        "SID Left Filter Curve": "6581",
        "SID Left Combined Waveforms": "6581",
        "SID Right Filter Curve": "6581",
        "SID Right Combined Waveforms": "6581",
    },
)


def _u2plus_with(category: dict[str, str]) -> FakeAPI:
    api = FakeAPI.u2plus()
    api.config_store[CAT_EMUSID] = dict(category)
    return api


class SnoopLabelTest(unittest.TestCase):
    """The enum-label round trip, including what the enum can't express."""

    def test_parse_snoop_base(self):
        self.assertEqual(parse_snoop_base("Snoop $D400"), 0xD400)
        self.assertEqual(parse_snoop_base("Snoop $D680"), 0xD680)

    def test_parse_rejects_io_and_garbage(self):
        self.assertIsNone(parse_snoop_base("IO $DE00"))
        self.assertIsNone(parse_snoop_base(""))
        self.assertIsNone(parse_snoop_base("Snoop $XYZ"))

    def test_snoop_label_round_trips_the_snoopable_set(self):
        self.assertEqual(snoop_label(0xD420), "Snoop $D420")
        self.assertEqual(parse_snoop_base(snoop_label(0xD780)), 0xD780)

    def test_snoop_label_refuses_inexpressible_addresses(self):
        # $D440 (ASID's third chip) is not in the firmware enum — no snapping.
        self.assertIsNone(snoop_label(0xD440))


class TopologyTest(unittest.TestCase):
    """{source: snooped address} from the raw category."""

    def test_stock_topology(self):
        self.assertEqual(emusid_topology(_STOCK), {"emusid1": 0xD400, "emusid2": 0xD680})

    def test_disabled_side_is_absent(self):
        category = dict(_STOCK, **{"SID Right": "Disabled"})
        self.assertEqual(emusid_topology(category), {"emusid1": 0xD400})

    def test_io_mapped_side_is_absent(self):
        category = dict(_STOCK, **{"SID Right Base": "IO $DE00"})
        self.assertEqual(emusid_topology(category), {"emusid1": 0xD400})

    def test_sources_for_addresses(self):
        category = dict(_STOCK, **{"SID Right Base": "Snoop $D420"})
        self.assertEqual(
            emusid_sources_for_addresses((0xD400, 0xD420, 0xD440), category),
            ("emusid1", "emusid2", None),
        )

    def test_shared_address_prefers_the_left_side(self):
        category = dict(_STOCK, **{"SID Right Base": "Snoop $D400"})
        self.assertEqual(emusid_sources_for_addresses((0xD400,), category), ("emusid1",))


class PlanRoutingTest(unittest.TestCase):
    """The retarget planner: chip coverage from spare enabled sides only."""

    def test_single_sid_already_covered_plans_nothing(self):
        plan, remaining = plan_emusid_routing((0xD400,), _STOCK)
        self.assertEqual(plan, {})
        self.assertEqual(remaining, ())

    def test_2sid_retargets_the_spare_right_side(self):
        plan, remaining = plan_emusid_routing((0xD400, 0xD420), _STOCK)
        self.assertEqual(plan, {(CAT_EMUSID, "SID Right Base"): "Snoop $D420"})
        self.assertEqual(remaining, ())

    def test_uncovered_primary_takes_a_spare_side(self):
        # Left disabled, right parked on $D680: the single enabled side is
        # spare and moves to the tune's only address.
        category = dict(_STOCK, **{"SID Left": "Disabled"})
        plan, remaining = plan_emusid_routing((0xD400,), category)
        self.assertEqual(plan, {(CAT_EMUSID, "SID Right Base"): "Snoop $D400"})
        self.assertEqual(remaining, ())

    def test_disabled_side_is_never_retargeted(self):
        category = dict(_STOCK, **{"SID Right": "Disabled"})
        plan, remaining = plan_emusid_routing((0xD400, 0xD420), category)
        self.assertEqual(plan, {})
        self.assertEqual(remaining, (0xD420,))

    def test_redundant_mirror_side_counts_as_spare(self):
        # Both sides snooping $D400: the mirror is redundant, and an
        # uncovered chip made audible beats a covered one doubled.
        category = dict(_STOCK, **{"SID Right Base": "Snoop $D400"})
        plan, remaining = plan_emusid_routing((0xD400, 0xD420), category)
        self.assertEqual(plan, {(CAT_EMUSID, "SID Right Base"): "Snoop $D420"})
        self.assertEqual(remaining, ())

    def test_inexpressible_address_stays_uncovered(self):
        plan, remaining = plan_emusid_routing((0xD400, 0xD440), _STOCK)
        self.assertEqual(plan, {})
        self.assertEqual(remaining, (0xD440,))

    def test_three_chips_exhaust_the_spares(self):
        plan, remaining = plan_emusid_routing((0xD400, 0xD420, 0xD500), _STOCK)
        self.assertEqual(plan, {(CAT_EMUSID, "SID Right Base"): "Snoop $D420"})
        self.assertEqual(remaining, (0xD500,))


class ApplyRoutingTest(unittest.TestCase):
    """The impure entry point: gating, writes, and restore originals."""

    def test_no_surface_is_a_no_op(self):
        api = FakeAPI.ultimate()  # U64-shaped: no emusid flag
        api.config_store[CAT_EMUSID] = dict(_STOCK)
        self.assertEqual(apply_emusid_routing(api, (0xD400, 0xD420)), {})
        self.assertEqual(api.config_puts, [])

    def test_empty_category_is_a_no_op(self):
        # The firmware answers a GET for a missing category with an empty
        # body; a surface that can't be read back is never written.
        api = FakeAPI.u2plus()
        self.assertEqual(apply_emusid_routing(api, (0xD400,)), {})
        self.assertEqual(api.config_puts, [])

    def test_retarget_writes_and_returns_originals(self):
        api = _u2plus_with(_STOCK)
        originals = apply_emusid_routing(api, (0xD400, 0xD420))
        self.assertEqual(originals, {(CAT_EMUSID, "SID Right Base"): "Snoop $D680"})
        self.assertEqual(api.config_puts, [(CAT_EMUSID, "SID Right Base", "Snoop $D420")])

    def test_covered_tune_writes_nothing(self):
        api = _u2plus_with(_STOCK)
        self.assertEqual(apply_emusid_routing(api, (0xD400,)), {})
        self.assertEqual(api.config_puts, [])

    def test_uncovered_chip_warns(self):
        api = _u2plus_with(dict(_STOCK, **{"SID Right": "Disabled"}))
        with self.assertLogs("c64cast.sid.emusid_mixer", level="WARNING") as cm:
            apply_emusid_routing(api, (0xD400, 0xD420))
        self.assertIn("no spare enabled emulated SID", cm.output[0])

    def test_read_emusid_category_requires_the_enable_fields(self):
        api = FakeAPI.u2plus()
        api.config_store[CAT_EMUSID] = {"Vol EmuSid1": " 0 dB"}
        self.assertIsNone(read_emusid_category(api))


class PlanModelTest(unittest.TestCase):
    """The model planner: which sides get told to be which chip."""

    def test_already_matching_plans_nothing(self):
        self.assertEqual(plan_emusid_model((0xD400,), ("6581",), _STOCK_6581), {})

    def test_mismatch_sets_both_halves_of_the_model(self):
        self.assertEqual(
            plan_emusid_model((0xD400,), ("8580",), _STOCK_6581),
            {
                (CAT_EMUSID, "SID Left Filter Curve"): "8580",
                (CAT_EMUSID, "SID Left Combined Waveforms"): "8580",
            },
        )

    def test_only_the_half_that_differs_is_written(self):
        category = dict(_STOCK_6581, **{"SID Left Filter Curve": "8580"})
        self.assertEqual(
            plan_emusid_model((0xD400,), ("8580",), category),
            {(CAT_EMUSID, "SID Left Combined Waveforms"): "8580"},
        )

    def test_each_chip_gets_its_own_side(self):
        category = dict(_STOCK_6581, **{"SID Right Base": "Snoop $D420"})
        self.assertEqual(
            plan_emusid_model((0xD400, 0xD420), ("6581", "8580"), category),
            {
                (CAT_EMUSID, "SID Right Filter Curve"): "8580",
                (CAT_EMUSID, "SID Right Combined Waveforms"): "8580",
            },
        )

    def test_headers_without_a_definite_requirement_are_left_alone(self):
        for required in (None, "?", "6581+8580"):
            with self.subTest(required=required):
                self.assertEqual(plan_emusid_model((0xD400,), (required,), _STOCK_6581), {})

    def test_chip_no_side_snoops_is_left_alone(self):
        # $D420 is uncovered in _STOCK — routing already warned about it, and
        # there is no side to set a model on.
        self.assertEqual(plan_emusid_model((0xD420,), ("8580",), _STOCK_6581), {})

    def test_model_matching_off_plans_nothing(self):
        # required_models_for returns () under sid_model = "off".
        self.assertEqual(plan_emusid_model((0xD400,), (), _STOCK_6581), {})


class ApplyModelTest(unittest.TestCase):
    """The impure model pass: gating, writes, restore originals, ordering."""

    def test_no_surface_is_a_no_op(self):
        api = FakeAPI.ultimate()  # U64-shaped: matched by sid_autoconfig instead
        api.config_store[CAT_EMUSID] = dict(_STOCK_6581)
        self.assertEqual(apply_emusid_model(api, (0xD400,), ("8580",)), {})
        self.assertEqual(api.config_puts, [])

    def test_mismatch_writes_and_returns_originals(self):
        api = _u2plus_with(_STOCK_6581)
        originals = apply_emusid_model(api, (0xD400,), ("8580",))
        self.assertEqual(
            originals,
            {
                (CAT_EMUSID, "SID Left Filter Curve"): "6581",
                (CAT_EMUSID, "SID Left Combined Waveforms"): "6581",
            },
        )
        self.assertEqual(
            sorted(api.config_puts),
            [
                (CAT_EMUSID, "SID Left Combined Waveforms", "8580"),
                (CAT_EMUSID, "SID Left Filter Curve", "8580"),
            ],
        )

    def test_matching_tune_writes_nothing(self):
        api = _u2plus_with(_STOCK_6581)
        self.assertEqual(apply_emusid_model(api, (0xD400,), ("6581",)), {})
        self.assertEqual(api.config_puts, [])

    def test_model_follows_the_side_routing_just_retargeted(self):
        # The whole reason the model pass re-reads the category: the right side
        # is parked on $D680 until routing moves it to the tune's second chip,
        # and only then is there a side to give that chip's model to.
        api = _u2plus_with(_STOCK_6581)
        apply_emusid_routing(api, (0xD400, 0xD420))
        apply_emusid_model(api, (0xD400, 0xD420), ("6581", "8580"))
        self.assertIn((CAT_EMUSID, "SID Right Filter Curve", "8580"), api.config_puts)
        self.assertIn((CAT_EMUSID, "SID Right Combined Waveforms", "8580"), api.config_puts)


if __name__ == "__main__":
    unittest.main()
