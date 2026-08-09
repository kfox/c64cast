"""Shared U64 multi-SID hardware-config plumbing.

The REST side of the multi-SID story: snapshot the SID address/socket config,
apply a :class:`~c64cast.sid.asid_sidmap.SidMap`, and restore the snapshot on
teardown. Extracted from :class:`~c64cast.sid.asid_scene.AsidScene` so
:class:`~c64cast.sid.waveform.WaveformScene` (playing a multi-SID `.sid` file) can
reuse it verbatim — both need the U64's extra SID cores mapped to the tune's
chip addresses, and both must put the user's config back afterward.

Every function is best-effort and swallows REST errors (logging at debug/warn):
a config read/write failure must never crash a scene. All are gated by the
caller on ``api.profile.supports_config`` (U64 only; TeensyROM has no config
API — the display still works, chip 0 stays audible).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .asid_sidmap import (
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
    ITEM_ULTISID2_FILTER,
)

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

    from .asid_sidmap import SidMap

log = logging.getLogger(__name__)

# The SID-address + socket-enable config items a multi-SID map touches — the
# exact set snapshot/restore must round-trip. Must match the firmware names
# (u64_config.cc) and the items plan_sid_map* emit.
MANAGED_ADDRESSING_ITEMS = (
    "SID Socket 1 Address",
    "SID Socket 2 Address",
    "UltiSID 1 Address",
    "UltiSID 2 Address",
    "UltiSID Range Split",
    "Auto Address Mirroring",
)
MANAGED_SOCKET_ITEMS = ("SID Socket 1", "SID Socket 2")
# UltiSID filter-curve items a model-autoconfig plan touches (see
# sid_autoconfig.py). A third sibling to MANAGED_ADDRESSING_ITEMS/
# MANAGED_SOCKET_ITEMS rather than folded into either — distinct category
# (CAT_ULTISID), distinct concern (chip model, not address routing).
MANAGED_MODEL_ITEMS = (ITEM_ULTISID1_FILTER, ITEM_ULTISID2_FILTER)


def _parse_dxxx(value: str) -> int | None:
    """Parse a ``"$D400"``-style address enum value; None for "Unmapped" or any
    other non-``$xxxx`` value."""
    if not value.startswith("$"):
        return None
    try:
        return int(value[1:], 16)
    except ValueError:
        return None


def current_source_map(api: C64Backend) -> dict[int, str]:
    """Which source — ``"socket1"``/``"socket2"``/``"ultisid1"``/``"ultisid2"``
    — currently answers each ``$Dxxx`` address, per the live `SID Addressing` +
    `SID Sockets Configuration` REST categories (best-effort; ``{}`` on any read
    failure). Physical sockets are populated LAST so they win an
    ``Auto Address Mirroring`` collision (a socket's real chip is what a listener
    hears; an UltiSID core "at" the same address in that state is just
    mirroring). Used by :mod:`c64cast.sid.sid_autoconfig` (chip-model matching) and
    :mod:`c64cast.sid.sid_panning` (which mixer source to pan for a non-remapped
    single-SID tune).

    v1 simplification: an UltiSID core is tracked only at its own configured base
    address, not the full window a split (`1/2`/`1/4`) expands it across — a chip
    at a split core's *secondary* instance address won't be recognized as
    already served by that core and may get a harmless re-route."""
    try:
        addressing = api.get_config_category(CAT_ADDRESSING)
        sockets = api.get_config_category(CAT_SOCKETS)
    except Exception:
        log.debug("sid_hw_config: live addressing read failed", exc_info=True)
        return {}
    addr_map: dict[int, str] = {}
    for core, item in (("ultisid1", ITEM_ULTISID1_ADDR), ("ultisid2", ITEM_ULTISID2_ADDR)):
        base = _parse_dxxx(addressing.get(item, ""))
        if base is not None:
            addr_map[base] = core
    if sockets.get(ITEM_SOCKET1_EN) == "Enabled":
        base = _parse_dxxx(addressing.get(ITEM_SOCKET1_ADDR, ""))
        if base is not None:
            addr_map[base] = "socket1"
    if sockets.get(ITEM_SOCKET2_EN) == "Enabled":
        base = _parse_dxxx(addressing.get(ITEM_SOCKET2_ADDR, ""))
        if base is not None:
            addr_map[base] = "socket2"
    return addr_map


def detect_sockets(api: C64Backend) -> tuple[bool, bool]:
    """Which physical SID sockets carry a detected chip (best-effort; (False,
    False) on any read failure)."""
    try:
        sockets = api.get_config_category(CAT_SOCKETS)
    except Exception:
        log.debug("sid_hw_config: socket detection read failed", exc_info=True)
        return (False, False)
    s1 = sockets.get(ITEM_SOCKET1_TYPE, "None") not in ("None", "")
    s2 = sockets.get(ITEM_SOCKET2_TYPE, "None") not in ("None", "")
    return (s1, s2)


def detect_socket_models(api: C64Backend) -> tuple[str | None, str | None]:
    """Which chip model each physical SID socket reports (e.g. "6581"/"8580"),
    or None for an empty/undetected socket (best-effort; (None, None) on any
    read failure). Same read as detect_sockets, un-collapsed to the string
    identity — dac_calibration_store.active_socket_at_d400 already treats these
    values as chip identity strings."""
    try:
        sockets = api.get_config_category(CAT_SOCKETS)
    except Exception:
        log.debug("sid_hw_config: socket model detection read failed", exc_info=True)
        return (None, None)

    def _model(value: str) -> str | None:
        return value if value not in ("None", "") else None

    return (
        _model(sockets.get(ITEM_SOCKET1_TYPE, "None")),
        _model(sockets.get(ITEM_SOCKET2_TYPE, "None")),
    )


def snapshot_sid_config(api: C64Backend) -> dict[tuple[str, str], str]:
    """Read the managed SID-address/socket/model config so teardown can
    restore it. Returns ``{(category, item): value}`` (empty on read
    failure)."""
    saved: dict[tuple[str, str], str] = {}
    try:
        addressing = api.get_config_category(CAT_ADDRESSING)
        sockets = api.get_config_category(CAT_SOCKETS)
        ultisid = api.get_config_category(CAT_ULTISID)
    except Exception:
        log.debug("sid_hw_config: config snapshot read failed", exc_info=True)
        return saved
    for item in MANAGED_ADDRESSING_ITEMS:
        if item in addressing:
            saved[(CAT_ADDRESSING, item)] = addressing[item]
    for item in MANAGED_SOCKET_ITEMS:
        if item in sockets:
            saved[(CAT_SOCKETS, item)] = sockets[item]
    for item in MANAGED_MODEL_ITEMS:
        if item in ultisid:
            saved[(CAT_ULTISID, item)] = ultisid[item]
    return saved


def _put_all(api: C64Backend, mapping: dict[tuple[str, str], str], *, warn: bool) -> None:
    """PUT every ``(category, item) -> value`` entry in `mapping` to the U64
    (best-effort per item). `warn` selects the log level on failure — WARNING
    for a forward apply (the caller wanted this change to take), DEBUG for a
    teardown restore (a failure there just leaves the U64 as the user last
    configured it, not broken)."""
    log_fn = log.warning if warn else log.debug
    for (category, item), value in mapping.items():
        try:
            api.put_config_item(category, item, value)
        except Exception:
            log_fn("sid_hw_config: failed to set %s/%s=%s", category, item, value, exc_info=True)


def apply_sid_map(api: C64Backend, sid_map: SidMap) -> None:
    """PUT every config item in `sid_map` to the U64 (best-effort per item)."""
    _put_all(api, sid_map.config, warn=True)


def apply_config(api: C64Backend, mapping: dict[tuple[str, str], str]) -> None:
    """PUT an arbitrary ``(category, item) -> value`` mapping to the U64
    (best-effort per item). For config changes that aren't a full
    :class:`~c64cast.sid.asid_sidmap.SidMap` — e.g. sid_autoconfig's model/filter-
    curve plan. `apply_sid_map` remains the SidMap-specific entry point for
    multi-SID address planning."""
    _put_all(api, mapping, warn=True)


def restore_sid_config(api: C64Backend, saved: dict[tuple[str, str], str]) -> None:
    """Restore a snapshot from :func:`snapshot_sid_config` (best-effort)."""
    _put_all(api, saved, warn=False)


class SidHwSession:
    """Tracks every SID hardware-config change one scene/tool makes so a single
    :meth:`restore` puts it all back: the :func:`snapshot_sid_config` baseline
    plus any originals folded in along the way (``apply_panning`` /
    ``apply_volume`` / ``sid_autoconfig`` return the values they overwrote).

    :meth:`snapshot` is idempotent — the first call wins — which mechanically
    enforces the single-snapshot rule (a re-snapshot mid-session would capture
    our own edits as if they were the user's config; see the SID architecture
    notes). :meth:`restore` resets the session so a re-setup of the same scene
    starts from a fresh baseline. Also a context manager for function-scoped
    sessions (dac_calibration's per-socket measurement loop), restoring on the
    way out of the ``with`` even on error."""

    def __init__(self, api: C64Backend) -> None:
        self._api = api
        self._saved: dict[tuple[str, str], str] | None = None

    @property
    def saved(self) -> dict[tuple[str, str], str] | None:
        """The recorded restore set (None before anything was recorded).
        Read-only view for tests and logging."""
        return self._saved

    def snapshot(self) -> None:
        """Record the managed-config baseline (first call wins)."""
        if self._saved is None:
            self._saved = snapshot_sid_config(self._api)

    def fold(self, originals: dict[tuple[str, str], str]) -> None:
        """Merge mixer/model originals into the restore set, so one restore
        puts addressing, model and mixer state back together."""
        if not originals:
            return
        if self._saved is None:
            self._saved = {}
        self._saved.update(originals)

    def restore(self) -> None:
        """Restore everything recorded (best-effort), then reset for reuse."""
        if self._saved:
            restore_sid_config(self._api, self._saved)
        self._saved = None

    def __enter__(self) -> SidHwSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()
