"""Persistence + identity for per-system DAC calibrations: the stable device
key a calibration is filed under, reading the applicable per-socket sidtable
back at playback time (:func:`load_calibrated_table`), and writing a measured
run out (:func:`save_calibration`).

Identity keys (not host/IP)
----------------------------
A calibration file is keyed by a *stable device identity*, not the connection
target, so a DHCP re-lease or a USB replug doesn't orphan it:

* **Ultimate (U64 or U2+)** — the REST ``GET /v1/info`` ``unique_id`` (e.g.
  ``"5D327C"``), fetched live via :meth:`~c64cast.hw.api.Ultimate64API.get_device_info`.
* **TeensyROM, serial transport** — the attached board's USB serial number
  (:func:`c64cast.hw.teensyrom_dma.usb_serial_number`), which identifies the
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

The measurement itself lives in :mod:`c64cast.audio.dac_calibration` (the run) and
:mod:`c64cast.audio.dac_slot_ring` (the DSP); which table playback actually uses is
:mod:`c64cast.audio.dac_curve_resolve`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from c64cast import paths
from c64cast.control.transport import atomic_write_text
from c64cast.sid.asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_SOCKET2_TYPE,
)

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from c64cast.config import Config
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

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
        from c64cast.hw.teensyrom_dma import usb_serial_number

        sn = usb_serial_number(tr.serial_port)
        if sn:
            return f"tr-{_sanitize(sn)}"
    return f"tr-serial-{_sanitize(tr.serial_port or 'auto')}"


def path_for_key(cfg: Config, key: str) -> Path:
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
    return path_for_key(cfg, resolve_calibration_key(cfg, be))


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
            socket = active_socket_at_d400(be)
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


def active_socket_at_d400(be: C64Backend) -> int | None:
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


def load_calibrated_table(
    cfg: Config, *, be: C64Backend | None = None, path: Path | None = None
) -> bytes | None:
    """Return the 256-byte calibrated sidtable applicable to this system right
    now, or None if no (valid/applicable) calibration exists. Malformed files
    and schema mismatches return None rather than raising, so a stale or
    corrupt cache degrades to the baked/linear default.

    ``path`` lets a caller that has already resolved the file (resolving the
    key can cost a live device round-trip on the Ultimate) skip the internal
    resolution; see ``dac_curve_resolve``."""
    if path is None:
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
        # socket map (see active_socket_at_d400) resolved it or chose not to
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
class CalibrationDocument:
    """One measured run's persistable content — everything
    :func:`save_calibration` writes, minus where it lands (``path_for_key``
    derives that from ``cfg`` + ``key``)."""

    key: str
    entries: dict[str, CalibrationResult]  # "1" / "2" / "default" -> result
    device: dict[str, str]  # free-form provenance (REST info / transport endpoint)
    d400_socket: int | None = None


def save_calibration(cfg: Config, doc: CalibrationDocument) -> Path:
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

    path = path_for_key(cfg, doc.key)
    record: dict[str, Any] = {
        "schema": _SCHEMA_VERSION,
        "key": doc.key,
        "backend": cfg.hardware.backend,
        "device": doc.device,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "sids": {name: entry(r) for name, r in doc.entries.items()},
    }
    if doc.d400_socket is not None:
        record["d400_socket"] = doc.d400_socket
    atomic_write_text(path, json.dumps(record, indent=2) + "\n")
    return path
