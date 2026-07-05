"""Tests for the pure U64 multi-SID address planner (c64cast/asid_sidmap.py).

The planner emits ``{(category, item): value}`` REST-config PUTs. To prove those
PUTs actually realize the intended distinct SID addresses, we port the firmware's
address math (u64_config.cc: u64_sid_offsets / split_bits / fix_splits) into a
small oracle here and assert the realized instance addresses match the planner's
`addresses` and are all distinct.
"""

from __future__ import annotations

import unittest

from c64cast import asid_sidmap as m

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
        # Both cores unmapped when only sockets are used.
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID1_ADDR)], m.ADDR_UNMAPPED)
        self.assertEqual(sm.config[(m.CAT_ADDRESSING, m.ITEM_ULTISID2_ADDR)], m.ADDR_UNMAPPED)

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


class RealizationOracleTest(unittest.TestCase):
    """Every planned map must realize its routed addresses as distinct."""

    def _assert_realizable(self, sm: m.SidMap):
        realized: list[int] = []
        cfg = sm.config
        # Sockets (no split in our plans).
        for addr_item in (m.ITEM_SOCKET1_ADDR, m.ITEM_SOCKET2_ADDR):
            v = cfg.get((m.CAT_ADDRESSING, addr_item))
            if v and v != m.ADDR_UNMAPPED:
                realized.append(int(v.lstrip("$"), 16))
        # UltiSID cores (with split).
        split = cfg.get((m.CAT_ADDRESSING, m.ITEM_ULTISID_SPLIT), m.SPLIT_OFF)
        for core_item in (m.ITEM_ULTISID1_ADDR, m.ITEM_ULTISID2_ADDR):
            v = cfg.get((m.CAT_ADDRESSING, core_item))
            if v and v != m.ADDR_UNMAPPED:
                realized.extend(_realize_core(int(v.lstrip("$"), 16), split))
        # All realized addresses distinct (no aliasing).
        self.assertEqual(len(realized), len(set(realized)), f"aliased addresses: {realized}")
        # Every routed address is actually realized by the hardware config.
        for a in sm.addresses:
            self.assertIn(a, realized, f"routed ${a:04X} not realized by {sm.config}")

    def test_all_counts_and_socket_combos(self):
        for n in range(1, m.MAX_SIDS + 1):
            for s1 in (False, True):
                for s2 in (False, True):
                    sm = m.plan_sid_map(n, socket1_present=s1, socket2_present=s2)
                    with self.subTest(n=n, s1=s1, s2=s2):
                        self.assertEqual(len(set(sm.addresses)), sm.n)  # routed distinct
                        self._assert_realizable(sm)


def _realized_addresses(sm: m.SidMap) -> set[int]:
    """Every $Dxxx base the config in `sm` makes a chip answer at (port of the
    firmware address math via _realize_core)."""
    realized: list[int] = []
    cfg = sm.config
    for addr_item in (m.ITEM_SOCKET1_ADDR, m.ITEM_SOCKET2_ADDR):
        v = cfg.get((m.CAT_ADDRESSING, addr_item))
        if v and v != m.ADDR_UNMAPPED:
            realized.append(int(v.lstrip("$"), 16))
    split = cfg.get((m.CAT_ADDRESSING, m.ITEM_ULTISID_SPLIT), m.SPLIT_OFF)
    for core_item in (m.ITEM_ULTISID1_ADDR, m.ITEM_ULTISID2_ADDR):
        v = cfg.get((m.CAT_ADDRESSING, core_item))
        if v and v != m.ADDR_UNMAPPED:
            realized.extend(_realize_core(int(v.lstrip("$"), 16), split))
    return set(realized)


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
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420), socket1_present=True, socket2_present=True
        )
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


if __name__ == "__main__":
    unittest.main()
