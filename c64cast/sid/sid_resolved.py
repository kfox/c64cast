"""One log line naming the chip a listener will actually hear.

Every other module in this area logs its *intent* — sid_autoconfig says "chip at
$D400 (8580) → ultisid1", asid_sidmap says which chip it mapped where,
sid_volume says which mixer levels it moved. None of them says what came out the
other end, and the three can disagree: a core can be configured and then never
reach the mixer, a socket can keep answering an address a core was just handed,
a level the user trimmed to OFF can silence a chip that is otherwise routed
perfectly. Each of those produces **no error anywhere** — the config writes all
succeed — so the only symptom is wrong-sounding or silent playback, and
diagnosing it means reading four REST categories by hand.

So after routing, model matching, panning and volume have all settled, the live
state is read back once and rendered as a single line per tune chip: the source
answering its address, the chip model that source presents, and its mixer level
and pan. A chip that ends up on the wrong model, or inaudible, or unmapped makes
the line a WARNING instead of INFO.

Read-back, not a summary of what the planners decided: the planners are exactly
what this is here to catch. Pure renderer (:func:`describe_resolved_audio`) plus
one best-effort reader (:func:`log_resolved_audio`), like the rest of the SID
hardware-config modules — and a REST failure logs nothing rather than crashing
a scene.

A backend without the multi-SID surface but with the U2+ emulated-stereo-SID
surface renders from that instead (:func:`read_emusid_hardware_state`): the
side snooping each chip, its filter curve as the model, and its mixer level
and pan — with the declared host-SID verdict appended, because on such a
device the host machine's own SID plays the tune too, on its own output.

A backend that can't read the SID hardware state at all (TeensyROM has no
config API) still gets a model-match verdict when the machine's chip is
declared: ``[hardware].host_sid_model`` rides in on the backend profile, and
:func:`describe_declared_audio` renders the primary chip against it — the tune
wants an 8580, this machine is declared (or NTSC/PAL-assumed) to carry a 6581.
That mismatch is the single most audible mis-set on such a link, and without
the declaration nothing anywhere could say so.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .asid_sidmap import (
    CAT_ULTISID,
    ITEM_ULTISID1_FILTER,
    ITEM_ULTISID2_FILTER,
    NO_MODEL_REQUIREMENT,
)
from .emusid_mixer import ITEM_FILTER, emusid_topology, read_emusid_category
from .sid_hw_config import current_source_map, detect_socket_models
from .sid_panning import CAT_MIXER, PAN_ITEM
from .sid_volume import VOL_ITEM, VOL_OFF

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

_ULTISID_FILTER_ITEM: Final[dict[str, str]] = {
    "ultisid1": ITEM_ULTISID1_FILTER,
    "ultisid2": ITEM_ULTISID2_FILTER,
}
_SOCKET_INDEX: Final[dict[str, int]] = {"socket1": 0, "socket2": 1}

_UNMAPPED = "nothing mapped"
_NO_CHIP = "empty socket"
_UNKNOWN_LEVEL = "level unknown"

# Set once the NTSC/PAL host-model assumption has been logged, so a playlist
# of SID scenes states it on the first verdict that rides on it rather than
# at every scene activation.
_assumed_model_logged = False


@dataclass(frozen=True)
class SidHardwareState:
    """The live SID routing + mixer state a resolved-audio line renders from.

    `addr_map` is ``{$Dxxx: source}`` per
    :func:`~c64cast.sid.sid_hw_config.current_source_map`, `socket_models` the
    detected chip per physical socket, `ultisid_curves` each FPGA core's filter
    curve, and `mixer` the raw ``Audio Mixer`` category.

    The emulated-stereo-SID surface renders through the same shape
    (:func:`read_emusid_hardware_state`): `addr_map` maps snooped addresses to
    ``emusid1``/``emusid2``, `ultisid_curves` carries those sides' filter
    curves, `socket_models` is empty, and `mixer` is the raw ``Audio Output
    Settings`` category."""

    addr_map: dict[int, str]
    socket_models: tuple[str | None, str | None]
    ultisid_curves: dict[str, str]
    mixer: dict[str, str]

    def model_of(self, source: str) -> str:
        """The chip model `source` presents — a socket's detected chip, or the
        filter curve an FPGA core is emulating."""
        if (index := _SOCKET_INDEX.get(source)) is not None:
            return self.socket_models[index] or _NO_CHIP
        return self.ultisid_curves.get(source) or "?"

    def level_of(self, source: str) -> str:
        """`source`'s mixer volume label, or a placeholder when the mixer read
        didn't report it."""
        return self.mixer.get(VOL_ITEM.get(source, ""), _UNKNOWN_LEVEL)

    def pan_of(self, source: str) -> str:
        """`source`'s mixer pan label, or ``""`` when the mixer didn't report
        it."""
        return self.mixer.get(PAN_ITEM.get(source, ""), "")

    def audible(self, source: str) -> bool:
        """Whether `source` is at a level a listener can hear. An unreported
        level counts as audible — claiming silence we didn't measure would send
        someone hunting a mixer problem that isn't there."""
        return self.level_of(source) != VOL_OFF


@dataclass(frozen=True)
class ResolvedAudio:
    """A rendered resolved-audio line and whether it describes a clean result.
    `clean` is False when any chip is unmapped, muted, or on a model the tune
    didn't ask for — the cases worth a WARNING."""

    summary: str
    clean: bool


def _describe_chip(address: int, required: str | None, state: SidHardwareState) -> tuple[str, bool]:
    """One chip's rendered fragment and whether it landed as the tune asked."""
    source = state.addr_map.get(address)
    if source is None:
        return f"${address:04X} → {_UNMAPPED}", False

    model = state.model_of(source)
    fragment = f"${address:04X} → {source} ({model}) @ {state.level_of(source).strip()}"
    if pan := state.pan_of(source):
        fragment = f"{fragment} {pan}"

    if not state.audible(source):
        return f"{fragment} — INAUDIBLE", False
    if required not in NO_MODEL_REQUIREMENT and not model.startswith(required or ""):
        return f"{fragment} — tune wants {required}", False
    return fragment, True


def _describe_bystanders(claimed: set[str], state: SidHardwareState) -> str:
    """The sources the tune does not play on that are still mapped *and*
    audible, rendered as a trailing clause (``""`` when there are none). One
    left up bleeds into the mix, which sounds like a detuned double rather than
    like a configuration mistake.

    Drawn from the address map rather than the mixer, so a source that is merely
    unmuted but answers no address — a disabled socket the user never turned
    down — isn't reported as something they can hear."""
    others = [
        f"{source} ({state.model_of(source)}) at ${address:04X} @ {state.level_of(source).strip()}"
        for address, source in sorted(state.addr_map.items())
        if source not in claimed and state.model_of(source) != _NO_CHIP and state.audible(source)
    ]
    return f"; also audible: {', '.join(others)}" if others else ""


def describe_resolved_audio(
    state: SidHardwareState,
    addresses: Sequence[int],
    required_models: Sequence[str | None] = (),
) -> ResolvedAudio:
    """Pure renderer: what a listener hears for a tune whose chips sit at
    `addresses`, given the live hardware `state`. `required_models` is the
    model each chip asked for (parallel to `addresses`); a short or empty
    sequence just means those chips are reported without a match check."""
    required = tuple(required_models) + (None,) * (len(addresses) - len(required_models))
    described = [
        _describe_chip(address, want, state)
        for address, want in zip(addresses, required, strict=False)
    ]
    claimed = {source for a in addresses if (source := state.addr_map.get(a)) is not None}
    summary = "; ".join(fragment for fragment, _ok in described)
    return ResolvedAudio(
        summary=summary + _describe_bystanders(claimed, state),
        clean=all(ok for _fragment, ok in described),
    )


def describe_declared_audio(
    host_model: str, assumed: bool, address: int, required: str | None
) -> ResolvedAudio:
    """Pure renderer for the no-hardware-state fallback: what a listener hears
    at `address` given only the declared (or NTSC/PAL-assumed) host SID
    model. One chip only — a link that can't read the SID state also can't
    route extra chips, so the host machine's own SID is all that plays."""
    origin = "assumed" if assumed else "declared"
    fragment = f"${address:04X} → host SID ({host_model} {origin})"
    if required not in NO_MODEL_REQUIREMENT and not host_model.startswith(required or ""):
        return ResolvedAudio(f"{fragment} — tune wants {required}", clean=False)
    return ResolvedAudio(fragment, clean=True)


def _declared_host_verdict(
    api: C64Backend, address: int, required: str | None
) -> ResolvedAudio | None:
    """The primary chip's verdict from ``[hardware].host_sid_model``, or None
    when no model is declared (`host_sid_model = "unknown"`, or a profile
    predating the field). Also logs the once-per-run note that the NTSC/PAL
    convention is an assumption, the first time a verdict rides on it."""
    global _assumed_model_logged
    host_model = getattr(api.profile, "host_sid_model", None)
    if host_model is None:
        return None
    assumed = bool(getattr(api.profile, "host_sid_model_assumed", False))
    if assumed and not _assumed_model_logged:
        _assumed_model_logged = True
        log.info(
            "sid hardware: assuming this machine's SID is a %s (the NTSC=6581 / "
            "PAL=8580 convention — an assumption, not a measurement; set "
            "[hardware].host_sid_model if it's wrong)",
            host_model,
        )
    return describe_declared_audio(host_model, assumed, address, required)


def _log_declared_audio(api: C64Backend, address: int, required: str | None) -> None:
    """Render the primary chip's verdict from ``[hardware].host_sid_model``
    when the live state can't be read. Silent when no model is declared."""
    resolved = _declared_host_verdict(api, address, required)
    if resolved is None:
        return
    log_fn = log.info if resolved.clean else log.warning
    log_fn("sid hardware: %s", resolved.summary)


def read_sid_hardware_state(api: C64Backend) -> SidHardwareState | None:
    """Read the live SID routing + mixer state (best-effort; None on a backend
    without the multi-SID config surface or any read failure)."""
    if not getattr(api.profile, "supports_sid_config", False):
        return None
    try:
        ultisid = api.get_config_category(CAT_ULTISID)
        mixer = api.get_config_category(CAT_MIXER)
    except Exception:
        log.debug("sid hardware: resolved-audio read failed", exc_info=True)
        return None
    return SidHardwareState(
        addr_map=current_source_map(api),
        socket_models=detect_socket_models(api),
        ultisid_curves={
            source: ultisid.get(item, "") for source, item in _ULTISID_FILTER_ITEM.items()
        },
        mixer=mixer,
    )


def read_emusid_hardware_state(api: C64Backend) -> SidHardwareState | None:
    """Read the emulated-stereo-SID surface into the same renderable shape
    (best-effort; None on a backend without that surface or any read
    failure). One category carries everything — topology, curves, and the
    mixer items — so this is a single REST read."""
    category = read_emusid_category(api)
    if category is None:
        return None
    addr_map: dict[int, str] = {}
    for source, address in emusid_topology(category).items():
        addr_map.setdefault(address, source)  # emusid1 first, wins a shared address
    return SidHardwareState(
        addr_map=addr_map,
        socket_models=(None, None),
        ultisid_curves={source: category.get(item, "") for source, item in ITEM_FILTER.items()},
        mixer=category,
    )


def log_resolved_audio(
    api: C64Backend,
    addresses: Sequence[int],
    required_models: Sequence[str | None] = (),
) -> None:
    """Read the settled SID hardware state back and log what will be heard.
    Call once per scene setup, after routing/model/panning/volume have all been
    applied. Best-effort and silent on any failure.

    A backend without the multi-SID surface renders from the emulated-stereo-
    SID surface instead when it has one, with the declared host-SID verdict
    appended — on such a device the host machine's own SID plays the tune too,
    just on a different output than the snooped emulations. A backend that
    can't read any SID state (no config API — as opposed to a capable one
    whose read failed transiently) still renders the primary chip against the
    declared host model alone (see :func:`describe_declared_audio`)."""
    if not addresses:
        return
    required0 = required_models[0] if required_models else None
    if not getattr(api.profile, "supports_sid_config", False):
        state = read_emusid_hardware_state(api)
        if state is None:
            _log_declared_audio(api, addresses[0], required0)
            return
        resolved = describe_resolved_audio(state, addresses, required_models)
        if (host := _declared_host_verdict(api, addresses[0], required0)) is not None:
            resolved = ResolvedAudio(
                summary=f"{resolved.summary}; {host.summary} on the machine's own audio output",
                clean=resolved.clean and host.clean,
            )
        log_fn = log.info if resolved.clean else log.warning
        log_fn("sid hardware: %s", resolved.summary)
        return
    state = read_sid_hardware_state(api)
    if state is None:
        return
    resolved = describe_resolved_audio(state, addresses, required_models)
    log_fn = log.info if resolved.clean else log.warning
    log_fn("sid hardware: %s", resolved.summary)
