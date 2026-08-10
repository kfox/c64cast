"""SID mixer volume: make every chip a tune actually plays on audible, and
everything else silent.

The panning sibling of :mod:`c64cast.sid.sid_panning`, and the reason it exists: the
U64 mixes each audio *source* — physical SID socket 1/2, UltiSID FPGA core 1/2 —
at an independent level, and the two UltiSID levels are commonly left at
``OFF``. Routing a chip onto an UltiSID core (which multi-SID address planning
and model autoconfig both do freely) then produces **silence with no error** —
the chip is mapped, the player writes to it, and nothing comes out. The mirror
failure is just as real: a tune that deliberately uses the socketed chips is
polluted by an UltiSID core still mapped at the same address with its level up.

So a tune's sources are driven in both directions:

  * a source the tune plays on is made audible — the configured level from
    ``[ultimate64].sid_volume`` if there is one, else ``" 0 dB"`` when it was
    ``OFF``. A source already at a deliberate non-``OFF`` level (a rig trimmed
    to ``-6 dB``) is left exactly as the user set it.
  * every other SID source is muted to ``OFF``, so nothing that isn't part of
    the tune can bleed into the mix.

Like panning, this is a **pure** planner (`plan_sid_volume`, `resolve_volumes`,
label/int conversion) plus one best-effort impure entry point (`apply_volume`)
that reads the mixer once, writes only what differs, and returns the originals
for the caller to fold into its existing SID-config restore snapshot. Source
derivation is shared, not duplicated: `sid_panning.distinct_sources` decides
which sources a tune claims and in what order, so the ``sid_volume`` list is
indexed exactly like ``sid_panning`` — entry *k* is the *k*-th source claimed.

U64 only, best-effort — a no-op on a backend without a SID config API
(TeensyROM), and a REST failure never crashes a scene.

Two firmware naming traps, both confirmed live via
``GET /v1/configs/Audio%20Mixer``:

  * the volume items spell it ``Vol UltiSid 1``/``Vol UltiSid 2`` (lowercase
    ``id``) while the pan items spell it ``Pan UltiSID 1``/``Pan UltiSID 2``
    (uppercase ``SID``). The inconsistency is the firmware's; do not "fix" it.
  * every non-negative level carries a **leading space** — ``" 0 dB"``, not
    ``"0 dB"``. Exact-match comparisons against a stripped string never match,
    so the mixer would be rewritten on every setup.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from .sid_hw_config import apply_config
from .sid_panning import CAT_MIXER, distinct_sources

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

# Per-source volume item names — must match the firmware exactly (see the
# module docstring's note on `UltiSid` vs `UltiSID`). One per mixable SID
# source, keyed identically to sid_panning.PAN_ITEM so the two share
# `distinct_sources` for source derivation.
VOL_ITEM: Final[dict[str, str]] = {
    "socket1": "Vol Socket 1",
    "socket2": "Vol Socket 2",
    "ultisid1": "Vol UltiSid 1",
    "ultisid2": "Vol UltiSid 2",
}

# The mixer's level enum, in firmware order. NOT a uniform 1 dB ladder: it is
# dense from -18 dB up and sparse below, so an arbitrary int (e.g. -20) has no
# representation and is rejected rather than silently snapped to a neighbor.
VOL_OFF: Final = "OFF"
VOL_UNITY: Final = " 0 dB"  # leading space is the firmware's, not a typo
VOL_LABELS: Final[tuple[str, ...]] = (
    VOL_OFF,
    "-42 dB",
    "-36 dB",
    "-30 dB",
    "-27 dB",
    "-24 dB",
    "-18 dB",
    "-17 dB",
    "-16 dB",
    "-15 dB",
    "-14 dB",
    "-13 dB",
    "-12 dB",
    "-11 dB",
    "-10 dB",
    "-9 dB",
    "-8 dB",
    "-7 dB",
    "-6 dB",
    "-5 dB",
    "-4 dB",
    "-3 dB",
    "-2 dB",
    "-1 dB",
    VOL_UNITY,
    "+1 dB",
    "+2 dB",
    "+3 dB",
    "+4 dB",
    "+5 dB",
    "+6 dB",
)
_LABEL_TO_CANON: Final[dict[str, str]] = {label.strip().lower(): label for label in VOL_LABELS}
_VALID_DB: Final[tuple[int, ...]] = tuple(
    int(label.strip().removesuffix(" dB")) for label in VOL_LABELS if label != VOL_OFF
)

# One volume control per source, so a sid_volume list longer than this can never
# take effect (config load rejects it) — same ceiling as sid_panning.
MAX_VOLUME_SOURCES: Final = len(VOL_ITEM)


def _db_to_label(db: int) -> str:
    """Format a dB int the way the firmware enum spells it: an explicit ``+``
    above zero, a leading space at zero, a bare ``-`` below."""
    if db > 0:
        return f"+{db} dB"
    return VOL_UNITY if db == 0 else f"{db} dB"


def volume_to_label(v: int | str) -> str:
    """Normalize a volume spec value — a dB int, a case-insensitive label
    (``"-6 dB"``/``"off"``/``" 0 dB"``), or a stringified int (``"0"``,
    ``"-6"``) for TOML friendliness — to a canonical enum label. Raises
    :class:`ValueError` naming the representable levels for anything else."""
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        raise ValueError(f"invalid volume {v!r}: expected a dB int, a label, or 'off'")
    if isinstance(v, int):
        label = _db_to_label(v)
        if label not in _LABEL_TO_CANON.values():
            raise ValueError(
                f"volume {v} dB is not one of the mixer's levels — the ladder is "
                f"sparse below -18 dB; representable: {', '.join(map(str, _VALID_DB))}"
            )
        return label
    spec = v.strip()
    try:
        as_int = int(spec)
    except ValueError:
        pass
    else:
        return volume_to_label(as_int)
    canonical = _LABEL_TO_CANON.get(spec.lower())
    if canonical is None:
        raise ValueError(
            f"unrecognized volume {v!r}: expected 'off', a label like '-6 dB', "
            f"or a dB int from {', '.join(map(str, _VALID_DB))}"
        )
    return canonical


def normalize_volume_spec(spec: Sequence[int | str]) -> tuple[str, ...]:
    """Coerce a config `sid_volume` list to canonical enum labels. Raises
    :class:`ValueError` (naming the offending entry) on any bad value — the
    config validator surfaces this at load time."""
    return tuple(volume_to_label(v) for v in spec)


def resolve_volumes(
    configured: Sequence[int | str] | None, n_sources: int
) -> tuple[str | None, ...]:
    """The level for each of `n_sources` claimed sources: a canonical label from
    ``[ultimate64].sid_volume``, or ``None`` meaning "decide from what the mixer
    is already at" (:func:`target_level`).

    A configured list is truncated to `n_sources` and padded with ``None`` —
    padding with an explicit level instead would let a one-entry list silently
    dictate the level of every other source."""
    n_sources = max(0, min(n_sources, MAX_VOLUME_SOURCES))
    if n_sources == 0:
        return ()
    if not configured:
        return (None,) * n_sources
    levels = normalize_volume_spec(configured)[:n_sources]
    return levels + (None,) * (n_sources - len(levels))


def target_level(*, in_use: bool, configured: str | None, current: str | None) -> str | None:
    """The level a single source should end up at, or ``None`` to leave it
    alone. A source the tune doesn't play on is muted; a source it does play on
    honors an explicit `configured` level, is raised to ``" 0 dB"`` when it
    would otherwise be inaudible, and is otherwise left at whatever deliberate
    level the user already trimmed it to. A source the mixer read didn't report
    is left alone — writing it would produce a change with no original to
    restore."""
    if current is None:
        return None
    if not in_use:
        return VOL_OFF
    if configured is not None:
        return configured
    return VOL_UNITY if current == VOL_OFF else None


def plan_sid_volume(
    sources: Sequence[str | None], levels: Sequence[str | None], current: dict[str, str]
) -> dict[tuple[str, str], str]:
    """Pure planner: the mixer PUT that makes the tune's sources audible and
    mutes the rest. ``sources[i]`` is the audio source playing tune chip *i*;
    ``levels[k]`` is the configured level of the *k*-th distinct source claimed
    (``None`` = auto); `current` is the live ``Audio Mixer`` category. Returns
    ``{(CAT_MIXER, "Vol <Source>"): <label>}`` covering every source whose level
    should change from `current`."""
    claimed = distinct_sources(sources)
    configured_by_source = dict(zip(claimed, levels, strict=False))
    plan = {}
    for source, item in VOL_ITEM.items():
        level = target_level(
            in_use=source in claimed,
            configured=configured_by_source.get(source),
            current=current.get(item),
        )
        if level is not None:
            plan[(CAT_MIXER, item)] = level
    return plan


def apply_volume(
    api: C64Backend, sources: Sequence[str | None], configured: Sequence[int | str] | None
) -> dict[tuple[str, str], str]:
    """Resolve and apply the mixer levels for a tune (U64 only, best-effort).
    `sources` is the audio source playing each chip, in chip order; `configured`
    is ``[ultimate64].sid_volume``. Returns the ``{(category, item): value}``
    originals for the caller's restore snapshot — empty when nothing changed.

    Reads the mixer once and writes only the sources whose level actually
    differs, so a rig already configured the way the tune wants does no writes
    and leaves nothing to put back at teardown."""
    if not getattr(api.profile, "supports_sid_config", False):
        return {}
    claimed = distinct_sources(sources)
    if not claimed:
        # Nothing resolved to a known source — muting on that basis would risk
        # silencing the very chip that is playing.
        log.debug("sid volume: no known audio source for this tune — leaving the mixer alone")
        return {}

    try:
        mixer = api.get_config_category(CAT_MIXER)
    except Exception:
        log.debug("sid volume: mixer read failed — skipping", exc_info=True)
        return {}

    desired = plan_sid_volume(sources, resolve_volumes(configured, len(claimed)), mixer)
    changes = {key: label for key, label in desired.items() if mixer.get(key[1]) != label}
    if not changes:
        log.info("sid volume: mixer already at the target — no change")
        return {}
    originals = {key: mixer[key[1]] for key in changes}

    apply_config(api, changes)
    log.info(
        "sid volume: %s",
        ", ".join(f"{item}={label}" for (_category, item), label in sorted(changes.items())),
    )
    return originals
