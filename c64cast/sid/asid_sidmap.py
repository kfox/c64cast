"""Pure U64 multi-SID address planner for :class:`~c64cast.sid.asid_scene.AsidScene`.

An ASID stream can carry several SID chips (commands ``0x50``-``0x5F`` =
SID2..SID17; see :mod:`c64cast.sid.asid`). To play them on genuine hardware, the
Ultimate 64 is **dynamically configured for multiple SIDs** — up to 8 across two
physical sockets plus two "UltiSID" FPGA cores, each core splittable across
address lines into 2 or 4 instances. This module decides, for *N* required chips
and which physical sockets carry a detected SID, the U64 **address map**: which
``$Dxxx`` base each ASID chip index is written to, and the exact
``PUT /v1/configs/<category>/<item>`` values that realize it live on the U64.

This is a **pure** planner — no hardware, no REST — so it's unit-tested against a
Python port of the firmware's address math (``_realize_addresses``, mirroring
``u64_config.cc``: ``u64_sid_offsets`` / ``split_bits`` / ``fix_splits``). The
:class:`~c64cast.sid.asid_scene.AsidScene` owns the actual REST calls + restore.

Policy — **prefer physical socket SIDs** (the user's real chips sound better than
the emulated cores for the primary voices):

  1. The lowest ASID indices go to present sockets first, at ``$D400`` (socket 1)
     then ``$D420`` (socket 2).
  2. The remaining chips come from the UltiSID cores, placed on the ``$D5xx``
     page — clear of the sockets in ``$D4xx`` regardless of split level, which
     matters because the firmware force-aligns a split core's base
     (``1/2`` → ``$40``-aligned, ``1/4`` → ``$80``-aligned).
  3. ``Auto Address Mirroring`` is disabled so every base responds distinctly.
  4. Every socket the plan does **not** claim is explicitly ``Disabled``. A
     socket left enabled at an address the plan just handed to an UltiSID core
     answers it too, so the tune plays on both the real chip and the core at
     once — audible as a detuned double, and the reason a stale enable from a
     previous run must never be allowed to survive into this one.
  5. A core left over after every chip is placed is **mirrored** onto a
     socket-served address rather than unmapped (see :func:`mirror_bases`).

:func:`plan_sid_map_for_addresses` additionally takes the tune's per-chip model
requirements, so a socket only claims an address when its chip is the model the
tune asked for. Without that, a 3×8580 tune on a 6581-socketed machine gets its
first two chips routed onto the wrong chips here, and the model-autoconfig pass
that runs afterwards has to fight this planner to undo it — the two disagree,
and the chip that loses ends up mapped to nothing at all.

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

# UltiSID filter-curve config — a separate category from CAT_ADDRESSING/
# CAT_SOCKETS (confirmed live against a U64: GET /v1/configs/UltiSID%20Configuration).
CAT_ULTISID = "UltiSID Configuration"
ITEM_ULTISID1_FILTER = "UltiSID 1 Filter Curve"
ITEM_ULTISID2_FILTER = "UltiSID 2 Filter Curve"
# Fixed representative curve per requested model (the full enum also has
# "8580 Hi", "6581 Alt", "U2 Low/Mid/High" — not exposed as a config knob in
# this pass). Confirmed live values via GET .../UltiSID%201%20Filter%20Curve.
FILTER_CURVE_6581 = "6581"
FILTER_CURVE_8580 = "8580 Lo"

# PSID header model values that carry no definite requirement — any chip
# satisfies them, so they never force a socket swap or an UltiSID fallback.
# Shared with sid_autoconfig (which cannot be imported from here: it imports
# these names).
NO_MODEL_REQUIREMENT = (None, "?", "6581+8580")

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

# (address item, enable item, fixed base) per physical socket, socket 1 first.
_SOCKET_SPECS: tuple[tuple[str, str, int], ...] = (
    (ITEM_SOCKET1_ADDR, ITEM_SOCKET1_EN, _SOCKET_BASES[0]),
    (ITEM_SOCKET2_ADDR, ITEM_SOCKET2_EN, _SOCKET_BASES[1]),
)
_CORE_ADDR_ITEMS: tuple[str, str] = (ITEM_ULTISID1_ADDR, ITEM_ULTISID2_ADDR)
_CORE_FILTER_ITEMS: tuple[str, str] = (ITEM_ULTISID1_FILTER, ITEM_ULTISID2_FILTER)
N_ULTISID_CORES = len(_CORE_ADDR_ITEMS)


def curve_for_model(model: str | None) -> str | None:
    """The representative UltiSID filter curve for a required chip model, or
    None when the model carries no definite requirement."""
    if model in NO_MODEL_REQUIREMENT:
        return None
    return FILTER_CURVE_6581 if model == "6581" else FILTER_CURVE_8580


def disable_unclaimed_sockets(config: dict[tuple[str, str], str], claimed: set[str]) -> None:
    """Explicitly ``Disabled`` every socket not in `claimed` (a set of
    ``"socket1"``/``"socket2"``). Unconditional rather than gated on chip
    detection: an enabled socket sitting at an address this plan just gave to an
    UltiSID core answers alongside it, and detection is exactly the thing that
    can't be trusted when a stale config is the problem."""
    for index, (_addr_item, en_item, _base) in enumerate(_SOCKET_SPECS):
        if f"socket{index + 1}" not in claimed:
            config[(CAT_SOCKETS, en_item)] = "Disabled"


def mirror_bases(split: str, core_bases: list[int], socket_bases: list[int]) -> list[int]:
    """Socket-served addresses for the leftover UltiSID cores to shadow.

    The U64's built-in LED display is driven by UltiSID core activity, so a tune
    playing entirely on socketed chips lights nothing unless a core is also
    listening at those addresses. Pointing the spare cores at the socket
    addresses restores it — this is how the firmware ships by default
    (``UltiSID 1 = $D400``, ``UltiSID 2 = $D420``, both at ``Vol OFF``). The
    mirrors stay muted, because :mod:`c64cast.sid.sid_volume` only raises sources a
    tune actually plays on, so they contribute LEDs and no audio.

    Skipped unless the split is off: a split core answers a window of 2 or 4
    addresses and a mirror's extra instances would collide with the real chips.
    That costs nothing in practice — a split is only ever chosen when both cores
    are already hosting real chips, leaving nothing spare to mirror with."""
    if split != SPLIT_OFF:
        return []
    spare = N_ULTISID_CORES - len(core_bases)
    return socket_bases[: max(0, spare)]


def _assign_cores(
    config: dict[tuple[str, str], str], bases: list[int], curves: list[str | None]
) -> None:
    """Write each UltiSID core's address (and filter curve, where the model is
    definite), unmapping the cores `bases` doesn't reach so a stale mapping from
    a previous run can't answer an address this plan didn't intend."""
    for index, addr_item in enumerate(_CORE_ADDR_ITEMS):
        base = bases[index] if index < len(bases) else None
        config[(CAT_ADDRESSING, addr_item)] = ADDR_UNMAPPED if base is None else f"${base:04X}"
        curve = curves[index] if index < len(curves) else None
        if curve is not None:
            config[(CAT_ULTISID, _CORE_FILTER_ITEMS[index])] = curve


@dataclass(frozen=True)
class SidMap:
    """The realized multi-SID plan.

    ``addresses[i]`` is the ``$Dxxx`` base ASID chip *i* is written to.
    ``config`` is the ordered ``{(category, item): value}`` set of REST PUTs that
    realize this map on the U64. ``requested`` is the chip count asked for and
    ``n`` the count actually realized (clamped to what the hardware can host).

    ``sources[i]`` names the audio *source* — ``"socket1"``/``"socket2"``/
    ``"ultisid1"``/``"ultisid2"`` — realizing chip *i*, parallel to
    ``addresses``. The U64 mixes each source at its own stereo pan, so
    :mod:`c64cast.sid.sid_panning` needs the source (not the address) to pan a
    chip. Two chips can share one source when a split core hosts both (≥5
    SIDs); they then share that source's pan."""

    addresses: tuple[int, ...]
    config: dict[tuple[str, str], str] = field(default_factory=dict)
    requested: int = 0
    sources: tuple[str, ...] = ()

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
    (and will be enabled) in each socket; sockets the plan doesn't claim are
    explicitly disabled. The result is clamped to what the hardware can realize
    (2 sockets + up to 8 UltiSID instances, overall :data:`MAX_SIDS`).

    An ASID stream carries no chip-model information, so this planner routes on
    presence alone — :func:`plan_sid_map_for_addresses` is the model-aware one."""
    requested = n_sids
    n_sids = max(0, min(n_sids, MAX_SIDS))

    addresses: list[int] = []
    sources: list[str] = []
    config: dict[tuple[str, str], str] = {}

    # 1) Sockets first (real chips), lowest indices.
    sockets = []
    if socket1_present:
        sockets.append((ITEM_SOCKET1_ADDR, ITEM_SOCKET1_EN, _SOCKET_BASES[0], "socket1"))
    if socket2_present:
        sockets.append((ITEM_SOCKET2_ADDR, ITEM_SOCKET2_EN, _SOCKET_BASES[1], "socket2"))

    used_sockets = min(len(sockets), n_sids)
    for addr_item, en_item, base, source in sockets[:used_sockets]:
        addresses.append(base)
        sources.append(source)
        config[(CAT_ADDRESSING, addr_item)] = f"${base:04X}"
        config[(CAT_SOCKETS, en_item)] = "Enabled"

    # 2) UltiSID cores fill the tail on the $D5xx page. Cores left over after
    #    the tail is placed mirror the socket addresses (LED display); cores
    #    beyond even that are unmapped so a stale mapping can't collide.
    tail = n_sids - used_sockets
    split = _pick_split(tail)
    config[(CAT_ADDRESSING, ITEM_ULTISID_SPLIT)] = split
    core_bases: list[int] = []
    if tail > 0:
        cap = _SPLIT_CAPACITY[split]
        core1_base = _ULTISID_PAGE_NO_SOCKETS if used_sockets == 0 else _ULTISID_PAGE_WITH_SOCKETS
        core_bases.append(core1_base)
        # Realize instances core-by-core, lowest address first, until tail met.
        instances = [core1_base + k * _SPLIT_STRIDE for k in range(cap)]
        instance_sources = ["ultisid1"] * cap
        if tail > cap:
            core2_base = core1_base + cap * _SPLIT_STRIDE
            core_bases.append(core2_base)
            instances += [core2_base + k * _SPLIT_STRIDE for k in range(cap)]
            instance_sources += ["ultisid2"] * cap
        addresses.extend(instances[:tail])
        sources.extend(instance_sources[:tail])

    mirrors = mirror_bases(split, core_bases, addresses[:used_sockets])
    _assign_cores(config, core_bases + mirrors, curves=[])

    # 3) Distinct addresses only.
    config[(CAT_ADDRESSING, ITEM_AUTO_MIRROR)] = "Disabled"

    disable_unclaimed_sockets(config, set(sources[:used_sockets]))

    return SidMap(
        addresses=tuple(addresses),
        config=config,
        requested=requested,
        sources=tuple(sources),
    )


# Split label → (per-core instance capacity, base-address alignment). The
# firmware force-aligns a split core's base: 1/2 → $40, 1/4 → $80 (see the
# module docstring / u64_config.cc fix_splits). Split off → any $20-granular
# base. Both UltiSID cores share ONE split setting.
_SPLIT_LEVELS: tuple[tuple[str, int, int], ...] = (
    (SPLIT_OFF, 1, 0x20),
    (SPLIT_HALF, 2, 0x40),
    (SPLIT_QUARTER, 4, 0x80),
)
# Lowest base an UltiSID core may sit at ($D400 page; below this is unmapped).
_ULTISID_MIN_BASE = 0xD400


def _plan_ultisid_cores(targets: list[int]) -> tuple[str, list[int]] | None:
    """Cover `targets` (a list of $Dxx0 bases) with up to two UltiSID cores that
    share one split. Returns ``(split_label, [core_base, ...])`` or None if two
    cores can't realize the set. Extra instances a split creates beyond the
    targets are harmless (that address simply stays silent)."""
    if not targets:
        return (SPLIT_OFF, [])
    for split, cap, align in _SPLIT_LEVELS:
        bases: list[int] = []
        covered: set[int] = set()
        realizable = True
        for t in sorted(set(targets)):
            if t in covered:
                continue
            base = t & ~(align - 1) & 0xFFFF
            if base < _ULTISID_MIN_BASE:
                realizable = False
                break
            window = {base + k * _SPLIT_STRIDE for k in range(cap)}
            if t not in window:  # alignment pushed the window past t
                realizable = False
                break
            bases.append(base)
            covered |= window
            if len(bases) > 2:
                realizable = False
                break
        if realizable and len(bases) <= 2 and all(t in covered for t in targets):
            return (split, bases)
    return None


def _source_for_address(
    addr: int, served_by_socket: dict[int, str], core_bases: list[int], capacity: int
) -> str:
    """Which audio source realizes `addr`: its physical socket if one serves it,
    else the UltiSID core whose split window covers it (``""`` if neither —
    an address the map doesn't actually answer). Core windows are disjoint by
    construction, so the first match is the only match."""
    socket = served_by_socket.get(addr)
    if socket:
        return socket
    for core_index, core_base in enumerate(core_bases):
        window = {core_base + k * _SPLIT_STRIDE for k in range(capacity)}
        if addr in window:
            return f"ultisid{core_index + 1}"
    return ""


def _models_by_address(
    addresses: tuple[int, ...], required_models: tuple[str | None, ...]
) -> dict[int, str | None]:
    """The definite model requirement at each address, or None where the tune
    states none. Two chips at one address can't disagree in practice (a PSID
    header lists each base once), so first-wins is enough."""
    models: dict[int, str | None] = {}
    for address, model in zip(addresses, required_models, strict=False):
        if models.get(address) is None:
            models[address] = None if model in NO_MODEL_REQUIREMENT else model
    return models


def _core_curve(base: int, capacity: int, models: dict[int, str | None]) -> str | None:
    """The filter curve for a core at `base`: the model required by the first
    chip in its split window that asks for one."""
    window = [base + k * _SPLIT_STRIDE for k in range(capacity)]
    required = next((models.get(addr) for addr in window if models.get(addr)), None)
    return curve_for_model(required)


def plan_sid_map_for_addresses(
    addresses: tuple[int, ...],
    *,
    socket_models: tuple[str | None, str | None] = (None, None),
    required_models: tuple[str | None, ...] = (),
) -> SidMap | None:
    """Plan a U64 address map that answers at a SID *file's own* fixed chip
    addresses (chip 0 = $D400), unlike :func:`plan_sid_map` which chooses its own
    canonical layout. A `.sid` tune writes to the exact $Dxxx bases in its PSID
    header, so the hardware must respond there or those chips stay silent.

    `socket_models` is the chip each physical socket carries (``None`` = empty,
    which is also how "no socket routing wanted" is expressed).
    `required_models` is the model each chip in `addresses` requires, parallel to
    it — from the PSID header, or forced by ``[ultimate64].sid_model``. An empty
    tuple means no chip states a requirement.

    A socket (fixed $D400/$D420) serves a target only when it carries the model
    that target asks for; everything else comes from up to two UltiSID cores
    sharing one split, with each core's filter curve set to the model its chips
    want. **Model matching happens here, in the same pass as routing** — a
    separate model-correction pass afterwards would be working from addresses
    this planner had already assigned and would move chips out from under it.
    Sockets the plan doesn't claim are disabled; cores left spare mirror the
    socket addresses (see :func:`mirror_bases`).

    Returns a :class:`SidMap` whose ``addresses`` echo the requested bases
    verbatim, or **None** when the set isn't realizable on 2 sockets + 2 cores
    (caller falls back to :func:`plan_sid_map`)."""
    if not addresses:
        return None
    targets = sorted(set(addresses))
    models = _models_by_address(addresses, required_models)
    config: dict[tuple[str, str], str] = {}

    served_by_socket: dict[int, str] = {}
    for index, (addr_item, en_item, base) in enumerate(_SOCKET_SPECS):
        socketed = socket_models[index]
        required = models.get(base)
        if socketed is None or base not in targets or (required and socketed != required):
            continue
        config[(CAT_ADDRESSING, addr_item)] = f"${base:04X}"
        config[(CAT_SOCKETS, en_item)] = "Enabled"
        served_by_socket[base] = f"socket{index + 1}"

    remaining = [t for t in targets if t not in served_by_socket]
    core_plan = _plan_ultisid_cores(remaining)
    if core_plan is None:
        return None
    split_label, core_bases = core_plan
    capacity = _SPLIT_CAPACITY[split_label]
    config[(CAT_ADDRESSING, ITEM_ULTISID_SPLIT)] = split_label

    mirrors = mirror_bases(split_label, core_bases, sorted(served_by_socket))
    curves = [_core_curve(base, capacity, models) for base in core_bases]
    curves += [curve_for_model(models.get(base)) for base in mirrors]
    _assign_cores(config, core_bases + mirrors, curves)

    config[(CAT_ADDRESSING, ITEM_AUTO_MIRROR)] = "Disabled"
    disable_unclaimed_sockets(config, set(served_by_socket.values()))

    sources = tuple(
        _source_for_address(addr, served_by_socket, core_bases, capacity) for addr in addresses
    )
    return SidMap(
        addresses=tuple(addresses),
        config=config,
        requested=len(addresses),
        sources=sources,
    )
