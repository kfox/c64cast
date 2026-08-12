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

Those two outputs can disagree, and when they do the verdict alone is not
enough. A tune matched to an 8580 emulation still plays on the machine's own
6581 through the AV cable, so it sounds thin and scratchy there while the log
says everything matched — which reads exactly like a failing SID.
:func:`_warn_output_split` therefore names the consequence and the remedy once
per run, rather than leaving a listener to infer either from a line about
configuration.

A backend that can't read the SID hardware state at all (TeensyROM has no
config API) still gets a model-match verdict when the machine's chips are
declared, since nothing on such a link can *ask* what the host C64 carries.
``[hardware].host_sid_model`` rides in on the backend profile and
:func:`describe_declared_audio` renders the primary chip against it — the tune
wants an 8580, this machine is declared (or NTSC/PAL-assumed) to carry a 6581.
That mismatch is the single most audible mis-set on such a link, and without
the declaration nothing anywhere could say so.

One chip is not always the whole machine, though. A C64 with an internal
dual-SID mod (ARM2SID, SIDFX, DualSID) answers at a second address in its own
hardware, and a multi-SID tune plays on both chips with no routing required —
the mod has already done in silicon what the U64 does in config. Such a
machine is declared per chip with ``[hardware].host_sid_chips``, and
:func:`describe_declared_chips` gives every tune chip its own verdict, which
matters most where these mods usually land: one 6581 and one 8580 at once.

Most machines have no such mod, though, and a multi-SID tune on one of them is
usually a tune picked by mistake. The declared-chip verdict can only say so
once someone has declared their chips, which leaves the default configuration
— nothing declared — silent about exactly the case most likely to be an error.
:func:`_warn_unplaceable_chips` closes that: it needs no declaration, because
"this machine has one SID" is the safe assumption to warn from when the
alternative is a mod the user would have had to install deliberately.
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
# A host_sid_chips entry whose model the user doesn't know — the chip exists,
# so the address is covered, but no model verdict can be passed on it.
_MODEL_UNDECLARED = "unknown"

# Set once the NTSC/PAL host-model assumption has been logged, so a playlist
# of SID scenes states it on the first verdict that rides on it rather than
# at every scene activation.
_assumed_model_logged = False

# Set once the two-outputs guidance has been given. The per-scene verdict keeps
# reporting the mismatch; the advice about which cable to listen to is the same
# every time, so it is said once rather than at every scene activation.
_output_split_logged = False

# Set once a tune has been found to drive more chips than the machine can place.
# Same reasoning as the two above: the condition is a property of the machine
# and the tune choice, not something a listener needs restated per scene.
_unplaceable_logged = False

# A real SID decodes its address only partially and answers all of this range,
# so a second chip addressed anywhere inside it lands on the first one.
_SID_DECODE_WINDOW = range(0xD400, 0xD800)


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
    model. The primary chip only — ``[hardware].host_sid_model`` describes one
    chip, so a machine with more than one goes through
    :func:`describe_declared_chips` instead."""
    origin = "assumed" if assumed else "declared"
    fragment = f"${address:04X} → host SID ({host_model} {origin})"
    if required not in NO_MODEL_REQUIREMENT and not host_model.startswith(required or ""):
        return ResolvedAudio(f"{fragment} — tune wants {required}", clean=False)
    return ResolvedAudio(fragment, clean=True)


def describe_declared_chips(
    chips: Sequence[tuple[int, str]],
    addresses: Sequence[int],
    required_models: Sequence[str | None] = (),
) -> ResolvedAudio:
    """Pure renderer for a machine whose internal SIDs are declared per chip
    (``[hardware].host_sid_chips`` — a dual-SID mod). Every tune chip gets its
    own verdict against the chip declared at that address.

    A tune address with no declared chip is reported as such rather than
    dropped: on a partly-declared machine that is the honest statement, and
    silently omitting it would hide the one chip most likely to be misplaced.
    A chip declared ``"unknown"`` is reported without a model verdict — the
    user has said a chip is there and that they don't know which."""
    declared = dict(chips)
    required = tuple(required_models) + (None,) * (len(addresses) - len(required_models))
    fragments: list[str] = []
    ok = True
    for address, want in zip(addresses, required, strict=False):
        model = declared.get(address)
        if model is None:
            fragments.append(f"${address:04X} → no chip declared")
            ok = False
        elif model == _MODEL_UNDECLARED:
            fragments.append(f"${address:04X} → host SID (model unknown)")
        elif want not in NO_MODEL_REQUIREMENT and not model.startswith(want or ""):
            fragments.append(f"${address:04X} → host SID ({model} declared) — tune wants {want}")
            ok = False
        else:
            fragments.append(f"${address:04X} → host SID ({model} declared)")
    # No bystander clause, unlike describe_resolved_audio's: a declared chip the
    # tune doesn't drive is receiving no writes, so it makes no sound. The U64's
    # bystanders are audible (mapped and unmuted); a silent chip has no place in
    # a line about what a listener hears.
    return ResolvedAudio("; ".join(fragments), clean=ok)


def host_chip_fit(
    api: C64Backend,
    addresses: Sequence[int],
    required_models: Sequence[str | None] = (),
) -> bool | None:
    """Whether the machine's *own* SID chips can play a tune driving chips at
    `addresses` as authored — the same judgement :func:`log_resolved_audio`
    renders after the fact, asked ahead of time so a multi-file pool can pick a
    tune that will pass it.

    Routed through the same renderers as the verdict rather than re-deriving
    the match: a picker that disagreed with the line logged moments later would
    be worse than no picker at all.

    Note what each declaration buys. ``host_sid_chips`` describes the whole
    machine, so a tune driving a chip at an address it doesn't list fails —
    that is what skips 2SID tunes on a single-SID machine. ``host_sid_model``
    alone judges the primary chip's model only: it names a chip without
    claiming it is the only one, and inferring a chip *count* from it would
    reject working tunes on a machine whose second chip we were simply never
    told about.

    ``None`` means "no opinion", and a caller must treat it as "don't filter":

    * a link that reads the real SID state (U64) re-places chips per tune, so
      there is no fixed hardware for a tune to fit or miss;
    * ``host_sid_model = "unknown"`` with no ``host_sid_chips`` declares nothing
      to judge against;
    * an *assumed* model is the NTSC/PAL convention, and dropping tunes out of
      someone's directory on the strength of a guess is not a trade worth
      making — the verdict may warn on a guess, but selection won't act on one.
    """
    if getattr(api.profile, "supports_sid_config", False):
        return None
    if chips := tuple(getattr(api.profile, "host_sid_chips", ())):
        return describe_declared_chips(chips, addresses, required_models).clean
    host_model = getattr(api.profile, "host_sid_model", None)
    if host_model is None or getattr(api.profile, "host_sid_model_assumed", False):
        return None
    if not addresses:
        return None
    required = required_models[0] if required_models else None
    return describe_declared_audio(host_model, False, addresses[0], required).clean


def _declared_host_verdict(
    api: C64Backend, addresses: Sequence[int], required_models: Sequence[str | None]
) -> ResolvedAudio | None:
    """The host machine's verdict from what the config declares about it, or
    None when it declares nothing (`host_sid_model = "unknown"` with no
    `host_sid_chips`, or a profile predating the fields).

    ``host_sid_chips`` wins when present: it describes every chip, including
    the second one a dual-SID mod adds, which the single-valued
    ``host_sid_model`` can't reach. Falling back to that model covers the
    ordinary one-SID machine, and warns once per run when the NTSC/PAL
    convention is what a verdict rests on."""
    global _assumed_model_logged
    if chips := tuple(getattr(api.profile, "host_sid_chips", ())):
        return describe_declared_chips(chips, addresses, required_models)
    address = addresses[0]
    required = required_models[0] if required_models else None
    host_model = getattr(api.profile, "host_sid_model", None)
    if host_model is None:
        return None
    assumed = bool(getattr(api.profile, "host_sid_model_assumed", False))
    if assumed and not _assumed_model_logged:
        _assumed_model_logged = True
        # WARNING, not INFO: every model verdict on this link is only as good as
        # this guess, and the NTSC/PAL convention is a weak one — plenty of NTSC
        # machines carry an 8580. A guess that silently underwrites a verdict is
        # worth interrupting for once; declaring the model silences it for good.
        log.warning(
            "sid hardware: this machine's SID model is undeclared and cannot be "
            "read over this link — assuming %s from the NTSC=6581 / PAL=8580 "
            "convention. That is a guess, and every model verdict below rests on "
            "it: set [hardware].host_sid_model to the chip this machine actually "
            "carries (or 'unknown' to stop guessing).",
            host_model,
        )
    return describe_declared_audio(host_model, assumed, address, required)


def _warn_output_split(*, emu_clean: bool, host: ResolvedAudio) -> None:
    """Say, once per run, what a *listener* should do when the two outputs
    disagree — the emulations play the tune as authored, the machine's own
    chip cannot.

    The per-chip verdict above already states the mismatch, but it states it as
    a fact about configuration, and the symptom reaches the user as sound: a
    tune going thin and scratchy through the monitor while the config log says
    everything matched. The obvious reading of that is a failing SID, and
    someone can lose an evening to it before suspecting the cable. So when the
    Ultimate's own output is correct and the machine's is not, name the
    consequence and the remedy instead of leaving both to be inferred.

    Not gated on a mismatch alone: if the emulations are *also* wrong, the
    problem is configuration and pointing at a cable would misdirect."""
    global _output_split_logged
    if _output_split_logged or host.clean or not emu_clean:
        return
    _output_split_logged = True
    log.warning(
        "sid hardware: this tune plays as authored on the Ultimate's own audio "
        "output, and on the wrong chip model through the C64's AV output — the "
        "machine's internal SID is what it is and no setting can change it. "
        "That is expected here, not a failing SID: listen on the Ultimate's "
        "audio jack to hear the tune as written."
    )


def _unplaceable_chips(api: C64Backend, addresses: Sequence[int]) -> tuple[int, ...]:
    """The tune's chip addresses that nothing on this machine will answer as a
    SID of its own: the ones past the first, on a link with no routing surface,
    with no ``[hardware].host_sid_chips`` entry to say a mod covers them.

    Chip 0 is excluded rather than checked against the declaration: every C64
    has a SID at ``$D400``, so it is placeable whether or not anyone has
    declared it, and including it would fire this on ordinary single-SID
    tunes."""
    if getattr(api.profile, "supports_sid_config", False):
        return ()
    declared = dict(getattr(api.profile, "host_sid_chips", ()))
    return tuple(address for address in addresses[1:] if address not in declared)


def _warn_unplaceable_chips(api: C64Backend, addresses: Sequence[int], *, emulated: bool) -> bool:
    """Say, once per run, that this tune asks for more SID chips than the
    machine has — and return whether that is the case, logged or not.

    The gap this fills is which machines get told. A partly-declared machine
    already gets ``$D420 → no chip declared`` in its verdict, but the common
    case is a machine that has declared nothing at all, and there the verdict
    is silent about the second chip (``host_sid_model`` describes one chip) or
    absent entirely. That is backwards: the person who described their hardware
    carefully is warned, and the person likeliest to have grabbed a 2SID tune
    by mistake hears only the result.

    Stated as a consequence rather than a configuration fact, for the same
    reason as :func:`_warn_output_split`: the symptom reaches a listener as a
    scratchy mess, which reads as a broken tune or a failing SID rather than as
    a tune this machine was never able to play.

    Returns True on the condition, not on having logged, so a caller can
    suppress a second which-cable message for the same tune on a later pass."""
    global _unplaceable_logged
    extra = _unplaceable_chips(api, addresses)
    if not extra:
        return False
    if _unplaceable_logged:
        return True
    _unplaceable_logged = True
    chips = ", ".join(f"${address:04X}" for address in addresses)
    missing = ", ".join(f"${address:04X}" for address in extra)
    if any(address in _SID_DECODE_WINDOW for address in extra):
        fate = (
            "a single SID answers all of $D400-$D7FF, so both chips' writes land on "
            "that one chip and the tune will not sound as written"
        )
    else:
        fate = (
            "those addresses are cartridge I/O rather than SID space, so writes to "
            "them make no sound at all"
        )
    if emulated:
        log.warning(
            "sid hardware: this tune drives %d SID chips (%s) and this machine is not "
            "declared to have one at %s. It plays as authored on the Ultimate's own "
            "audio output, but through the C64's AV output %s — you may have picked a "
            "multi-SID tune by mistake. Listen on the Ultimate's audio jack, or "
            "declare an internal dual-SID mod with [hardware].host_sid_chips.",
            len(addresses),
            chips,
            missing,
            fate,
        )
    else:
        log.warning(
            "sid hardware: this tune drives %d SID chips (%s) and this machine is not "
            "declared to have one at %s. On a stock C64 %s — you may have picked a "
            "multi-SID tune by mistake. Play a single-SID tune, or declare an internal "
            "dual-SID mod with [hardware].host_sid_chips.",
            len(addresses),
            chips,
            missing,
            fate,
        )
    return True


def _log_declared_audio(
    api: C64Backend, addresses: Sequence[int], required_models: Sequence[str | None]
) -> None:
    """Render the host machine's verdict from what the config declares about
    it, when the live state can't be read. Silent when it declares nothing."""
    resolved = _declared_host_verdict(api, addresses, required_models)
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
    whose read failed transiently) renders against what the config declares
    about the machine instead — per chip when ``[hardware].host_sid_chips``
    describes a dual-SID mod, otherwise the primary chip alone (see
    :func:`describe_declared_chips` / :func:`describe_declared_audio`)."""
    if not addresses:
        return
    if not getattr(api.profile, "supports_sid_config", False):
        state = read_emusid_hardware_state(api)
        unplaceable = _warn_unplaceable_chips(api, addresses, emulated=state is not None)
        if state is None:
            _log_declared_audio(api, addresses, required_models)
            return
        resolved = describe_resolved_audio(state, addresses, required_models)
        if (host := _declared_host_verdict(api, addresses, required_models)) is not None:
            # Label the host route as a *group* rather than trailing the phrase
            # after it: with two declared chips a suffix reads as if only the
            # last fragment were on the machine's own output.
            if not unplaceable:
                # Both messages end in "listen on the Ultimate's jack", but the
                # split warning attributes the AV output's problem to the chip
                # model, which is the wrong diagnosis when the real cause is a
                # chip that isn't there. The more specific one has already run.
                _warn_output_split(emu_clean=resolved.clean, host=host)
            resolved = ResolvedAudio(
                summary=f"{resolved.summary}; on the machine's own audio output: {host.summary}",
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
