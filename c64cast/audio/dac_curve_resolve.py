"""Resolve ``[audio].dac_curve`` to the effective ``(label, table)`` pair for
the connected system — the policy layer between the calibration store
(:mod:`c64cast.audio.dac_calibration_store`) and the audio path that plays
through the result.

See docs/architecture/audio.md#table-selection-auto-and-per-system-calibration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from c64cast.sid import armsid
from c64cast.sid.sid_hw_config import detect_socket_models

from .dac_calibration_store import (
    D400_UNKNOWN,
    calibrated_chip,
    d400_owner,
    load_calibrated_table,
    path_for_key,
    resolve_calibration_key,
)
from .dac_curves import resolve_dac_curve

if TYPE_CHECKING:
    from c64cast.app.config import Config
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)


def _resolve_auto_curve(cfg: Config, be: C64Backend | None, key: str) -> tuple[str, bytes | None]:
    """The ``"auto"`` arm: a calibrated table when one applies, the baked
    emulated-UltiSID table only when an UltiSID core answers ``$D400``, else
    the safe 4-bit linear path. ``key`` arrives already resolved because
    resolving it can cost a live device round-trip on the Ultimate."""
    path = path_for_key(cfg, key)
    table = load_calibrated_table(cfg, be=be, path=path)
    if table is not None:
        measured = calibrated_chip(cfg, be=be, path=path)
        if measured is not None and armsid.is_armsid(measured[1]):
            # Its ladder metrics matched a good 6581's, yet it played a click
            # track as a splat that linear plays clean (#587), so no metric
            # here can vouch for it; "calibrated" is the explicit opt-in.
            log.warning(
                "the DAC calibration at %s was measured on an %s, which `auto` does not "
                "play through; using the 4-bit linear DAC. Set [audio].dac_curve = "
                '"calibrated" to use it anyway.',
                path,
                measured[1],
            )
            return ("linear", None)
        return (f"calibrated:{key}", table)
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
            return ("linear", None)
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
            return ("linear", None)
        if be is not None:
            log.info(
                "no per-unit DAC calibration found for %s; using the baked "
                "mahoney_ultisid table. Run `--calibrate-dac` to measure a "
                "socketed physical SID.",
                key,
            )
        return ("mahoney_ultisid", resolve_dac_curve("mahoney_ultisid"))
    if be is not None:
        log.warning(
            "no DAC calibration found for %s; falling back to the 4-bit "
            "linear DAC. Run `c64cast -u <target> --calibrate-dac` to "
            "measure this SID for full-fidelity playback.",
            key,
        )
    return ("linear", None)


def resolve_dac_curve_for_backend(
    cfg: Config, be: C64Backend | None = None
) -> tuple[str, bytes | None]:
    """Resolve ``[audio].dac_curve`` to an effective ``(label, table)`` pair for
    this system/backend. ``table`` is a 256-byte amplitude→``$D418`` map or None
    (the legacy linear 4-bit path).

    * ``"auto"`` (default) — prefer a calibrated table applicable to this
      system/socket if one exists, unless it was measured on an ARMSID
      (``linear`` then); else ``mahoney_ultisid`` when an UltiSID
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
    :func:`load_calibrated_table`). Without it (e.g. offline ``--doctor
    --skip-probe``), resolution is best-effort."""
    name = cfg.audio.dac_curve
    if name == "calibrated":
        key = resolve_calibration_key(cfg, be)
        path = path_for_key(cfg, key)
        table = load_calibrated_table(cfg, be=be, path=path)
        if table is None:
            raise ValueError(
                "[audio].dac_curve = 'calibrated' but no usable calibration was found "
                f"at {path} (key {key}). "
                "Run `c64cast -u <target> --calibrate-dac` first, point "
                "[audio].dac_calibration_profile at an existing calibration file, or "
                "use 'auto'."
            )
        return (f"calibrated:{key}", table)
    if name == "auto":
        # Ahead of resolve_calibration_key: this arm must not pay its live
        # round-trip. digi_boost + an explicit curve is validate_dac_curve_cfg's.
        if cfg.audio.digi_boost:
            return ("linear", None)
        return _resolve_auto_curve(cfg, be, resolve_calibration_key(cfg, be))
    return (name, resolve_dac_curve(name))


def provision_calibrated_chip_model(
    cfg: Config, be: C64Backend, dac_curve_label: str
) -> dict[tuple[str, str], str] | None:
    """Put an ARMSID back into the model its calibrated table was measured in,
    for a run that plays through that table; return what to restore at teardown
    (None when nothing changed).

    An ARMSID's ``$D418`` ladder depends on its model, and the model is a
    setting that the menu, a tune's autoconfig or another tool can leave either
    way, so a table measured in one model and played in the other is a table
    for a different chip. The calibration records the model in the chip's
    label; this enforces it. A chip of fixed model, or a table that names none,
    is left alone."""
    if not dac_curve_label.startswith("calibrated:"):
        return None
    measured = calibrated_chip(cfg, be=be, path=path_for_key(cfg, dac_curve_label.split(":", 1)[1]))
    if measured is None:
        return None
    socket, recorded = measured
    wanted = armsid.label_model(recorded) if armsid.is_reconfigurable(recorded) else None
    if wanted is None:
        return None
    live = detect_socket_models(be)[socket - 1]
    if not armsid.is_reconfigurable(live) or armsid.is_right_channel(live):
        log.warning(
            "audio: the DAC calibration was measured on an %s in socket %d, which now "
            "reports %s; playing through it unchanged",
            recorded,
            socket,
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
