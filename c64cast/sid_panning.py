"""SID stereo panning: spread a tune's SID chips across the U64 mixer's field.

The U64 firmware mixes each audio *source* — physical SID socket 1/2, UltiSID
FPGA core 1/2 — at an independent stereo pan, exposed as the `Audio Mixer`
config items ``Pan Socket 1``/``Pan Socket 2``/``Pan UltiSID 1``/``Pan UltiSID 2``
(enum ``Left 5 … Left 1, Center, Right 1 … Right 5``, confirmed live via
``GET /v1/configs/Audio%20Mixer``). Panning is therefore a property of the
*source*, not the ``$Dxxx`` address — so we pan whichever source each tune chip
is routed onto (see :func:`plan_sid_panning`).

Because a pan belongs to a source, the ``sid_panning`` config list is indexed by
*source*, not by chip: entry *k* positions the *k*-th source the tune claims. At
most :data:`MAX_PANNED_SOURCES` (4) entries can ever apply, and fewer when the
machine has no socketed SIDs — then only the 2 UltiSID cores are pannable, so a
3+ chip tune necessarily doubles chips onto a shared pan (warned at apply time).

This module is the panning sibling of :mod:`c64cast.sid_autoconfig` (chip-model
matching) and :mod:`c64cast.asid_sidmap` (address routing): a **pure** planner
(`plan_sid_panning`, `resolve_panning`, `default_pan_spread`, `window_order_for_pans`,
label/int conversion) plus one best-effort impure entry point (`apply_panning`)
that every SID-playing scene calls, reusing :mod:`c64cast.sid_hw_config`'s REST
plumbing (`apply_config`, `current_source_map`) rather than duplicating it.

`apply_panning` also reports the scope's column order, so the oscilloscope's
side-by-side chip windows run left-to-right across the stereo field and the
picture matches what you hear.

U64 only, best-effort — every function no-ops on a backend without a SID config
API (TeensyROM), and a REST failure never crashes a scene. The scene folds the
returned originals into its existing SID-config restore snapshot so the user's
mixer is put back on teardown.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .sid_hw_config import apply_config, current_source_map

if TYPE_CHECKING:
    from .backend import C64Backend

log = logging.getLogger(__name__)

# Config category + per-source pan item names — must match the firmware exactly
# (u64_config.cc / live GET). One pan item per mixable SID source.
CAT_MIXER: Final = "Audio Mixer"
PAN_ITEM: Final[dict[str, str]] = {
    "socket1": "Pan Socket 1",
    "socket2": "Pan Socket 2",
    "ultisid1": "Pan UltiSID 1",
    "ultisid2": "Pan UltiSID 2",
}

# Pan value space: int -5..+5 (negative = left) ↔ enum label. Index 0..10 of
# PAN_LABELS maps to value PAN_MIN..PAN_MAX.
PAN_MIN: Final = -5
PAN_MAX: Final = 5
PAN_LABELS: Final[tuple[str, ...]] = (
    "Left 5",
    "Left 4",
    "Left 3",
    "Left 2",
    "Left 1",
    "Center",
    "Right 1",
    "Right 2",
    "Right 3",
    "Right 4",
    "Right 5",
)
_LABEL_TO_VALUE: Final[dict[str, int]] = {
    lbl.lower(): PAN_MIN + i for i, lbl in enumerate(PAN_LABELS)
}

# The U64 has exactly one pan control per source, so at most this many distinct
# pan positions exist — and a sid_panning list longer than this can never take
# effect. The *achievable* count is lower without socketed SIDs: 2 UltiSID cores
# + one per populated socket (_warn_if_sources_limited surfaces the shortfall).
MAX_PANNED_SOURCES: Final = len(PAN_ITEM)

# Default stereo spreads by pannable-source count, ordered by musical
# importance rather than as a uniform fan: an odd count puts the primary chip
# dead center with the rest flanking it; an even count keeps the first two
# closest to center and spreads later ones wider.
_DEFAULT_SPREAD: Final[dict[int, tuple[int, ...]]] = {
    0: (),
    1: (0,),
    2: (-3, 3),
    3: (0, -3, 3),
    4: (-2, 2, -5, 5),
}


@dataclass(frozen=True)
class SidPanning:
    """What :func:`apply_panning` did, and what the scope should do about it.

    ``originals`` is the ``{(category, item): value}`` set the caller folds into
    its SID-config restore snapshot (empty when nothing was changed).
    ``window_order`` lists chip indices left-to-right by pan position, so scope
    column *w* shows ``window_order[w]`` — the display then matches the stereo
    image instead of raw chip order."""

    originals: dict[tuple[str, str], str]
    window_order: tuple[int, ...]

    @classmethod
    def identity(cls, n_chips: int) -> SidPanning:
        """Nothing panned: no restore set, columns left in chip order."""
        return cls(originals={}, window_order=tuple(range(n_chips)))


def pan_to_label(v: int | str) -> str:
    """Normalize a pan spec value (int ``-5..5`` or a case-insensitive label
    like ``"Left 3"``/``"center"``/``"0"``) to a canonical enum label. Raises
    :class:`ValueError` for out-of-range ints or unrecognized labels."""
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        raise ValueError(f"invalid pan value {v!r}: expected int -5..5 or a label")
    if isinstance(v, int):
        if not PAN_MIN <= v <= PAN_MAX:
            raise ValueError(f"pan value {v} out of range {PAN_MIN}..{PAN_MAX}")
        return PAN_LABELS[v - PAN_MIN]
    s = v.strip()
    # A stringified int ("0", "-3", "+2") is accepted for TOML friendliness.
    # Parse and range-check separately so an out-of-range "-9" reports the range
    # error rather than falling through to "unrecognized label".
    try:
        as_int = int(s)
    except ValueError:
        pass
    else:
        return pan_to_label(as_int)
    value = _LABEL_TO_VALUE.get(s.lower())
    if value is None:
        raise ValueError(
            f"unrecognized pan label {v!r}: expected one of {', '.join(PAN_LABELS)} "
            f"or an int {PAN_MIN}..{PAN_MAX}"
        )
    return PAN_LABELS[value - PAN_MIN]


def pan_from_label(label: str) -> int:
    """Inverse of :func:`pan_to_label` for a canonical label → int ``-5..5``."""
    value = _LABEL_TO_VALUE.get(label.strip().lower())
    if value is None:
        raise ValueError(f"unrecognized pan label {label!r}")
    return value


def normalize_pan_spec(spec: Sequence[int | str]) -> tuple[int, ...]:
    """Coerce a config `sid_panning` list to validated int values ``-5..5``.
    Raises :class:`ValueError` (with the offending entry) on any bad value — the
    config validator surfaces this at load time."""
    return tuple(pan_from_label(pan_to_label(v)) for v in spec)


def default_pan_spread(n_sources: int) -> tuple[int, ...]:
    """The default pan positions for `n_sources` pannable sources.

    The values encode *musical importance*, not a uniform fan: with an odd
    count the primary chip sits dead center and the rest flank it; with an even
    count the first two chips sit closest to center and later ones spread
    wider. `n_sources` is clamped to :data:`MAX_PANNED_SOURCES`."""
    return _DEFAULT_SPREAD[max(0, min(n_sources, MAX_PANNED_SOURCES))]


def resolve_panning(configured: Sequence[int | str] | None, n_sources: int) -> tuple[int, ...]:
    """The pan value (int ``-5..5``) for each of `n_sources` pannable sources.
    A non-empty `configured` list (from ``[ultimate64].sid_panning``) wins,
    truncated or center-extended to length `n_sources`; otherwise
    :func:`default_pan_spread`. Assumes `configured` already passed
    :func:`normalize_pan_spec` validation."""
    n_sources = max(0, min(n_sources, MAX_PANNED_SOURCES))
    if n_sources == 0:
        return ()
    if configured:
        values = normalize_pan_spec(configured)[:n_sources]
        return values + (0,) * (n_sources - len(values))
    return default_pan_spread(n_sources)


def distinct_sources(sources: Sequence[str | None]) -> tuple[str, ...]:
    """The pannable sources `sources` uses, in first-claim (chip) order.

    This is what the ``sid_panning`` list indexes: entry *k* is the pan of the
    *k*-th source the tune claims, NOT of chip *k*. The two coincide while every
    chip lands on its own source (through 4 chips); beyond that, chips sharing a
    split UltiSID core share its single pan control."""
    claimed: list[str] = []
    for source in sources:
        if source in PAN_ITEM and source not in claimed:
            claimed.append(str(source))
    return tuple(claimed)


def source_pans(sources: Sequence[str | None], pans: Sequence[int]) -> dict[str, int]:
    """Map each pannable source to its pan: the *k*-th distinct source claimed
    takes ``pans[k]``. Sources beyond `pans` are left unset."""
    return dict(zip(distinct_sources(sources), pans, strict=False))


def chip_pan_values(sources: Sequence[str | None], pans: Sequence[int]) -> tuple[int, ...]:
    """The effective pan of each *chip* — its source's pan (0 for a chip on no
    pannable source). Chips sharing a source all report that shared value. This
    is what the scope orders its columns by, so the display matches the stereo
    image."""
    by_source = source_pans(sources, pans)
    return tuple(by_source.get(source or "", 0) for source in sources)


def window_order_for_pans(chip_pans: Sequence[int]) -> tuple[int, ...]:
    """Chip indices ordered left-to-right by pan position, so scope column *w*
    shows the *w*-th chip across the stereo field. Ties (chips sharing a source,
    or equal pans) keep chip order, and an all-equal set yields the identity
    order — so a single-chip or all-centered tune renders exactly as before."""
    return tuple(sorted(range(len(chip_pans)), key=lambda chip: (chip_pans[chip], chip)))


def plan_sid_panning(
    sources: Sequence[str | None], pans: Sequence[int]
) -> dict[tuple[str, str], str]:
    """Pure planner: the mixer PUT that pans each source the tune uses.
    ``sources[i]`` is the audio source (``"socket1"``/``"socket2"``/
    ``"ultisid1"``/``"ultisid2"``) playing tune chip *i*; ``pans[k]`` is the
    position of the *k*-th distinct source claimed. Returns
    ``{(CAT_MIXER, "Pan <Source>"): <label>}``.

    A chip on no pannable source is skipped. Chips sharing one source (two split
    instances of a single UltiSID core) share its one pan control."""
    return {
        (CAT_MIXER, PAN_ITEM[source]): pan_to_label(pan)
        for source, pan in source_pans(sources, pans).items()
    }


def sources_for_addresses(api: C64Backend, addresses: Sequence[int]) -> tuple[str | None, ...]:
    """Which mixer source currently answers each of `addresses` (tune-chip
    order), via :func:`c64cast.sid_hw_config.current_source_map`. Used for the
    non-remapped single-SID case, where no :class:`~c64cast.asid_sidmap.SidMap`
    supplies the source ordering (best-effort; ``None`` per address on a read
    failure or an address nothing answers)."""
    src_map = current_source_map(api)
    return tuple(src_map.get(a) for a in addresses)


def _warn_if_sources_limited(
    sources: Sequence[str | None], configured: Sequence[int | str]
) -> None:
    """Warn when the hardware can't give every chip (or every configured entry)
    its own pan position. The ceiling is one position per *source*: the two
    UltiSID cores plus one per populated SID socket. With no socketed SIDs only
    two positions exist, so a 3+ chip tune necessarily doubles chips up."""
    claimed = distinct_sources(sources)
    if not claimed:
        return  # nothing routed to pan at all — not a "limited sources" case
    n_chips = len(sources)
    if n_chips > len(claimed):
        socketed = [s for s in claimed if s.startswith("socket")]
        why = (
            "no socketed SIDs, so only the 2 UltiSID cores are pannable"
            if not socketed
            else f"{len(claimed)} pannable source(s) in use"
        )
        log.warning(
            "sid panning: %d SID chips but only %d distinct pan position(s) "
            "available (%s) — chips sharing a source share its pan",
            n_chips,
            len(claimed),
            why,
        )
    if len(configured) > len(claimed):
        log.warning(
            "sid panning: sid_panning has %d entries but only %d source(s) are "
            "pannable here — the extra entries are ignored",
            len(configured),
            len(claimed),
        )


def apply_panning(
    api: C64Backend, sources: Sequence[str | None], configured: Sequence[int | str] | None
) -> SidPanning:
    """Resolve, apply, and report the panning for a tune (U64 only,
    best-effort). `sources` is the audio source playing each chip, in chip
    order; `configured` is ``[ultimate64].sid_panning``.

    Reads the current pan items once and writes only the sources whose pan
    actually differs, so an already-correct configuration (a centered single-SID
    tune on a mixer already at Center) does no writes and returns an empty
    restore set — nothing to put back on teardown. The returned
    :class:`SidPanning` carries the originals for the caller's restore snapshot
    plus the scope's left-to-right column order."""
    if not getattr(api.profile, "supports_config", False):
        return SidPanning.identity(len(sources))

    pans = resolve_panning(configured, len(distinct_sources(sources)))
    _warn_if_sources_limited(sources, configured or ())
    chip_pans = chip_pan_values(sources, pans)
    window_order = window_order_for_pans(chip_pans)
    if window_order != tuple(range(len(sources))):
        log.info(
            "sid panning: scope columns left→right = %s",
            ", ".join(f"chip {chip} ({pan_to_label(chip_pans[chip])})" for chip in window_order),
        )

    desired = plan_sid_panning(sources, pans)
    if not desired:
        return SidPanning.identity(len(sources))
    try:
        mixer = api.get_config_category(CAT_MIXER)
    except Exception:
        log.debug("sid panning: mixer read failed — skipping", exc_info=True)
        return SidPanning.identity(len(sources))

    changes = {key: label for key, label in desired.items() if mixer.get(key[1]) != label}
    if not changes:
        log.info("sid panning: mixer already at the target — no change")
        return SidPanning(originals={}, window_order=window_order)
    originals = {key: mixer[item] for key in changes if (item := key[1]) in mixer}

    apply_config(api, changes)
    log.info(
        "sid panning: %s",
        ", ".join(f"{item}={label}" for (_category, item), label in sorted(changes.items())),
    )
    return SidPanning(originals=originals, window_order=window_order)
