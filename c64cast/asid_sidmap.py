"""Pure U64 multi-SID address planner for :class:`~c64cast.asid_scene.AsidScene`.

An ASID stream can carry several SID chips (commands ``0x50``-``0x5F`` =
SID2..SID17; see :mod:`c64cast.asid`). To play them on genuine hardware, the
Ultimate 64 is **dynamically configured for multiple SIDs** — up to 8 across two
physical sockets plus two "UltiSID" FPGA cores, each core splittable across
address lines into 2 or 4 instances. This module decides, for *N* required chips
and which physical sockets carry a detected SID, the U64 **address map**: which
``$Dxxx`` base each ASID chip index is written to, and the exact
``PUT /v1/configs/<category>/<item>`` values that realize it live on the U64.

This is a **pure** planner — no hardware, no REST — so it's unit-tested against a
Python port of the firmware's address math (``_realize_addresses``, mirroring
``u64_config.cc``: ``u64_sid_offsets`` / ``split_bits`` / ``fix_splits``). The
:class:`~c64cast.asid_scene.AsidScene` owns the actual REST calls + restore.

Policy — **prefer physical socket SIDs** (the user's real chips sound better than
the emulated cores for the primary voices):

  1. The lowest ASID indices go to present sockets first, at ``$D400`` (socket 1)
     then ``$D420`` (socket 2).
  2. The remaining chips come from the UltiSID cores, placed on the ``$D5xx``
     page — clear of the sockets in ``$D4xx`` regardless of split level, which
     matters because the firmware force-aligns a split core's base
     (``1/2`` → ``$40``-aligned, ``1/4`` → ``$80``-aligned).
  3. ``Auto Address Mirroring`` is disabled so every base responds distinctly.

Hardware ceiling: 2 sockets + 2 cores × 4 (``1/4`` split) = 10 theoretical, but
ASID tops out at chip 16 and real multi-SID tunes are 2-3 SID. We support up to
:data:`MAX_SIDS` (8); a stream asking for more is clamped (the caller warns).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Config category / item names — must match the firmware exactly (u64_config.cc).
CAT_ADDRESSING = "SID Addressing"
CAT_SOCKETS = "SID Sockets Configuration"

ITEM_SOCKET1_ADDR = "SID Socket 1 Address"
ITEM_SOCKET2_ADDR = "SID Socket 2 Address"
ITEM_ULTISID1_ADDR = "UltiSID 1 Address"
ITEM_ULTISID2_ADDR = "UltiSID 2 Address"
ITEM_ULTISID_SPLIT = "UltiSID Range Split"
ITEM_AUTO_MIRROR = "Auto Address Mirroring"
ITEM_SOCKET1_EN = "SID Socket 1"
ITEM_SOCKET2_EN = "SID Socket 2"
ITEM_SOCKET1_TYPE = "SID Detected Socket 1"
ITEM_SOCKET2_TYPE = "SID Detected Socket 2"

# Address enum value for a disabled slot (u64_sid_base[0]).
ADDR_UNMAPPED = "Unmapped"

# Split enum labels → per-core instance count (u64_config.cc `sid_split`).
SPLIT_OFF = "Off"
SPLIT_HALF = "1/2 (A5)"  # two instances at base, base+$20
SPLIT_QUARTER = "1/4 (A5,A6)"  # four instances at base, +$20, +$40, +$60
_SPLIT_CAPACITY = {SPLIT_OFF: 1, SPLIT_HALF: 2, SPLIT_QUARTER: 4}
# Per-instance stride within a split core (bytes): consecutive $20 boundaries.
_SPLIT_STRIDE = 0x20

# The two socket base addresses (real chips take the low $D4xx slots).
_SOCKET_BASES = (0xD400, 0xD420)
# UltiSID core base pages. With no physical sockets in play the cores start at
# the conventional $D400 (chip 0 stays at $D400, no mid-stream move). When
# sockets occupy $D400/$D420 the cores move to the $D5xx page so they never
# collide — both pages are $80-aligned, so any split (incl. 1/4) realizes
# cleanly after the firmware's base alignment (see fix_splits).
_ULTISID_PAGE_NO_SOCKETS = 0xD400
_ULTISID_PAGE_WITH_SOCKETS = 0xD500

MAX_SIDS = 8


@dataclass(frozen=True)
class SidMap:
    """The realized multi-SID plan.

    ``addresses[i]`` is the ``$Dxxx`` base ASID chip *i* is written to.
    ``config`` is the ordered ``{(category, item): value}`` set of REST PUTs that
    realize this map on the U64. ``requested`` is the chip count asked for and
    ``n`` the count actually realized (clamped to what the hardware can host)."""

    addresses: tuple[int, ...]
    config: dict[tuple[str, str], str] = field(default_factory=dict)
    requested: int = 0

    @property
    def n(self) -> int:
        return len(self.addresses)

    @property
    def clamped(self) -> bool:
        return self.requested > self.n


def _pick_split(tail: int) -> str:
    """Smallest split whose two-core capacity covers `tail` UltiSID instances."""
    for split in (SPLIT_OFF, SPLIT_HALF, SPLIT_QUARTER):
        if 2 * _SPLIT_CAPACITY[split] >= tail:
            return split
    return SPLIT_QUARTER  # capped upstream; 8 is the max two cores can host


def plan_sid_map(
    n_sids: int, *, socket1_present: bool = False, socket2_present: bool = False
) -> SidMap:
    """Plan the U64 address map for `n_sids` ASID chips, preferring physical
    socket SIDs. See the module docstring for the policy.

    `socket1_present` / `socket2_present` reflect whether a real SID is detected
    (and will be enabled) in each socket. The result is clamped to what the
    hardware can realize (2 sockets + up to 8 UltiSID instances, overall
    :data:`MAX_SIDS`)."""
    requested = n_sids
    n_sids = max(0, min(n_sids, MAX_SIDS))

    addresses: list[int] = []
    config: dict[tuple[str, str], str] = {}

    # 1) Sockets first (real chips), lowest indices.
    sockets = []
    if socket1_present:
        sockets.append((ITEM_SOCKET1_ADDR, ITEM_SOCKET1_EN, _SOCKET_BASES[0]))
    if socket2_present:
        sockets.append((ITEM_SOCKET2_ADDR, ITEM_SOCKET2_EN, _SOCKET_BASES[1]))

    used_sockets = min(len(sockets), n_sids)
    for addr_item, en_item, base in sockets[:used_sockets]:
        addresses.append(base)
        config[(CAT_ADDRESSING, addr_item)] = f"${base:04X}"
        config[(CAT_SOCKETS, en_item)] = "Enabled"

    # 2) UltiSID cores fill the tail on the $D5xx page. Unused cores are
    #    explicitly unmapped so a stale prior mapping can't collide.
    tail = n_sids - used_sockets
    if tail > 0:
        split = _pick_split(tail)
        cap = _SPLIT_CAPACITY[split]
        config[(CAT_ADDRESSING, ITEM_ULTISID_SPLIT)] = split
        core1_base = _ULTISID_PAGE_NO_SOCKETS if used_sockets == 0 else _ULTISID_PAGE_WITH_SOCKETS
        config[(CAT_ADDRESSING, ITEM_ULTISID1_ADDR)] = f"${core1_base:04X}"
        # Realize instances core-by-core, lowest address first, until tail met.
        instances = [core1_base + k * _SPLIT_STRIDE for k in range(cap)]
        if tail > cap:
            core2_base = core1_base + cap * _SPLIT_STRIDE
            config[(CAT_ADDRESSING, ITEM_ULTISID2_ADDR)] = f"${core2_base:04X}"
            instances += [core2_base + k * _SPLIT_STRIDE for k in range(cap)]
        else:
            config[(CAT_ADDRESSING, ITEM_ULTISID2_ADDR)] = ADDR_UNMAPPED
        addresses.extend(instances[:tail])
    else:
        config[(CAT_ADDRESSING, ITEM_ULTISID1_ADDR)] = ADDR_UNMAPPED
        config[(CAT_ADDRESSING, ITEM_ULTISID2_ADDR)] = ADDR_UNMAPPED

    # 3) Distinct addresses only.
    config[(CAT_ADDRESSING, ITEM_AUTO_MIRROR)] = "Disabled"

    return SidMap(addresses=tuple(addresses), config=config, requested=requested)
