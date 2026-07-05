"""Shared U64 multi-SID hardware-config plumbing.

The REST side of the multi-SID story: snapshot the SID address/socket config,
apply a :class:`~c64cast.asid_sidmap.SidMap`, and restore the snapshot on
teardown. Extracted from :class:`~c64cast.asid_scene.AsidScene` so
:class:`~c64cast.waveform.WaveformScene` (playing a multi-SID `.sid` file) can
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
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_TYPE,
)

if TYPE_CHECKING:
    from .asid_sidmap import SidMap
    from .backend import C64Backend

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


def snapshot_sid_config(api: C64Backend) -> dict[tuple[str, str], str]:
    """Read the managed SID-address/socket config so teardown can restore it.
    Returns ``{(category, item): value}`` (empty on read failure)."""
    saved: dict[tuple[str, str], str] = {}
    try:
        addressing = api.get_config_category(CAT_ADDRESSING)
        sockets = api.get_config_category(CAT_SOCKETS)
    except Exception:
        log.debug("sid_hw_config: config snapshot read failed", exc_info=True)
        return saved
    for item in MANAGED_ADDRESSING_ITEMS:
        if item in addressing:
            saved[(CAT_ADDRESSING, item)] = addressing[item]
    for item in MANAGED_SOCKET_ITEMS:
        if item in sockets:
            saved[(CAT_SOCKETS, item)] = sockets[item]
    return saved


def apply_sid_map(api: C64Backend, sid_map: SidMap) -> None:
    """PUT every config item in `sid_map` to the U64 (best-effort per item)."""
    for (category, item), value in sid_map.config.items():
        try:
            api.put_config_item(category, item, value)
        except Exception:
            log.warning(
                "sid_hw_config: failed to set %s/%s=%s", category, item, value, exc_info=True
            )


def restore_sid_config(api: C64Backend, saved: dict[tuple[str, str], str]) -> None:
    """Restore a snapshot from :func:`snapshot_sid_config` (best-effort)."""
    for (category, item), value in saved.items():
        try:
            api.put_config_item(category, item, value)
        except Exception:
            log.debug(
                "sid_hw_config: config restore of %s/%s failed", category, item, exc_info=True
            )
