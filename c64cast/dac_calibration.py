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

Measurement method: one slot ring, signed levels read directly
---------------------------------------------------------------
The SID → capture path is AC-coupled (~8.5 Hz measured), so a static code
produces no steady signal and a level can only be read as a *change*. The ring
therefore holds ``SLOT_SAMPLES``-long slots alternating ``[code][ref]``, with
``ref = $00`` — master volume 0, i.e. silence — behind a leading run of
``SYNC_SLOTS`` reference slots that marks where a pass begins. See
:func:`build_slot_ring`.

Every code is then measured against the *same* baseline inside one capture, so
its signed level comes straight off the waveform and no sign has to be inferred.
:func:`extract_slot_levels` locates the pass boundaries, tracks the slot grid
edge by edge, undoes the AC coupling, and differences each code slot against the
reference slots bracketing it. A ring holds 112 codes, so 256 codes take 3 rings
of ~5 s rather than 512 separate captures.

**This replaced a two-reference scheme** that toggled each code against ``$00``
*and* ``$0F`` at 500 Hz and took the FFT amplitude as ``|L(code) − L(ref)|``,
inferring each sign from which of ``p + q`` or ``q − p`` came closer to ``lmax``.
That primitive did not return consistent levels: on a 6581 whose filter path is
alive it missed the volume-0 ground truth below by 52 %, and 89 of its 256 codes
violated the triangle inequality ``p + q ≥ lmax`` by up to 51 % of ``lmax`` — so
no 1-D embedding of those numbers existed and the sign inference was not
ill-conditioned but unfounded. It was exactly reproducible (Pearson +0.9992
against a curve measured three weeks earlier), independent of toggle frequency
from 500 Hz down to 31.25 Hz, and reproduced within a single capture, ruling out
noise, drift, capture gain, clipping and stereo folding. Reading levels directly
sidesteps the whole construction; the same chip now passes the ground truth at
1.3 %.

Stereo capture is folded to mono by averaging, so the SID pan setting only
scales all measurements uniformly and cancels in the normalized ladder — no
mixer changes needed.

Context dependence, and why every code is measured three times
---------------------------------------------------------------
A 6581's output for a ``$D418`` byte is not quite a function of that byte alone.
Measured on a socketed 6581 by planting one probe code at twelve positions in an
otherwise ordinary ring: a positive code reads **20 % lower** at the end of a
ring pass than at its start, a negative code 2 % *higher*, and the apparent level
correlates at |r| ≈ 0.9 with the mean level of the surrounding slots. It is in
the raw waveform, before any processing, so it is the chip's operating point
sliding with the accumulated signal, not a measurement artifact.

Measure each code at one fixed slot and that bias is baked into the ladder,
ordered by code, looking exactly like curve structure — the tell is that the
volume ramp within a nibble band stops being monotone. So the whole code set is
measured :data:`MEASURE_ROUNDS` times, each round rotating every ring's slot
order by another fraction of a ring, and the readings are averaged: every code
then carries the same mean context, which is a common scale factor, and the
ladder is scale-invariant. Three rounds lands within one ladder step of a
six-round reference (max 0.9 % of span, rms 0.2 %); one round is off by 5.2 %
and leaves six non-monotone codes. ``context_spread_frac`` records how far a
code moved between rounds — the honest bound on how well *any* static table can
describe this chip.

The volume-0 self-test (why a calibration can be rejected)
----------------------------------------------------------
The 16 codes ``$h0`` set the master volume nibble to 0, so their output level is
``$00``'s *whatever* the upper nibble does: ``L($h0)`` must measure zero, for
every ``h``, with no model assumptions at all.
:func:`build_sidtable_from_levels` checks that and refuses to emit a ladder when
the worst one exceeds :data:`SELFTEST_TOLERANCE`; ``run_calibration`` then
persists ``raw_signed_levels`` + ``metrics`` for the socket but no ``sidtable``,
so playback degrades to the baked/linear curve instead of to a wrong table. That
matters because the failure is otherwise silent — a badly reconstructed ladder
looks exactly like a good one and plays back worse than no calibration at all.

On the socketed 6581 the residual is ~1 %, and it is not noise: it tracks the
filter routing bits (LP set → ≈ 1 % of full scale, no filter → ≈ 0.1 %) and does
not move when the plateau read window is widened from 8 to 72 samples, so it is
the chip's filter path leaking a little DC past a volume DAC set to zero rather
than anything the measurement is doing wrong.
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
from typing import TYPE_CHECKING, Any, NamedTuple
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
from .dac_curves import resolve_dac_curve
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
        return f"profile-{_sanitize(cfg.audio.dac_calibration_profile)}"

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


def calibration_path(cfg: Config, be: C64Backend | None = None) -> Path:
    override = profile_path_override(cfg)
    if override is not None:
        return override
    return paths.calibration_dir() / f"{resolve_calibration_key(cfg, be)}.json"


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


def _select_sid_entry(cfg: Config, be: C64Backend | None, sids: dict[str, Any]) -> str | None:
    """Which entry in a loaded calibration's ``sids`` map applies right now."""
    has_socket_entries = "1" in sids or "2" in sids
    if (
        has_socket_entries
        and be is not None
        and cfg.hardware.backend == "ultimate"
        and getattr(be.profile, "supports_config", False)
    ):
        socket = _active_socket_at_d400(be)
        if socket is None:
            # The file has physical-chip table(s), but $D400 is currently
            # owned by something else (an UltiSID core) — applying a
            # physical-chip table there would be wrong. Let "auto" fall back
            # to the baked mahoney_ultisid table instead.
            return None
        key = str(socket)
        return key if key in sids else None
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
    entry_key = _select_sid_entry(cfg, be, sids)
    if entry_key is None:
        return None
    entry = sids.get(entry_key)
    table = entry.get("sidtable") if isinstance(entry, dict) else None
    if not isinstance(table, list) or len(table) != 256:
        return None
    try:
        return bytes(int(v) & 0xFF for v in table)
    except (TypeError, ValueError):
        return None


def save_calibration(
    cfg: Config,
    key: str,
    entries: dict[str, CalibrationResult],
    device_info: dict[str, str],
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
    ``metrics`` means the failure can be investigated without re-measuring."""

    def entry(r: CalibrationResult) -> dict[str, Any]:
        out: dict[str, Any] = {"detected": r.detected}
        if r.sidtable is not None:
            out["sidtable"] = [int(v) & 0xFF for v in r.sidtable]
        out["metrics"] = r.metrics
        if r.raw is not None:
            out["raw_signed_levels"] = [[int(c), round(v, 8)] for c, v in r.raw]
        return out

    override = profile_path_override(cfg)
    path = override if override is not None else paths.calibration_dir() / f"{key}.json"
    doc = {
        "schema": _SCHEMA_VERSION,
        "key": key,
        "backend": cfg.hardware.backend,
        "device": device_info,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "sids": {name: entry(r) for name, r in entries.items()},
    }
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


# --- measurement core -------------------------------------------------------

NMI_RATE = 8000  # consumer rate; well under the ~14 kHz NMI DAC handler ceiling
CAP_SR = 48000  # preferred capture rate (what a Cam Link presents)
#: Rates to fall back to, in order, when a capture device won't do `CAP_SR`.
#: The cheap MacroSilicon-based HDMI→USB dongles are frequently 96 kHz-only,
#: and some UVC inputs only offer 44.1 kHz. Every consumer of a capture takes
#: its rate as a parameter, so any of these measures correctly.
CAP_SR_FALLBACKS = (96000, 44100, 32000)
REF_ZERO = 0x00  # master-volume-0 floor: the common baseline every level is vs.
ANCHOR_CODE = 0x0F  # positive full scale; first pair of every ring (see below)

#: NMI samples per ring slot. One slot must be long enough that its plateau is
#: many capture samples wide (32 NMI samples ≈ 4.0 ms ≈ 192 capture samples at
#: 48 kHz) and short enough that a useful number of codes fit one 8 KB ring.
SLOT_SAMPLES = 32

#: Leading run of ``REF_ZERO`` slots that marks the start of a ring pass. The
#: SID output is constant across it, so the capture shows no level steps for
#: ≈128 ms — the one feature in the waveform that cannot be confused with a
#: code boundary. See :func:`_find_ring_anchors`.
SYNC_SLOTS = 32

#: How far the volume-0 self-test may miss before a measurement is rejected, as
#: a fraction of ``lmax``. Codes ``$h0`` set the master volume nibble to 0, so
#: their output level *is* ``$00``'s whatever the upper nibble does: ``L($h0)``
#: must measure 0, for every ``h``, with no model assumptions at all. Any
#: deviation is measurement error against a known answer, or chip leakage.
#: A sound measurement lands around 1 % (a socketed 6581, where the residual
#: tracks the filter routing bits — see the module docstring), and the primitive
#: this replaced missed by 52 %, so 10 % separates the two with room to spare.
SELFTEST_TOLERANCE = 0.10

#: Peak capture amplitude (of float32 full scale) below which the input simply
#: isn't carrying the SID. A ring drives the SID between full-scale codes and
#: silence, so *any* correctly routed capture sees far more than this; below it,
#: re-recording is pointless and only the rig can be at fault.
SILENT_CAPTURE_PEAK = 0.002

#: How far ``pass_spread_frac`` may go before a captured ring is refused rather
#: than merged. Every pass of one capture measures the same levels, so on
#: hardware the spread is 0.01–0.2 % — two orders of magnitude below this gate.
#: A capture of something that isn't the ring (a laptop microphone picking up
#: room noise is the one seen in the field) still yields *numbers*: the peak
#: finder locks onto noise, a couple of "sync markers" turn up, and the levels
#: come back near zero with the passes disagreeing by ~100 %. Ungated, those
#: numbers merged into the table and the run only fell over later — at whichever
#: ring happened to find fewer than two markers, with a traceback and 30 s of
#: measuring already spent. Gating each ring on the metric that already exists
#: to answer this question turns that into an immediate, actionable failure.
RING_TRUST_MAX_SPREAD = 0.10

#: Captures per ring before giving up. A retry costs one settle + one capture
#: window (~5 s) and rescues a ring spoiled by a transient — a USB hiccup, a
#: host stall long enough to break the grid tracking — without letting a
#: genuinely wrong rig grind through the whole 9-ring run.
RING_ATTEMPTS = 2


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


class MeasurementError(RuntimeError):
    """The capture doesn't contain a readable slot ring — no signal, too short,
    or the NMI isn't running. Distinct from a measurement that read fine and
    then failed its self-test, which is a chip/primitive result, not a rig fault."""


def codes_per_ring(ring_size: int) -> int:
    """How many codes one ring can carry, *including* the leading anchor pair."""
    return (ring_size // SLOT_SAMPLES - SYNC_SLOTS) // 2


def plan_code_batches(per_ring: int, total: int = 256) -> list[list[int]]:
    """Split the codes across the fewest rings that hold them, striding rather
    than slicing: ring ``j`` gets ``j, j+n, j+2n, …``.

    Consecutive ring slots then hold codes whose *volume nibble* differs by the
    stride, so a chip that is silent over some contiguous run of codes can never
    put a long silent run into consecutive slots — which is what would fake a
    sync gap for :func:`_find_ring_anchors`. Slicing 0-110 / 111-221 / 222-255
    puts sixteen same-upper-nibble codes side by side and does exactly that."""
    n = max(1, -(-total // per_ring))
    return [list(range(j, total, n)) for j in range(n)]


#: How many times the whole code set is measured, each round rotating every
#: ring's slot order by a further ``1/MEASURE_ROUNDS`` of a ring.
#:
#: A 6581's output for a given ``$D418`` byte is not quite a function of that
#: byte alone: it drifts with the *accumulated* signal since the last quiet
#: stretch, as the parked voices' operating point slides. Measured on a socketed
#: 6581 (see docs/architecture/audio.md): a positive code reads 20 % lower at the
#: end of a ring pass than at the start, a negative code 2 % higher, and the
#: apparent level correlates at |r| ≈ 0.9 with the mean level of the surrounding
#: slots. Measure every code at one fixed position and that bias is baked into
#: the ladder, ordered by code, looking exactly like curve structure.
#:
#: Rotating the slot order and averaging gives every code the same *mean*
#: context, so the bias becomes a common scale factor — and the ladder is built
#: from a linspace over the measured span, so a common scale factor cancels
#: completely. The spread across rounds is kept as ``context_spread_frac``.
MEASURE_ROUNDS = 3


def plan_capture_rounds(
    per_ring: int, total: int = 256, rounds: int = MEASURE_ROUNDS
) -> list[list[list[int]]]:
    """``rounds`` × rings × codes. Each round rotates every ring's slot order by
    another ``1/rounds`` of the ring, so each code visits ``rounds`` evenly
    spaced positions across the runs. See :data:`MEASURE_ROUNDS`."""
    base = plan_code_batches(per_ring, total)
    out = []
    for r in range(rounds):
        out.append([b[(r * len(b)) // rounds :] + b[: (r * len(b)) // rounds] for b in base])
    return out


def build_slot_ring(codes: Sequence[int], ring_size: int, *, ref: int = REF_ZERO) -> bytes:
    """Ring holding ``SYNC_SLOTS`` reference slots followed by one
    ``[code][ref]`` slot pair per entry of `codes`, padded out with `ref`.

    Slot ``s`` occupies ring bytes ``[s·SLOT_SAMPLES, (s+1)·SLOT_SAMPLES)``, so
    every code is bracketed by the *same* reference level on both sides. Bytes
    are FULL 8-bit ``$D418`` values; the pattern tiles the ring exactly, so the
    NMI handler loops it with no wrap glitch."""
    slots = ring_size // SLOT_SAMPLES
    if len(codes) > codes_per_ring(ring_size):
        raise ValueError(f"{len(codes)} codes exceed the {codes_per_ring(ring_size)} a ring holds")
    seq = [ref & 0xFF] * SYNC_SLOTS
    for c in codes:
        seq += [int(c) & 0xFF, ref & 0xFF]
    seq += [ref & 0xFF] * (slots - len(seq))
    return np.repeat(np.asarray(seq, dtype=np.uint8), SLOT_SAMPLES).tobytes()


def _boxcar_step(x: np.ndarray, half: int) -> np.ndarray:
    """``s[n] = mean(x[n:n+half]) − mean(x[n−half:n])`` — a matched filter for a
    level step. ``|s|`` has a sharp triangular peak centered exactly on each
    boundary and is flat-ish elsewhere, which is what the alignment keys off."""
    n = x.size
    c = np.concatenate(([0.0], np.cumsum(x)))
    idx = np.arange(n)
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half, 0, n)
    before = (c[idx] - c[lo]) / np.maximum(idx - lo, 1)
    after = (c[hi] - c[idx]) / np.maximum(hi - idx, 1)
    return after - before


def _peak_positions(mag: np.ndarray, min_sep: float, thresh: float) -> np.ndarray:
    """Positions of well-separated local maxima of `mag` above `thresh`, with
    a parabolic sub-sample refinement. Greedy non-maximum suppression: take the
    largest remaining peak, blank ``±min_sep`` around it, repeat."""
    cand = np.flatnonzero(mag > thresh)
    if cand.size == 0:
        return np.zeros(0)
    order = cand[np.argsort(mag[cand])[::-1]]
    taken = np.zeros(mag.size, dtype=bool)
    sep = max(1, int(min_sep))
    out: list[float] = []
    for i in order:
        if taken[i]:
            continue
        taken[max(0, i - sep) : i + sep + 1] = True
        pos = float(i)
        if 0 < i < mag.size - 1:
            a, b, c = mag[i - 1], mag[i], mag[i + 1]
            denom = a - 2 * b + c
            if denom < 0:
                pos += float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
        out.append(pos)
    return np.sort(np.asarray(out))


def _find_ring_anchors(peaks: np.ndarray, slot_p: float) -> np.ndarray:
    """Ring-pass start positions: the edges that end a sync gap. The gap is
    ``SYNC_SLOTS`` constant-reference slots, so it is the longest stretch of the
    waveform with no level step in it; the edge that ends it is the
    ``ref → ANCHOR_CODE`` transition into slot ``SYNC_SLOTS``, guaranteed full
    scale and therefore never missed by the peak finder.

    A run of codes that all happen to sit at the reference level also leaves no
    edges, and on a chip whose Mahoney path has partly died that run can be
    long. So the test is *relative*: a candidate must be within 25 % of the
    longest gap seen, not merely long. Mistaking such a run for the marker
    would offset every code by a fixed number of slots and still repeat
    identically each pass — a wrong answer that looks perfectly stable."""
    if peaks.size < 2:
        return np.zeros(0)
    gaps = np.diff(peaks)
    floor = (SYNC_SLOTS - 8) * slot_p
    if gaps.max() < floor:
        return np.zeros(0)
    return peaks[1:][gaps >= max(floor, 0.75 * float(gaps.max()))]


def _fit_period(anchors: np.ndarray, nominal: float) -> tuple[float, float, float]:
    """Least-squares ``anchor_k = t0 + k·period`` over the observed pass starts,
    with ``k`` recovered from the nominal period. Returns (t0, period, rms)."""
    k = np.round((anchors - anchors[0]) / nominal)
    fit = np.polyfit(k, anchors, 1) if k.size > 1 else np.array([nominal, anchors[0]])
    resid = anchors - np.polyval(fit, k)
    return float(fit[1]), float(fit[0]), float(np.sqrt(np.mean(resid**2)))


def _dc_restore_gain(x: np.ndarray, c: np.ndarray, windows: np.ndarray) -> float:
    """The gain ``k`` that best undoes the capture path's AC coupling.

    The SID → capture path is high-passed (measured ≈8.5 Hz, τ ≈ 19 ms), so a
    plateau that should be flat sags visibly across one 4 ms slot and the naive
    "level = plateau mean" is biased by whatever the previous slots did. For a
    one-pole high-pass ``y = v − b`` with ``ḃ = y/τ``, the inverse is exactly
    ``v = y + cumsum(y)/(τ·fs)`` — one unknown scalar.

    Fit it from the data instead of trusting a nominal τ: the restored signal
    is affine in ``k``, so the total within-plateau variance is a quadratic in
    ``k`` with a closed-form minimum. `c` is ``cumsum(x)``; `windows` is an
    (n, w) index array of plateau interiors, which *should* be flat."""
    xw = x[windows]
    cw = c[windows]
    xw = xw - xw.mean(axis=1, keepdims=True)
    cw = cw - cw.mean(axis=1, keepdims=True)
    denom = float(np.sum(cw * cw))
    if denom <= 0:
        return 0.0
    return -float(np.sum(xw * cw)) / denom


@dataclass(frozen=True)
class SlotLevels:
    """Signed per-code output levels recovered from one slot-ring capture."""

    levels: np.ndarray  # (n_codes,) mean across ring passes, ref level = 0
    per_pass: np.ndarray  # (n_passes, n_codes) — spread here is the trust metric
    diagnostics: dict[str, Any]


def extract_slot_levels(
    cap: np.ndarray,
    n_codes: int,
    ring_size: int,
    *,
    sr: int = CAP_SR,
    nmi_rate: float = NMI_RATE,
    guard: int = 24,
) -> SlotLevels:
    """Recover each code's *signed* output level, relative to the reference
    slots, from a capture of the ring :func:`build_slot_ring` built.

    Every code shares one baseline inside one capture, so a level is read
    directly off the waveform and its sign needs no inference — that is the
    whole point of the slot ring. The steps are:

    1. **Locate the boundaries.** ``|_boxcar_step|`` peaks on every level step;
       the peaks that follow a sync gap start a ring pass (:func:`_find_ring_anchors`).
    2. **Track the grid.** The pass period comes from a least-squares fit across
       the observed pass starts, and each pass then follows its own edges
       (:func:`_track_slot_grid`) rather than stepping a nominal pitch — see
       that function for why open-loop indexing sank two earlier attempts.
    3. **Undo the AC coupling** (:func:`_dc_restore_gain`) so a plateau mean is
       a level rather than a level plus the sag of whatever preceded it.
    4. **Difference against the neighbors.** Each code slot is bracketed by
       reference slots, so ``level = mean(code) − mean(both neighbors)/2``
       cancels any residual slow drift locally.
    """
    x = np.asarray(cap, dtype=np.float64)
    x = x - x.mean()
    ring_slots = ring_size // SLOT_SAMPLES
    slot_p = SLOT_SAMPLES * sr / float(nmi_rate)
    ring_p = ring_slots * slot_p

    step = _boxcar_step(x, max(2, int(round(0.4 * slot_p))))
    mag = np.abs(step)
    thresh = 0.15 * float(np.percentile(mag, 99.5))
    peaks = _peak_positions(mag, 0.5 * slot_p, thresh)
    anchors = _find_ring_anchors(peaks, slot_p)
    if anchors.size < 2:
        raise MeasurementError(
            f"found {anchors.size} ring sync marker(s) in the capture, need ≥2 — "
            "it holds no readable ring pass"
        )
    _, ring_p, anchor_rms = _fit_period(anchors, ring_p)
    slot_p = ring_p / ring_slots

    # Code i occupies slot SYNC_SLOTS + 2i, bracketed by reference slots.
    code_slots = SYNC_SLOTS + 2 * np.arange(n_codes)

    cum = np.cumsum(x)
    per_pass: list[np.ndarray] = []
    pitches: list[float] = []
    dc_gains: list[float] = []
    for a in anchors:
        starts, pitch = _track_slot_grid(peaks, a, slot_p, ring_slots, n_codes)
        w = int(pitch) - 2 * guard
        if w < 8:
            continue
        first = int(np.floor(starts[0])) + guard
        last = int(np.ceil(starts[ring_slots - 1])) + guard + w
        if first < 0 or last >= x.size:
            continue
        idx = np.round(starts[:ring_slots, None] + guard).astype(int) + np.arange(w)
        k = _dc_restore_gain(x, cum, idx)
        dc_gains.append(k)
        pitches.append(pitch)
        means = (x[idx] + cum[idx] * k).mean(axis=1)
        ref_mean = 0.5 * (means[code_slots - 1] + means[code_slots + 1])
        per_pass.append(means[code_slots] - ref_mean)
    if not per_pass:
        raise MeasurementError("no complete ring pass fell inside the capture window")

    passes = np.vstack(per_pass)
    levels = passes.mean(axis=0)
    scale_ref = float(np.max(np.abs(levels))) or 1.0
    tracked_slot_p = float(np.mean(pitches))
    diagnostics = {
        "passes": int(passes.shape[0]),
        "ring_period_samples": round(ring_p, 3),
        "slot_period_samples": round(tracked_slot_p, 4),
        "nmi_rate_implied_hz": round(SLOT_SAMPLES * sr / tracked_slot_p, 2),
        "anchor_fit_rms_samples": round(anchor_rms, 2),
        # The trust metric. Every pass measures the same 256 levels, so a large
        # spread means the grid is not landing on the same thing twice — the one
        # symptom that distinguishes a mistracked capture from a real curve.
        "pass_spread_frac": round(float(np.max(passes.std(axis=0))) / scale_ref, 5),
        # The fitted AC-coupling corner, as a sanity check on the capture rig:
        # k = 1/(τ·fs), so f_c = k·fs/2π. Expect a few Hz to a few tens of Hz.
        "ac_coupling_hz": round(float(np.mean(dc_gains)) * sr / (2 * np.pi), 2),
    }
    return SlotLevels(levels=levels, per_pass=passes, diagnostics=diagnostics)


def read_ring_capture(
    cap: np.ndarray, n_codes: int, ring_size: int, *, sr: int = CAP_SR
) -> SlotLevels:
    """:func:`extract_slot_levels` behind the two gates that decide whether the
    capture is of the ring *at all*, rather than of something else entirely.

    The extraction is a reader: handed a waveform it reports what it found, and
    it can only refuse what it cannot parse. But a recording of the wrong input
    parses fine — the peak finder locks onto noise, a sync gap or two turns up,
    and levels come back near zero with the passes contradicting each other. So
    the judgment about whether a *recording* is usable lives here, where it can
    be applied to every ring before its numbers reach the table.

    Raises :class:`MeasurementError` whose message is a phrase, for
    :func:`_capture_fault_message` to finish with the device and the advice.
    """
    peak = float(np.max(np.abs(cap))) if cap.size else 0.0
    if peak < SILENT_CAPTURE_PEAK:
        raise MeasurementError("it recorded silence")
    got = extract_slot_levels(cap, n_codes, ring_size, sr=sr)
    spread = float(got.diagnostics["pass_spread_frac"])
    if spread > RING_TRUST_MAX_SPREAD:
        raise MeasurementError(
            f"its ring passes disagree by {spread * 100:.1f}%, where hardware "
            "reads 0.01–0.2% — the levels in it are noise"
        )
    return got


#: Alpha-beta gains for :func:`_track_slot_grid`. Alpha smooths the ±½-sample
#: jitter in a single edge position; beta learns a *rate* of drift, which is
#: what keeps the tracker from lagging when the capture timebase is stretched
#: (avfoundation dropping samples under load). A pure position tracker at these
#: gains lags a 12 %-drop stretch by ~15 samples; with the rate term it doesn't.
_TRACK_ALPHA = 0.5
_TRACK_BETA = 0.1

#: How far from its prediction an edge may be found before the tracker calls it
#: a miss and coasts. Wide enough to acquire a badly stretched timebase, narrow
#: enough that it can never latch onto the *neighboring* boundary.
_TRACK_CAPTURE_FRAC = 0.35


def _track_slot_grid(
    peaks: np.ndarray, anchor: float, slot_p: float, ring_slots: int, n_codes: int
) -> tuple[np.ndarray, float]:
    """Start position of every slot in one ring pass, tracked edge by edge.

    This is the part two earlier attempts got wrong, so it is worth being
    explicit about why open-loop indexing cannot work. A slot is 192.24 capture
    samples — not an integer, and not even a fixed number, because the capture
    clock and the C64's NMI clock are independent and avfoundation drops
    samples under load. Stepping a nominal 192 samples per slot from the sync
    marker walks the read window off the boundary and into the middle of a
    sagging plateau within a fraction of a pass, which produces levels that are
    stable across repeats (so they look trustworthy) and wrong.

    So the grid follows the signal instead of predicting it: each boundary is
    matched to the nearest detected edge, and an alpha-beta filter folds that
    into a smoothed offset *and* a drift rate. Boundaries with no detectable
    edge — a code whose level happens to equal the reference, and the whole
    sync gap — coast on the current rate. Returns the slot starts plus the
    median tracked slot length."""
    starts = np.empty(ring_slots + 1)
    last_edge_slot = SYNC_SLOTS + 2 * n_codes
    off = 0.0
    rate = 0.0
    for s in range(SYNC_SLOTS, ring_slots + 1):
        pred = anchor + (s - SYNC_SLOTS) * slot_p + off
        if s < last_edge_slot and peaks.size:
            j = int(np.searchsorted(peaks, pred))
            near = peaks[max(0, j - 1) : j + 1]
            if near.size:
                d = min((p - pred for p in near), key=abs)
                if abs(d) < _TRACK_CAPTURE_FRAC * slot_p:
                    off += _TRACK_ALPHA * d
                    rate += _TRACK_BETA * d
                    pred += _TRACK_ALPHA * d
        off += rate
        starts[s] = pred
    # The sync gap carries no edges to track, so extrapolate backwards from the
    # anchor at the nominal pitch. Only the single ref slot at SYNC_SLOTS-1 is
    # ever read (it brackets the first code), one slot from the anchor.
    for s in range(SYNC_SLOTS - 1, -1, -1):
        starts[s] = anchor - (SYNC_SLOTS - s) * slot_p
    return starts, float(np.median(np.diff(starts[SYNC_SLOTS:])))


def merge_measurements(
    measured: Sequence[tuple[Sequence[int], SlotLevels]],
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    """Fold every captured ring into one 256-entry signed level table.

    Two corrections, in order:

    * **Anchor rescale.** Every ring leads with :data:`ANCHOR_CODE` at the same
      slot, so each capture carries its own full-scale reading. Capture gain is
      stable *within* a capture but not guaranteed across them, so each ring is
      rescaled onto the mean anchor. That is what lets several rings stand in
      for the one 256-code ring that does not fit.
    * **Round average.** Each code is measured once per rotation
      (:func:`plan_capture_rounds`); averaging equalises the context bias
      described at :data:`MEASURE_ROUNDS`.

    ``context_spread_frac`` — how far a code's readings move between rotations,
    as a fraction of full scale — is the honest measure of how well a single
    static table can describe this chip at all."""
    anchors = np.array([m.levels[0] for _, m in measured])
    gain = float(np.mean(anchors))
    seen: dict[int, list[float]] = {}
    for codes, got in measured:
        k = gain / float(got.levels[0]) if got.levels[0] else 1.0
        for c, v in zip(codes, got.levels[1:], strict=True):
            seen.setdefault(int(c), []).append(float(v) * k)
    raw = [(c, float(np.mean(seen[c]))) for c in sorted(seen)]
    spreads = [float(np.ptp(v)) for v in seen.values() if len(v) > 1]
    metrics = {
        "rings": len(measured),
        "anchor_spread_frac": round(
            float(np.max(np.abs(anchors / gain - 1.0))) if gain else 0.0, 5
        ),
        "context_spread_frac": round(max(spreads) / abs(gain), 4) if spreads and gain else 0.0,
        "context_spread_median_frac": (
            round(float(np.median(spreads)) / abs(gain), 4) if spreads and gain else 0.0
        ),
    }
    return raw, metrics


def build_sidtable_from_levels(
    raw: Sequence[tuple[int, float]],
) -> tuple[list[int] | None, dict[str, Any]]:
    """Fold 256 measured signed output levels into the amplitude→code sidtable
    and its quality metrics.

    The levels arrive signed and on one common baseline (``L($00) = 0``) — the
    slot ring reads them straight off the waveform — so there is nothing to
    reconstruct here: the table maps 256 uniform target levels across the
    measured span to the code whose level is nearest.

    The targets span ``[min, max]`` rather than being centered on silence, and
    that is deliberate. What the encoder needs is *uniformity* — index ``128+k``
    must sit ``k`` equal steps above index 128 — not that index 128 be silent:
    the SID output is AC-coupled, so a constant offset is removed downstream and
    only the step size reaches the listener. Measured Mahoney spans are markedly
    asymmetric (socket 1: −0.656 to +0.461), and re-centring on zero would throw
    away the excess negative swing for nothing. It also has to work for a chip
    whose span is entirely one-sided — the degraded 6581 in socket 2 measures
    −0.001 to +0.287, where the largest symmetric swing is 0.001 and a
    zero-centered ladder collapses to noise.

    Returns ``(None, metrics)`` when the volume-0 self-test misses by more than
    :data:`SELFTEST_TOLERANCE`. Codes ``$h0`` set the master volume nibble to 0,
    so ``L($h0)`` must be 0 whatever the upper nibble does; a measurement that
    says otherwise is wrong about levels in general, and a ladder folded from it
    would look exactly like a good one while playing back worse than no
    calibration at all. Metrics are returned either way, so a rejected
    measurement stays fully diagnosable."""
    code = np.array([c for c, _ in raw])
    level = np.array([v for _, v in raw], dtype=np.float64)
    lmax = (
        float(level[code == ANCHOR_CODE][0]) if np.any(code == ANCHOR_CODE) else float(level.max())
    )
    scale = abs(lmax) or float(np.max(np.abs(level))) or 1.0

    # Ground truth, no model assumptions: master volume 0 is silence.
    at_vol0 = (code & 0x0F) == 0
    selftest = level[at_vol0] / scale
    worst = float(np.max(np.abs(selftest))) if selftest.size else 0.0

    lo, hi = float(level.min()), float(level.max())
    span = hi - lo
    targets = np.linspace(lo, hi, 256)
    sidtable = [int(code[np.argmin(np.abs(level - t))]) for t in targets]

    metrics: dict[str, Any] = {
        "signed_span": [round(lo, 6), round(hi, 6)],
        "lmax": round(lmax, 6),
        "volume0_selftest_worst": round(worst, 4),
        "volume0_selftest": [round(float(e), 4) for e in selftest],
        **_ladder_metrics(np.array([float(level[code == c][0]) for c in sidtable]), targets, span),
    }
    if worst > SELFTEST_TOLERANCE:
        return None, metrics
    return sidtable, metrics


def _ladder_metrics(achieved: np.ndarray, targets: np.ndarray, span: float) -> dict[str, float]:
    """Honest, capture-independent quality figures for a finished ladder.

    The metrics this replaced counted level differences exceeding the *capture
    noise floor*, which measured the recording rig rather than the DAC: across
    three runs a quieter capture scored 6.55 → 7.52 → 7.92 "effective bits" on
    the same hardware, rating a chip whose Mahoney path had degraded to roughly
    4 bits (7.52) above a working one (6.6). Everything here is a property of
    the reconstructed ladder alone.

    * ``ladder_bits`` — ENOB-style: the RMS distance between each of the 256
      requested target levels and the level actually achieved, expressed as the
      uniform quantizer that would have the same RMS error.
    * ``worst_gap_frac`` / ``worst_gap_from_zero_frac`` — the largest hole in
      the ladder, and where it sits. Position is what makes a gap benign or
      not: ~0 means it straddles silence (crossover distortion), ±0.5 means it
      is out at an extreme, where it costs almost nothing.
    * ``crossover_gap_frac`` — the gap spanning zero specifically, the one a
      listener hears as grit on quiet passages.
    """
    if span <= 0:
        return {
            "ladder_bits": 0.0,
            "ladder_rms_err_frac": 0.0,
            "ladder_max_err_frac": 0.0,
            "worst_gap_frac": 0.0,
            "worst_gap_from_zero_frac": 0.0,
            "crossover_gap_frac": 0.0,
        }
    resid = achieved - targets
    rms = float(np.sqrt(np.mean(resid**2)))
    srt = np.unique(achieved)
    gaps = np.diff(srt)
    wi = int(np.argmax(gaps)) if gaps.size else 0
    mid = float(srt[wi] + srt[wi + 1]) / 2 if gaps.size else 0.0
    below = float(srt[srt <= 0].max()) if np.any(srt <= 0) else 0.0
    above = float(srt[srt >= 0].min()) if np.any(srt >= 0) else 0.0
    return {
        # A perfect 256-step ladder is 8 bits; rms == 0 means every target was
        # hit exactly, which only happens on synthetic input.
        "ladder_bits": round(float(np.log2(span / (rms * np.sqrt(12)))), 2) if rms else 8.0,
        "ladder_rms_err_frac": round(rms / span, 5),
        "ladder_max_err_frac": round(float(np.max(np.abs(resid))) / span, 5),
        "worst_gap_frac": round(float(gaps[wi]) / span, 4) if gaps.size else 0.0,
        "worst_gap_from_zero_frac": round(mid / span, 3) if gaps.size else 0.0,
        "crossover_gap_frac": round((above - below) / span, 4),
    }


#: Name fragments that identify an input as video-capture hardware, most
#: specific first. The measurement needs the input the C64's audio arrives on,
#: which is essentially always an HDMI capture device — but only the author's
#: Cam Link used to be recognized, so every other rig silently fell through to
#: the *system default input*. On Windows that is the on-board microphone, which
#: records room noise and measures like a dead chip (see
#: :data:`RING_TRUST_MAX_SPREAD`). These cover the common sticks: Elgato, the
#: MacroSilicon-based HDMI→USB dongles ("USB Video", "USB3.0 HD Video Capture"),
#: and anything self-describing as a capture/HDMI input.
CAPTURE_NAME_HINTS = (
    "cam link",
    "elgato",
    "hdmi",
    "capture",
    "macrosilicon",
    "usb video",
    "av to usb",
)


def looks_like_capture_input(name: str) -> bool:
    """Whether an input device's name identifies it as video-capture hardware.
    Used to pick one automatically, and to warn when the fallback lands on
    something that is probably a microphone."""
    low = name.lower()
    return any(h in low for h in CAPTURE_NAME_HINTS)


def find_capture_device(preferred: int | None) -> int:
    """Resolve the capture device index: `preferred` if given, else the first
    input-capable device whose name looks like video-capture hardware
    (:data:`CAPTURE_NAME_HINTS`, in order), else the system default input.

    The hints are tried in order rather than scanning devices once, so a rig
    with both a Cam Link and some other HDMI input still picks the Cam Link."""
    import sounddevice as sd

    if preferred is not None:
        return preferred
    devices = list(sd.query_devices())
    for hint in CAPTURE_NAME_HINTS:
        for i, dev in enumerate(devices):
            if hint in str(dev["name"]).lower() and dev["max_input_channels"] > 0:
                return i
    default_in = sd.default.device[0]
    return int(default_in) if default_in is not None and default_in >= 0 else 0


class CaptureUnavailableError(RuntimeError):
    """Raised when sounddevice / a usable capture device isn't available."""


def _input_device_list() -> str:
    """One-line-per-device listing of every input-capable device, for error text."""
    import sounddevice as sd

    lines = [
        f"  {i}: {d['name']} ({d['max_input_channels']} in)"
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
    return "\n".join(lines) or "  (none)"


def _capture_fault_message(dev: int, reason: str, peak: float) -> str:
    """The message a capture that doesn't contain the slot ring fails with.

    Everything upstream of this can only say *what* it saw — "found 1 ring sync
    marker", "the passes disagree by 100 %" — and that reads like a bug in the
    measurement when it is almost always the rig. So the failure names the
    device it recorded from, states how loud that recording was, and lists the
    inputs to pick from instead."""
    import sounddevice as sd

    try:
        name = str(sd.query_devices(dev)["name"])
    except Exception:  # noqa: BLE001 — the name is decoration; the advice isn't
        name = "?"
    return (
        f"capture device {dev} ({name!r}) is not carrying the calibration ring "
        f"(peak {peak:.5f} of full scale): {reason}.\nLikely causes, in order:\n"
        "  • it is the wrong input. An on-board microphone records room noise, "
        "which measures exactly like this — the capture has to be the input the "
        "C64's audio actually arrives on (HDMI capture stick, Cam Link, or a "
        "line-in fed from the AV port).\n"
        "  • the C64's audio isn't reaching it — HDMI audio off, the cable in "
        "the wrong jack, or the input's gain at zero.\n"
        "  • the NMI DAC never came up on the C64. Re-run with -v and check the "
        "bring-up lines.\n"
        f"Pick the input with --audio-device N:\n{_input_device_list()}"
    )


class CaptureFormat(NamedTuple):
    """A channel count + sample rate the capture device actually accepts."""

    channels: int
    samplerate: int


def resolve_capture_format(dev: int) -> CaptureFormat:
    """Probe `dev` for a workable (channels, samplerate), preferring stereo at
    :data:`CAP_SR` and widening from there.

    Capture hardware is not all Cam Link. A mono-only UVC input rejects
    ``channels=2`` with PortAudio's generic -9998 "Invalid number of channels",
    and the cheap MacroSilicon-based HDMI→USB dongles are commonly 96 kHz-only
    — either of which used to abort a calibration run with a raw
    ``PortAudioError`` traceback, because the capture was hardcoded to stereo at
    48 kHz. Neither restriction actually prevents a measurement: the levels are
    read off one folded-to-mono channel, and every step of
    :func:`extract_slot_levels` derives its timing from the rate it is handed.

    Rate is the outer loop — a 48 kHz mono capture beats a 96 kHz stereo one,
    since the fallback rates are the compromise and the channel fold is free.
    The device's own ``default_samplerate`` is tried right after `CAP_SR`, ahead
    of the static fallbacks, so an unusual device still gets its native rate.
    ``check_input_settings`` probes without opening a stream, so a rejected
    combination costs nothing. Mirrors ``AudioStreamer._open_input_stream``'s
    channel fallback for the mic path.
    """
    import sounddevice as sd

    try:
        info = sd.query_devices(dev)
        max_in = int(info["max_input_channels"])
    except Exception as e:  # noqa: BLE001 — bad index / device vanished
        raise CaptureUnavailableError(
            f"capture device {dev} could not be queried: {e}\n"
            f"Pick one with --audio-device N:\n{_input_device_list()}"
        ) from e
    name = info["name"]
    if max_in <= 0:
        raise CaptureUnavailableError(
            f"capture device {dev} ({name!r}) has no input channels. "
            f"Pick one with --audio-device N:\n{_input_device_list()}"
        )

    channel_options: list[int] = []
    for ch in (2, max_in, 1):
        if 1 <= ch <= max_in and ch not in channel_options:
            channel_options.append(ch)

    rate_options: list[int] = [CAP_SR]
    for sr in (int(info.get("default_samplerate") or 0), *CAP_SR_FALLBACKS):
        if sr > 0 and sr not in rate_options:
            rate_options.append(sr)

    for sr in rate_options:
        for ch in channel_options:
            try:
                sd.check_input_settings(device=dev, channels=ch, samplerate=sr, dtype="float32")
                return CaptureFormat(ch, sr)
            except Exception:  # noqa: BLE001 — unsupported combination; try the next
                log.debug("calib: device %d rejected channels=%d sr=%d", dev, ch, sr, exc_info=True)
    raise CaptureUnavailableError(
        f"capture device {dev} ({name!r}) accepted no combination of channels "
        f"{channel_options} × rates {rate_options}. Pick another with "
        f"--audio-device N:\n{_input_device_list()}"
    )


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

    from .audio import (
        CIA2_CRA_STOP,
        CIA2_ICR_DISABLE_ALL,
        RING_BUFFER_ADDR,
        RING_BUFFER_SIZE,
        AudioStreamer,
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
            peak = float(np.max(np.abs(mono))) if mono.size else 0.0
            try:
                return read_ring_capture(mono, len(codes), RING_BUFFER_SIZE, sr=fmt.samplerate)
            except MeasurementError as e:
                reason = str(e)
            if attempt < RING_ATTEMPTS:
                log_fn(f"[calib]   unusable capture ({reason}) — retrying")
        raise MeasurementError(_capture_fault_message(dev, reason, peak))

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
                log_fn(
                    f"[calib]   {label} ring {n}/{total} (rotation {rnd}): "
                    f"{d['passes']} passes, L($0F)={got.levels[0]:+.5f}, "
                    f"pass spread {d['pass_spread_frac'] * 100:.2f}%"
                )
        raw, merge_metrics = merge_measurements(measured)
        sidtable, metrics = build_sidtable_from_levels(raw)
        metrics.update(merge_metrics)
        metrics["capture"] = [m.diagnostics for _, m in measured]
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
        # silent NMI is one of the three causes _capture_fault_message has to
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
                f"stop now and pick with --audio-device N:\n{_input_device_list()}"
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

    path = save_calibration(cfg, key, entries, device_info)
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
