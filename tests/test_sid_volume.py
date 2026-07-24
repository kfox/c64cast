"""Tests for SID mixer volume (c64cast/sid_volume.py): level/label conversion,
the auto policy, the pure plan_sid_volume mapping, and the live diff-only apply
(FakeAPI — no real hardware)."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide (same convention as test_sid_panning.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast import sid_volume as sv
from c64cast.backend import HardwareProfile

CAT = sv.CAT_MIXER
VOL_S1 = (CAT, "Vol Socket 1")
VOL_S2 = (CAT, "Vol Socket 2")
VOL_U1 = (CAT, "Vol UltiSid 1")
VOL_U2 = (CAT, "Vol UltiSid 2")

# The state the bug report was filed against: both UltiSID cores muted, both
# sockets at unity. Any chip routed onto a core is silent here.
CORES_OFF = {
    "Vol Socket 1": " 0 dB",
    "Vol Socket 2": " 0 dB",
    "Vol UltiSid 1": "OFF",
    "Vol UltiSid 2": "OFF",
}


def _ultimate_fake(*, supports_config: bool = True, mixer: dict[str, str] | None = None) -> FakeAPI:
    api = FakeAPI()
    api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=supports_config)
    api.config_store[sv.CAT_MIXER] = dict(mixer if mixer is not None else CORES_OFF)
    return api


class VolumeValueConversionTest(unittest.TestCase):
    """dB int ↔ label, the spellings a config may use."""

    def test_zero_carries_the_firmware_leading_space(self):
        # A stripped "0 dB" never equals the mixer's value, so the apply would
        # rewrite the item on every single setup.
        self.assertEqual(sv.volume_to_label(0), " 0 dB")
        self.assertNotEqual(sv.volume_to_label(0), "0 dB")

    def test_signed_ints(self):
        self.assertEqual(sv.volume_to_label(-6), "-6 dB")
        self.assertEqual(sv.volume_to_label(6), "+6 dB")

    def test_labels_are_case_and_space_insensitive(self):
        self.assertEqual(sv.volume_to_label("off"), "OFF")
        self.assertEqual(sv.volume_to_label("0 dB"), " 0 dB")
        self.assertEqual(sv.volume_to_label(" -6 DB "), "-6 dB")

    def test_stringified_ints_are_accepted_for_toml(self):
        self.assertEqual(sv.volume_to_label("-6"), "-6 dB")
        self.assertEqual(sv.volume_to_label("0"), " 0 dB")

    def test_every_label_round_trips(self):
        for label in sv.VOL_LABELS:
            self.assertEqual(sv.volume_to_label(label), label)

    def test_gap_in_the_sparse_ladder_is_rejected(self):
        # The enum jumps -24 → -18, so -20 has no representation.
        with self.assertRaises(ValueError) as caught:
            sv.volume_to_label(-20)
        self.assertIn("-18", str(caught.exception))

    def test_out_of_range_and_nonsense_rejected(self):
        for bad in (99, -99, "loud", True):
            with self.assertRaises(ValueError):
                sv.volume_to_label(bad)

    def test_normalize_spec_reports_the_offending_entry(self):
        with self.assertRaises(ValueError):
            sv.normalize_volume_spec([0, "nope"])


class ResolveVolumesTest(unittest.TestCase):
    def test_unset_means_auto_for_every_source(self):
        self.assertEqual(sv.resolve_volumes(None, 2), (None, None))

    def test_configured_wins_and_pads_with_auto(self):
        # Padding with a level instead would let one entry dictate the rest.
        self.assertEqual(sv.resolve_volumes([-6], 3), ("-6 dB", None, None))

    def test_truncated_to_the_source_count(self):
        self.assertEqual(sv.resolve_volumes([0, -6, -12], 2), (" 0 dB", "-6 dB"))

    def test_clamped_to_the_four_mixable_sources(self):
        self.assertEqual(len(sv.resolve_volumes(None, 9)), sv.MAX_VOLUME_SOURCES)


class TargetLevelTest(unittest.TestCase):
    """The per-source policy, stated once."""

    def test_an_unused_source_is_muted(self):
        self.assertEqual(
            sv.target_level(in_use=False, configured=None, current=" 0 dB"), sv.VOL_OFF
        )

    def test_an_inaudible_source_in_use_is_raised_to_unity(self):
        self.assertEqual(sv.target_level(in_use=True, configured=None, current="OFF"), sv.VOL_UNITY)

    def test_a_deliberate_level_is_left_alone(self):
        self.assertIsNone(sv.target_level(in_use=True, configured=None, current="-6 dB"))

    def test_configured_beats_both(self):
        self.assertEqual(
            sv.target_level(in_use=True, configured="-12 dB", current="-6 dB"), "-12 dB"
        )

    def test_a_source_the_mixer_did_not_report_is_left_alone(self):
        # Writing it would make a change with no original to restore.
        self.assertIsNone(sv.target_level(in_use=False, configured=None, current=None))


class PlanSidVolumeTest(unittest.TestCase):
    def _plan(self, sources, configured=None, mixer=None):
        levels = sv.resolve_volumes(configured, len(sv.distinct_sources(sources)))
        return sv.plan_sid_volume(sources, levels, dict(mixer if mixer is not None else CORES_OFF))

    def test_ultisid_tune_raises_the_cores_and_mutes_the_sockets(self):
        self.assertEqual(
            self._plan(("ultisid1", "ultisid2")),
            {VOL_U1: " 0 dB", VOL_U2: " 0 dB", VOL_S1: "OFF", VOL_S2: "OFF"},
        )

    def test_socket_tune_mutes_the_cores_even_though_they_mirror(self):
        # The spare cores shadow the socket addresses for the LEDs; muting them
        # is what keeps that from doubling the audio.
        plan = self._plan(("socket1",), mixer={**CORES_OFF, "Vol UltiSid 1": " 0 dB"})
        self.assertEqual(plan[VOL_U1], "OFF")
        self.assertEqual(plan[VOL_S2], "OFF")
        self.assertNotIn(VOL_S1, plan, "an audible in-use socket needs no change")

    def test_chips_sharing_a_split_core_claim_one_level(self):
        plan = self._plan(("ultisid1", "ultisid1", "ultisid2"), configured=[-6, -12])
        self.assertEqual(plan[VOL_U1], "-6 dB")
        self.assertEqual(plan[VOL_U2], "-12 dB")

    def test_unknown_sources_are_ignored(self):
        plan = self._plan(("socket1", None))
        self.assertEqual(plan[VOL_S2], "OFF")


class ApplyVolumeTest(unittest.TestCase):
    def test_writes_only_what_differs_and_returns_originals(self):
        api = _ultimate_fake()

        originals = sv.apply_volume(api, ("ultisid1",), None)

        self.assertEqual(api.config_store[CAT]["Vol UltiSid 1"], " 0 dB")
        self.assertEqual(api.config_store[CAT]["Vol Socket 1"], "OFF")
        self.assertEqual(
            originals,
            {VOL_U1: "OFF", VOL_S1: " 0 dB", VOL_S2: " 0 dB"},
            "Vol UltiSid 2 was already OFF and needed no write",
        )

    def test_already_correct_mixer_writes_nothing(self):
        api = _ultimate_fake(
            mixer={
                "Vol Socket 1": "OFF",
                "Vol Socket 2": "OFF",
                "Vol UltiSid 1": " 0 dB",
                "Vol UltiSid 2": "OFF",
            }
        )

        self.assertEqual(sv.apply_volume(api, ("ultisid1",), None), {})
        self.assertEqual(api.config_puts, [])

    def test_restoring_the_originals_returns_the_exact_prior_state(self):
        api = _ultimate_fake()
        before = dict(api.config_store[CAT])

        originals = sv.apply_volume(api, ("ultisid1", "ultisid2"), None)
        for (category, item), value in originals.items():
            api.put_config_item(category, item, value)

        self.assertEqual(api.config_store[CAT], before)

    def test_configured_levels_win(self):
        api = _ultimate_fake()

        sv.apply_volume(api, ("ultisid1", "ultisid2"), [-6, "off"])

        self.assertEqual(api.config_store[CAT]["Vol UltiSid 1"], "-6 dB")
        self.assertEqual(api.config_store[CAT]["Vol UltiSid 2"], "OFF")

    def test_backend_without_config_api_is_a_no_op(self):
        api = _ultimate_fake(supports_config=False)

        self.assertEqual(sv.apply_volume(api, ("ultisid1",), None), {})
        self.assertEqual(api.config_puts, [])

    def test_unknown_sources_leave_the_mixer_alone(self):
        # Muting on a source list we couldn't resolve risks silencing the very
        # chip that is playing.
        api = _ultimate_fake()

        self.assertEqual(sv.apply_volume(api, (None, None), None), {})
        self.assertEqual(api.config_puts, [])

    def test_mixer_read_failure_is_survivable(self):
        api = _ultimate_fake()

        def _boom(category, *, timeout=3.0):
            raise RuntimeError("REST down")

        api.get_config_category = _boom

        self.assertEqual(sv.apply_volume(api, ("ultisid1",), None), {})
        self.assertEqual(api.config_puts, [])


if __name__ == "__main__":
    unittest.main()
