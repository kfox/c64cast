"""Per-system Mahoney 8-bit ``$D418`` DAC calibration: measure the SID transfer
curve for the *actual* SID chip(s) on the connected machine and persist a
per-unit amplitude→``$D418`` "sidtable", so playback can use a table matched
to the real chip instead of the baked emulated-UltiSID one.

Why per-system calibration
--------------------------
The baked ``mahoney_ultisid`` table in :mod:`c64cast.audio.dac_curves` generalizes
perfectly across the U64's *emulated* UltiSID (deterministic, model-knob
irrelevant). But **physical 6581/8580 chips vary enormously** chip-to-chip
(measured: curve correlation 0.74 between two 6581s; one chip's table on the
other → ~29 % RMS level error), dominated by the analog filter — and SID
replacements (ARM2SID/SwinSID/FPGASID) differ again. So a baked table cannot
serve a physical/replacement chip; the only correct path is to measure the
transfer curve of the device in front of you. ``c64cast --calibrate-dac`` does
that (Cam Link / any UVC audio capture on the SID output required).

Multi-socket U64/U2+ calibration
---------------------------------
A real U64 (Elite I/II, C64U) can carry **two physical SID sockets**, each
potentially holding a different chip. ``run_calibration`` queries the live
config (``sid_hw_config.detect_sockets`` — ``"SID Detected Socket N"``) and,
for every socket reporting a real chip, isolates it to ``$D400`` (the fixed
address the NMI DAC handler's hand-assembled ``STA $D418`` reaches — see
:mod:`c64cast.sid.asid_sidmap`'s "chip 0 must land at $D400" trick, reused here
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
reach the table — lives with the DSP in :mod:`c64cast.audio.dac_slot_ring`; finding
and probing the capture device lives in :mod:`c64cast.audio.dac_capture_device`.
Identity keys and the calibration file live in
:mod:`c64cast.audio.dac_calibration_store`; which curve playback actually uses is
:mod:`c64cast.audio.dac_curve_resolve`. This module owns the run itself: hardware
bring-up, per-socket isolation, capture + retry, and handing the result to
the store.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from c64cast.app import paths
from c64cast.hw.c64 import CIA2, SCREEN
from c64cast.sid.asid_sidmap import (
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
from c64cast.sid.sid_hw_config import SidHwSession, detect_sockets
from c64cast.sid.sid_panning import CAT_MIXER
from c64cast.sid.sid_volume import VOL_ITEM, VOL_OFF, VOL_UNITY

from .audio_handlers import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
)
from .dac_calibration_store import (
    CalibrationDocument,
    CalibrationResult,
    active_socket_at_d400,
    resolve_calibration_key,
    save_calibration,
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

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from c64cast.app.config import Config
    from c64cast.hw.backend import C64Backend

    from .audio import AudioStreamer

log = logging.getLogger(__name__)

# --- calibration run ---------------------------------------------------------


@dataclass(frozen=True)
class CalibrationRun:
    key: str
    path: Path
    entries: dict[str, CalibrationResult]  # "1" / "2" / "default" -> result


def _plan_rounds() -> list[list[list[int]]]:
    """The capture plan every measurement runs: ring code-batches per rotation
    round. Shared by :func:`_measure_one` and the on-screen duration estimate
    so the two can't disagree about how many rings a run takes."""
    return plan_capture_rounds(codes_per_ring(RING_BUFFER_SIZE) - 1)


# --- on-screen status --------------------------------------------------------
# The machine spends the whole run parked in the BASIC clear loop with a dead
# screen; these two lines tell whoever is looking at it what is happening and
# for roughly how long. Both are painted BEFORE the first capture and never
# repainted: a host DMA halt spanning two CIA #2 Timer A underflows during a
# capture silently drops NMI samples (docs/architecture/audio.md), so the
# screen must not be written while a ring is being measured.

_TITLE_ROW = 10
_TITLE_TEXT = "SID DAC CALIBRATION IN PROGRESS"
_ESTIMATE_ROW = 12
_STATUS_COLOR = 1  # white
# Per-SID cost beyond the rings themselves: socket isolation config writes,
# the Mahoney-env settle, and the numpy fold.
_PER_SID_OVERHEAD_S = 2.0
_ESTIMATE_GRANULARITY_S = 15


def _screen_codes(text: str) -> bytes:
    """ASCII → C64 screen codes, uppercase set ('@A-Z' → $00-$1A; space,
    digits and punctuation $20-$3F pass through). Mirrors
    scenes/bitmap_text.ascii_to_screen_code rather than importing it —
    audio/ stays independent of scenes/."""
    return bytes(c - 0x40 if 0x40 <= c <= 0x5A else c for c in text.upper().encode("ascii"))


def _paint_status_line(be: C64Backend, row: int, text: str) -> None:
    """Center ``text`` on screen row ``row``, white on the cleared screen.

    Plain uncached writes: each line is painted exactly once, pre-measurement
    (see the section comment above for why never during one)."""
    codes = _screen_codes(text)
    base = row * SCREEN.W_CHARS + (SCREEN.W_CHARS - len(codes)) // 2
    be.write_memory_file(f"{SCREEN.RAM + base:04X}", codes)
    be.write_memory_file(f"{SCREEN.COLOR_RAM + base:04X}", bytes([_STATUS_COLOR]) * len(codes))


def _estimate_text(n_sids: int, secs: float, settle: float) -> str:
    """The duration line, computed from the same capture plan the measurement
    loop runs (so the message can't drift from the real ring count). "ABOUT"
    absorbs what it can't know: retried rings and capture bring-up."""
    rings = sum(len(batches) for batches in _plan_rounds())
    per_sid = rings * (secs + settle) + _PER_SID_OVERHEAD_S
    granules = n_sids * per_sid / _ESTIMATE_GRANULARITY_S
    total = max(1, math.floor(granules + 0.5)) * _ESTIMATE_GRANULARITY_S
    noun = "SID" if n_sids == 1 else "SIDS"
    return f"MEASURING {n_sids} {noun} - ABOUT {total} SECONDS"


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


def _require_sounddevice() -> None:
    """Fail before the machine is touched when the optional capture dep is
    absent — nothing below can produce a measurement without it, and the
    advice names the extra to install."""
    try:
        import sounddevice  # noqa: F401
    except Exception as e:  # noqa: BLE001 — optional dep
        raise CaptureUnavailableError(
            "audio capture (sounddevice) is required for --calibrate-dac. Install "
            "the 'mic' extra: uv tool install --force 'c64cast[all]'"
        ) from e


def _device_provenance(
    cfg: Config, be: C64Backend, log_fn: Callable[[str], None]
) -> dict[str, str]:
    """What the calibration file records about the measured device: the
    Ultimate's REST info when the link has it, the transport endpoint for a
    TeensyROM, else nothing."""
    if getattr(be.profile, "supports_config", False):
        try:
            return be.get_device_info()
        except Exception:  # noqa: BLE001 — best-effort provenance only
            log_fn("[calib] could not read device info (product/unique_id)")
            return {}
    if cfg.hardware.backend == "teensyrom":
        tr = cfg.teensyrom
        if tr.transport == "tcp":
            return {"transport": "tcp", "host": tr.host or "", "port": str(tr.tcp_port)}
        return {"transport": "serial", "port": tr.serial_port or ""}
    return {}


def _bring_up_dac_env(be: C64Backend, cfg: Config, log_fn: Callable[[str], None]) -> AudioStreamer:
    """Reset once (HDMI renegotiates), leave the IRQ clear loop running, then
    install the NMI DAC handler + neutral ring + the Mahoney SID env (the env
    lands via ``_upload_nmi_and_buffers`` when the curve is a companding one)."""
    from .audio import AudioStreamer
    from .dsp import DSPParams

    log_fn("[calib] resetting + bringing up NMI DAC + Mahoney env…")
    be.reset()
    time.sleep(1.5)
    be.run_basic_clear_loop()
    st = AudioStreamer(
        be,
        NMI_RATE,
        cfg.ultimate64.system,
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
    st.nmi.start(adaptive=st.nmi_rate_adaptive)
    return st


def _open_capture(device: int | None, log_fn: Callable[[str], None]) -> tuple[int, CaptureFormat]:
    """(Re)initialize PortAudio and resolve the capture device + format,
    saying immediately when the auto-pick fell through to the system default
    (a laptop's microphone would record room noise for ~50 s and fail)."""
    import sounddevice as sd

    log_fn("[calib] settling HDMI + (re)initializing capture…")
    time.sleep(3.0)
    sd._terminate()
    sd._initialize()
    dev = find_capture_device(device)
    fmt = resolve_capture_format(dev)
    dev_name = str(sd.query_devices(dev)["name"])
    log_fn(
        f"[calib] capture device idx {dev}: {dev_name} ({fmt.channels} ch @ {fmt.samplerate} Hz)"
    )
    if device is None and not looks_like_capture_input(dev_name):
        log_fn(
            f"[calib] warning: {dev_name!r} doesn't look like a video-capture "
            "input — this is the system default, picked because no capture "
            "device was recognized. If the C64's audio doesn't arrive on it, "
            + pick_device_hint("stop now and pick with")
        )
    return dev, fmt


@dataclass(frozen=True)
class _RunContext:
    """One calibration run's fixed context, frozen once capture is up — what
    lets the per-ring and per-socket steps live at module level (where they
    are testable) instead of as closures over ``run_calibration`` locals."""

    be: C64Backend
    key: str
    device: int
    fmt: CaptureFormat
    secs: float
    settle: float
    log_fn: Callable[[str], None]


def _capture_ring(ctx: _RunContext, codes: Sequence[int]) -> SlotLevels:
    """Record one ring and read its levels, retrying a spoiled capture.

    The ring is written once; only the recording repeats
    (:data:`RING_ATTEMPTS`), so a ring spoiled by a transient costs one
    capture window rather than the run. :func:`read_ring_capture` decides
    what counts as usable; a rig that never produces one fails here with the
    device named, instead of merging noise into the table and falling over
    at whichever later ring happens to be unreadable.
    """
    import sounddevice as sd

    ctx.be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", build_slot_ring(codes, RING_BUFFER_SIZE))
    reason = "no capture was taken"
    peak = 0.0
    unsteady: UnsteadyRingError | None = None
    last: np.ndarray | None = None
    for attempt in range(1, RING_ATTEMPTS + 1):
        time.sleep(ctx.settle)
        rec = sd.rec(
            int(ctx.secs * ctx.fmt.samplerate),
            samplerate=ctx.fmt.samplerate,
            channels=ctx.fmt.channels,
            device=ctx.device,
            dtype="float32",
        )
        sd.wait()
        # (N, channels) → mono; a 1-channel capture folds to itself.
        mono = rec.mean(axis=1).astype(np.float64)
        last = mono
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        try:
            return read_ring_capture(mono, len(codes), RING_BUFFER_SIZE, sr=ctx.fmt.samplerate)
        except MeasurementError as e:
            reason = str(e)
            unsteady = e if isinstance(e, UnsteadyRingError) else None
        if attempt < RING_ATTEMPTS:
            ctx.log_fn(f"[calib]   unusable capture ({reason}) — retrying")
    # Keep the waveform that was refused. It is the whole evidence for the
    # refusal, and re-creating it costs a fresh hardware run that may not
    # reproduce the fault.
    diag = unsteady.diagnostics if unsteady is not None else {}
    saved = (
        None if last is None else _save_unusable_capture(last, codes, ctx.fmt, ctx.key, dict(diag))
    )
    if unsteady is not None:
        raise UnsteadyRingError(_unsteady_ring_message(reason, diag, saved), diag)
    raise MeasurementError(capture_fault_message(ctx.device, reason, peak, saved))


def _measure_one(
    ctx: _RunContext, label: str
) -> tuple[list[int] | None, dict[str, Any], list[tuple[int, float]]]:
    """Measure all 256 codes through whatever SID answers $D400 right now:
    every ring of every rotation round, then the fold into a sidtable (or the
    self-test rejection) plus the run's metrics."""
    rounds = _plan_rounds()
    total = sum(len(r) for r in rounds)
    ctx.log_fn(
        f"[calib] measuring {label}: 256 codes × {len(rounds)} rotations = "
        f"{total} slot rings ({ctx.secs:.1f}s each, ~{total * (ctx.secs + ctx.settle) / 60:.1f} min)…"
    )
    measured: list[tuple[Sequence[int], SlotLevels]] = []
    n = 0
    for rnd, batches in enumerate(rounds, 1):
        for codes in batches:
            n += 1
            got = _capture_ring(ctx, [ANCHOR_CODE, *codes])
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
            ctx.log_fn(
                f"[calib]   {label} ring {n}/{total} (rotation {rnd}): "
                f"{d['passes']} passes, L($0F)={got.levels[0]:+.5f}, "
                f"pass spread {d['pass_spread_p95_frac'] * 100:.3f}% (worst slot {d['pass_spread_frac'] * 100:.2f}%){marginal}"
            )
    spreads = [m.diagnostics["pass_spread_p95_frac"] for _, m in measured]
    n_marginal, note = _marginal_run_summary(spreads, label)
    if note:
        ctx.log_fn(note)

    raw, merge_metrics = merge_measurements(measured)
    sidtable, metrics = build_sidtable_from_levels(raw)
    metrics.update(merge_metrics)
    metrics["capture"] = [m.diagnostics for _, m in measured]
    metrics["rings_marginal"] = n_marginal
    if sidtable is None:
        ctx.log_fn(
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


def _populated_sockets(be: C64Backend, log_fn: Callable[[str], None]) -> list[tuple[int, str]]:
    """Which physical SID sockets report a detected chip, as (socket, type)
    pairs — empty on detection failure, which falls back to the single
    unlabeled measurement."""
    out: list[tuple[int, str]] = []
    try:
        s1, s2 = detect_sockets(be)
        if s1 or s2:
            sockets_info = be.get_config_category(CAT_SOCKETS)
            if s1:
                out.append((1, sockets_info.get(ITEM_SOCKET1_TYPE, "")))
            if s2:
                out.append((2, sockets_info.get(ITEM_SOCKET2_TYPE, "")))
    except Exception:  # noqa: BLE001 — best-effort; fall back to single measurement
        log_fn("[calib] socket detection failed — falling back to a single measurement")
    return out


def _measure_each_socket(
    ctx: _RunContext, st: AudioStreamer, sockets: list[tuple[int, str]]
) -> dict[str, CalibrationResult]:
    """Isolate each populated socket at $D400 in turn and measure it, restoring
    the machine's own SID address/socket/mixer config afterward."""
    entries: dict[str, CalibrationResult] = {}
    # Mixer levels snapshot once, before the loop: _isolate_mixer
    # rewrites them per socket, so a per-iteration snapshot would
    # capture its own previous edit rather than the user's setting.
    with SidHwSession(ctx.be) as session:
        session.snapshot()
        session.fold(_snapshot_mixer(ctx.be))
        for socket, detected in sockets:
            ctx.log_fn(
                f"[calib] isolating SID socket {socket} ({detected or 'detected'}) at $D400…"
            )
            _isolate_socket(ctx.be, socket)
            _isolate_mixer(ctx.be, f"socket{socket}")
            # Re-park AFTER the routing change, not once at bring-up.
            # The env is a series of writes to $D400-$D418, so it lands
            # on whichever chip owned that window at the time — the
            # first socket measured. Every socket after it would be
            # measured with unparked voices, i.e. no DC for the volume
            # nibble to scale, which reads as a near-silent capture
            # rather than an obviously wrong one.
            st._enable_mahoney_env()
            time.sleep(0.2)
            sidtable, metrics, raw = _measure_one(ctx, f"socket {socket}")
            entries[str(socket)] = CalibrationResult(sidtable, metrics, detected or None, raw)
    return entries


def _silence_and_reset(be: C64Backend, log_fn: Callable[[str], None]) -> None:
    """Best-effort teardown: stop the CIA #2 NMI source, silence the SID,
    reset — a failure here must not mask the measurement's own outcome."""
    try:
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
        be.silence_sid()
        be.reset()
    except Exception as e:  # noqa: BLE001 — best-effort cleanup
        log_fn(f"[calib] cleanup warning: {e}")


def _report_run(
    entries: dict[str, CalibrationResult], path: Path, log_fn: Callable[[str], None]
) -> None:
    """The end-of-run summary: per-SID ladder quality, or why no table."""
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

    On a backend with the multi-SID config surface
    (``profile.supports_sid_config`` — U64 only), every physical SID socket
    reporting a detected chip (``sid_hw_config.detect_sockets``) is measured
    independently — isolated to ``$D400`` via :func:`_isolate_socket`,
    measured, then every socket's original SID address/socket config is
    restored. A board with no populated sockets, or a backend without that
    surface (TeensyROM has no config API; the Ultimate II+ has no sockets to
    isolate), falls back to a single unlabeled measurement of whatever SID
    currently answers ``$D400``.

    Raises :class:`CaptureUnavailableError` if capture can't be set up.
    """
    _require_sounddevice()

    key = resolve_calibration_key(cfg, be)
    supports_sid_config = bool(getattr(be.profile, "supports_sid_config", False))
    device_info = _device_provenance(cfg, be, log_fn)
    normal_d400: int | None = None
    try:
        st = _bring_up_dac_env(be, cfg, log_fn)
        _paint_status_line(be, _TITLE_ROW, _TITLE_TEXT)
        dev, fmt = _open_capture(device, log_fn)
        ctx = _RunContext(
            be=be, key=key, device=dev, fmt=fmt, secs=secs, settle=settle, log_fn=log_fn
        )

        sockets = _populated_sockets(be, log_fn) if supports_sid_config else []
        # Read before the measurement loop: _isolate_socket remaps every socket
        # to $D400 in turn, so asking afterwards answers with c64cast's own
        # edit rather than the mapping this machine actually runs under.
        normal_d400 = active_socket_at_d400(be) if supports_sid_config else None
        # Last screen write of the run: the duration line, painted once the
        # SID count is known and strictly before the first capture.
        _paint_status_line(be, _ESTIMATE_ROW, _estimate_text(max(1, len(sockets)), secs, settle))

        if sockets:
            entries = _measure_each_socket(ctx, st, sockets)
        else:
            sidtable, metrics, raw = _measure_one(ctx, "SID")
            entries = {"default": CalibrationResult(sidtable, metrics, None, raw)}
    finally:
        _silence_and_reset(be, log_fn)

    path = save_calibration(
        cfg,
        CalibrationDocument(key=key, entries=entries, device=device_info, d400_socket=normal_d400),
    )
    _report_run(entries, path, log_fn)
    return CalibrationRun(key=key, path=path, entries=entries)
