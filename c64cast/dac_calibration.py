"""Per-system Mahoney 8-bit ``$D418`` DAC calibration: measure the SID transfer
curve for the *actual* SID chip(s) on the connected machine and persist a
per-unit amplitude→``$D418`` "sidtable", so playback can use a table matched
to the real chip instead of the baked emulated-UltiSID one.

Why per-system calibration
--------------------------
The baked ``mahoney_ultisid`` table in :mod:`c64cast.dac_curves` generalizes
perfectly across the U64's *emulated* UltiSID (deterministic, model-knob
irrelevant). But **physical 6581/8580 chips vary enormously** chip-to-chip
(measured: curve correlation 0.74 between two 6581s; one chip's table on the
other → ~29 % RMS level error), dominated by the analog filter — and SID
replacements (ARM2SID/SwinSID/FPGASID) differ again. So a baked table cannot
serve a physical/replacement chip; the only correct path is to measure the
transfer curve of the device in front of you. ``c64cast --calibrate-dac`` does
that (Cam Link / any UVC audio capture on the SID output required).

Identity keys (not host/IP)
----------------------------
A calibration file is keyed by a *stable device identity*, not the connection
target, so a DHCP re-lease or a USB replug doesn't orphan it:

* **Ultimate (U64 or U2+)** — the REST ``GET /v1/info`` ``unique_id`` (e.g.
  ``"5D327C"``), fetched live via :meth:`~c64cast.api.Ultimate64API.get_device_info`.
* **TeensyROM, serial transport** — the attached board's USB serial number
  (:func:`c64cast.teensyrom_dma.usb_serial_number`), which identifies the
  *cartridge*, not whichever host machine it's plugged into.
* **Fallback** (no live backend — e.g. offline ``--doctor --skip-probe`` — or
  the live lookup fails): the pre-existing host/serial-device-path key.

``[audio].dac_calibration_profile`` overrides all of the above with a
user-chosen name. This is the only way to key a calibration correctly when
the connection itself can't identify the physical SID in front of it: a
TeensyROM+ has no config API, and it can be moved between different physical
C64s (or a U64) — its own USB serial number identifies the cartridge, not
whichever machine's SID it happens to be driving right now. A user who moves
a TR+ around names each host's calibration once (``--calibrate-dac
--dac-calibration-profile my-breadbin``) and passes the same name on every
playback run against that host.

The same setting also takes a **path** to a calibration file
(:func:`profile_path_override`), used as given. A name can only address this
backend's own key space, so it cannot express "drive the SID of a machine whose
calibration is already filed under a *different* backend's identity" — which is
exactly what a TR+ in a U64's cartridge port is: one physical SID, already
measured and filed under the Ultimate's ``unique_id``. Naming that file reuses
the measurement instead of repeating it.

Multi-socket U64/U2+ calibration
---------------------------------
A real U64 (Elite I/II, C64U) can carry **two physical SID sockets**, each
potentially holding a different chip. ``run_calibration`` queries the live
config (``sid_hw_config.detect_sockets`` — ``"SID Detected Socket N"``) and,
for every socket reporting a real chip, isolates it to ``$D400`` (the fixed
address the NMI DAC handler's hand-assembled ``STA $D418`` reaches — see
:mod:`c64cast.asid_sidmap`'s "chip 0 must land at $D400" trick, reused here
via ``_isolate_socket``) and measures it independently, restoring the
original SID address/socket config afterward. This is purely config-driven —
there's no U64-vs-U2+ model check — so it naturally measures 0, 1, or 2
sockets depending on what the live config reports (a U2+ with one socket +
one UltiSID core measures just that socket; a bare-UltiSID board measures
nothing and falls back to the single-measurement path below). A board with no
populated sockets, or a backend with no config API at all (TeensyROM), falls
back to one unlabeled measurement of whatever SID currently answers
``$D400``.

The resulting file (schema 2) holds one entry per measured SID, keyed
``"1"``/``"2"`` (socket number) or ``"default"`` (single-measurement
fallback) — see :func:`save_calibration`. At playback time,
``load_calibrated_table`` picks the entry matching whichever socket is
*currently* mapped to ``$D400`` (a live config read), so a calibrated
physical-chip table is never misapplied when ``$D400`` is actually owned by
an UltiSID core.

How a capture is *measured* — the slot ring, the context-dependence rounds,
the volume-0 self-test, and every gate a capture must pass before its numbers
reach the table — lives with the DSP in :mod:`c64cast.dac_slot_ring`; finding
and probing the capture device lives in :mod:`c64cast.dac_capture_device`.
This module owns the run itself: hardware bring-up, per-socket isolation,
capture + retry, and persistence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import numpy as np

from . import paths
from .asid_sidmap import (
    ADDR_UNMAPPED,
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_AUTO_MIRROR,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_SOCKET2_TYPE,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID2_ADDR,
)
from .dac_capture_device import (
    CaptureFormat,
    CaptureUnavailableError,
    capture_fault_message,
    find_capture_device,
    looks_like_capture_input,
    pick_device_hint,
    resolve_capture_format,
)
from .dac_curves import resolve_dac_curve
from .dac_slot_ring import (
    ANCHOR_CODE,
    NMI_RATE,
    RING_ATTEMPTS,
    RING_SPREAD_HEALTHY,
    SELFTEST_TOLERANCE,
    MeasurementError,
    SlotLevels,
    UnsteadyRingError,
    build_sidtable_from_levels,
    build_slot_ring,
    codes_per_ring,
    is_level_drift,
    merge_measurements,
    plan_capture_rounds,
    read_ring_capture,
)
from .sid_hw_config import detect_sockets, restore_sid_config, snapshot_sid_config
from .sid_panning import CAT_MIXER
from .sid_volume import VOL_ITEM, VOL_OFF, VOL_UNITY
from .transport import atomic_write_text

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from .backend import C64Backend
    from .config import Config

log = logging.getLogger(__name__)

# --- persistence ------------------------------------------------------------

# Calibration tables live under the canonical user data dir
# (`paths.calibration_dir()` = <data root>/calibration/dac), resolved at use
# time so the location works from a repo checkout or an installed wheel, not a PyPI
# wheel — and so `$C64CAST_DATA_DIR` (and tests) can redirect it. A calibration
# is machine-specific captured data, not source (never committed; only guarded
# by a .gitignore entry if a dev points $C64CAST_DATA_DIR at the checkout). See
# paths.py and the "per-system calibration" notes in docs/architecture/audio.md.

_SCHEMA_VERSION = 2


def _sanitize(text: str) -> str:
    """Filesystem-safe token: keep alnum/dot/dash, fold everything else to '_'."""
    return "".join(c if (c.isalnum() or c in ".-") else "_" for c in text) or "unknown"


def profile_path_override(cfg: Config) -> Path | None:
    """The file ``[audio].dac_calibration_profile`` points at, when it was given
    as a path rather than a bare name — else None.

    Both spellings are accepted because a name is folded through
    :func:`_sanitize` into one filesystem-safe token, so a path handed to a
    name-only flag came out as ``profile-_Users_me_....json`` and matched
    nothing: the separators looked escaped rather than honored. Naming the file
    directly is also the only way to point one machine's run at a calibration
    that was auto-keyed by a *different* backend — a TeensyROM+ driving the SID
    of a C64 whose own calibration is filed under the Ultimate's ``unique_id``
    is exactly that case, and it can't be expressed as a key at all."""
    value = cfg.audio.dac_calibration_profile
    if not value:
        return None
    separators = [sep for sep in ("/", os.sep, os.altsep) if sep]
    looks_like_path = (
        value.endswith(".json") or value.startswith("~") or any(sep in value for sep in separators)
    )
    return Path(value).expanduser() if looks_like_path else None


def resolve_calibration_key(cfg: Config, be: C64Backend | None = None) -> str:
    """Stable identity key for the connected system's calibration file.

    Resolution order — see the module docstring's "Identity keys" section:

    1. ``[audio].dac_calibration_profile``, if set — used verbatim (sanitized),
       or, when it names a file, that file's stem.
    2. A live device identity, when `be` is a reachable backend: the
       Ultimate's REST ``unique_id``, or a TeensyROM serial device's USB
       serial number.
    3. Fallback — host / serial-device-path, computable from `cfg` alone with
       no hardware access (used when `be` is None, e.g. offline
       ``--doctor --skip-probe``, or the live lookup fails).

    Two runs that resolve to the same key share a calibration file; different
    physical SIDs get different keys."""
    if cfg.audio.dac_calibration_profile:
        override = profile_path_override(cfg)
        if override is not None:
            return override.stem
        name = _sanitize(cfg.audio.dac_calibration_profile)
        # A bare name normally becomes "profile-<name>", which is what a run
        # calibrating *under* that profile writes. But the auto-keyed files a
        # plain --calibrate-dac produces are named for the device
        # ("ultimate-<unique-id>", "tr-<usb-serial>"), and naming one of those —
        # the obvious thing to type, since it is what is on disk — resolved to
        # "profile-ultimate-<unique-id>" and matched nothing. So an existing file
        # named exactly by the given name wins over the prefixed spelling.
        if (
            not (paths.calibration_dir() / f"profile-{name}.json").exists()
            and (paths.calibration_dir() / f"{name}.json").exists()
        ):
            return name
        return f"profile-{name}"

    backend = cfg.hardware.backend
    if backend == "ultimate":
        if be is not None:
            try:
                uid = be.get_device_info().get("unique_id")
            except Exception:  # noqa: BLE001 — best-effort; fall back to host key
                log.debug("dac_calibration: live device-info lookup failed", exc_info=True)
                uid = None
            if uid:
                return f"ultimate-{_sanitize(uid)}"
        host = urlparse(cfg.ultimate64.url).hostname or cfg.ultimate64.url
        return f"ultimate-{_sanitize(host)}"

    # teensyrom
    tr = cfg.teensyrom
    if tr.transport == "tcp":
        return f"tr-tcp-{_sanitize(tr.host or 'unknown')}-{tr.tcp_port}"
    if be is not None and tr.serial_port:
        from .teensyrom_dma import usb_serial_number

        sn = usb_serial_number(tr.serial_port)
        if sn:
            return f"tr-{_sanitize(sn)}"
    return f"tr-serial-{_sanitize(tr.serial_port or 'auto')}"


def _path_for_key(cfg: Config, key: str) -> Path:
    """Where the calibration filed under ``key`` lives — the file
    ``[audio].dac_calibration_profile`` names, when it named a path, else
    ``<calibration dir>/<key>.json``."""
    override = profile_path_override(cfg)
    return override if override is not None else paths.calibration_dir() / f"{key}.json"


def calibration_path(cfg: Config, be: C64Backend | None = None) -> Path:
    # Short-circuits on the override instead of delegating unconditionally,
    # because resolve_calibration_key can cost a live device round-trip that an
    # override makes irrelevant.
    override = profile_path_override(cfg)
    if override is not None:
        return override
    return _path_for_key(cfg, resolve_calibration_key(cfg, be))


def offline_key_is_authoritative(cfg: Config) -> bool:
    """True when ``resolve_calibration_key(cfg)`` (no live backend) already
    returns the same key a live run would use, so an offline check (e.g.
    ``--doctor --skip-probe``) can trust a hit *or* a miss against that key.

    False for the Ultimate and a TeensyROM serial link with no
    ``dac_calibration_profile`` override: both derive their real key from a
    live device identity (``unique_id`` / USB serial number) that's only
    reachable with a connected backend, so the offline fallback key (host /
    serial-device-path) may not match the file a live run would pick — a
    miss against it doesn't mean no calibration applies."""
    if cfg.audio.dac_calibration_profile:
        return True
    return cfg.hardware.backend == "teensyrom" and cfg.teensyrom.transport == "tcp"


def list_calibration_files(backend: str | None = None) -> list[Path]:
    """Calibration files on disk, optionally filtered to those recorded
    (at save time) as belonging to the given ``[hardware].backend``. Used by
    offline diagnostics to note "a calibration exists somewhere, but this
    pass can't confirm it's the one that applies" without needing hardware."""
    cal_dir = paths.calibration_dir()
    if not cal_dir.is_dir():
        return []
    files = sorted(cal_dir.glob("*.json"))
    if backend is None:
        return files
    out = []
    for path in files:
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("backend") == backend:
            out.append(path)
    return out


def _select_sid_entry(
    cfg: Config,
    be: C64Backend | None,
    sids: dict[str, Any],
    recorded_d400: int | None = None,
) -> str | None:
    """Which entry in a loaded calibration's ``sids`` map applies right now.

    ``recorded_d400`` is the socket the *calibrating* run saw answering $D400
    before it isolated anything (the file's ``d400_socket``), which is the only
    evidence available on a link that can't ask the machine itself."""
    has_socket_entries = "1" in sids or "2" in sids
    if has_socket_entries and be is not None:
        if cfg.hardware.backend == "ultimate" and getattr(be.profile, "supports_config", False):
            socket = _active_socket_at_d400(be)
            if socket is None:
                # The file has physical-chip table(s), but $D400 is currently
                # owned by something else (an UltiSID core) — applying a
                # physical-chip table there would be wrong. Let "auto" fall back
                # to the baked mahoney_ultisid table instead.
                return None
            key = str(socket)
            return key if key in sids else None
        # This link has no SID config query, so ownership of $D400 can't be read
        # back. That is "unknown", not the "an UltiSID owns it" the branch above
        # returns None for — treating the two the same discarded a perfectly
        # good multi-socket file (falling all the way back to the 4-bit linear
        # DAC) on exactly the cross-backend reuse dac_calibration_profile exists
        # to support: measure on the Ultimate, replay over a TeensyROM+ in the
        # same machine.
        if recorded_d400 is not None:
            # The file names the chip this machine reaches at $D400. If it holds
            # no table for that chip, then no table in it is the right one —
            # falling through to "the only entry" would apply the other socket's
            # ladder, which is the mismatch this whole selection exists to avoid.
            return str(recorded_d400) if str(recorded_d400) in sids else None
        if len(sids) > 1 and "1" in sids:
            log.warning(
                "audio: this calibration holds tables for %d SID sockets and the %s link "
                "cannot ask which one answers $D400, so socket 1 (the default mapping) is "
                "assumed. If this machine maps socket 2 there instead, the wrong chip's "
                "ladder is being applied — re-run `--calibrate-dac` over a link with a SID "
                "config query to record the mapping in the file.",
                len(sids),
                cfg.hardware.backend,
            )
            return "1"
    if "default" in sids:
        return "default"
    if len(sids) == 1:
        return next(iter(sids))
    return None


def _active_socket_at_d400(be: C64Backend) -> int | None:
    """Which physical SID socket (1 or 2), if any, currently answers $D400 —
    the fixed address the NMI DAC handler's hand-assembled ``STA $D418``
    reaches. None if neither socket owns it (an UltiSID core does, or
    nothing does)."""
    try:
        addressing = be.get_config_category(CAT_ADDRESSING)
        sockets = be.get_config_category(CAT_SOCKETS)
    except Exception:  # noqa: BLE001 — best-effort
        log.debug("dac_calibration: live SID addressing read failed", exc_info=True)
        return None
    for n, addr_item, en_item, type_item in (
        (1, ITEM_SOCKET1_ADDR, ITEM_SOCKET1_EN, ITEM_SOCKET1_TYPE),
        (2, ITEM_SOCKET2_ADDR, ITEM_SOCKET2_EN, ITEM_SOCKET2_TYPE),
    ):
        if (
            addressing.get(addr_item) == "$D400"
            and sockets.get(en_item) == "Enabled"
            and sockets.get(type_item, "None") not in ("None", "")
        ):
            return n
    return None


def load_calibrated_table(cfg: Config, *, be: C64Backend | None = None) -> bytes | None:
    """Return the 256-byte calibrated sidtable applicable to this system right
    now, or None if no (valid/applicable) calibration exists. Malformed files
    and schema mismatches return None rather than raising, so a stale or
    corrupt cache degrades to the baked/linear default."""
    path = calibration_path(cfg, be)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA_VERSION:
        return None
    sids = raw.get("sids")
    if not isinstance(sids, dict) or not sids:
        return None
    recorded = raw.get("d400_socket")
    entry_key = _select_sid_entry(cfg, be, sids, recorded if isinstance(recorded, int) else None)
    if entry_key is None:
        return None
    entry = sids.get(entry_key)
    table = entry.get("sidtable") if isinstance(entry, dict) else None
    if not isinstance(table, list) or len(table) != 256:
        return None
    if (
        entry_key == "default"
        and isinstance(entry, dict)
        and entry.get("detected") is None
        # Only on a link that *cannot* establish the identity. A backend with the
        # socket map (see _active_socket_at_d400) resolved it or chose not to
        # write per-socket entries, either way knowingly; saying this there would
        # fire on every Ultimate run that predates per-socket files.
        and be is not None
        and not getattr(be.profile, "supports_config", False)
    ):
        # A "default" entry means the measurement never established *which* SID
        # it was driving: it measured whatever answers $D400 and filed it under
        # one key. On a single-SID machine that is exactly right. On a machine
        # with a second chip — or with address mirroring on — the ladder is a
        # blend of both, and a blended ladder is signal-correlated distortion at
        # playback. Nothing on this side can tell those two cases apart, so say
        # which one is assumed.
        log.info(
            "audio: this calibration was measured without identifying the SID at $D400 "
            "(the %s link has no SID config query), so it assumes one SID. If this "
            "machine has a second SID or address mirroring, re-measure over a link "
            "that can isolate a socket, or set [audio].dac_curve explicitly.",
            cfg.hardware.backend,
        )
    try:
        return bytes(int(v) & 0xFF for v in table)
    except (TypeError, ValueError):
        return None


def save_calibration(
    cfg: Config,
    key: str,
    entries: dict[str, CalibrationResult],
    device_info: dict[str, str],
    d400_socket: int | None = None,
) -> Path:
    """Persist one or more per-socket sidtables + provenance for this system.

    ``raw_signed_levels`` is written additively under the *same* schema
    version: readers only ever require ``sidtable`` (see
    :func:`load_calibrated_table`), so old files keep loading and new files stay
    readable by older code. A version bump would orphan every calibration on
    disk and force a re-measure for no reader-visible reason. It is a distinct
    key from the ``raw_levels`` older files carry — those hold the two-reference
    ``[code, p, q]`` triples of the retired primitive, which are a different
    measurement, not a different encoding of this one.

    An entry whose measurement failed its self-test is written *without* a
    ``sidtable`` — same reason. ``load_calibrated_table`` already treats a
    missing/malformed table as "no calibration applies" and falls back, so the
    rejection needs no reader change, and keeping its ``raw_levels`` +
    ``metrics`` means the failure can be investigated without re-measuring.

    ``d400_socket`` — which socket answered ``$D400`` *before* the run isolated
    anything — is written the same additive way. Every socket is measured at
    ``$D400`` (that is what isolation does), so the entry keys alone can't say
    which chip a machine reaches there normally; without it, a link that can't
    query SID config has to guess (see :func:`_select_sid_entry`)."""

    def entry(r: CalibrationResult) -> dict[str, Any]:
        out: dict[str, Any] = {"detected": r.detected}
        if r.sidtable is not None:
            out["sidtable"] = [int(v) & 0xFF for v in r.sidtable]
        out["metrics"] = r.metrics
        if r.raw is not None:
            out["raw_signed_levels"] = [[int(c), round(v, 8)] for c, v in r.raw]
        return out

    path = _path_for_key(cfg, key)
    doc = {
        "schema": _SCHEMA_VERSION,
        "key": key,
        "backend": cfg.hardware.backend,
        "device": device_info,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "sids": {name: entry(r) for name, r in entries.items()},
    }
    if d400_socket is not None:
        doc["d400_socket"] = d400_socket
    atomic_write_text(path, json.dumps(doc, indent=2) + "\n")
    return path


# --- playback curve resolution ----------------------------------------------


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
      :func:`_active_socket_at_d400`, so a populated socket mapped there gets
      ``linear`` rather than a table measured on a different chip.
    * ``"calibrated"`` — force the applicable calibrated table; raise if absent.
    * ``"linear"`` / ``"mahoney_ultisid"`` — explicit; passed through.

    `be`, when given a live/reachable backend, lets the resolution pick the
    correct per-socket entry from a multi-SID calibration file (see
    :func:`load_calibrated_table`). Without it (e.g. offline ``--doctor
    --skip-probe``), resolution is best-effort."""
    name = cfg.audio.dac_curve
    if name == "calibrated":
        table = load_calibrated_table(cfg, be=be)
        if table is None:
            raise ValueError(
                "[audio].dac_curve = 'calibrated' but no usable calibration was found "
                f"at {calibration_path(cfg, be)} (key {resolve_calibration_key(cfg, be)}). "
                "Run `c64cast -u <target> --calibrate-dac` first, point "
                "[audio].dac_calibration_profile at an existing calibration file, or "
                "use 'auto'."
            )
        return (f"calibrated:{resolve_calibration_key(cfg, be)}", table)
    if name == "auto":
        # Yield to an explicit digi_boost: both commandeer the SID voices, and
        # a user who set digi_boost meant it. (An explicit non-linear curve +
        # digi_boost is rejected by validate_dac_curve_cfg instead.)
        if cfg.audio.digi_boost:
            return ("linear", None)
        table = load_calibrated_table(cfg, be=be)
        if table is not None:
            return (f"calibrated:{resolve_calibration_key(cfg, be)}", table)
        if cfg.audio.dac_calibration_profile:
            # A profile the user named by hand that resolves to nothing is a
            # typo or a wrong path — not the ordinary "this machine was never
            # calibrated" case the fallbacks below exist for. Name the file that
            # was missed, since the key alone doesn't say where it looked.
            log.warning(
                "[audio].dac_calibration_profile = %r → %s holds no usable "
                "calibration; falling back.",
                cfg.audio.dac_calibration_profile,
                calibration_path(cfg, be),
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
            # of the check _select_sid_entry already makes in the other
            # direction.
            socket = _active_socket_at_d400(be) if be is not None else None
            if socket is not None:
                log.warning(
                    "SID socket %d (a physical chip) answers $D400 and no "
                    "calibration for it was found at %s; falling back to the "
                    "4-bit linear DAC. Run `c64cast -u <target> --calibrate-dac` "
                    "to measure this chip for full-fidelity playback.",
                    socket,
                    resolve_calibration_key(cfg, be),
                )
                return ("linear", None)
            if be is not None:
                log.info(
                    "no per-unit DAC calibration found for %s; using the baked "
                    "mahoney_ultisid table. Run `--calibrate-dac` to measure a "
                    "socketed physical SID.",
                    resolve_calibration_key(cfg, be),
                )
            return ("mahoney_ultisid", resolve_dac_curve("mahoney_ultisid"))
        if be is not None:
            log.warning(
                "no DAC calibration found for %s; falling back to the 4-bit "
                "linear DAC. Run `c64cast -u <target> --calibrate-dac` to "
                "measure this SID for full-fidelity playback.",
                resolve_calibration_key(cfg, be),
            )
        return ("linear", None)
    return (name, resolve_dac_curve(name))


# --- calibration run ---------------------------------------------------------


@dataclass(frozen=True)
class CalibrationResult:
    # 256 entries: amplitude index → $D418 byte. None when the measurement
    # failed its self-test — the raw levels are still kept for diagnosis, but
    # no table is written, so playback falls back to the baked/linear curve.
    sidtable: list[int] | None
    metrics: dict[str, Any]
    detected: str | None = None  # e.g. "6581" (SID Detected Socket N), or None
    # Raw per-code signed output levels, in capture-amplitude units relative to
    # L($00) = 0 — the 256 numbers the ladder is folded from. Persisted so a
    # finished calibration stays diagnosable offline: alternative ladder
    # constructions, the self-test and every metric derive from these, and
    # without them a suspect table can only be re-examined by re-measuring.
    # None on results loaded from a file that predates them.
    raw: list[tuple[int, float]] | None = None


@dataclass(frozen=True)
class CalibrationRun:
    key: str
    path: Path
    entries: dict[str, CalibrationResult]  # "1" / "2" / "default" -> result


def _unsteady_ring_message(reason: str, diag: dict[str, Any], saved: Path | None) -> str:
    """The message an unsteady — as opposed to unreadable — ring fails with.

    Says up front that the rig is right, because the number it is built from
    ("the passes disagree by 1.85%") reads like the same class of fault as a
    mistracked capture and would otherwise send the user back to the cabling.

    Beyond that the two ways a ring can be unsteady get different advice, because
    they have different fixes: :func:`is_level_drift` separates a level that was
    moving *through* a faithful ring (re-measure once it has settled) from laps
    that genuinely differ (something else is reaching the output). A single
    combined list made the reader test all of it, and the first item — a second
    SID — is the expensive one to check."""
    resid = float(diag.get("pass_residual_frac", 0.0))
    spread = float(diag.get("pass_spread_p95_frac", 0.0))
    span = float(diag.get("pass_gain_span_frac", 0.0))
    gains = diag.get("pass_gains") or []

    if is_level_drift(diag):
        detail = (
            f"The disagreement is a level change, not a different ring: rescaling "
            f"each pass leaves only {resid * 100:.2f}%, and the per-pass levels "
            f"themselves span {span * 100:.1f}% ({', '.join(f'{g:.3f}' for g in gains)}). "
            "So the ring replayed faithfully and the level it was measured through "
            "was moving.\nLikely causes, in order:\n"
            "  • the audio path had not settled when the window opened — it "
            "starts quiet and climbs. Let the machine play for a few seconds "
            "before calibrating.\n"
            "  • something is changing gain during the capture: an input with "
            "AGC, or a capture app/OS mixer applying its own level control.\n"
            "  • a thermal or supply-level drift in the analog path, if it "
            "persists across runs at the same magnitude."
        )
    else:
        detail = (
            f"The passes differ in shape, not just level: rescaling each one still "
            f"leaves {resid * 100:.2f}% of the {spread * 100:.2f}%, so the laps are "
            "genuinely playing different levels.\nLikely causes, in order:\n"
            "  • something else is reaching the same audio output. The ring has "
            "to be the only thing making sound, and over a link with no config "
            "API nothing is muted for you: another SID, a sampler channel or a "
            "drive still up in the machine's mixer all add signal the fit reads "
            "as the chip's. Mute them in the machine's own settings first. A "
            "tune or a scene still playing does the same.\n"
            "  • the capture link is dropping or stretching samples — a busy "
            "host, or a hub shared with the video capture.\n"
            "  • the C64 is being driven by something else during the ~50 s "
            "measurement."
        )
    kept = (
        "Nothing is written: playback keeps the baked/linear curve, which is "
        "better than a table fitted to levels that move."
    )
    if saved is not None:
        kept += f"\nThe capture is saved at {saved} — it is what any diagnosis has to start from."
    # The "input is right" line leads both branches: the number this is built from
    # reads like the mistracked-capture fault, and without it either branch sends
    # the reader back to cabling that is already correct.
    return (
        f"the calibration ring is playing and is being recorded, but {reason}.\n"
        "The input is right — what moves is the level it reads back at, and a "
        f"ladder is only worth keeping if it reproduces.\n{detail}\n{kept}"
    )


def _marginal_run_summary(spreads: Sequence[float], label: str) -> tuple[int, str | None]:
    """How many rings measured above the healthy band, with one line naming a
    run that stayed under the trust gate ring by ring and is still not worth
    trusting (None when the run was clean).

    Per ring a marginal spread is a note; across a run it is the finding. A run
    whose rings all sat at 0.2–0.44 % — every one of them under
    :data:`RING_TRUST_MAX_SPREAD` — produced a table that disagreed with the same
    chip measured cleanly by 18 % RMS (corr 0.84), against the 0.12 % / corr
    1.0000 that two clean runs of that chip twelve days apart reproduce at.
    Nothing said so at the time, because no single ring had failed anything."""
    marginal = [s for s in spreads if s > RING_SPREAD_HEALTHY]
    if not marginal:
        return 0, None
    return len(marginal), (
        f"[calib] {label}: {len(marginal)}/{len(spreads)} rings measured above the healthy "
        f"band (≤{RING_SPREAD_HEALTHY * 100:.1f}%, worst {max(spreads) * 100:.2f}%). The "
        "table is still written, but a run like this has produced one that disagreed with "
        "the same chip measured cleanly — re-measure with nothing else driving the audio "
        "output, and over a link that can isolate the socket if you have one."
    )


def _save_unusable_capture(
    cap: np.ndarray, codes: Sequence[int], fmt: CaptureFormat, key: str, diag: dict[str, Any]
) -> Path | None:
    """Write a refused capture to :func:`paths.unusable_capture_dir`, returning
    the path (or None if it couldn't be written — a diagnosis aid must never be
    what turns a measurement failure into a crash).

    Self-contained on purpose: the waveform alone can't be re-extracted, because
    the codes it encodes and the rate it was taken at are what
    :func:`extract_slot_levels` needs to read it back. With those in the same
    file, ``np.load`` plus that one call reproduces the refusal offline."""
    try:
        out = paths.unusable_capture_dir()
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        path = out / f"{key}-{stamp}.npz"
        np.savez_compressed(
            path,
            capture=np.asarray(cap, dtype=np.float32),
            codes=np.asarray(codes, dtype=np.int32),
            samplerate=np.asarray(fmt.samplerate),
            diagnostics=np.asarray(json.dumps(diag)),
        )
        return path
    except Exception:  # noqa: BLE001 — diagnosis aid; never mask the real failure
        log.debug("calib: could not save the unusable capture", exc_info=True)
        return None


def _snapshot_mixer(be: C64Backend) -> dict[tuple[str, str], str]:
    """The Audio Mixer's per-SID-source levels, in ``restore_sid_config`` form.

    A sibling of ``snapshot_sid_config`` rather than part of it: that snapshot
    is the address/socket set multi-SID *planning* round-trips, and widening it
    would make every caller restore mixer state they never touched."""
    try:
        mixer = be.get_config_category(CAT_MIXER)
    except Exception:  # noqa: BLE001 — best-effort; no mixer to restore
        log.debug("calib: mixer read failed", exc_info=True)
        return {}
    return {(CAT_MIXER, item): mixer[item] for item in VOL_ITEM.values() if item in mixer}


def _isolate_mixer(be: C64Backend, source: str) -> None:
    """Route only `source` (``"socket1"``/``"ultisid2"``/…) into the mixer, at
    unity.

    Address routing alone does not make a source audible — the mixer carries an
    independent per-source level, and a board that has only ever used socketed
    chips ships its UltiSID cores at ``OFF``. Measuring through a muted source
    captures the noise floor, which reads as a bring-up or wiring failure rather
    than the routing one it is. Forcing unity rather than preserving a
    deliberate trim is what keeps two sources' ladders comparable."""
    for name, item in VOL_ITEM.items():
        try:
            be.put_config_item(CAT_MIXER, item, VOL_UNITY if name == source else VOL_OFF)
        except Exception:  # noqa: BLE001 — best-effort; a board may lack the item
            log.debug("calib: mixer put %s failed", item, exc_info=True)


def _isolate_socket(be: C64Backend, socket: int) -> None:
    """Route SID Socket `socket` (1 or 2) to $D400 — the fixed address the
    NMI DAC handler's hand-assembled ``STA $D418`` reaches — and silence
    everything else that could also respond there (the other socket, both
    UltiSID cores), so a capture measures only the target chip."""
    other = 2 if socket == 1 else 1
    addr_item = ITEM_SOCKET1_ADDR if socket == 1 else ITEM_SOCKET2_ADDR
    en_item = ITEM_SOCKET1_EN if socket == 1 else ITEM_SOCKET2_EN
    other_en_item = ITEM_SOCKET1_EN if other == 1 else ITEM_SOCKET2_EN
    be.put_config_item(CAT_ADDRESSING, addr_item, "$D400")
    be.put_config_item(CAT_SOCKETS, en_item, "Enabled")
    be.put_config_item(CAT_SOCKETS, other_en_item, "Disabled")
    be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID1_ADDR, ADDR_UNMAPPED)
    be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID2_ADDR, ADDR_UNMAPPED)
    be.put_config_item(CAT_ADDRESSING, ITEM_AUTO_MIRROR, "Disabled")


def run_calibration(
    be: C64Backend,
    cfg: Config,
    *,
    # A ring pass is ring_size/NMI_RATE ≈ 1.03 s; 4.5 s guarantees ≥3 complete
    # passes land inside the window wherever the capture happens to start.
    secs: float = 4.5,
    settle: float = 0.4,
    device: int | None = None,
    log_fn: Callable[[str], None] = print,
) -> CalibrationRun:
    """Measure the connected SID's (or SIDs', on a U64/U2+ with populated
    physical sockets) Mahoney transfer curve and persist a per-system
    calibration file. Leaves the machine silenced + reset. Requires a capture
    device on the SID output (the ``mic`` extra / sounddevice).

    On a backend with a config API (``profile.supports_config`` — Ultimate
    only), every physical SID socket reporting a detected chip
    (``sid_hw_config.detect_sockets``) is measured independently — isolated to
    ``$D400`` via :func:`_isolate_socket`, measured, then every socket's
    original SID address/socket config is restored. A board with no populated
    sockets, or a backend with no config API at all, falls back to a single
    unlabeled measurement of whatever SID currently answers ``$D400``.

    Raises :class:`CaptureUnavailableError` if capture can't be set up.
    """
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001 — optional dep
        raise CaptureUnavailableError(
            "audio capture (sounddevice) is required for --calibrate-dac. Install "
            "the 'mic' extra: uv tool install --force 'c64cast[all]'"
        ) from e

    from .audio import AudioStreamer
    from .audio_handlers import (
        CIA2_CRA_STOP,
        CIA2_ICR_DISABLE_ALL,
        RING_BUFFER_ADDR,
        RING_BUFFER_SIZE,
    )
    from .c64 import CIA2
    from .dsp import DSPParams

    system = cfg.ultimate64.system

    key = resolve_calibration_key(cfg, be)
    supports_config = bool(getattr(be.profile, "supports_config", False))
    device_info: dict[str, str] = {}
    if supports_config:
        try:
            device_info = be.get_device_info()
        except Exception:  # noqa: BLE001 — best-effort provenance only
            log_fn("[calib] could not read device info (product/unique_id)")
    elif cfg.hardware.backend == "teensyrom":
        tr = cfg.teensyrom
        device_info = (
            {"transport": "tcp", "host": tr.host or "", "port": str(tr.tcp_port)}
            if tr.transport == "tcp"
            else {"transport": "serial", "port": tr.serial_port or ""}
        )

    def capture_ring(codes: Sequence[int], dev: int, fmt: CaptureFormat) -> SlotLevels:
        """Record one ring and read its levels, retrying a spoiled capture.

        The ring is written once; only the recording repeats
        (:data:`RING_ATTEMPTS`), so a ring spoiled by a transient costs one
        capture window rather than the run. :func:`read_ring_capture` decides
        what counts as usable; a rig that never produces one fails here with the
        device named, instead of merging noise into the table and falling over
        at whichever later ring happens to be unreadable.
        """
        be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", build_slot_ring(codes, RING_BUFFER_SIZE))
        reason = "no capture was taken"
        peak = 0.0
        unsteady: UnsteadyRingError | None = None
        last: np.ndarray | None = None
        for attempt in range(1, RING_ATTEMPTS + 1):
            time.sleep(settle)
            rec = sd.rec(
                int(secs * fmt.samplerate),
                samplerate=fmt.samplerate,
                channels=fmt.channels,
                device=dev,
                dtype="float32",
            )
            sd.wait()
            # (N, channels) → mono; a 1-channel capture folds to itself.
            mono = rec.mean(axis=1).astype(np.float64)
            last = mono
            peak = float(np.max(np.abs(mono))) if mono.size else 0.0
            try:
                return read_ring_capture(mono, len(codes), RING_BUFFER_SIZE, sr=fmt.samplerate)
            except MeasurementError as e:
                reason = str(e)
                unsteady = e if isinstance(e, UnsteadyRingError) else None
            if attempt < RING_ATTEMPTS:
                log_fn(f"[calib]   unusable capture ({reason}) — retrying")
        # Keep the waveform that was refused. It is the whole evidence for the
        # refusal, and re-creating it costs a fresh hardware run that may not
        # reproduce the fault.
        diag = unsteady.diagnostics if unsteady is not None else {}
        saved = None if last is None else _save_unusable_capture(last, codes, fmt, key, dict(diag))
        if unsteady is not None:
            raise UnsteadyRingError(_unsteady_ring_message(reason, diag, saved), diag)
        raise MeasurementError(capture_fault_message(dev, reason, peak, saved))

    def measure_one(
        dev: int, fmt: CaptureFormat, label: str
    ) -> tuple[list[int] | None, dict[str, Any], list[tuple[int, float]]]:
        rounds = plan_capture_rounds(codes_per_ring(RING_BUFFER_SIZE) - 1)
        total = sum(len(r) for r in rounds)
        log_fn(
            f"[calib] measuring {label}: 256 codes × {len(rounds)} rotations = "
            f"{total} slot rings ({secs:.1f}s each, ~{total * (secs + settle) / 60:.1f} min)…"
        )
        measured: list[tuple[Sequence[int], SlotLevels]] = []
        n = 0
        for rnd, batches in enumerate(rounds, 1):
            for codes in batches:
                n += 1
                got = capture_ring([ANCHOR_CODE, *codes], dev, fmt)
                measured.append((codes, got))
                d = got.diagnostics
                # Marginal spread is called out rather than left as a bare
                # number: a run whose rings all sit just under the gate is the
                # one whose table is quietly poor, and nothing else in the
                # progress output says what "good" looks like.
                # A marginal ring also says which *kind* of marginal it is, so a
                # run that is drifting can be recognized while it is still
                # running rather than from the table it produces.
                marginal = ""
                if d["pass_spread_p95_frac"] > RING_SPREAD_HEALTHY:
                    kind = (
                        f"level drift, span {d['pass_gain_span_frac'] * 100:.1f}%"
                        if is_level_drift(d)
                        else f"ring differs, residual {d['pass_residual_frac'] * 100:.2f}%"
                    )
                    marginal = f" (marginal — healthy is ≤{RING_SPREAD_HEALTHY * 100:.1f}%; {kind})"
                log_fn(
                    f"[calib]   {label} ring {n}/{total} (rotation {rnd}): "
                    f"{d['passes']} passes, L($0F)={got.levels[0]:+.5f}, "
                    f"pass spread {d['pass_spread_p95_frac'] * 100:.3f}% (worst slot {d['pass_spread_frac'] * 100:.2f}%){marginal}"
                )
        spreads = [m.diagnostics["pass_spread_p95_frac"] for _, m in measured]
        n_marginal, note = _marginal_run_summary(spreads, label)
        if note:
            log_fn(note)

        raw, merge_metrics = merge_measurements(measured)
        sidtable, metrics = build_sidtable_from_levels(raw)
        metrics.update(merge_metrics)
        metrics["capture"] = [m.diagnostics for _, m in measured]
        metrics["rings_marginal"] = n_marginal
        if sidtable is None:
            log_fn(
                f"[calib] {label}: REJECTED — the volume-0 self-test is off by "
                f"{metrics['volume0_selftest_worst'] * 100:.1f}% (tolerance "
                f"{SELFTEST_TOLERANCE * 100:.0f}%). Codes $h0 set the master volume "
                "to 0, so they must measure as silence; that they don't means these "
                "are not consistent output levels, and any ladder folded from them "
                f"would be wrong. No table written for {label} — playback keeps the "
                "baked/linear curve, which is better than a bad table. The raw "
                "levels are saved for diagnosis."
            )
        return sidtable, metrics, raw

    sockets_present: list[tuple[int, str]] = []
    saved_sid_config: dict[tuple[str, str], str] = {}
    entries: dict[str, CalibrationResult] = {}
    try:
        # Bring-up: reset once (HDMI renegotiates), running IRQ clear loop, then
        # the NMI handler + neutral ring + the Mahoney SID env (installed by
        # _upload_nmi_and_buffers when the curve is a companding one).
        log_fn("[calib] resetting + bringing up NMI DAC + Mahoney env…")
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            NMI_RATE,
            system,
            dither=False,
            digi_boost=False,
            dac_curve="mahoney_ultisid",
            host_dma_servo=False,
            nmi_rate_adaptive=False,
            dsp_params=DSPParams(enabled=False),
        )
        st.running = True
        st._upload_nmi_and_buffers()
        # The streamer's own arm, so a dropped CIA write is retried here too: a
        # silent NMI is one of the three causes capture_fault_message has to
        # guess between after 50 s of measuring nothing.
        st._start_nmi_timer()

        log_fn("[calib] settling HDMI + (re)initializing capture…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = find_capture_device(device)
        fmt = resolve_capture_format(dev)
        dev_name = str(sd.query_devices(dev)["name"])
        log_fn(
            f"[calib] capture device idx {dev}: {dev_name} "
            f"({fmt.channels} ch @ {fmt.samplerate} Hz)"
        )
        # An auto-pick that matched nothing landed on the system default input,
        # which on a laptop is the built-in microphone — it will record room
        # noise for ~50 s and fail. Say so now, while the run is 5 s old.
        if device is None and not looks_like_capture_input(dev_name):
            log_fn(
                f"[calib] warning: {dev_name!r} doesn't look like a video-capture "
                "input — this is the system default, picked because no capture "
                "device was recognized. If the C64's audio doesn't arrive on it, "
                + pick_device_hint("stop now and pick with")
            )

        if supports_config:
            try:
                s1, s2 = detect_sockets(be)
                if s1 or s2:
                    sockets_info = be.get_config_category(CAT_SOCKETS)
                    if s1:
                        sockets_present.append((1, sockets_info.get(ITEM_SOCKET1_TYPE, "")))
                    if s2:
                        sockets_present.append((2, sockets_info.get(ITEM_SOCKET2_TYPE, "")))
            except Exception:  # noqa: BLE001 — best-effort; fall back to single measurement
                log_fn("[calib] socket detection failed — falling back to a single measurement")

        # Read before the loop: _isolate_socket remaps every socket to $D400 in
        # turn, so asking afterwards answers with c64cast's own edit rather than
        # the mapping this machine actually runs under.
        normal_d400 = _active_socket_at_d400(be) if supports_config else None

        if sockets_present:
            # Mixer levels snapshot once, before the loop: _isolate_mixer
            # rewrites them per socket, so a per-iteration snapshot would
            # capture its own previous edit rather than the user's setting.
            saved_sid_config = {**snapshot_sid_config(be), **_snapshot_mixer(be)}
            try:
                for socket, detected in sockets_present:
                    log_fn(
                        f"[calib] isolating SID socket {socket} "
                        f"({detected or 'detected'}) at $D400…"
                    )
                    _isolate_socket(be, socket)
                    _isolate_mixer(be, f"socket{socket}")
                    # Re-park AFTER the routing change, not once at bring-up.
                    # The env is a series of writes to $D400-$D418, so it lands
                    # on whichever chip owned that window at the time — the
                    # first socket measured. Every socket after it would be
                    # measured with unparked voices, i.e. no DC for the volume
                    # nibble to scale, which reads as a near-silent capture
                    # rather than an obviously wrong one.
                    st._enable_mahoney_env()
                    time.sleep(0.2)
                    sidtable, metrics, raw = measure_one(dev, fmt, f"socket {socket}")
                    entries[str(socket)] = CalibrationResult(
                        sidtable, metrics, detected or None, raw
                    )
            finally:
                restore_sid_config(be, saved_sid_config)
        else:
            sidtable, metrics, raw = measure_one(dev, fmt, "SID")
            entries["default"] = CalibrationResult(sidtable, metrics, None, raw)
    finally:
        try:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.silence_sid()
            be.reset()
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            log_fn(f"[calib] cleanup warning: {e}")

    path = save_calibration(cfg, key, entries, device_info, normal_d400)
    for name, r in entries.items():
        if r.sidtable is None:
            log_fn(
                f"[calib] {name}: no table — self-test off by "
                f"{r.metrics['volume0_selftest_worst'] * 100:.1f}%"
            )
            continue
        log_fn(
            f"[calib] {name}: ~{r.metrics['ladder_bits']} ladder bits, span "
            f"{r.metrics['signed_span']}, worst gap {r.metrics['worst_gap_frac'] * 100:.1f}% "
            f"of span at {r.metrics['worst_gap_from_zero_frac']:+.2f} from silence"
        )
    log_fn(f"[calib] wrote {path}")
    return CalibrationRun(key=key, path=path, entries=entries)
