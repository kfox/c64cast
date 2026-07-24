"""SID stereo panning: spread a tune's SID chips across the U64 mixer's field.

The U64 firmware mixes each audio *source* — physical SID socket 1/2, UltiSID
FPGA core 1/2 — at an independent stereo pan, exposed as the `Audio Mixer`
config items ``Pan Socket 1``/``Pan Socket 2``/``Pan UltiSID 1``/``Pan UltiSID 2``
(enum ``Left 5 … Left 1, Center, Right 1 … Right 5``, confirmed live via
``GET /v1/configs/Audio%20Mixer``). Panning is therefore a property of the
*source*, not the ``$Dxxx`` address — so we pan whichever source each tune chip
is routed onto (see :func:`plan_sid_panning`).

This module is the panning sibling of :mod:`c64cast.sid_autoconfig` (chip-model
matching) and :mod:`c64cast.asid_sidmap` (address routing): a **pure** planner
(`plan_sid_panning`, `resolve_panning`, `default_pan_spread`, label/int
conversion) plus one best-effort impure entry point (`plan_and_apply_panning`)
that every SID-playing scene calls, reusing :mod:`c64cast.sid_hw_config`'s REST
plumbing (`apply_config`, `current_source_map`) rather than duplicating it.

U64 only, best-effort — every function no-ops on a backend without a SID config
API (TeensyROM), and a REST failure never crashes a scene. The scene folds the
returned originals into its existing SID-config restore snapshot so the user's
mixer is put back on teardown.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
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

# Default stereo spreads by SID count. 1 → dead center; 2-4 hand-picked for a
# comfortable gap (outer chips wider as the count grows); 5+ fall through to an
# even spread across the full field (see default_pan_spread).
_DEFAULT_SPREAD: Final[dict[int, tuple[int, ...]]] = {
    0: (),
    1: (0,),
    2: (-3, 3),
    3: (0, -3, 3),
    4: (-2, 2, -5, 5),
}


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


def default_pan_spread(n: int) -> tuple[int, ...]:
    """The default pan positions for `n` SID chips: a hand-tuned table for the
    common 1-4 counts, else an even spread across the full ``[-5, 5]`` field."""
    if n <= 0:
        return ()
    if n in _DEFAULT_SPREAD:
        return _DEFAULT_SPREAD[n]
    # Even spread: chip i at -5 + 10*i/(n-1), rounded into range.
    span = PAN_MAX - PAN_MIN
    return tuple(round(PAN_MIN + span * i / (n - 1)) for i in range(n))


def resolve_panning(configured: Sequence[int | str] | None, n: int) -> tuple[int, ...]:
    """The pan value (int ``-5..5``) for each of `n` SID chips. A non-empty
    `configured` list (from ``[ultimate64].sid_panning``) wins, truncated or
    zero-extended (Center) to length `n`; otherwise :func:`default_pan_spread`.
    Assumes `configured` already passed :func:`normalize_pan_spec` validation."""
    if n <= 0:
        return ()
    if configured:
        vals = normalize_pan_spec(configured)
        if len(vals) >= n:
            return vals[:n]
        return vals + (0,) * (n - len(vals))
    return default_pan_spread(n)


def plan_sid_panning(
    sources: Sequence[str | None], pans: Sequence[int]
) -> dict[tuple[str, str], str]:
    """Pure planner: map each ``(source, pan)`` to the mixer PUT that pans that
    source. ``sources[i]`` is the audio source (``"socket1"``/``"socket2"``/
    ``"ultisid1"``/``"ultisid2"``) playing tune chip *i*; ``pans[i]`` its desired
    position. Returns ``{(CAT_MIXER, "Pan <Source>"): <label>}``.

    A ``None`` or unknown source is skipped (its chip isn't hardware-routed). If
    two chips share one source (two split instances of one UltiSID core — only
    possible at ≥5 SIDs), the **first** chip's pan wins, since the core has one
    pan control; a differing later request is logged and ignored."""
    plan: dict[tuple[str, str], str] = {}
    for i, source in enumerate(sources):
        if i >= len(pans):
            break
        item = PAN_ITEM.get(source) if source else None
        if item is None:
            continue
        key = (CAT_MIXER, item)
        label = pan_to_label(pans[i])
        if key in plan:
            if plan[key] != label:
                log.debug(
                    "sid panning: %s already set to %s; chip %d wants %s — "
                    "sharing one pan control, keeping the first",
                    item,
                    plan[key],
                    i,
                    label,
                )
            continue
        plan[key] = label
    return plan


def sources_for_addresses(api: C64Backend, addresses: Sequence[int]) -> tuple[str | None, ...]:
    """Which mixer source currently answers each of `addresses` (tune-chip
    order), via :func:`c64cast.sid_hw_config.current_source_map`. Used for the
    non-remapped single-SID case, where no :class:`~c64cast.asid_sidmap.SidMap`
    supplies the source ordering (best-effort; ``None`` per address on a read
    failure or an address nothing answers)."""
    src_map = current_source_map(api)
    return tuple(src_map.get(a) for a in addresses)


def plan_and_apply_panning(
    api: C64Backend, sources: Sequence[str | None], pans: Sequence[int]
) -> dict[tuple[str, str], str]:
    """Apply the desired panning live (U64 only, best-effort) and return the
    **originals** to restore, for the caller to fold into its SID-config restore
    snapshot. No-ops (returns ``{}``) on a backend without a SID config API.

    Reads the current pan items once and writes only the sources whose pan
    actually differs, so an already-correct configuration (e.g. a centered
    single-SID tune on a mixer already at Center) does no writes and returns an
    empty restore set — nothing to put back on teardown."""
    if not getattr(api.profile, "supports_config", False):
        return {}
    desired = plan_sid_panning(sources, pans)
    if not desired:
        return {}
    try:
        mixer = api.get_config_category(CAT_MIXER)
    except Exception:
        log.debug("sid panning: mixer read failed — skipping", exc_info=True)
        return {}
    changes = {key: label for key, label in desired.items() if mixer.get(key[1]) != label}
    if not changes:
        log.info("sid panning: mixer already at the target — no change")
        return {}
    originals = {key: mixer[item] for key in changes if (item := key[1]) in mixer}

    apply_config(api, changes)
    log.info(
        "sid panning: %s",
        ", ".join(f"{item}={label}" for (_category, item), label in sorted(changes.items())),
    )
    return originals
