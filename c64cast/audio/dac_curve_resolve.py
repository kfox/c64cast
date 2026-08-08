"""Resolve ``[audio].dac_curve`` to the effective ``(label, table)`` pair for
the connected system — the policy layer between the calibration store
(:mod:`c64cast.audio.dac_calibration_store`) and the audio path that plays through
the result. The part worth reading is ``"auto"``'s fallback ladder: a
calibrated table when one applies to the SID actually answering ``$D400``,
the baked emulated-UltiSID table only when an UltiSID core owns that address,
and the safe 4-bit linear path otherwise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .dac_calibration_store import (
    active_socket_at_d400,
    load_calibrated_table,
    path_for_key,
    resolve_calibration_key,
)
from .dac_curves import resolve_dac_curve

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from c64cast.app.config import Config
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)


def _resolve_auto_curve(cfg: Config, be: C64Backend | None, key: str) -> tuple[str, bytes | None]:
    """The ``"auto"`` arm: a calibrated table when one applies, the baked
    emulated-UltiSID table only when an UltiSID core answers ``$D400``, else
    the safe 4-bit linear path. ``key`` arrives already resolved because
    resolving it can cost a live device round-trip on the Ultimate — it used
    to be recomputed for every log line below."""
    path = path_for_key(cfg, key)
    table = load_calibrated_table(cfg, be=be, path=path)
    if table is not None:
        return (f"calibrated:{key}", table)
    if cfg.audio.dac_calibration_profile:
        # A profile the user named by hand that resolves to nothing is a
        # typo or a wrong path — not the ordinary "this machine was never
        # calibrated" case the fallbacks below exist for. Name the file that
        # was missed, since the key alone doesn't say where it looked.
        log.warning(
            "[audio].dac_calibration_profile = %r → %s holds no usable calibration; falling back.",
            cfg.audio.dac_calibration_profile,
            path,
        )
    # Went looking for a per-unit calibration and found none. With a live
    # backend — a real playback resolution, not an offline --doctor pass,
    # which can't confirm the identity key and reports separately — say so
    # in the log, so a missing calibration isn't a silent fidelity
    # downgrade. Level matches the fallback: the emulated-UltiSID baked
    # table is a correct default (info); the 4-bit linear path is a real
    # downgrade for a physical SID (warning).
    if cfg.hardware.backend == "ultimate":
        # The baked table is the *emulated* UltiSID's curve, so it only
        # applies when an UltiSID core is what the handler's hand-assembled
        # `STA $D418` actually reaches. Handing it to a physical chip is
        # worse than shipping no table at all — a cross-chip table measured
        # ~29% RMS level error (see dac_curves.py), which lands as
        # signal-correlated distortion, not a level trim. This is the mirror
        # of the check dac_calibration_store._select_sid_entry already makes
        # in the other direction.
        socket = active_socket_at_d400(be) if be is not None else None
        if socket is not None:
            log.warning(
                "SID socket %d (a physical chip) answers $D400 and no "
                "calibration for it was found at %s; falling back to the "
                "4-bit linear DAC. Run `c64cast -u <target> --calibrate-dac` "
                "to measure this chip for full-fidelity playback.",
                socket,
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
      system/socket if one exists; else ``mahoney_ultisid`` when an UltiSID
      core answers ``$D400`` (the baked table *is* that core's curve); else
      ``linear`` (a physical/unknown SID with no calibration: the baked
      emulated table would not match it, so stay on the safe 4-bit path).
      Which source owns ``$D400`` is resolved live via
      :func:`active_socket_at_d400`, so a populated socket mapped there gets
      ``linear`` rather than a table measured on a different chip.
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
        # Yield to an explicit digi_boost: both commandeer the SID voices, and
        # a user who set digi_boost meant it. (An explicit non-linear curve +
        # digi_boost is rejected by validate_dac_curve_cfg instead.) Checked
        # before the key is resolved: this path never touches the store, so
        # it must not pay the live round-trip either.
        if cfg.audio.digi_boost:
            return ("linear", None)
        return _resolve_auto_curve(cfg, be, resolve_calibration_key(cfg, be))
    return (name, resolve_dac_curve(name))
