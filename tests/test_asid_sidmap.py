"""Tests for the pure U64 multi-SID address planner (c64cast/sid/asid_sidmap.py).

The planner emits ``{(category, item): value}`` REST-config PUTs. To prove those
PUTs actually realize the intended distinct SID addresses, we port the firmware's
address math (u64_config.cc: u64_sid_offsets / split_bits / fix_splits) into a
small oracle here and assert the realized instance addresses match the planner's
`addresses` and are all distinct.
"""

from __future__ import annotations

import unittest

from c64cast.sid import asid_sidmap as m

# --- firmware address-math oracle (port of u64_config.cc) --------------------

# sid_split enum → split_bits (offset-space bits, i.e. address bits >> 4).
_SPLIT_BITS = {
    m.SPLIT_OFF: 0x00,
    m.SPLIT_HALF: 0x02,  # A5
    m.SPLIT_QUARTER: 0x06,  # A5,A6
}


def _addr_to_offset(addr: int) -> int:
    """Firmware base byte = (addr >> 4) & 0xFF (u64_sid_offsets space)."""
    return (addr >> 4) & 0xFF


def _offset_to_addr(off: int) -> int:
    return 0xD000 | (off << 4)


def _realize_core(base_addr: int, split_label: str) -> list[int]:
    """Realize the distinct instance addresses a split UltiSID core answers at,
    applying the firmware's fix_splits base-alignment (base &= ~split)."""
    split = _SPLIT_BITS[split_label]
    base_off = _addr_to_offset(base_addr) & ~split  # fix_splits
    # Instances = base OR every subset of the split bits.
    subbits = [b for b in (0x02, 0x04) if split & b]
    offs = {base_off}
    for combo in range(1 << len(subbits)):
        off = base_off
        for i, b in enumerate(subbits):
            if combo & (1 << i):
                off |= b
        offs.add(off)
    return sorted(_offset_to_addr(o) for o in offs)


class PlanBasicsTest(unittest.TestCase):
    def test_single_socket_only(self):
        sm = m.plan_sid_map(1, socket1_present=True)
        self.assertEqual(sm.addresses, (0xD400,))
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_SOCKET1_ADDR)], "$D400")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Enabled")
        # The spare core shadows the socket so the U64's LED display still
        # lights (it plays no chip of its own, and sid_volume leaves it muted).
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID1_ADDR)], "$D400")
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID2_ADDR)], m.ADDR_UNMAPPED)
        self.assertEqual(sm.sources, ("socket1",))

    def test_unclaimed_socket_is_disabled(self):
        # A socket left enabled at an address the plan gave to a core answers
        # alongside it — the tune would play on both chips at once.
        sm = m.plan_sid_map(1, socket1_present=True, socket2_present=True)
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Enabled")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET2_EN)], "Disabled")

    def test_both_sockets_disabled_when_cores_play_everything(self):
        sm = m.plan_sid_map(2)
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Disabled")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET2_EN)], "Disabled")

    def test_no_mirror_when_both_cores_carry_chips(self):
        sm = m.plan_sid_map(3, socket1_present=True)
        cores = {
            sm.config[(m.CAT_ADDRESSING, item)]
            for item in (m.ITEM_ULTISID1_ADDR, m.ITEM_ULTISID2_ADDR)
        }
        self.assertNotIn("$D400", cores, "socket address must not be shadowed by a playing core")

    def test_single_no_socket_uses_ultisid_at_d400(self):
        # No sockets → cores stay on the conventional $D400 page (chip 0 = $D400).
        sm = m.plan_sid_map(1)
        self.assertEqual(sm.addresses, (0xD400,))
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID1_ADDR)], "$D400")

    def test_two_no_sockets_ultisid_pair(self):
        sm = m.plan_sid_map(2)
        self.assertEqual(sm.addresses, (0xD400, 0xD420))

    def test_two_sockets(self):
        sm = m.plan_sid_map(2, socket1_present=True, socket2_present=True)
        self.assertEqual(sm.addresses, (0xD400, 0xD420))

    def test_ultisid_moves_to_d5xx_when_socket_used(self):
        # 2 chips, socket1 present: chip 0 → socket $D400, chip 1 → UltiSID $D5xx.
        sm = m.plan_sid_map(2, socket1_present=True)
        self.assertEqual(sm.addresses[0], 0xD400)
        self.assertGreaterEqual(sm.addresses[1], 0xD500)

    def test_mirroring_always_disabled(self):
        sm = m.plan_sid_map(3)
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_AUTO_MIRROR)], "Disabled")

    def test_prefer_physical_sockets_take_low_indices(self):
        # 3 chips, socket1 present: chip 0 → socket at $D400, chips 1-2 → UltiSID.
        sm = m.plan_sid_map(3, socket1_present=True)
        self.assertEqual(sm.addresses[0], 0xD400)
        self.assertTrue(all(a >= m._ULTISID_PAGE_WITH_SOCKETS for a in sm.addresses[1:]))

    def test_clamped_above_max(self):
        sm = m.plan_sid_map(12)
        self.assertEqual(sm.requested, 12)
        self.assertLessEqual(sm.n, m.MAX_SIDS)
        self.assertTrue(sm.clamped)


def _realized_by_source(sm: m.SidMap) -> dict[str, list[int]]:
    """Every $Dxxx base each audio source answers at under `sm`'s config (port
    of the firmware address math via _realize_core). A disabled socket answers
    nothing, so the enable item gates it."""
    cfg = sm.config
    by_source: dict[str, list[int]] = {}
    for index, (addr_item, en_item) in enumerate(
        ((m.ITEM_SOCKET1_ADDR, m.ITEM_SOCKET1_EN), (m.ITEM_SOCKET2_ADDR, m.ITEM_SOCKET2_EN))
    ):
        value = cfg.get((m.CAT_ADDRESSING, addr_item))
        if cfg.get((m.CAT_SOCKETS, en_item)) == "Enabled" and value and value != m.ADDR_UNMAPPED:
            by_source[f"socket{index + 1}"] = [int(value.lstrip("$"), 16)]
    split = cfg.get((m.CAT_ADDRESSING, m.ITEM_ULTISID_SPLIT), m.SPLIT_OFF)
    for index, core_item in enumerate((m.ITEM_ULTISID1_ADDR, m.ITEM_ULTISID2_ADDR)):
        value = cfg.get((m.CAT_ADDRESSING, core_item))
        if value and value != m.ADDR_UNMAPPED:
            by_source[f"ultisid{index + 1}"] = _realize_core(int(value.lstrip("$"), 16), split)
    return by_source


def _realized_addresses(sm: m.SidMap) -> set[int]:
    """Every $Dxxx base the config in `sm` makes some source answer at."""
    return {addr for addrs in _realized_by_source(sm).values() for addr in addrs}


class RealizationOracleTest(unittest.TestCase):
    """Every planned map must realize each routed chip on the source that plans
    to play it, with no aliasing beyond the deliberate LED mirrors."""

    def _assert_realizable(self, sm: m.SidMap):
        by_source = _realized_by_source(sm)
        for address, source in zip(sm.addresses, sm.sources, strict=True):
            self.assertIn(
                address,
                by_source.get(source, []),
                f"routed ${address:04X} not realized by {source} in {sm.config}",
            )

    def _assert_only_mirrors_alias(self, sm: m.SidMap):
        """Two sources may answer one address only when one of them is a spare
        core shadowing a socket for the LEDs — never two sources both playing
        chips, which would sound as a detuned double."""
        by_source = _realized_by_source(sm)
        playing = set(sm.sources)
        for source, addrs in by_source.items():
            for other, other_addrs in by_source.items():
                overlap = set(addrs) & set(other_addrs)
                if other <= source or not overlap:
                    continue
                spares = [
                    s for s in (source, other) if s not in playing and s.startswith("ultisid")
                ]
                self.assertEqual(
                    len(spares),
                    1,
                    f"{source} and {other} both answer "
                    f"{[hex(a) for a in sorted(overlap)]} in {sm.config}",
                )

    def test_all_counts_and_socket_combos(self):
        for n in range(1, m.MAX_SIDS + 1):
            for s1 in (False, True):
                for s2 in (False, True):
                    sm = m.plan_sid_map(n, socket1_present=s1, socket2_present=s2)
                    with self.subTest(n=n, s1=s1, s2=s2):
                        self.assertEqual(len(set(sm.addresses)), sm.n)  # routed distinct
                        self._assert_realizable(sm)
                        self._assert_only_mirrors_alias(sm)


class PlanForAddressesTest(unittest.TestCase):
    """plan_sid_map_for_addresses: realize a SID file's *own* fixed chip
    addresses, or return None when the hardware can't."""

    def _assert_realizes(self, addrs, **kw):
        sm = m.plan_sid_map_for_addresses(tuple(addrs), **kw)
        self.assertIsNotNone(sm, f"{[hex(a) for a in addrs]} unexpectedly unrealizable")
        assert sm is not None  # narrow for type checker
        self.assertEqual(sm.addresses, tuple(addrs))  # routed verbatim
        realized = _realized_addresses(sm)
        for a in addrs:
            self.assertIn(a, realized, f"${a:04X} not realized by {sm.config}")

    def test_single_sid(self):
        self._assert_realizes([0xD400])

    def test_consecutive_two(self):
        self._assert_realizes([0xD400, 0xD420])

    def test_consecutive_three(self):
        self._assert_realizes([0xD400, 0xD420, 0xD440])

    def test_two_distinct_pages(self):
        self._assert_realizes([0xD400, 0xD500])

    def test_second_sid_at_de00(self):
        self._assert_realizes([0xD400, 0xDE00])

    def test_socket_serves_matching_target(self):
        sm = m.plan_sid_map_for_addresses((0xD400, 0xD420), socket_models=("6581", "6581"))
        assert sm is not None
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Enabled")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET2_EN)], "Enabled")
        self.assertIn(0xD400, _realized_addresses(sm))
        self.assertIn(0xD420, _realized_addresses(sm))

    def test_three_scattered_pages_unrealizable(self):
        # $D400 + $DE00 + $DF00 needs 3 core windows — only 2 cores exist.
        self.assertIsNone(m.plan_sid_map_for_addresses((0xD400, 0xDE00, 0xDF00)))

    def test_empty_returns_none(self):
        self.assertIsNone(m.plan_sid_map_for_addresses(()))


class ModelAwareRoutingTest(unittest.TestCase):
    """A socket may only claim an address when it carries the model that chip
    asks for — routing and model matching decided in the same pass."""

    def test_light_years_3x8580_on_6581_sockets_goes_all_ultisid(self):
        # HW repro: Jammer's "Light Years" (3 chips at $D400/$D420/$D440, all
        # tagged 8580) on a machine with 6581s in both sockets. Routing the
        # first two onto those sockets and letting a later model pass move them
        # is what left the third chip addressed to nothing.
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420, 0xD440),
            socket_models=("6581", "6581"),
            required_models=("8580", "8580", "8580"),
        )
        assert sm is not None
        self.assertEqual(sm.sources, ("ultisid1", "ultisid1", "ultisid2"))
        for address in (0xD400, 0xD420, 0xD440):
            self.assertIn(address, _realized_addresses(sm))
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Disabled")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET2_EN)], "Disabled")
        self.assertEqual(sm.config[(m.CAT_ULTISID, m.ITEM_ULTISID1_FILTER)], m.FILTER_CURVE_8580)
        self.assertEqual(sm.config[(m.CAT_ULTISID, m.ITEM_ULTISID2_FILTER)], m.FILTER_CURVE_8580)

    def test_matching_socket_still_claims_its_address(self):
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420),
            socket_models=("8580", "6581"),
            required_models=("8580", "8580"),
        )
        assert sm is not None
        self.assertEqual(sm.sources, ("socket1", "ultisid1"))
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Enabled")
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET2_EN)], "Disabled")

    def test_no_requirement_lets_any_socket_claim(self):
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420), socket_models=("6581", "6581"), required_models=(None, "?")
        )
        assert sm is not None
        self.assertEqual(sm.sources, ("socket1", "socket2"))

    def test_curve_follows_the_model_each_core_hosts(self):
        sm = m.plan_sid_map_for_addresses(
            (0xD400,), socket_models=(None, None), required_models=("6581",)
        )
        assert sm is not None
        self.assertEqual(sm.config[(m.CAT_ULTISID, m.ITEM_ULTISID1_FILTER)], m.FILTER_CURVE_6581)

    def test_no_curve_written_when_the_tune_states_no_model(self):
        sm = m.plan_sid_map_for_addresses((0xD400,))
        assert sm is not None
        self.assertNotIn((m.CAT_ULTISID, m.ITEM_ULTISID1_FILTER), sm.config)

    def test_socket_tune_mirrors_spare_cores_for_the_leds(self):
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420), socket_models=("6581", "6581"), required_models=("6581", "6581")
        )
        assert sm is not None
        self.assertEqual(sm.sources, ("socket1", "socket2"))
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID1_ADDR)], "$D400")
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID2_ADDR)], "$D420")


class SidMapSourcesTest(unittest.TestCase):
    """`sources` names the audio source realizing each chip, parallel to
    `addresses` — what sid_panning pans (a pan is per source, not per address)."""

    def test_sources_are_parallel_to_addresses(self):
        for n in range(1, m.MAX_SIDS + 1):
            for s1, s2 in ((False, False), (True, False), (True, True)):
                sm = m.plan_sid_map(n, socket1_present=s1, socket2_present=s2)
                self.assertEqual(len(sm.sources), len(sm.addresses), f"n={n} s1={s1} s2={s2}")

    def test_every_source_is_a_known_mixer_source(self):
        known = {"socket1", "socket2", "ultisid1", "ultisid2"}
        for n in range(1, m.MAX_SIDS + 1):
            sm = m.plan_sid_map(n, socket1_present=True, socket2_present=True)
            self.assertTrue(set(sm.sources) <= known, sm.sources)

    def test_sockets_are_preferred_then_cores(self):
        sm = m.plan_sid_map(4, socket1_present=True, socket2_present=True)
        self.assertEqual(sm.sources, ("socket1", "socket2", "ultisid1", "ultisid2"))

    def test_cores_only_when_no_sockets(self):
        sm = m.plan_sid_map(2)
        self.assertEqual(sm.sources, ("ultisid1", "ultisid2"))

    def test_through_four_chips_every_source_is_distinct(self):
        # The pannable-independently guarantee sid_panning documents.
        for n in range(1, 5):
            sm = m.plan_sid_map(n, socket1_present=True, socket2_present=True)
            self.assertEqual(len(set(sm.sources)), n, sm.sources)

    def test_split_core_hosts_several_chips_on_one_source(self):
        sm = m.plan_sid_map(6, socket1_present=True, socket2_present=True)
        self.assertEqual(sm.sources.count("ultisid1"), 2)
        self.assertEqual(sm.sources.count("ultisid2"), 2)

    def test_for_addresses_sources_follow_the_requested_order(self):
        sm = m.plan_sid_map_for_addresses((0xD400, 0xD420, 0xD440), socket_models=("6581", "6581"))
        assert sm is not None
        self.assertEqual(sm.sources, ("socket1", "socket2", "ultisid1"))

    def test_for_addresses_uses_cores_when_no_sockets(self):
        sm = m.plan_sid_map_for_addresses((0xD400, 0xD500))
        assert sm is not None
        self.assertEqual(sm.sources, ("ultisid1", "ultisid2"))

    def test_for_addresses_sources_are_parallel_to_addresses(self):
        addresses = (0xD400, 0xD420, 0xD440, 0xD460)
        sm = m.plan_sid_map_for_addresses(addresses, socket_models=("6581", "6581"))
        assert sm is not None
        self.assertEqual(len(sm.sources), len(addresses))


if __name__ == "__main__":
    unittest.main()
