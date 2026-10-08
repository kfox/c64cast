"""Resolve ``[audio].dac_curve`` to the effective label and table (a
:class:`DacCurve`) for the connected system — the policy layer between the
calibration store (:mod:`c64cast.audio.dac_calibration_store`) and the audio
path that plays through the result.

See docs/architecture/audio.md#table-selection-auto-and-per-system-calibration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from c64cast.hw.c64 import SID
from c64cast.sid import armsid
from c64cast.sid.sid_hw_config import detect_socket_models

from .dac_calibration_store import (
    D400_UNKNOWN,
    d400_owner,
    load_calibrated_table_and_chip,
    path_for_key,
    resolve_calibration_key,
)
from .dac_curves import resolve_dac_curve

if TYPE_CHECKING:
    from c64cast.app.config import Config
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)


def auto_declined_chip(measured: tuple[int | None, str] | None) -> str | None:
    """The chip label of a calibrated entry that ``"auto"`` will not play
    through, or None when it would. ``measured`` is the entry's
    ``(socket, detected)`` from
    :func:`~c64cast.audio.dac_calibration_store.load_calibrated_table_and_chip`."""
    if measured is not None and armsid.is_armsid(measured[1]):
        return measured[1]
    return None


@dataclass(frozen=True)
class DacCurve:
    """What :func:`resolve_dac_curve_for_backend` chose.

    ``measured`` is the ``(socket, detected)`` of the calibrated entry whose
    table it read — the one ``table`` holds, or the one ``"auto"`` declined —
    and None when it read no table or the entry names no chip. Its socket is
    None for a ``"default"`` entry, measured without isolating one. A
    consumer that needs the chip reads it here rather than from the file again:
    each read of the file makes its own socket-map read, and one that fails
    falls back to the file's recorded mapping, which can name the other
    socket. ``key`` is the calibration key the resolution looked up, None
    when it looked up none, for the same reason: deriving it again is another
    live round-trip, and one that fails falls back to the host key."""

    label: str
    table: bytes | None
    measured: tuple[int | None, str] | None = None
    key: str | None = None

    @property
    def declined_chip(self) -> str | None:
        """The chip label of the calibration ``"auto"`` resolved past, or None
        when it played one or none applied."""
        return None if self.table is not None else auto_declined_chip(self.measured)


def _resolve_auto_curve(cfg: Config, be: C64Backend | None, key: str) -> DacCurve:
    """The ``"auto"`` arm: a calibrated table when one applies, the baked
    emulated-UltiSID table only when an UltiSID core answers ``$D400``, else
    the safe 4-bit linear path. ``key`` arrives already resolved because
    resolving it can cost a live device round-trip on the Ultimate."""
    path = path_for_key(cfg, key)
    table, measured = load_calibrated_table_and_chip(
        cfg, be=be, path=path, declines=lambda chip: auto_declined_chip(chip) is not None
    )
    if table is not None:
        declined = auto_declined_chip(measured)
        if declined is not None:
            # Its ladder metrics matched a good 6581's, yet it played a click
            # track as a splat that linear plays clean (#587), so no metric
            # here can vouch for it; "calibrated" is the explicit opt-in.
            log.warning(
                "the DAC calibration at %s was measured on an %s, which `auto` does not "
                "play through; using the 4-bit linear DAC. Set [audio].dac_curve = "
                '"calibrated" to use it anyway.',
                path,
                declined,
            )
            return DacCurve("linear", None, measured, key)
        return DacCurve(f"calibrated:{key}", table, measured, key)
    if cfg.audio.dac_calibration_profile:
        log.warning(
            "[audio].dac_calibration_profile = %r → %s holds no usable calibration; falling back.",
            cfg.audio.dac_calibration_profile,
            path,
        )
    # The baked table is the *emulated* UltiSID's curve, so it only applies when
    # an UltiSID core is what `STA $D418` actually reaches. A physical chip gets
    # the linear path instead: a cross-chip table measures ≈29% RMS level error
    # (audio.md#table-selection-auto-and-per-system-calibration), which lands as
    # signal-correlated distortion rather than a level trim.
    if cfg.hardware.backend == "ultimate":
        owner = d400_owner(be) if be is not None else None
        if isinstance(owner, int):
            log.warning(
                "SID socket %d (a physical chip) answers $D400 and no "
                "calibration for it was found at %s; falling back to the "
                "4-bit linear DAC. Run `c64cast -u <target> --calibrate-dac` "
                "to measure this chip for full-fidelity playback.",
                owner,
                key,
            )
            return DacCurve("linear", None, key=key)
        if owner == D400_UNKNOWN:
            # Not "an UltiSID core owns it": an Ultimate II+ has no socket
            # map to read and drives the C64's own chip, and a failed read
            # says nothing. The baked table on a physical chip is the ≈29%
            # RMS mismatch, so an unknown owner gets the safe path.
            log.warning(
                "could not tell which SID answers $D400 (no SID socket "
                "configuration on this device, or reading it failed) and no "
                "calibration was found at %s; falling back to the 4-bit linear "
                "DAC. Run `c64cast -u <target> --calibrate-dac` to measure the "
                "SID for full-fidelity playback.",
                key,
            )
            return DacCurve("linear", None, key=key)
        if be is not None:
            log.info(
                "no per-unit DAC calibration found for %s; using the baked "
                "mahoney_ultisid table. Run `--calibrate-dac` to measure a "
                "socketed physical SID.",
                key,
            )
        return DacCurve("mahoney_ultisid", resolve_dac_curve("mahoney_ultisid"), key=key)
    if be is not None:
        log.warning(
            "no DAC calibration found for %s; falling back to the 4-bit "
            "linear DAC. Run `c64cast -u <target> --calibrate-dac` to "
            "measure this SID for full-fidelity playback.",
            key,
        )
    return DacCurve("linear", None, key=key)


def resolve_dac_curve_for_backend(cfg: Config, be: C64Backend | None = None) -> DacCurve:
    """Resolve ``[audio].dac_curve`` to an effective label and table for this
    system/backend, with the calibrated entry the table came from (see
    :class:`DacCurve`). ``table`` is a 256-byte amplitude→``$D418`` map or None
    (the legacy linear 4-bit path).

    * ``"auto"`` (default) — prefer a calibrated table applicable to this
      system/socket if one exists, unless the calibrating run identified its chip as an
      ARMSID or ARM2SID (``linear`` then); else ``mahoney_ultisid`` when an UltiSID
      core answers ``$D400`` (the baked table *is* that core's curve); else
      ``linear`` (a physical/unknown SID with no calibration: the baked
      emulated table would not match it, so stay on the safe 4-bit path).
      Which source owns ``$D400`` is resolved live via :func:`d400_owner`,
      so a populated socket mapped there gets ``linear`` rather than a table
      measured on a different chip — and so does an owner that cannot be
      read (an Ultimate II+, or a failed read).
    * ``"calibrated"`` — force the applicable calibrated table; raise if absent.
    * ``"linear"`` / ``"mahoney_ultisid"`` — explicit; passed through.

    `be`, when given a live/reachable backend, lets the resolution pick the
    correct per-socket entry from a multi-SID calibration file (see
    :func:`~c64cast.audio.dac_calibration_store.load_calibrated_table`).
    Without it (e.g. offline ``--doctor --skip-probe``), resolution is
    best-effort."""
    name = cfg.audio.dac_curve
    if name == "calibrated":
        key = resolve_calibration_key(cfg, be)
        path = path_for_key(cfg, key)
        table, measured = load_calibrated_table_and_chip(cfg, be=be, path=path)
        if table is None:
            raise ValueError(
                "[audio].dac_curve = 'calibrated' but no usable calibration was found "
                f"at {path} (key {key}). "
                "Run `c64cast -u <target> --calibrate-dac` first, point "
                "[audio].dac_calibration_profile at an existing calibration file, or "
                "use 'auto'."
            )
        return DacCurve(f"calibrated:{key}", table, measured, key)
    if name == "auto":
        # Ahead of resolve_calibration_key: this arm must not pay its live
        # round-trip. digi_boost + an explicit curve is validate_dac_curve_cfg's.
        if cfg.audio.digi_boost:
            return DacCurve("linear", None)
        return _resolve_auto_curve(cfg, be, resolve_calibration_key(cfg, be))
    return DacCurve(name, resolve_dac_curve(name))


def provision_calibrated_chip_model(
    be: C64Backend, dac_curve: DacCurve
) -> dict[tuple[str, str], str] | None:
    """Put an ARMSID back into the model its calibrated table was measured in,
    for a run that plays through that table; return what to restore at teardown
    (None when nothing changed).

    An ARMSID's ``$D418`` ladder depends on its model, and the model is a
    setting that the menu, a tune's autoconfig or another tool can leave either
    way, so a table measured in one model and played in the other is a table
    for a different chip. The calibration records the model in the chip's
    label; this enforces it. A chip of fixed model, or a table that names none,
    is left alone.

    A calibration measured without socket detection recorded the chip only as
    whatever answered ``$D400``, so that chip is the one switched: through the
    socket an Ultimate maps there when it names one, else through the chip's
    own register protocol, which works on every link."""
    if not dac_curve.label.startswith("calibrated:") or dac_curve.measured is None:
        return None
    socket, recorded = dac_curve.measured
    measured_at = f"socket {socket}"
    wanted = armsid.label_model(recorded) if armsid.is_reconfigurable(recorded) else None
    if wanted is None:
        return None
    if socket is None:
        owner = d400_owner(be)
        if not isinstance(owner, int):
            return _provision_d400_model(be, recorded, wanted)
        # A register write at $D400 would leave the socket's config item, the
        # label cache and a scene's snapshot naming the old model, and a scene
        # restore then sets the item from its probe while teardown sets the
        # register back: the menu ends the run disagreeing with the chip.
        socket = owner
        measured_at = f"$D400 (now socket {socket})"
    if not be.profile.supports_sid_config:
        return None
    live = detect_socket_models(be)[socket - 1]
    if not armsid.is_reconfigurable(live) or armsid.is_right_channel(live):
        log.warning(
            "audio: the DAC calibration was measured on an %s at %s, which now "
            "reports %s; playing through it unchanged",
            recorded,
            measured_at,
            live or "nothing",
        )
        return None
    current = armsid.label_model(live)
    if current is None or current == wanted:
        return None
    source = f"socket{socket}"
    # Returned even when the switch fails: a write that took before its reply
    # was lost still gets put back, and putting back an unchanged model is a no-op.
    restore = {(armsid.CAT_SOCKET_MODEL, source): current}
    try:
        armsid.set_socket_model(be, source, wanted)
    except Exception:  # noqa: BLE001 — best-effort, like every SID config write
        log.warning(
            "audio: could not switch socket %d to %s for its DAC calibration; "
            "playing through it unchanged",
            socket,
            wanted,
            exc_info=True,
        )
        return restore
    log.info(
        "audio: switched the %s in socket %d to %s, the model its DAC calibration was measured in",
        (live or "").rsplit(" ", 1)[0],
        socket,
        wanted,
    )
    return restore


def _provision_d400_model(
    be: C64Backend, recorded: str, wanted: str
) -> dict[tuple[str, str], str] | None:
    """:func:`provision_calibrated_chip_model` for an entry with no socket: ask
    the chip at ``$D400`` for its model and switch it to `wanted` there."""
    reply = armsid.probe(be, SID.BASE)
    if reply is None or reply.model is None:
        log.warning(
            "audio: the DAC calibration was measured on an %s at $D400, which now "
            "reports %s; playing through it unchanged",
            recorded,
            "an unreadable model" if reply is not None else "no ARMSID",
        )
        return None
    if reply.model == wanted:
        return None
    # Returned even when the switch fails, for the reason the socket arm gives.
    restore = {(armsid.CAT_SOCKET_MODEL, armsid.SOURCE_D400): reply.model}
    try:
        armsid.set_socket_model(be, armsid.SOURCE_D400, wanted)
    except Exception:  # noqa: BLE001 — best-effort, like every SID config write
        log.warning(
            "audio: could not switch the %s at $D400 to %s for its DAC calibration; "
            "playing through it unchanged",
            reply.kind,
            wanted,
            exc_info=True,
        )
        return restore
    log.info(
        "audio: switched the %s at $D400 to %s, the model its DAC calibration was measured in",
        reply.kind,
        wanted,
    )
    return restore
