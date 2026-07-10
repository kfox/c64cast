"""SID Player Autoconfig: match a tune's requested chip model (6581/8580) to
the U64's actual SID hardware.

Port of the 1541ultimate firmware's "SID Player Autoconfig" (`u64_config.cc`,
`CFG_PLAYER_AUTOCONFIG`) into c64cast's own SID playback path — independent of
whatever the firmware's own (device-local) autoconfig is doing, since c64cast
drives SID playback via its hand-rolled 6502 player over DMA rather than the
firmware's PSID player.

A `.sid` file's PSID header can declare which chip model each voice expects
(:func:`c64cast.sid_host_emu.parse_sid_header`'s `sid_models`). Without this
module, c64cast ignores that entirely — a tune tagged "needs 8580" just plays
on whatever chip currently answers its address, silently wrong-sounding if
that's a 6581. This module reads the header, compares it against what's
actually socketed (via :mod:`c64cast.sid_hw_config`), and — best-effort, like
every other REST config helper in this codebase — reconfigures the U64 so the
tune lands on a matching chip: swap to a physical socket with a matching chip
if one exists, else fall back to an UltiSID FPGA core set to a representative
filter curve for that model, else warn and leave the chip on whatever answers
its address already.

Mirrors :mod:`c64cast.dac_calibration`'s auto/explicit-override +
snapshot/apply/restore shape, reusing :mod:`c64cast.sid_hw_config` (REST
plumbing) and :mod:`c64cast.asid_sidmap` (category/item name constants)
rather than duplicating either.

Hardware limitation inherited from firmware: a genuinely fixed physical
6581/8580 chip cannot be reconfigured to the other model. Autoconfig can only
*route around* it (swap sockets, or fall back to UltiSID), never transmute
it — see docs/caveats.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from .asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    CAT_ULTISID,
    FILTER_CURVE_6581,
    FILTER_CURVE_8580,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID1_FILTER,
    ITEM_ULTISID2_ADDR,
    ITEM_ULTISID2_FILTER,
)
from .sid_hw_config import apply_config, detect_socket_models, snapshot_sid_config

if TYPE_CHECKING:
    from .backend import C64Backend
    from .config import Config
    from .sid_host_emu import SidHeader

log = logging.getLogger(__name__)

# [ultimate64].sid_model / --sid-model value space. "auto" reads the tune's
# header per chip; an explicit "6581"/"8580" forces that model for every chip,
# ignoring the header; "off" disables header inspection + hardware
# reconfiguration entirely (matches firmware's CFG_PLAYER_AUTOCONFIG disabled
# state).
SID_MODEL_CHOICES: Final[tuple[str, ...]] = ("auto", "6581", "8580", "off")

# A chip whose header model is one of these carries no definite requirement —
# always a no-op regardless of what's socketed.
_NO_REQUIREMENT = (None, "?", "6581+8580")


def _parse_dxxx(value: str) -> int | None:
    """Parse a ``"$D400"``-style address enum value; None for "Unmapped" or
    any other non-``$xxxx`` value."""
    if not value.startswith("$"):
        return None
    try:
        return int(value[1:], 16)
    except ValueError:
        return None


def _current_addr_map(api: C64Backend) -> dict[int, str]:
    """Which source — ``"socket1"``/``"socket2"``/``"ultisid1"``/
    ``"ultisid2"`` — currently answers each ``$Dxxx`` address, per the live
    `SID Addressing` + `SID Sockets Configuration` REST categories (best-
    effort; ``{}`` on any read failure).

    v1 simplification: an UltiSID core is tracked only at its own configured
    base address, not the full window a split (`1/2`/`1/4`) would expand it
    across — a chip requesting a model at a split core's *secondary* instance
    address won't be recognized as "already served by an UltiSID core" and
    may get an unnecessary (harmless) re-route. Reasonable follow-up, not MVP
    scope — multi-SID tunes needing model-autoconfig on a split core are rare.

    HW-observed: with `Auto Address Mirroring` enabled (the factory-default
    resting state — c64cast's own multi-SID/model plans explicitly disable
    it), an UltiSID core and an enabled physical socket can both report the
    *same* configured base (e.g. both default to `$D400`) even though only
    the socket is actually audible there. Physical sockets are populated
    LAST so they win any such collision — a socket's real chip is what a
    listener hears; an UltiSID core "at" the same address in this state is
    just mirroring, not actually driving it."""
    try:
        addressing = api.get_config_category(CAT_ADDRESSING)
        sockets = api.get_config_category(CAT_SOCKETS)
    except Exception:
        log.debug("sid_autoconfig: live addressing read failed", exc_info=True)
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


def plan_sid_model_config(
    chips: tuple[tuple[int, str | None], ...],
    current_addr_map: dict[int, str],
    socket_models: tuple[str | None, str | None],
    *,
    ultisid_allowed: bool,
) -> dict[tuple[str, str], str] | None:
    """Pure, hardware-free decision function: given a tune's ``(address,
    required_model)`` chips, what currently answers each address, and what
    model each physical socket reports, decide the minimal REST PUT set that
    makes every chip with a definite model requirement play on a matching
    chip. No hardware access — see :func:`apply_sid_autoconfig` for the live
    wiring.

    `required_model` values of `None`/`"?"`/`"6581+8580"` carry no definite
    requirement and are always a no-op (any chip is fine). For chips that
    do:

      1. Whatever currently answers that address already matches → no-op.
      2. The *other* physical socket reports the required model (and isn't
         already claimed by an earlier chip in this same pass) → remap that
         socket's address to this chip's address (same address-swap
         mechanism :func:`c64cast.asid_sidmap.plan_sid_map_for_addresses`
         uses for multi-SID routing).
      3. `ultisid_allowed` and a free UltiSID core remains → route this
         chip's address to that core and set its filter-curve item to the
         fixed representative curve for the required model (`"6581"` /
         `"8580 Lo"` — the exact curve variant, e.g. `"8580 Hi"`, isn't
         exposed as a config knob in this pass).
      4. Otherwise: log a warning (best-effort — never raises) and leave the
         chip unchanged.

    Returns `None` (not an empty dict) when nothing needs to change — every
    chip already matches, or no chip has a definite requirement — so
    :func:`apply_sid_autoconfig` can skip the snapshot/apply dance entirely."""
    plan: dict[tuple[str, str], str] = {}
    reserved: set[str] = set()

    for address, required in chips:
        if required in _NO_REQUIREMENT:
            continue

        current_source = current_addr_map.get(address)
        if current_source in ("socket1", "socket2"):
            idx = 0 if current_source == "socket1" else 1
            if socket_models[idx] == required:
                log.info(
                    "sid autoconfig: chip at $%04X (%s) already on %s — no change",
                    address,
                    required,
                    current_source,
                )
                reserved.add(current_source)
                continue

        matched_idx = next(
            (
                idx
                for idx in (0, 1)
                if socket_models[idx] == required and f"socket{idx + 1}" not in reserved
            ),
            None,
        )
        if matched_idx is not None:
            addr_item = ITEM_SOCKET1_ADDR if matched_idx == 0 else ITEM_SOCKET2_ADDR
            en_item = ITEM_SOCKET1_EN if matched_idx == 0 else ITEM_SOCKET2_EN
            plan[(CAT_ADDRESSING, addr_item)] = f"${address:04X}"
            plan[(CAT_SOCKETS, en_item)] = "Enabled"
            reserved.add(f"socket{matched_idx + 1}")
            log.info(
                "sid autoconfig: chip at $%04X (%s) → socket %d (swap)",
                address,
                required,
                matched_idx + 1,
            )
            continue

        if ultisid_allowed:
            core = next((c for c in ("ultisid1", "ultisid2") if c not in reserved), None)
            if core is not None:
                addr_item = ITEM_ULTISID1_ADDR if core == "ultisid1" else ITEM_ULTISID2_ADDR
                filter_item = ITEM_ULTISID1_FILTER if core == "ultisid1" else ITEM_ULTISID2_FILTER
                curve = FILTER_CURVE_6581 if required == "6581" else FILTER_CURVE_8580
                plan[(CAT_ADDRESSING, addr_item)] = f"${address:04X}"
                plan[(CAT_ULTISID, filter_item)] = curve
                reserved.add(core)
                log.info(
                    "sid autoconfig: chip at $%04X (%s) → %s (filter curve %r, "
                    "no matching physical socket)",
                    address,
                    required,
                    core,
                    curve,
                )
                continue

        log.warning(
            "sid autoconfig: chip at $%04X wants %s but no matching physical "
            "socket or free UltiSID core is available — leaving it on "
            "whatever currently answers $%04X",
            address,
            required,
            address,
        )

    return plan or None


def resolve_sid_model_cfg(cfg: Config) -> str:
    """The resolved `[ultimate64].sid_model` value. Already validated/merged
    by `merge_cli` — unlike `[audio].dac_curve`'s `"auto"`/`"calibrated"`,
    every value here (`"auto"`/`"6581"`/`"8580"`/`"off"`) is a valid stored
    value with no further backend-dependent resolution needed."""
    return cfg.ultimate64.sid_model


def plan_model_config_for_header(
    api: C64Backend, header: SidHeader, sid_model: str
) -> dict[tuple[str, str], str] | None:
    """Resolve `sid_model`, read the *current* live SID hardware state, and
    decide the REST plan (if any) that makes the header's chip-model
    requirements match reality — but does not touch the hardware. Split out
    from :func:`apply_sid_autoconfig` so a caller that also plans multi-SID
    *address* routing (:class:`~c64cast.waveform.WaveformScene`) can apply
    that first, then call this against the now-current addressing (so a
    model swap doesn't fight an address remap decided moments earlier), all
    under one outer snapshot/apply/restore. Read-only; a REST read failure
    degrades to "nothing to change" (best-effort, like the rest of this
    module) rather than raising."""
    if sid_model == "off":
        log.info("sid autoconfig: off — leaving SID hardware config untouched")
        return None
    if not getattr(api.profile, "supports_config", False):
        log.info(
            "sid autoconfig: mode=%s but backend has no SID config API — cannot "
            "verify or correct chip model; playing on whatever answers each "
            "address now",
            sid_model,
        )
        return None

    n = len(header.sid_addresses)
    required = header.sid_models if sid_model == "auto" else tuple(sid_model for _ in range(n))
    chips = tuple(zip(header.sid_addresses, required, strict=True))

    current_addr_map = _current_addr_map(api)
    socket_models = detect_socket_models(api)
    plan = plan_sid_model_config(chips, current_addr_map, socket_models, ultisid_allowed=True)
    if not plan:
        log.info(
            "sid autoconfig: mode=%s — every chip already matches (or none has "
            "a definite model requirement); nothing to change",
            sid_model,
        )
    return plan


def apply_sid_autoconfig(
    api: C64Backend, header: SidHeader, sid_model: str
) -> dict[tuple[str, str], str]:
    """Single entry point scenes call, immediately before `run_sid_player`:
    resolves `sid_model`, decides + applies any SID hardware reconfiguration
    the tune's header requires, and returns a snapshot dict for the caller to
    restore via :func:`c64cast.sid_hw_config.restore_sid_config` at teardown.
    Empty dict = nothing changed (also safe to pass to `restore_sid_config`
    unconditionally — restoring `{}` is a no-op).

    A caller that also applies multi-SID address routing (WaveformScene)
    should use :func:`plan_model_config_for_header` + its own outer
    snapshot/apply instead, so both changes share one pre-change snapshot —
    see that function's docstring."""
    plan = plan_model_config_for_header(api, header, sid_model)
    if not plan:
        return {}
    saved = snapshot_sid_config(api)
    apply_config(api, plan)
    return saved
