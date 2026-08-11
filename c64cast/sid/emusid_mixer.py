"""Ultimate II+ emulated stereo SID: snoop routing for a tune's chips.

The U2+ has no SID sockets and no UltiSID cores — in a cartridge port, the
tune plays on whatever the host C64 carries. But its audio jack is fed by two
FPGA SID *emulations* ("SID Left"/"SID Right" in the firmware's `Audio Output
Settings` category, mixed as `Vol/Pan EmuSid1/2`), each snooping bus writes at
one configurable base address (``"Snoop $D400"``). Whether a chip is heard on
that jack is therefore pure configuration, and two silent-failure modes mirror
the U64 ones :mod:`c64cast.sid.sid_volume` exists for:

  * a multi-SID tune's second chip is inaudible unless some emulated SID
    snoops its address — the stock right-side base is not ``$D420``, so 2SID
    tunes ship half-silent with no error anywhere;
  * on a *stock* machine the host C64's own SID can't help: a lone real SID's
    partial address decode answers the entire ``$D4xx-$D7xx`` region, so on
    the machine's own audio out a multi-SID tune collapses onto one chip as
    mush, and the snooped emulations are the only output where it can sound
    as authored.

That last point inverts on a machine with an internal dual-SID mod (ARM2SID,
SIDFX, DualSID). There the second chip is properly decoded at its own address,
so the machine's own output carries the tune as authored with no help from
this module — and carries it *more* faithfully than the emulations do, because
the snoop cannot tell a write to ``$D4xx-$D7xx`` from a write to the RAM
underneath (the cartridge port carries no signal distinguishing the two), so a
tune using that RAM sprays clicks and stray notes into the emulated SIDs.
Which output a listener should trust is therefore a property of the machine,
not of this surface; ``[hardware].host_sid_chips`` is how that gets declared,
and c64cast/sid/sid_resolved.py renders the two routes separately.

So this module *routes*: a spare enabled emulated SID (one snooping an address
the tune doesn't play) is retargeted to an uncovered tune chip, and the
original base goes back at teardown via the caller's
:class:`~c64cast.sid.sid_hw_config.SidHwSession`. A side the user disabled stays
disabled — enabling hardware the user turned off is a bigger intervention than
retargeting what they left on. Panning and volume for the routed sides ride on
:mod:`c64cast.sid.sid_panning` / :mod:`c64cast.sid.sid_volume`, whose item maps
include the ``emusid1``/``emusid2`` sources defined here.

It also *matches the model*: each side snooping a tune chip is set to the
6581 or 8580 the tune's PSID header asked for (:func:`apply_emusid_model`,
under the same ``[ultimate64].sid_model`` knob as the U64's SID Player
Autoconfig). That pass is trivial next to the U64's, where matching means
finding a different chip — swapping sockets or falling back to an FPGA core.
Here the side *is* an emulation, so it is simply told which chip to be, and a
mismatch is always fixable in place. The host C64's own SID still plays the
tune unmatched on the machine's own output; nothing on this surface can change
that, which is why the resolved-audio line reports both routes separately.

Same shape as the siblings: pure planners plus best-effort impure entry
points (:func:`apply_emusid_routing`, :func:`apply_emusid_model`), gated on
``profile.supports_emusid_mixer`` — granted by ``refine_capabilities`` from
the device's category list, so it is never true alongside the U64's
``supports_sid_config`` surface (the two firmwares register different
categories). A REST failure never crashes a scene.

Field names confirmed live (``GET /v1/configs/Audio%20Output%20Settings``)
and against the firmware source (audio_select.cc, built only for the
U2/U2+/U2+L targets — the U64's u64_config.cc registers `Audio Mixer`
instead): the volume items spell it ``Vol EmuSid1`` (no space, lowercase
``id``), the enum ladders are byte-identical to the U64 mixer's (including
the leading space in ``" 0 dB"``), and the snoop-base enum covers only the
twelve standard multi-SID addresses plus cartridge-I/O mappings.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from .sid_hw_config import apply_config

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

# The one config category carrying the whole surface — topology, volume and
# pan. Registered only by U2/U2+/U2+L firmware; backend.EMUSID_MIXER_CATEGORY
# mirrors this string for the capability probe (hw can't import sid — the
# same pinning arrangement as SID_CONFIG_CATEGORIES).
CAT_EMUSID: Final = "Audio Output Settings"

# Per-source items. "emusid1" is the firmware's "SID Left" instance and
# "emusid2" its "SID Right" — the Left/Right names are historical (each side
# has its own mixer pan), so the sources are named after the Vol/Pan items.
ITEM_ENABLE: Final[dict[str, str]] = {"emusid1": "SID Left", "emusid2": "SID Right"}
ITEM_BASE: Final[dict[str, str]] = {"emusid1": "SID Left Base", "emusid2": "SID Right Base"}
ITEM_FILTER: Final[dict[str, str]] = {
    "emusid1": "SID Left Filter Curve",
    "emusid2": "SID Right Filter Curve",
}
ITEM_WAVEFORMS: Final[dict[str, str]] = {
    "emusid1": "SID Left Combined Waveforms",
    "emusid2": "SID Right Combined Waveforms",
}
VOL_ITEM_EMU: Final[dict[str, str]] = {"emusid1": "Vol EmuSid1", "emusid2": "Vol EmuSid2"}
PAN_ITEM_EMU: Final[dict[str, str]] = {"emusid1": "Pan EmuSid1", "emusid2": "Pan EmuSid2"}

ENABLED: Final = "Enabled"

# Both model items take the firmware's two-entry `sidchip_sel` ladder, so the
# emulation is told which chip to *be* rather than which curve to approximate —
# none of the U64 UltiSID's "8580 Lo"/"8580 Hi"/"6581 Alt" variants to choose
# between, and the labels are already the model names the PSID header uses.
EMU_MODELS: Final[tuple[str, ...]] = ("6581", "8580")

# Filter curve and combined waveforms are separate config items, but they are
# two halves of one question — a side set to an 8580 curve with 6581 waveform
# combining emulates neither chip. A tune asks for a chip, so they move
# together.
_MODEL_ITEMS: Final[tuple[dict[str, str], ...]] = (ITEM_FILTER, ITEM_WAVEFORMS)

# The bus addresses the snoop-base enum can express (audio_select.cc
# sid_base[]) — the twelve standard multi-SID bases. The enum's remaining
# entries map the emulation into cartridge I/O ($DExx/$DFxx) instead of
# snooping the SID range; a side parked there hears no tune and counts as
# retargetable.
SNOOPABLE_ADDRESSES: Final[tuple[int, ...]] = (
    0xD400,
    0xD420,
    0xD480,
    0xD500,
    0xD520,
    0xD580,
    0xD600,
    0xD620,
    0xD680,
    0xD700,
    0xD720,
    0xD780,
)

_SNOOP_PREFIX: Final = "Snoop $"


def parse_snoop_base(value: str) -> int | None:
    """The bus address a ``"Snoop $D400"``-style base value snoops, or None
    for a cartridge-I/O mapping or anything unrecognized."""
    if not value.startswith(_SNOOP_PREFIX):
        return None
    try:
        return int(value[len(_SNOOP_PREFIX) :], 16)
    except ValueError:
        return None


def snoop_label(address: int) -> str | None:
    """The enum label snooping `address`, or None when the enum can't express
    it (the snoopable set is sparse — e.g. ASID's third chip at $D440)."""
    if address not in SNOOPABLE_ADDRESSES:
        return None
    return f"{_SNOOP_PREFIX}{address:04X}"


def emusid_topology(category: Mapping[str, str]) -> dict[str, int]:
    """``{source: snooped address}`` for each enabled side of the given
    ``Audio Output Settings`` category. A disabled side, or one mapped into
    cartridge I/O, doesn't appear."""
    topology = {}
    for source, enable_item in ITEM_ENABLE.items():
        if category.get(enable_item) != ENABLED:
            continue
        base = parse_snoop_base(category.get(ITEM_BASE[source], ""))
        if base is not None:
            topology[source] = base
    return topology


def plan_emusid_routing(
    addresses: Sequence[int], category: Mapping[str, str]
) -> tuple[dict[tuple[str, str], str], tuple[int, ...]]:
    """Pure planner: the base retargets that make every tune chip audible on
    the emulated stereo SID output, plus the addresses that stay uncovered.

    A side already snooping a tune address keeps it. A spare enabled side —
    snooping an address the tune doesn't play, parked in cartridge I/O, or
    merely duplicating an address another side already covers — is retargeted
    to the first uncovered tune address the enum can express, in chip order
    (an uncovered chip made audible beats a covered one doubled). Disabled
    sides are never touched, and an address outside
    :data:`SNOOPABLE_ADDRESSES` stays uncovered rather than being snapped to
    a neighbor."""
    topology = emusid_topology(category)
    covered = set(topology.values())
    uncovered = [a for a in dict.fromkeys(addresses) if a not in covered]
    # The first side covering a tune address is that chip's primary; every
    # other enabled side is spare and retargetable.
    primaries = set(emusid_sources_for_addresses(addresses, category)) - {None}
    spare = [
        source
        for source, enable_item in ITEM_ENABLE.items()
        if category.get(enable_item) == ENABLED and source not in primaries
    ]

    plan: dict[tuple[str, str], str] = {}
    remaining: list[int] = []
    for address in uncovered:
        label = snoop_label(address)
        if label is None or not spare:
            remaining.append(address)
            continue
        plan[(CAT_EMUSID, ITEM_BASE[spare.pop(0)])] = label
    return plan, tuple(remaining)


def emusid_sources_for_addresses(
    addresses: Sequence[int], category: Mapping[str, str]
) -> tuple[str | None, ...]:
    """Which emulated SID plays each of `addresses` (tune-chip order) per the
    given category state — the first enabled side snooping that address, or
    None. The emu-surface counterpart of
    :func:`c64cast.sid.sid_panning.sources_for_addresses`'s U64 read."""
    topology = emusid_topology(category)
    by_address: dict[int, str] = {}
    for source in ITEM_ENABLE:  # emusid1 first, so it wins a shared address
        if source in topology:
            by_address.setdefault(topology[source], source)
    return tuple(by_address.get(a) for a in addresses)


def plan_emusid_model(
    addresses: Sequence[int],
    required_models: Sequence[str | None],
    category: Mapping[str, str],
) -> dict[tuple[str, str], str]:
    """Pure planner: set each snooping side to the chip model its tune chip
    asked for — the emulated-SID analog of
    :func:`c64cast.sid.sid_autoconfig.plan_sid_model_config`, and far shorter,
    because here there is nothing to route around. A U64 chip *is* a 6581 or an
    8580 and autoconfig can only find a different one; an emulation is told
    which to be, so a mismatch is always fixable in place and never displaces
    anything.

    `required_models` is the model each chip asked for, parallel to `addresses`
    (a short or empty sequence — what ``sid_model = "off"`` produces — just
    leaves those chips alone). Only sides snooping a tune address are touched,
    and only when they don't already present the wanted model, so an
    already-matching tune plans nothing and produces no REST traffic at all.

    A requirement the ladder can't express is skipped rather than approximated:
    that covers the PSID header's "unknown" and "6581+8580" (which any chip
    satisfies) without a separate no-requirement check."""
    sources = emusid_sources_for_addresses(addresses, category)
    plan: dict[tuple[str, str], str] = {}
    for source, required in zip(sources, required_models, strict=False):
        if source is None or required is None or required not in EMU_MODELS:
            continue
        for items in _MODEL_ITEMS:
            item = items[source]
            if category.get(item) != required:
                plan[(CAT_EMUSID, item)] = required
    return plan


def read_emusid_category(api: C64Backend) -> dict[str, str] | None:
    """The live ``Audio Output Settings`` category, or None when the backend
    doesn't carry the surface or the read failed — callers treat None as
    "leave everything alone" (best-effort, like every sibling)."""
    if not getattr(api.profile, "supports_emusid_mixer", False):
        return None
    try:
        category = api.get_config_category(CAT_EMUSID)
    except Exception:
        log.debug("emusid: %s read failed — skipping", CAT_EMUSID, exc_info=True)
        return None
    if not any(item in category for item in ITEM_ENABLE.values()):
        log.debug("emusid: %s carries no SID enable fields — skipping", CAT_EMUSID)
        return None
    return category


def _log_uncovered(addresses: Sequence[int], remaining: Sequence[int]) -> None:
    """Say why a chip will be silent on this output — no enum label for its
    address, or no spare enabled side left to retarget."""
    for address in remaining:
        if snoop_label(address) is None:
            log.warning(
                "emusid routing: no snoop base can express $%04X — that chip "
                "will be silent on the emulated stereo SID output",
                address,
            )
        else:
            log.warning(
                "emusid routing: no spare enabled emulated SID for $%04X — "
                "that chip will be silent on the emulated stereo SID output "
                "(%d chips, but only the enabled sides can snoop)",
                address,
                len(addresses),
            )


def apply_emusid_routing(api: C64Backend, addresses: Sequence[int]) -> dict[tuple[str, str], str]:
    """Make every tune chip audible on the emulated stereo SID output
    (best-effort; a no-op on any backend without the surface). Returns the
    ``{(category, item): value}`` originals for the caller's restore
    snapshot — empty when the topology already covers the tune."""
    if not addresses:
        return {}
    category = read_emusid_category(api)
    if category is None:
        return {}

    plan, remaining = plan_emusid_routing(addresses, category)
    _log_uncovered(addresses, remaining)
    if not plan:
        log.debug("emusid routing: topology already covers the tune — no change")
        return {}
    originals = {key: category[key[1]] for key in plan if key[1] in category}

    apply_config(api, plan)
    log.info(
        "emusid routing: %s",
        ", ".join(f"{item}={label}" for (_category, item), label in sorted(plan.items())),
    )
    return originals


def apply_emusid_model(
    api: C64Backend, addresses: Sequence[int], required_models: Sequence[str | None]
) -> dict[tuple[str, str], str]:
    """Set each snooping emulated SID to the chip model its tune chip asked
    for (best-effort; a no-op on any backend without the surface). Returns the
    ``{(category, item): value}`` originals for the caller's restore snapshot —
    empty when every side already presents the wanted model.

    Call this *after* :func:`apply_emusid_routing`: routing decides which side
    snoops which address, and the category is re-read here so the models land on
    where each chip actually ended up. Same discipline as the U64 path, where
    :func:`c64cast.sid.sid_autoconfig.plan_model_config_for_header` re-derives
    against the now-current addressing after a map is applied."""
    if not addresses:
        return {}
    category = read_emusid_category(api)
    if category is None:
        return {}

    plan = plan_emusid_model(addresses, required_models, category)
    if not plan:
        log.debug("emusid model: every snooping side already matches — no change")
        return {}
    originals = {key: category[key[1]] for key in plan if key[1] in category}

    apply_config(api, plan)
    log.info(
        "emusid model: %s",
        ", ".join(f"{item}={value}" for (_category, item), value in sorted(plan.items())),
    )
    return originals
