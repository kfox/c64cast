"""Tests for SID Player Autoconfig (c64cast/sid_autoconfig.py): the pure
plan_sid_model_config decision matrix, the resolver, and the live-config
apply/short-circuit paths (FakeAPI — no real hardware)."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide (same convention as test_dac_calibration.py / test_waveform.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast import sid_autoconfig as sa
from c64cast.asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    CAT_ULTISID,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_SOCKET2_TYPE,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID1_FILTER,
    ITEM_ULTISID2_ADDR,
)
from c64cast.backend import HardwareProfile
from c64cast.config import Config
from c64cast.sid_host_emu import SidHeader


def _ultimate_fake(*, supports_config: bool = True) -> FakeAPI:
    api = FakeAPI()
    api.profile = HardwareProfile(name="Fake U64", family="fake", supports_config=supports_config)
    return api


def _header(
    *, sid_addresses: tuple[int, ...] = (0xD400,), sid_models: tuple[str | None, ...] = (None,)
) -> SidHeader:
    return SidHeader(
        magic="PSID",
        version=2,
        num_songs=1,
        start_song=1,
        name="",
        author="",
        released="",
        clock="PAL",
        sid_model=sid_models[0],
        sid_addresses=sid_addresses,
        sid_models=sid_models,
    )


class PlanSidModelConfigTest(unittest.TestCase):
    """The pure decision matrix — no hardware, no I/O beyond logging."""

    def test_already_matching_is_a_noop(self):
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "6581"),),
            current_addr_map={0xD400: "socket1"},
            socket_models=("6581", "8580"),
            ultisid_allowed=True,
        )
        self.assertIsNone(plan)

    def test_other_socket_matches_emits_swap(self):
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "8580"),),
            current_addr_map={0xD400: "socket1"},
            socket_models=("6581", "8580"),
            ultisid_allowed=True,
        )
        self.assertEqual(
            plan,
            {
                (CAT_ADDRESSING, ITEM_SOCKET2_ADDR): "$D400",
                (CAT_SOCKETS, ITEM_SOCKET2_EN): "Enabled",
            },
        )

    def test_unclaimed_address_prefers_matching_socket(self):
        # Nothing currently answers $D400; socket2 reports the required model.
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "8580"),),
            current_addr_map={},
            socket_models=("6581", "8580"),
            ultisid_allowed=True,
        )
        self.assertEqual(
            plan,
            {
                (CAT_ADDRESSING, ITEM_SOCKET2_ADDR): "$D400",
                (CAT_SOCKETS, ITEM_SOCKET2_EN): "Enabled",
            },
        )

    def test_ultisid_fallback_when_no_socket_matches(self):
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "8580"),),
            current_addr_map={0xD400: "socket1"},
            socket_models=("6581", "6581"),  # both sockets 6581, tune wants 8580
            ultisid_allowed=True,
        )
        self.assertEqual(
            plan,
            {
                (CAT_ADDRESSING, ITEM_ULTISID1_ADDR): "$D400",
                (CAT_ULTISID, ITEM_ULTISID1_FILTER): "8580 Lo",
            },
        )

    def test_ultisid_fallback_uses_6581_curve_for_6581_request(self):
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "6581"),),
            current_addr_map={0xD400: "socket1"},
            socket_models=("8580", "8580"),
            ultisid_allowed=True,
        )
        assert plan is not None
        self.assertEqual(plan[(CAT_ULTISID, ITEM_ULTISID1_FILTER)], "6581")

    def test_no_match_and_ultisid_disallowed_warns_and_leaves_unchanged(self):
        with self.assertLogs("c64cast.sid_autoconfig", level="WARNING") as cm:
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "8580"),),
                current_addr_map={0xD400: "socket1"},
                socket_models=("6581", "6581"),
                ultisid_allowed=False,
            )
        self.assertIsNone(plan)
        self.assertTrue(any("no matching physical" in msg for msg in cm.output))

    def test_no_free_ultisid_core_warns(self):
        # Both cores already reserved by earlier chips in this pass.
        with self.assertLogs("c64cast.sid_autoconfig", level="WARNING") as cm:
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "8580"), (0xD420, "8580"), (0xD440, "8580")),
                current_addr_map={},
                socket_models=("6581", "6581"),
                ultisid_allowed=True,
            )
        # First two chips consume both UltiSID cores; the third has nowhere to go.
        self.assertEqual(len(plan), 4)  # 2 addr + 2 filter-curve entries
        self.assertTrue(any("no matching physical" in msg for msg in cm.output))

    def test_unspecified_or_either_model_is_always_a_noop(self):
        for required in (None, "?", "6581+8580"):
            with self.subTest(required=required):
                plan = sa.plan_sid_model_config(
                    chips=((0xD400, required),),
                    current_addr_map={},
                    socket_models=(None, None),
                    ultisid_allowed=True,
                )
                self.assertIsNone(plan)

    def test_multi_chip_reserves_sockets_so_two_chips_dont_collide(self):
        # Two chips both want 8580; only socket2 is 8580 — the second chip
        # must fall through to UltiSID rather than also claiming socket2.
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "8580"), (0xD420, "8580")),
            current_addr_map={},
            socket_models=("6581", "8580"),
            ultisid_allowed=True,
        )
        assert plan is not None
        self.assertEqual(plan[(CAT_ADDRESSING, ITEM_SOCKET2_ADDR)], "$D400")
        self.assertEqual(plan[(CAT_ADDRESSING, ITEM_ULTISID1_ADDR)], "$D420")
        self.assertEqual(plan[(CAT_ULTISID, ITEM_ULTISID1_FILTER)], "8580 Lo")


class ResolveSidModelCfgTest(unittest.TestCase):
    def test_returns_raw_configured_value(self):
        cfg = Config()
        cfg.ultimate64.sid_model = "8580"
        self.assertEqual(sa.resolve_sid_model_cfg(cfg), "8580")

    def test_default_is_auto(self):
        self.assertEqual(sa.resolve_sid_model_cfg(Config()), "auto")


class CurrentAddrMapTest(unittest.TestCase):
    def test_reads_live_addressing_and_sockets(self):
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {ITEM_SOCKET1_EN: "Enabled", ITEM_SOCKET2_EN: "Disabled"}
        api.config_store[CAT_ADDRESSING] = {
            ITEM_SOCKET1_ADDR: "$D400",
            ITEM_SOCKET2_ADDR: "$D420",
            ITEM_ULTISID1_ADDR: "$D500",
            ITEM_ULTISID2_ADDR: "Unmapped",
        }
        addr_map = sa._current_addr_map(api)
        self.assertEqual(
            addr_map, {0xD400: "socket1", 0xD500: "ultisid1"}
        )  # socket2 disabled, ultisid2 unmapped

    def test_read_failure_returns_empty(self):
        class BrokenAPI:
            profile = HardwareProfile(name="Broken", family="fake", supports_config=True)

            def get_config_category(self, category, *, timeout=3.0):
                raise RuntimeError("boom")

        self.assertEqual(sa._current_addr_map(BrokenAPI()), {})

    def test_socket_wins_over_mirrored_ultisid_at_same_address(self):
        # HW-observed: with Auto Address Mirroring enabled (the factory
        # default resting state), an UltiSID core and an enabled socket can
        # both report the same base — only the socket is actually audible.
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {ITEM_SOCKET1_EN: "Enabled"}
        api.config_store[CAT_ADDRESSING] = {
            ITEM_SOCKET1_ADDR: "$D400",
            ITEM_ULTISID1_ADDR: "$D400",
        }
        self.assertEqual(sa._current_addr_map(api), {0xD400: "socket1"})


class PlanModelConfigForHeaderTest(unittest.TestCase):
    def test_off_short_circuits(self):
        api = _ultimate_fake()
        plan = sa.plan_model_config_for_header(api, _header(), "off")
        self.assertIsNone(plan)
        self.assertEqual(api.config_puts, [])

    def test_no_config_api_short_circuits(self):
        api = _ultimate_fake(supports_config=False)
        header = _header(sid_addresses=(0xD400,), sid_models=("8580",))
        plan = sa.plan_model_config_for_header(api, header, "auto")
        self.assertIsNone(plan)
        self.assertEqual(api.config_puts, [])

    def test_auto_uses_header_models(self):
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {
            ITEM_SOCKET1_EN: "Enabled",
            ITEM_SOCKET1_TYPE: "6581",
            ITEM_SOCKET2_EN: "Enabled",
            ITEM_SOCKET2_TYPE: "8580",
        }
        api.config_store[CAT_ADDRESSING] = {
            ITEM_SOCKET1_ADDR: "$D400",
            ITEM_SOCKET2_ADDR: "$D420",
        }
        header = _header(sid_addresses=(0xD400,), sid_models=("8580",))
        plan = sa.plan_model_config_for_header(api, header, "auto")
        self.assertEqual(
            plan,
            {
                (CAT_ADDRESSING, ITEM_SOCKET2_ADDR): "$D400",
                (CAT_SOCKETS, ITEM_SOCKET2_EN): "Enabled",
            },
        )

    def test_explicit_override_forces_every_chip_ignoring_header(self):
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {
            ITEM_SOCKET1_EN: "Enabled",
            ITEM_SOCKET1_TYPE: "6581",
            ITEM_SOCKET2_EN: "Enabled",
            ITEM_SOCKET2_TYPE: "8580",
        }
        api.config_store[CAT_ADDRESSING] = {
            ITEM_SOCKET1_ADDR: "$D400",
            ITEM_SOCKET2_ADDR: "$D420",
        }
        # Header claims both chips are "6581" — the explicit override must
        # ignore that and force "8580" on every chip anyway.
        header = _header(sid_addresses=(0xD400, 0xD420), sid_models=("6581", "6581"))
        plan = sa.plan_model_config_for_header(api, header, "8580")
        # $D400 (socket1=6581) doesn't match -> swap to socket2. $D420
        # (socket2=8580) already matches -> no-op for that chip.
        self.assertEqual(
            plan,
            {
                (CAT_ADDRESSING, ITEM_SOCKET2_ADDR): "$D400",
                (CAT_SOCKETS, ITEM_SOCKET2_EN): "Enabled",
            },
        )


class ApplySidAutoconfigTest(unittest.TestCase):
    def test_off_makes_no_rest_calls_and_returns_empty(self):
        api = _ultimate_fake()
        saved = sa.apply_sid_autoconfig(api, _header(), "off")
        self.assertEqual(saved, {})
        self.assertEqual(api.config_puts, [])

    def test_no_config_api_returns_empty(self):
        api = _ultimate_fake(supports_config=False)
        saved = sa.apply_sid_autoconfig(api, _header(sid_models=("8580",)), "auto")
        self.assertEqual(saved, {})

    def test_already_matching_applies_nothing(self):
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {ITEM_SOCKET1_EN: "Enabled", ITEM_SOCKET1_TYPE: "6581"}
        api.config_store[CAT_ADDRESSING] = {ITEM_SOCKET1_ADDR: "$D400"}
        header = _header(sid_addresses=(0xD400,), sid_models=("6581",))
        saved = sa.apply_sid_autoconfig(api, header, "auto")
        self.assertEqual(saved, {})
        self.assertEqual(api.config_puts, [])

    def test_swap_applies_and_returns_restorable_snapshot(self):
        api = _ultimate_fake()
        api.config_store[CAT_SOCKETS] = {
            ITEM_SOCKET1_EN: "Enabled",
            ITEM_SOCKET1_TYPE: "6581",
            ITEM_SOCKET2_EN: "Disabled",
            ITEM_SOCKET2_TYPE: "8580",
        }
        api.config_store[CAT_ADDRESSING] = {ITEM_SOCKET1_ADDR: "$D400"}
        header = _header(sid_addresses=(0xD400,), sid_models=("8580",))
        saved = sa.apply_sid_autoconfig(api, header, "auto")
        # The change actually happened...
        self.assertEqual(api.config_store[CAT_ADDRESSING][ITEM_SOCKET2_ADDR], "$D400")
        self.assertEqual(api.config_store[CAT_SOCKETS][ITEM_SOCKET2_EN], "Enabled")
        # ...and the snapshot holds the PRE-change values so it can restore.
        self.assertEqual(saved[(CAT_SOCKETS, ITEM_SOCKET2_EN)], "Disabled")


if __name__ == "__main__":
    unittest.main()
