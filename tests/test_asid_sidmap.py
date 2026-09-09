"""Tests for the pure U64 multi-SID address planner (c64cast/sid/asid_sidmap.py).

The planner emits ``{(category, item): value}`` REST-config PUTs. To prove those
PUTs actually realize the intended distinct SID addresses, we port the firmware's
address math (u64_config.cc: u64_sid_offsets / split_bits / fix_splits) into a
small oracle here and assert the realized instance addresses match the planner's
`addresses` and are all distinct.
"""

from __future__ import annotations

import itertools
import unittest

from c64cast.hw.c64 import RESERVED_IO_WINDOWS
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


# Target sets the address-driven planner is swept over: consecutive runs, a
# split across two pages, a cartridge-I/O base, and one that doesn't start at
# $D400 (a PSID header need not).
_TARGET_SETS: tuple[tuple[int, ...], ...] = (
    (0xD400,),
    (0xD400, 0xD420),
    (0xD400, 0xD420, 0xD440),
    (0xD400, 0xD420, 0xD440, 0xD460),
    (0xD400, 0xD420, 0xD500, 0xD520),
    (0xD400, 0xD500),
    (0xD400, 0xDE00),
    (0xD420, 0xD440),
)
_SOCKET_MODEL_COMBOS: tuple[tuple[str | None, str | None], ...] = (
    (None, None),
    ("6581", None),
    (None, "6581"),
    ("6581", "6581"),
    ("8580", "6581"),
)


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

    def test_for_addresses_over_target_sets_and_socket_models(self):
        """The same oracle over the *other* planner.

        It used to run against `plan_sid_map` only — which gets socket/core
        non-collision for free by moving cores to the $D5xx page — while
        `plan_sid_map_for_addresses`, the one that runs for every .sid file with
        a multi-SID header, went unchecked. That is why a 4-SID header whose
        first base matched a socketed chip's model could enable SID Socket 1 at
        $D400 *and* place a 1/2-split UltiSID core over it.
        """
        for addresses in _TARGET_SETS:
            for socket_models in _SOCKET_MODEL_COMBOS:
                for required in ((), ("6581",) * 8, ("8580",) * 8):
                    sm = m.plan_sid_map_for_addresses(
                        addresses, socket_models=socket_models, required_models=required
                    )
                    if sm is None:
                        continue
                    with self.subTest(a=addresses, s=socket_models, r=required[:1]):
                        self._assert_realizable(sm)
                        self._assert_only_mirrors_alias(sm)

    def test_a_split_core_never_covers_an_enabled_sockets_address(self):
        # The repro: the firmware aligns a 1/2-split core's base down to $D400,
        # pulling its window back over the socket the planner just enabled, so
        # chip 0 sounds on the real chip and the core at once — the "detuned
        # double" the module docstring forbids.
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420, 0xD440, 0xD460),
            socket_models=("6581", None),
            required_models=("6581",) * 4,
        )
        assert sm is not None
        self._assert_realizable(sm)
        self._assert_only_mirrors_alias(sm)
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Disabled")


def _instances_on_reserved_io(sm: m.SidMap) -> list[int]:
    """Every address the firmware would make some source answer at (via the
    _realize_core oracle, not the planner's own arithmetic) whose $20-granular
    span overlaps I/O c64cast drives itself."""
    return sorted(
        {
            addr
            for addrs in _realized_by_source(sm).values()
            for addr in addrs
            if any(addr <= high and addr + 0x1F >= low for low, high in RESERVED_IO_WINDOWS)
        }
    )


class ReservedIoTest(unittest.TestCase):
    """No plan may put a SID where c64cast's own hardware answers.

    The firmware force-aligns a split core's base DOWNWARD, so what the planner
    emits is not what a caller declared — and the declared-address guard in
    `sid_host_emu._decode_extra_sid_addr` never sees the emitted base. A PSID v4
    header with second/third SID bytes $F2 and $F6 declares $DF20 and $DF60,
    both spec-legal, and the 1/4 split that covers them realized at $DF00: a
    live REST PUT putting a SID on the REU's status/command/address/length
    registers, which the DAC audio pump and the ASID ring player both drive and
    which the audio NMI handler reads $DF03 back from mid-transfer. Two bytes of
    a downloaded `.sid` chose it.
    """

    def test_the_reserved_windows_are_the_devices_they_name(self):
        # LITERAL on purpose. Every other assertion in this class asks whether a
        # plan landed in RESERVED_IO_WINDOWS, so an expectation read out of that
        # same tuple moves with it: emptying the tuple left the whole class
        # green while the planner happily based a core on $DF20. $DF00-$DF0A is
        # the REU's status/command/address/length file; $DF20-$DFFF is the
        # Ultimate Audio sampler's seven 32-byte channel files, the range the
        # firmware switch is named after.
        self.assertEqual(RESERVED_IO_WINDOWS, ((0xDF00, 0xDF0A), (0xDF20, 0xDFFF)))

    def test_every_dfxx_base_is_refused_outright(self):
        # Literal inputs, literal expectation, no reference to the tuple under
        # test: with the REU on $DF00 and the sampler filling the rest of the
        # page, no $20-granular base in $DFxx can carry a chip, so each one
        # falls through every split level to the caller's fallback.
        for base in range(0xDF00, 0xE000, 0x20):
            with self.subTest(base=hex(base)):
                self.assertIsNone(m.plan_sid_map_for_addresses((base,)))
                self.assertIsNone(m.plan_sid_map_for_addresses((0xD400, base)))
        # The page below it is untouched — the guard is a carve-out, not a ban
        # on the cartridge window.
        for base in range(0xDE00, 0xDF00, 0x20):
            with self.subTest(base=hex(base)):
                self.assertIsNotNone(m.plan_sid_map_for_addresses((0xD400, base)))

    def test_a_window_inside_one_instance_span_is_still_reached(self):
        # An instance answers its whole $20, not the 25 SID registers, so a
        # reserved window that starts mid-span still has to be caught. Both
        # windows are $20-aligned today, which makes the span and the base
        # equivalent — this is what keeps the wider test honest if one moves.
        self.assertTrue(m._reaches_reserved_io(0xDE00, ((0xDE04, 0xDE0A),)))
        self.assertTrue(m._reaches_reserved_io(0xDE00, ((0xDE1F, 0xDE1F),)))
        self.assertFalse(m._reaches_reserved_io(0xDE00, ((0xDE20, 0xDE2A),)))
        self.assertFalse(m._reaches_reserved_io(0xDE20, ((0xDE04, 0xDE1F),)))

    def test_the_header_pair_that_aligned_a_core_onto_the_reu_is_refused(self):
        # The exploit as run: $D400 + the two declared cartridge bases. No split
        # level can cover them clear of $DF00, so the planner runs out of levels
        # and refuses — WaveformScene then warns and falls back to the canonical
        # layout, which is the loud direction to fail in.
        self.assertIsNone(m.plan_sid_map_for_addresses((0xD400, 0xDF20, 0xDF60)))

    def test_every_split_level_is_exactly_as_wide_as_its_alignment(self):
        # The retry comment's proof that dropping a socket claim cannot rescue a
        # reserved-window exhaustion rests on this and nothing else: a window is
        # `align`-aligned and `align` wide, so any target inside one has
        # `align_down(t) == base` and therefore the same realized base — and the
        # same set of instances for the reserved test to walk — whether it opens
        # its own window or rides in a lower target's. A level whose capacity and
        # alignment disagreed would break that silently, leaving a comment that
        # argues for behavior the code no longer has.
        for split, cap, align in m._SPLIT_LEVELS:
            with self.subTest(split=split):
                self.assertEqual(cap * m._SPLIT_STRIDE, align)

    def test_giving_the_socket_up_does_not_rescue_a_reserved_window(self):
        # Same targets, one field varied: a socket that carries a chip for
        # $D400. The caller drops the claim and re-plans when the cores run out
        # of levels, and the comment there used to say a claimed socket could be
        # what left every level *reserved* — so a reader would expect this to
        # come back with a plan. It cannot: $DF20's realized base is $DF20 at
        # the `Off` level and $DF00 at the two wider ones, and all three of
        # those addresses sit inside RESERVED_IO_WINDOWS ($DF00-$DF0A, the REU;
        # $DF20-$DFFF, the sampler) whether or not $D400 is claimed. The retry
        # addresses `blocked` and only `blocked`.
        self.assertIsNone(
            m.plan_sid_map_for_addresses((0xD400, 0xDF20, 0xDF60), socket_models=("6581", None))
        )

    def test_a_core_alone_is_never_based_on_reserved_io(self):
        # One field varied: the pair, and whether a socket is in play at all.
        # A near miss that still lands a core on a reserved register would make
        # the fix a speed bump.
        for targets in (
            (0xD400, 0xDF20, 0xDF40),
            (0xD400, 0xDF40, 0xDF60),
            (0xD400, 0xDFC0, 0xDFE0),
            (0xD400, 0xDF80, 0xDFE0),
            (0xDF20,),
            (0xDF00,),
            (0xDFE0,),
            (0xDE00, 0xDF20),
        ):
            for socket_models in ((None, None), ("6581", "6581")):
                sm = m.plan_sid_map_for_addresses(targets, socket_models=socket_models)
                with self.subTest(t=[hex(t) for t in targets], s=socket_models):
                    if sm is None:
                        continue
                    self.assertEqual(_instances_on_reserved_io(sm), [], sm.config)

    def test_no_target_set_at_all_can_place_a_core_on_reserved_io(self):
        """Close the class rather than the two bytes that opened it: sweep every
        1-, 2- and 3-target set drawn from every base the firmware's own enum
        permits, through both planners, and assert the *realized* instances
        clear every reserved window. The declared-address guard is the first
        line; this is the one that holds when alignment moves the base."""
        every_base = [
            base
            for low, high in m._ULTISID_BASE_WINDOWS
            for base in range(low, high + 1, m._SPLIT_STRIDE)
        ]
        for size in (1, 2, 3):
            for targets in itertools.combinations(every_base, size):
                for socket_models in ((None, None), ("6581", "6581")):
                    sm = m.plan_sid_map_for_addresses(targets, socket_models=socket_models)
                    if sm is None:
                        continue
                    landed = _instances_on_reserved_io(sm)
                    if landed:  # subTest per case would cost more than the sweep
                        self.fail(
                            f"{[hex(t) for t in targets]} / {socket_models} realized "
                            f"{[hex(a) for a in landed]} in {sm.config}"
                        )
        for n in range(1, m.MAX_SIDS + 1):
            for s1 in (False, True):
                for s2 in (False, True):
                    sm = m.plan_sid_map(n, socket1_present=s1, socket2_present=s2)
                    with self.subTest(n=n, s1=s1, s2=s2):
                        self.assertEqual(_instances_on_reserved_io(sm), [], sm.config)


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

    def test_core_base_outside_the_firmware_enum_is_unrealizable(self):
        # The base is bounded below ($D400) and now above: the firmware's
        # u64_sid_base[] enum covers $D400-$D7E0 and $DE00-$DFE0 only, so a
        # header-chosen target elsewhere must fall back, not be PUT verbatim.
        self.assertIsNone(m.plan_sid_map_for_addresses((0xDA00,)))
        self.assertIsNone(m.plan_sid_map_for_addresses((0xD400, 0xD800)))
        # Both ends of the legal windows still plan — except the top of the
        # cartridge one, which RESERVED_IO_WINDOWS trims (see ReservedIoTest);
        # $DEE0 is the highest base whose $20 span clears the REU at $DF00.
        for legal in (0xD400, 0xD7E0, 0xDE00, 0xDEE0):
            self.assertIsNotNone(m.plan_sid_map_for_addresses((legal,)), hex(legal))


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

    def test_a_claim_that_boxes_the_cores_in_is_given_up_for_the_map(self):
        # The one thing the socket-give-up retry is for, and it had no test at
        # all. Socket 1 carries a chip, so it claims $D400, and the other three
        # addresses then defeat all three levels: `Off` gives one address per
        # core and there are only two cores, while the two levels wide enough to
        # cover three ($40- and $80-aligned) both align back onto $D400 and are
        # rejected as `blocked`. Measured per level, because "blocked at every
        # level" would be the wrong reason for the first one.
        self.assertIsNone(
            m._plan_ultisid_cores([0xD420, 0xD440, 0xD460], blocked=frozenset({0xD400}))
        )

        # So the claim is dropped rather than the map. Asserted on what the
        # caller can observe: every chip is answered, by a core rather than the
        # socket, and the socket is explicitly disabled despite carrying a
        # 6581 — "every chip audible on emulated cores beats handing back None".
        sm = m.plan_sid_map_for_addresses(
            (0xD400, 0xD420, 0xD440, 0xD460), socket_models=("6581", None)
        )
        assert sm is not None
        self.assertEqual(sm.addresses, (0xD400, 0xD420, 0xD440, 0xD460))
        self.assertEqual(sm.sources, ("ultisid1", "ultisid1", "ultisid2", "ultisid2"))
        self.assertEqual(sm.config[(m.CAT_SOCKETS, m.ITEM_SOCKET1_EN)], "Disabled")


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

    def test_through_four_chips_every_source_is_distinct_with_both_sockets(self):
        # The pannable-independently guarantee sid_panning documents — which
        # holds only when both sockets are populated. See the no-socket case
        # below for what the ordinary ASID stream on a stock U64 actually gets.
        for n in range(1, 5):
            sm = m.plan_sid_map(n, socket1_present=True, socket2_present=True)
            self.assertEqual(len(set(sm.sources)), n, sm.sources)

    def test_with_no_socket_in_play_sharing_starts_at_three_chips(self):
        # Two UltiSID cores are the only sources, so the third chip necessarily
        # doubles onto a split core and shares its pan. SidMap's docstring used
        # to promise distinct sources until 5 chips.
        self.assertEqual(m.plan_sid_map(2).sources, ("ultisid1", "ultisid2"))
        self.assertEqual(m.plan_sid_map(3).sources, ("ultisid1", "ultisid1", "ultisid2"))

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
