"""NMI-driven 4-bit SID DAC audio via the master volume register ($D418).

A small 6502 routine at $C020 pulls one sample per NMI from an 8 KB ring
buffer at $4000-$5FFF, writing the low nibble to $D418. Python feeds the
ring buffer via Socket DMA. CIA #2 Timer A fires NMIs at the configured
sample rate (default 12 kHz — lifts the Nyquist to ~6.0 kHz so fricatives
survive; HW-verified to stay under the handler's badline cycle budget on both
NTSC and PAL. c64.nmi_rate_safety guards against rates that would overrun it).

The handler byte arrays, the ring/pump memory-map constants, the control-loop
tuning constants, and the pure pacing helpers live in audio_handlers.py
(along with the ring-placement and why-not-PWM design rationale); this
module's AudioStreamer uploads and drives them.

SID digi-boost (optional): lock all 3 voices into a steady DC pulse so the
ADSR envelope D/As feed a constant bias into the master mixer. The $D418 DAC
trick scales this bias; without it (or the 6581's natural ADSR DC offset),
writes barely move the speaker on 8580s / emulated SIDs.

The NMI fires independently of whatever the 6502 is doing — typically a
tiny `10 PRINT CHR$(147) : 20 GOTO 20` BASIC loop kicked off by
C64Backend.run_basic_clear_loop() at startup. The loop also clears
the BASIC banner and keeps the kernal cursor IRQ suppressed (BASIC never
returns to direct-input mode, so the blink stays off).

The worker is paced on a strict absolute schedule: each chunk write lands
exactly chunk_period after the previous one, never snapping forward to
wall-clock when a write overruns. The earlier snap-forward variant let
the worker's effective sample rate slip below NMI consumption (DMA round
trip + Python wakeup add several ms per chunk), so NMI started padding
with NEUTRAL between real samples and the audible output was both ~16 dB
quieter than the source and modulated at the chunk rate — speech sounded
muffled with a strong tremolo-buzz on every consonant. The 8 KB ring
absorbs DMA stalls and GC pauses.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .audio_handlers import (
    AUDIO_HEALTH_LOG_INTERVAL_S,
    AUDIO_QUEUE_MAX_BLOBS,
    AUDIO_WRITE_RATE_SHARE,
    BACKPRESSURE_SPIN_S,
    CIA1_TIMER_A_LATCH_KERNAL_NTSC,
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    HOST_DMA_SERVO_TARGET_GAP,
    INT16_FULL_SCALE,
    MAX_QUEUED_SAMPLES,
    NEUTRAL_SAMPLE,
    NMI_RATE_LOOP_WARMUP_S,
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    NMI_ROUTINE_PATCH_OFFSET_READ_HI,
    NMI_ROUTINE_PATCH_OFFSET_RESET_HI,
    NMI_ROUTINE_PATCH_OFFSET_WRAP_HI,
    PREBUFFER_CHUNKS,
    QUEUE_PUT_TIMEOUT_S,
    READ_PTR_LO_ADDR,
    REU_AUDIO_BASE,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_IRQ_HANDLER,
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_TRACKED,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_IRQ_HANDLER,
    REU_MIC_SIZE,
    REU_PUMP_BODY_SUBROUTINE,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_INITIAL_MARGIN,
    REU_PUMP_TICK_COUNTER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    RING_BUFFER_SIZE,
    SAMPLE_TAP_SIZE,
    SID_DIGIBOOST_CONTROL,
    SID_DIGIBOOST_SR,
    SID_GATE_OFF,
    SID_MAHONEY_AD,
    SID_MAHONEY_CONTROL,
    SID_MAHONEY_RES_FILT,
    SID_MAHONEY_SR,
    encode_floats_to_dac,
    stomp_spans,
)
from .audio_rate import NmiTimer, RateServo
from .backend import C64Backend
from .c64 import (
    CIA1,
    CIA2,
    KERNAL,
    REU,
    SID,
    VECTORS,
    halt_quantum_bytes,
)
from .dac_curves import NEUTRAL_INDEX, resolve_dac_curve
from .dsp import AudioDSP, DSPParams

log = logging.getLogger(__name__)

# Typed as Any so Pyright doesn't flag every sd.XXX as accessing attributes
# of None — the AUDIO_AVAILABLE flag is the runtime guard. Assigned via an
# intermediate name so both branches see the same annotation (mypy strict
# rejects re-declaring a name that an `import as` already bound).
try:
    import sounddevice as _sounddevice

    sd: Any = _sounddevice
    AUDIO_AVAILABLE = True
except ImportError:
    sd = None
    AUDIO_AVAILABLE = False


def resolve_audio_input_device(device: int | str) -> int:
    """Map a ``[audio].device`` value (int index, int-in-string, or a device
    *name substring*) to a sounddevice input index.

    Returns ``-1`` ("use the system default input") for a negative/empty value,
    when sounddevice is unavailable, or when a name matches nothing. Unlike the
    camera resolver (:func:`c64cast.camera.resolve_camera_index`) this never
    raises: audio degrades to the default input with a warning, matching
    :meth:`AudioStreamer._resolve_input_device`'s forgiving fallback. PortAudio
    exposes no USB VID:PID, so the only string form is a name substring, matched
    case-insensitively against *input-capable* devices (first match on a tie,
    with a warning). Names come from ``sd.query_devices()`` — the same listing
    ``c64cast --list-devices`` prints."""
    if isinstance(device, int):
        return device
    token = device.strip()
    if not token:
        return -1
    try:
        return int(token)
    except ValueError:
        pass

    if not AUDIO_AVAILABLE or sd is None:
        log.warning(
            "selecting an audio device by name (%r) needs sounddevice (the 'mic' "
            "extra); using the system default input",
            token,
        )
        return -1

    low = token.lower()
    matches: list[tuple[int, str]] = []
    try:
        for idx, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) <= 0:
                continue
            name = str(info.get("name", ""))
            if low in name.lower():
                matches.append((idx, name))
    except Exception as e:  # pragma: no cover - defensive; enumerates the OS
        log.warning("audio device enumeration failed (%s); using system default input", e)
        return -1

    if not matches:
        log.warning(
            "no audio input device matched %r; using the system default input "
            "(run `c64cast --list-devices` to see names + indices)",
            token,
        )
        return -1
    if len(matches) > 1:
        others = ", ".join(f"[{i}] {n}" for i, n in matches)
        log.warning(
            "audio device %r matched %d input devices (%s) — using [%d] %s; "
            "narrow it with a more specific name or an index",
            token,
            len(matches),
            others,
            matches[0][0],
            matches[0][1],
        )
    idx, name = matches[0]
    log.info("resolved audio device %r -> index %d (%s)", token, idx, name)
    return idx


class AudioStreamer:
    """Threaded NMI audio with anti-underrun pad."""

    def __init__(
        self,
        api: C64Backend,
        sample_rate: int,
        system: str,
        *,
        dither: bool = True,
        digi_boost: bool = False,
        dac_curve: str = "linear",
        dac_table: bytes | None = None,
        sid_filter_cutoff: int = 0,
        use_reu_pump: bool = False,
        reu_pump_governor: bool = True,
        host_dma_servo: bool = True,
        nmi_rate_adaptive: bool = False,
        dsp_params: DSPParams | None = None,
    ):
        # The U64 DMA service accepts only one connection at a time — a
        # second concurrent socket is allowed to TCP-accept but its IDENTIFY
        # never gets answered, and the first connection blocks subsequent
        # ones until it closes and a settle window passes. So audio shares
        # the render path's C64Backend. The shared SocketDMAClient is
        # already thread-safe (per-command mutex around sendall), and the
        # combined write rate (audio ~8/sec + render ~30-60/sec) stays well
        # under the ~200/sec DMA ceiling.
        self.api = api
        self.sample_rate = sample_rate
        self.system = system
        self.dither_enabled = dither
        self.digi_boost = digi_boost
        # Mahoney 8-bit $D418 companding curve (see dac_curves.py). "linear"
        # (default) → self._dac_curve is None and the encoder keeps the legacy
        # 4-bit path bit-identical. An active curve is a uint8[256] amplitude→
        # $D418 table; it requires the Mahoney SID env (voices parked as DC
        # sources) which _upload_nmi_and_buffers installs, and is mutually
        # exclusive with digi_boost (both commandeer the 3 voices differently).
        # dac_table lets a caller pass an already-resolved table (e.g. a
        # per-system calibrated one, or the system-aware "auto"/"calibrated"
        # resolution done in cli); the dac_curve string is then just the label
        # shown in logs. Without it we resolve the baked-table name here.
        table = dac_table if dac_table is not None else resolve_dac_curve(dac_curve)
        if table is not None and digi_boost:
            # Config validation should have caught this; be safe and let the
            # curve win (digi_boost's DC bias would corrupt the Mahoney levels).
            log.warning("audio: dac_curve=%s overrides digi_boost (mutually exclusive)", dac_curve)
            self.digi_boost = False
        self.dac_curve_name = dac_curve
        # Ring rest value: the curve's mid-scale (silence) byte when companding,
        # else the linear 4-bit neutral. Used for ring prefill + underrun/EOF pads.
        self._dac_curve: np.ndarray | None
        if table is not None:
            self._dac_curve = np.frombuffer(table, dtype=np.uint8)
            self._neutral_byte = int(self._dac_curve[NEUTRAL_INDEX])
        else:
            self._dac_curve = None
            self._neutral_byte = NEUTRAL_SAMPLE
        self.sid_filter_cutoff = sid_filter_cutoff
        # Host-side DSP applied to float samples before the 4-bit DAC encode.
        # Built per input source: line sources (video/WAV) default to a
        # line chain here; the mic start methods rebuild it with is_mic=True so
        # the AGC stage activates. Disabled params → an identity chain (active
        # is False), so the encode paths short-circuit to the legacy behavior.
        self._dsp_params = dsp_params if dsp_params is not None else DSPParams()
        self._dsp = AudioDSP(self._dsp_params, sample_rate=sample_rate, is_mic=False)
        # REU-staged audio mode: when True, scenes that know the full track
        # upfront (e.g. VideoScene) can call start_for_reu_staged() to
        # preload the audio into REU memory and let a C64-side IRQ pump
        # refill the ring instead of the host-DMA worker thread. See module
        # docstring + REU_IRQ_HANDLER constants. False = default host-DMA
        # path via start_for_external_source / start_mic.
        self.use_reu_pump = use_reu_pump
        # C64-side rate governor for the REU pump (see the governor handler +
        # REU_GOVERNOR_GAP_THRESHOLD_HI). When True, start_for_reu_staged
        # uploads the skip-when-ahead handler so the pump self-throttles to the
        # consumer with zero host bus writes. False uploads the open-loop
        # handler (original drift/echo) for A/B. Plain (non-bank-swap) path
        # only for now; the tracked/video path is a follow-up.
        self.reu_pump_governor = reu_pump_governor
        # Closed-loop pacing for the host-DMA worker (start_for_external_source
        # / start_mic). When True, the worker reads R once per chunk and runs a
        # PI controller (servo_period) on its sleep so the ring gap locks near
        # half a ring instead of free-running and lapping (~26s echo). Pure
        # host-side timing — no C64 writes. False = open-loop wall-clock pacing
        # (original drift/echo) for A/B. Does not affect the REU pump path.
        self.host_dma_servo = host_dma_servo
        # Adaptive NMI-rate compensation (closed loop on measured R rate). When
        # True, the worker runs the slow outer loop (RateServo.update_rate_loop) that
        # raises the nominal NMI rate so the bus-halt-throttled consumer lands at
        # sample_rate — fixing the content-dependent video slowdown while keeping
        # full bandwidth. Mutually exclusive with the static pitch_mult_* path:
        # in adaptive mode set_nmi_latch_for_mode no-ops so nmi.pitch_multiplier
        # stays 1.0 and the loop owns the latch from nominal. See the
        # NMI_RATE_LOOP_* constants + nmi_rate_step.
        self.nmi_rate_adaptive = nmi_rate_adaptive
        # NMI timer + rate-control collaborators (see audio_rate.py): the
        # CIA #2 latch machinery and the worker-thread closed loops. Their
        # state lives on them; the streamer orchestrates.
        self.nmi = NmiTimer(self)
        self.servo = RateServo(self, self.nmi)
        # REU pump state: tracked so stop() can do the right teardown.
        # _reu_pump_armed flips True between arm_reu_pump and disarm_reu_pump.
        # _reu_pump_start_time supports position_seconds() in REU mode where
        # the host-side queue counter doesn't apply (NMI consumes from C64
        # ring, host never sees the samples).
        self._reu_pump_armed = False
        self._reu_pump_start_time = 0.0
        self._reu_pump_total_samples = 0
        self._reu_cia1_latch_nominal = REU_PUMP_CIA1_LATCH
        # REU mic mode: tracks the host's REU write position (wraps at
        # REU_MIC_SIZE). 0 until _start_mic_for_reu_pump() seeds it with
        # REU_MIC_BOOTSTRAP_BYTES.
        self._mic_reu_write_pos = 0
        # Underrun telemetry. Incremented by the worker whenever the
        # producer (PyAV demuxer / mic / WAV) fails to supply samples
        # by the pace deadline. Distinguishes the two failure modes:
        #  - full_underruns: queue was empty → entire chunk is NEUTRAL
        #    (audible as a brief click / drop-out at chunk_period).
        #  - partial_underruns: producer supplied some but not all of
        #    the chunk → NEUTRAL padding at the tail (less audible,
        #    typically a softer click).
        # Logged on stop() so a scene-end report shows whether the
        # producer is keeping up. If counts correlate with perceived
        # stutters in known-deterministic source material, the
        # producer-side decode is the bottleneck (not DMA pacing).
        self._full_underruns = 0
        self._partial_underruns = 0
        # Drip-schedule telemetry. _drip_chunk paces each sub-write to its own
        # slot deadline; a slot reached after its deadline has already passed
        # gets written immediately, so the remaining sub-writes of that chunk
        # bunch up at the end of the period. That degrades the spread back
        # toward the one-write-per-chunk cadence the split exists to escape
        # (measured: 4-20 Hz modulation 0.65 spread vs 8.33 bursted), without
        # showing up in the underrun counts. Counted here so a run can be
        # scored on whether the spread actually held.
        self._late_slots = 0
        self._total_slots = 0
        self._late_worst_s = 0.0
        # Health-line window state: the wall-clock of the last emitted line,
        # the counter snapshot taken with it (for per-window deltas), and the
        # servo gap's excursion within the window.
        self._health_last_log = 0.0
        self._health_mark: tuple[int, int, int, int] = (0, 0, 0, 0)
        # Bytes-per-item queue: each item is a pre-encoded bytes blob of
        # 4-bit volume codes (one byte per sample). This collapses the old
        # per-sample put/get (which hit ~88K lock acquisitions/sec on a
        # 44.1 kHz PyAV demux) to one lock per audio chunk. Backpressure is
        # tracked separately in self._queued_samples since q.qsize() now
        # counts blobs, not samples; q.full() is unused because the cap
        # below is in bytes, not items.
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_QUEUE_MAX_BLOBS)
        self._queued_samples = 0
        # Transport resync (MIDI live-tune Phase 4). flush() bumps _flush_epoch;
        # the push side (_encode_and_enqueue) and the consumer side (_worker)
        # each capture the epoch and discard in-hand/queued stale audio when it
        # changes, so a seek/loop/pause splice can't leak pre-splice samples that
        # were mid-commit in a blocked pusher or held by the worker. _count_lock
        # pairs the _pushed_count/_queued_samples mutations so position_seconds()
        # (= pushed - queued) stays exactly invariant across a flush drain.
        # _stomp_requested asks the worker (which owns write_addr) to NEUTRAL-fill
        # the unplayed ring on a pause — done worker-side so the playlist thread
        # never issues ring DMA concurrently with the servo.
        self._flush_epoch = 0
        self._count_lock = threading.Lock()
        self._stomp_requested = False
        # Cap the buffered audio (MAX_QUEUED_SAMPLES) so a stalled consumer
        # doesn't accumulate a wall of stale audio.
        self._max_queued_samples = MAX_QUEUED_SAMPLES
        self.running = False
        self.chunk_size = 1024
        self.sensitivity = 1.0
        self.noise_gate = 0.05
        self.mic_stream: Any = None
        self._worker_thread: threading.Thread | None = None
        # Set True by start_listen(): a capture-only session that feeds the
        # analysis sink and nothing else — no NMI, no worker, no DAC/SID writes.
        # stop() short-circuits its DAC teardown when this is set. The other
        # start_* methods clear it, since the streamer is reused across scenes.
        self._listen_mode = False

        # Audio-master clock bookkeeping (used by PyAV-driven scenes).
        self._pushed_count = 0

        # Sample tap for FFT overlays. Lockless write from input threads,
        # locked read from the render thread — readers tolerate a torn frame
        # because the next FFT is ~16 ms away.
        self._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
        self._tap_write = 0
        self._tap_lock = threading.Lock()

        # Optional PRE-DSP analysis sink for the music-feature analyzer
        # (audio_features.AnalysisTap.push). Set by a reactive audio source at
        # setup() and cleared at teardown(). Deliberately NOT the tap above: this
        # one is fed before the noise gate and _apply_dsp, because AGC +
        # compressor + limiter flatten exactly the transients an onset detector
        # reads. See audio_features.py for the full rationale.
        self.analysis_sink: Callable[[np.ndarray], None] | None = None
        self._analysis_sink_failed = False

    # ---- 6502 bring-up -------------------------------------------------------
    @property
    def dac_curve(self) -> np.ndarray | None:
        """Active Mahoney companding table (uint8[256] amplitude→$D418), or
        None for the legacy linear 4-bit path. Read by scenes doing offline
        REU pre-encoding so their bytes match the realtime callback paths."""
        return self._dac_curve

    def _upload_nmi_and_buffers(self) -> None:
        nmi = bytearray(NMI_ROUTINE)
        nmi[NMI_ROUTINE_PATCH_OFFSET_READ_HI] = RING_BUFFER_HI
        nmi[NMI_ROUTINE_PATCH_OFFSET_WRAP_HI] = RING_BUFFER_END_HI
        nmi[NMI_ROUTINE_PATCH_OFFSET_RESET_HI] = RING_BUFFER_HI
        self.api.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", bytes(nmi))
        self.api.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", bytes([self._neutral_byte] * RING_BUFFER_SIZE)
        )
        # Disable CIA #2 IRQs + stop Timer A, then point NMI vector → $C020.
        # _arm_nmi_once re-lands the vector when the timer arms, so a dropped
        # write here is recoverable.
        self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
        self.api.write_regs(
            f"{VECTORS.NMI:04X}", NMI_ROUTINE_ADDR & 0xFF, (NMI_ROUTINE_ADDR >> 8) & 0xFF
        )
        if self._dac_curve is not None:
            self._enable_mahoney_env()
        elif self.digi_boost:
            self._enable_digi_boost()

    def _enable_mahoney_env(self) -> None:
        """Install the Mahoney 8-bit ``$D418`` DAC environment (white paper
        §XIV): park all 3 SID voices as steady DC sources (pulse + TEST + GATE,
        ADSR sustained) with voices 1+2 routed through the analog filter.

        With this env in place, the full ``$D418`` byte the NMI handler writes
        per sample selects one of ~256 distinct output levels (the volume
        nibble scales the parked DC, and the filter-mode + voice-3-OFF bits
        re-route it additively/subtractively) — ~6-7 effective bits vs the 16
        the volume nibble gives alone. Written ONCE; the per-sample NMI handler
        is unchanged. Mutually exclusive with digi-boost. See dac_curves.py.
        """
        for v in range(SID.N_VOICES):
            base = SID.voice_base(v)
            # AD (attack=0, decay=15) + adjacent SR (sustain=15, release=15).
            self.api.write_regs(f"{base + SID.OFF_AD:04X}", SID_MAHONEY_AD, SID_MAHONEY_SR)
            self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{SID_MAHONEY_CONTROL:02X}")
        # Filter cutoff maxed ($D415/$D416 adjacent) then route voices 1+2
        # through the filter with resonance 0 ($D417).
        self.api.write_regs(f"{SID.FC_LO:04X}", 0xFF, 0xFF)
        self.api.write_memory(f"{SID.RES_FILT:04X}", f"{SID_MAHONEY_RES_FILT:02X}")
        log.info("audio: Mahoney 8-bit $D418 env engaged (dac_curve=%s)", self.dac_curve_name)

    def _disable_mahoney_env(self) -> None:
        """Release the gate on all 3 voices. Best-effort — called from stop()."""
        for v in range(SID.N_VOICES):
            base = SID.voice_base(v)
            try:
                self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{SID_GATE_OFF:02X}")
            except Exception as e:
                log.debug("mahoney env teardown voice %d failed: %s", v, e)

    def _enable_digi_boost(self) -> None:
        """Lock all 3 SID voices into a steady DC pulse so the master volume
        DAC has a constant bias to scale. EXPERIMENTAL.

        The $D418 trick works because the SID's ADSR envelope D/As leak a DC
        voltage into the master mixer; writing to $D418 scales that offset.
        On a 6581 there's enough residual DC without help; on 8580s and
        emulated SIDs there isn't, and digi playback is near-silent. Setting
        three voices to sustain=$F with the TEST bit locked (oscillator frozen
        at zero, pulse output at steady DC) gives the mixer a strong bias.
        Three voices stack additively — ~3x the output of one.
        """
        for v in range(SID.N_VOICES):
            base = SID.voice_base(v)
            self.api.write_regs(f"{base + SID.OFF_AD:04X}", 0x00, SID_DIGIBOOST_SR)
            self.api.write_regs(f"{base + SID.OFF_PW_LO:04X}", 0x00, 0x08)
            self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{SID_DIGIBOOST_CONTROL:02X}")
        log.info("audio: digi-boost engaged (3 voices, test bit locked)")

    def _disable_digi_boost(self) -> None:
        """Release gate on all 3 voices. Best-effort — called from stop()."""
        for v in range(SID.N_VOICES):
            base = SID.voice_base(v)
            try:
                self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{SID_GATE_OFF:02X}")
            except Exception as e:
                log.debug("digi-boost teardown voice %d failed: %s", v, e)

    @property
    def effective_rate(self) -> float:
        """The rate the C64 NMI consumer *actually* runs at, in Hz.

        `sample_rate` is a request: it selects a CIA #2 Timer A latch, and the
        period is an integer cycle count, so the achievable rates are the grid
        PHI2/(latch+1) — you land on the nearest one, not on what you asked
        for. NTSC@12kHz (the default) resolves to latch 84 = **12032.08 Hz**,
        +0.267%; NTSC@8kHz to 7990.05, -0.124%; PAL@12kHz to 12015.22, +0.127%.

        That offset is common-mode and inaudible in itself (+0.267% is 4.6
        cents), but it is a standing bias everywhere samples are converted to
        real time, and the host-DMA servo has to absorb it before it can start
        correcting for actual bus-halt loss. So the timebase is this, not
        `sample_rate`: producer pacing, the adaptive loop's target, and
        `position_seconds()` all read it, and the file paths resample content
        to it (a decoded track then plays at exactly real time and pitch).

        Deliberately NOT included: the mic capture-device open rate (an odd
        rate gets rejected by some devices, and the servo already handles a
        mic clock that doesn't match) and the DSP filter rates (a 0.27% shift
        in a corner frequency is nothing). It also ignores `pitch_mult_*` and
        the adaptive loop — those are deliberate offsets away from nominal,
        not corrections to it.

        Mirrors `UltimateAudioSampler`, whose `sample_rate` has always been the
        divider's achieved rate rather than the request; that class exposes
        `effective_rate` too so scenes can read either sink the same way.
        """
        return self.nmi.effective_rate

    def _collect_until(
        self, chunk_buf: bytearray, n: int, leftover: bytes, deadline: float
    ) -> tuple[int, int, bytes]:
        """Fill ``chunk_buf`` from ``leftover`` then the queue until it holds
        ``chunk_size`` bytes or ``deadline`` passes.

        Returns ``(new_n, taken_this_call, new_leftover)``. Split out of the
        worker so collection can be resumed across several short deadlines —
        the drip schedule calls it once per quantum slot, which is what lets the
        next chunk be gathered *while* the current one is being written out.
        """
        taken = 0
        size = self.chunk_size
        if leftover and n < size:
            take = min(len(leftover), size - n)
            chunk_buf[n : n + take] = leftover[:take]
            n += take
            taken += take
            leftover = leftover[take:]
        while n < size and not leftover and self.running:
            remaining = deadline - time.monotonic()
            # Past the deadline, still take anything already waiting rather than
            # reporting an underrun over a full queue. The drip schedule calls
            # this once per quantum slot, and a write that runs longer than its
            # slot leaves every later slot already expired — without the
            # non-blocking drain that starves collection completely and
            # NEUTRAL-pads every chunk.
            try:
                piece = self.q.get(timeout=remaining) if remaining > 0 else self.q.get_nowait()
            except queue.Empty:
                break
            take = min(len(piece), size - n)
            chunk_buf[n : n + take] = piece[:take]
            n += take
            taken += take
            if take < len(piece):
                leftover = piece[take:]
        return n, taken, leftover

    def _drip_chunk(
        self,
        payload: bytes,
        addr: int,
        chunk_buf: bytearray,
        leftover: bytes,
        base_time: float,
        chunk_period: float,
    ) -> tuple[int, int, bytes]:
        """Write `payload` into the ring as sub-NMI-period pieces spread evenly
        across `chunk_period`, collecting the *next* chunk in the gaps between
        them. Returns that collection's ``(n, taken, leftover)``.

        Two separate effects, and it is easy to bank only the first. Splitting
        keeps each write's CPU halt inside one NMI period, so it cannot swallow a
        second CIA #2 underflow and lose the tick. Spreading keeps those halts
        from re-bunching into one low-frequency event — which matters because
        modulation sensitivity peaks around 4-20 Hz, right where a single write
        per chunk period lands. Measured on hardware at the 12 kHz NTSC default,
        against a 376 Hz carrier: one 1024-byte write gives 27.3 Hz of FM
        deviation, the same bytes as 64-byte writes issued back-to-back give
        10.8 Hz, and spread across the period they give 5.3 Hz.

        Collecting between the writes rather than before them is what keeps the
        producer's full period of collect time — the writes now occupy the
        period that used to be spent asleep waiting for the pace deadline.
        """
        quantum = self._halt_quantum() or len(payload)
        slots = max(1, (len(payload) + quantum - 1) // quantum)
        slot_period = chunk_period / slots
        n = 0
        taken_total = 0
        for i in range(slots):
            slot_deadline = base_time + i * slot_period
            n, taken, leftover = self._collect_until(chunk_buf, n, leftover, slot_deadline)
            taken_total += taken
            if not self.running:
                break
            sleep_s = slot_deadline - time.monotonic()
            self._total_slots += 1
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                self._late_slots += 1
                self._late_worst_s = max(self._late_worst_s, -sleep_s)
            piece = payload[i * quantum : (i + 1) * quantum]
            if not piece:
                break
            self.api.write_memory_file(f"{addr:04X}", piece)
            addr += len(piece)
            if addr >= RING_BUFFER_END:
                addr = RING_BUFFER_ADDR
        return n, taken_total, leftover

    def _maybe_log_health(self, now: float) -> None:
        """Emit one worker-health line per window, as deltas over that window.

        Deltas rather than totals because the question this answers is *when*,
        not *how much*: a fault that appears a few seconds in, deepens, clears
        and returns is indistinguishable from a steady one in the session
        totals stop() prints. Every field is already maintained by the worker,
        so this costs one clock read per chunk.
        """
        if AUDIO_HEALTH_LOG_INTERVAL_S <= 0:
            return
        mark = (self._full_underruns, self._partial_underruns, self._late_slots, self._total_slots)
        if self._health_last_log == 0.0:
            self._health_last_log = now
            self._health_mark = mark
            return
        dt = now - self._health_last_log
        if dt < AUDIO_HEALTH_LOG_INTERVAL_S:
            return
        d_full, d_part, d_late, d_slots = (
            a - b for a, b in zip(mark, self._health_mark, strict=True)
        )
        servo = self.servo
        gap = (
            "n/a" if servo.health_gap_min < 0 else f"{servo.health_gap_min}..{servo.health_gap_max}"
        )
        r = (
            "n/a"
            if servo.r_rate_ema < 0
            else f"{servo.r_rate_ema:.0f}({servo.r_rate_min:.0f}..{servo.r_rate_max:.0f})"
        )
        log.info(
            "audio: gap=%s late=%d/%d (worst +%.1fms) under=%d/%d writes=%.0f/s "
            "quantum=%dB R=%s Hz latch=%d",
            gap,
            d_late,
            d_slots,
            self._late_worst_s * 1000.0,
            d_full,
            d_part,
            d_slots / dt,
            self._halt_quantum(),
            r,
            self.nmi.latch,
        )
        self._health_last_log = now
        self._health_mark = mark
        servo.health_gap_min = -1
        servo.health_gap_max = -1
        servo.r_rate_min = -1.0
        servo.r_rate_max = -1.0
        self._late_worst_s = 0.0

    def _halt_quantum(self) -> int:
        """Bytes per ring write, sized so each write's CPU halt fits inside one
        NMI period.

        Derived from the live latch rather than a constant, so it tracks the
        configured rate, PAL vs NTSC, and any pitch-multiplier retune — the
        period it has to fit inside is exactly ``latch + 1`` cycles.

        That halt-derived size is then floored by what the link can actually
        carry. The quantum sets the write *rate* — chunk_size/quantum writes per
        chunk period — and the render thread shares this one socket. Ask for
        more writes than the link sustains and each one runs past its slot,
        which starves collection and NEUTRAL-pads chunks over a full queue: on
        hardware a 65-byte quantum (188 writes/s) produced 1744 full underruns
        and lapped the ring. The perceptual cost of backing off is small — the
        measured 4-20 Hz modulation at 128 B is 1.96 against 2.41 at 64 B, i.e.
        slightly *better* — because what matters most is the write cadence
        clearing that band at all, not how far past it lands.
        """
        period_cycles = (self.nmi.latch or self.nmi.compensated_latch()) + 1
        quantum = halt_quantum_bytes(period_cycles)
        max_hz = getattr(getattr(self.api, "profile", None), "max_write_rate_hz", None)
        if max_hz:
            chunk_period = self.chunk_size / self.effective_rate
            max_slots = max(1, int(chunk_period * max_hz * AUDIO_WRITE_RATE_SHARE))
            quantum = max(quantum, -(-self.chunk_size // max_slots))
        return min(self.chunk_size, quantum)

    def read_consumer_ptr(self) -> int | None:
        """The NMI consumer's read pointer R — the self-modifying LDA operand at
        ``$C025`` — or None when the read failed or came back outside the ring.

        None means "couldn't tell": a torn or dropped read, or a backend with no
        read capability at all (`profile.supports_read` false, older TeensyROM
        firmware, where read_memory raises rather than returning None). Every
        caller degrades to its open-loop behavior on None rather than treating a
        bad read as data.
        """
        try:
            r = self.api.read_memory(READ_PTR_LO_ADDR, 2)
        except Exception as e:
            log.debug("read R failed: %s", e)
            return None
        if r is None or len(r) != 2:
            return None
        r_addr = r[0] | (r[1] << 8)
        if not (RING_BUFFER_ADDR <= r_addr < RING_BUFFER_END):
            return None
        return r_addr

    def set_nmi_latch_for_mode(
        self, display_mode: str, calibration: dict[str, float] | None = None
    ) -> None:
        """Retune the NMI consumer rate for a display mode to restore pitch.

        The host-DMA servo locks audio playback speed to the NMI consumer R,
        which loses ~1-14% of its ticks to video DMA bus-halts (heavier video =
        more halts = slower R), so playback comes out slow. ``calibration`` maps
        a display-mode name to a **playback-rate multiplier** (from
        ``[audio] pitch_mult_*``): >1.0 means "play this much faster to cancel
        the slowdown." Call at scene setup when the display mode changes; the
        servo then tracks the new R automatically (it controls on the measured
        ring gap, with no nominal-rate feed-forward to keep in sync).

        Rate and CIA #2 Timer A latch are *inversely* related — the NMI period is
        (latch+1) cycles, so a faster rate needs a *smaller* latch. We divide the
        nominal period by the multiplier:

            period = (nominal_latch + 1) / multiplier;  latch = period − 1

        Only applies under the host-DMA servo with a running worker; the REU pump
        has its own C64-side rate governor and open-loop needs no adjustment.
        """
        if not self.host_dma_servo or not self._worker_thread:
            # REU pump has its own governor; open-loop doesn't need adjustment.
            return
        if self.nmi_rate_adaptive:
            # Adaptive mode owns the latch via the closed loop, so the static
            # multiplier stays 1.0. But record the mode here so the loop SEEDS its
            # starting latch from a close per-mode estimate (no start glide). On a
            # mid-stream mode change (timer already running), re-seed + re-acquire.
            self.nmi.mode = display_mode.lower()
            if self.nmi.started:
                seed = self.nmi.seed_latch_for_mode(self.nmi.mode)
                if seed != self.nmi.latch:
                    self.nmi.write_latch(seed)
                self.servo.loop_acquiring = True
                # A mode change shifts the bus-halt profile (hence R); hold the
                # latch at the new seed until the new mode's load settles.
                self.servo.warmup_until = time.monotonic() + NMI_RATE_LOOP_WARMUP_S
            return

        # `hires_edges` scenes report display_mode.name == "hires" (same VIC
        # fetch), so they already resolve to the `hires` multiplier here.
        multiplier = 1.0 if calibration is None else calibration.get(display_mode.lower(), 1.0)
        # Remember it so NmiTimer.start applies it when the timer first arms.
        # At scene setup the worker is usually still prebuffering (timer not
        # started yet), so we just stash the value and let the timer pick it up.
        self.nmi.pitch_multiplier = multiplier
        if not self.nmi.started:
            return

        adjusted_latch = self.nmi.compensated_latch()
        # Only write if it changed (avoid spurious bus traffic).
        if adjusted_latch == self.nmi.latch:
            return

        log.debug(
            f"[audio] retune NMI for {display_mode}: latch "
            f"{self.nmi.latch} → {adjusted_latch} "
            f"(rate ×{multiplier:.4f})"
        )
        self.nmi.write_latch(adjusted_latch)

    # ---- worker --------------------------------------------------------------
    def _worker(self) -> None:
        """Drain the bytes-blob queue into the C64 ring buffer, paced to
        NMI consumption.

        Pacing is required because the producer is not always the rate
        authority — PyAV's demuxer decodes far faster than real time,
        so without pacing the worker would burn through the queue and
        the audio would play many times too fast. The mic producer is
        naturally real-time, but the worker can't know which it has.

        Per iteration: collect chunk_size bytes from the queue by the
        next pace deadline; if it expires with nothing, ship a NEUTRAL
        chunk (real underrun — keeps NMI from replaying stale audio);
        if the chunk is partial, pad with NEUTRAL to keep pace math in
        chunk-sized steps; sleep until the pace point; write.

        The pace schedule is `next_write_time + chunk_period` exactly —
        strict absolute, no snap-forward when a write overruns. Earlier
        the schedule was `max(next_write_time, now) + chunk_period`,
        which let the worker's effective sample rate slip below NMI
        consumption (DMA round-trip + Python wakeup add several ms per
        chunk). NMI then padded with NEUTRAL repeatedly, producing
        strong AM sidebands at chunk_rate around every audio carrier
        (audible as ~50 % chunk-rate tremolo on speech / music). Strict
        pacing keeps writes locked to chunk_period; the 8 KB ring
        absorbs occasional overshoots without lapping NMI.

        With `host_dma_servo` on (default), the per-chunk increment is the
        closed-loop `servo.next_pace_increment(...)` (a PI controller on the gap
        to R) instead of the bare `chunk_period`. This still adds to the
        *absolute* `next_write_time` — the no-snap-forward property above is
        preserved — but lets W's average rate track the (bus-halt-throttled)
        NMI consumer so the gap can't drift and lap (the ~26s echo). The
        increment is clamped to [0.5, 1.5]·chunk_period so a single bad
        reading can't stall or sprint the schedule."""
        try:
            write_addr = RING_BUFFER_ADDR
            # Just past the last byte actually written — what the servo needs as
            # the live W head, which the pipeline separates from `write_addr`.
            w_head = RING_BUFFER_ADDR
            prebuffered = False
            bytes_prebuffered = 0
            chunk_buf = bytearray(self.chunk_size)
            leftover = b""
            # effective_rate, not sample_rate: the consumer eats at the rate the
            # CIA latch actually yields, so pacing the producer to the *request*
            # leaves the servo a standing offset to chase before it can correct
            # for anything real.
            chunk_period = self.chunk_size / self.effective_rate
            prebuffer_bytes = PREBUFFER_CHUNKS * self.chunk_size
            # Pace + collect deadlines. Zero until NMI starts.
            next_write_time = 0.0
            # The chunk collected last iteration, dripped out over this one. One
            # chunk_period of extra latency buys collection and writing the
            # concurrency they need to overlap; the queued-sample count is not
            # decremented until it is actually written, so position_seconds()
            # still reports where the audio really is.
            pending: bytes | None = None
            pending_addr = RING_BUFFER_ADDR
            pending_from_queue = 0
            pending_epoch = 0

            while self.running:
                # Transport-flush epoch (Phase 4): captured before we collect a
                # chunk; if flush() bumps it while this iteration holds data, the
                # data is stale (pre-splice) and is discarded before the ring
                # write below rather than played.
                epoch = self._flush_epoch
                pace_deadline = next_write_time if prebuffered else 0.0

                n = 0
                from_queue = 0

                if prebuffered and pending is not None:
                    if epoch != pending_epoch:
                        # The splice landed after this chunk left the queue: drop
                        # it unplayed, with the same paired subtract the
                        # freshly-collected case uses below.
                        if pending_from_queue:
                            with self._count_lock:
                                self._queued_samples = max(
                                    0, self._queued_samples - pending_from_queue
                                )
                                self._pushed_count = max(0, self._pushed_count - pending_from_queue)
                        pending = None
                        pending_from_queue = 0
                    else:
                        # Pause fast mute (Phase 4): NEUTRAL-fill the unplayed ring
                        # ahead of the read head so already-queued content goes
                        # silent quickly. Stomping from pending_addr keeps the
                        # original ordering — the chunk about to be written lands
                        # at the front of the stomped span, exactly as it did when
                        # the write was one unsplit call.
                        if self._stomp_requested:
                            self._stomp_requested = False
                            self._stomp_ring(pending_addr)
                        n, from_queue, leftover = self._drip_chunk(
                            pending, pending_addr, chunk_buf, leftover, pace_deadline, chunk_period
                        )
                        if pending_from_queue:
                            with self._count_lock:
                                self._queued_samples = max(
                                    0, self._queued_samples - pending_from_queue
                                )
                        w_head = pending_addr + len(pending)
                        if w_head >= RING_BUFFER_END:
                            w_head -= RING_BUFFER_SIZE
                        pending = None
                        pending_from_queue = 0

                if pending is None and n < self.chunk_size:
                    # Either priming the pipeline (nothing to drip yet) or the
                    # drip's interleaved slots did not fill the chunk. Fall back
                    # to the plain blocking collect against the same deadline.
                    collect_deadline = (
                        pace_deadline if prebuffered else time.monotonic() + chunk_period
                    )
                    n, taken, leftover = self._collect_until(
                        chunk_buf, n, leftover, collect_deadline
                    )
                    from_queue += taken

                if not self.running:
                    break

                if n == 0:
                    if not prebuffered:
                        # Idle: no producer data, no NMI to feed.
                        continue
                    # Real underrun: refresh ring with silence.
                    chunk_buf[:] = bytes([self._neutral_byte] * self.chunk_size)
                    n = self.chunk_size
                    self._full_underruns += 1
                elif n < self.chunk_size and prebuffered:
                    # Partial chunk: pad to keep pace math simple. Pad
                    # bytes are NOT counted in from_queue.
                    pad = self.chunk_size - n
                    chunk_buf[n : n + pad] = bytes([self._neutral_byte]) * pad
                    n = self.chunk_size
                    self._partial_underruns += 1

                # Phase 4 flush: a splice landed while this chunk was in hand.
                # The from_queue + leftover bytes are pre-splice — count them as
                # never pushed (paired subtract keeps position invariant) and
                # skip the ring write + pace increment for this iteration.
                if self._flush_epoch != epoch:
                    discard = from_queue + len(leftover)
                    if discard:
                        with self._count_lock:
                            self._queued_samples = max(0, self._queued_samples - discard)
                            self._pushed_count = max(0, self._pushed_count - discard)
                    leftover = b""
                    continue

                # Pause fast mute on a priming iteration — the pending path above
                # handles the steady-state case, where the stomp has to land
                # against the chunk that is about to go out rather than this one.
                if self._stomp_requested and prebuffered:
                    self._stomp_requested = False
                    self._stomp_ring(write_addr)

                if prebuffered:
                    # Hand the chunk to the next iteration, which drips it into
                    # the ring while collecting its successor. Nothing is written
                    # here, so the queued-sample count stays untouched until the
                    # bytes actually land.
                    sleep_s = pace_deadline - time.monotonic()
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    pending = bytes(chunk_buf[:n])
                    pending_addr = write_addr
                    pending_from_queue = from_queue
                    pending_epoch = epoch
                    write_addr += n
                    if write_addr >= RING_BUFFER_END:
                        write_addr = RING_BUFFER_ADDR
                    next_write_time += self.servo.next_pace_increment(w_head, chunk_period)
                    self._maybe_log_health(time.monotonic())
                    continue

                # Prebuffer fill: the NMI is not consuming yet, so there is no
                # halt to hide from — one unsplit write is both correct and the
                # quickest way to get the ring primed.
                self.api.write_memory_file(f"{write_addr:04X}", bytes(chunk_buf[:n]))
                if from_queue:
                    with self._count_lock:
                        self._queued_samples = max(0, self._queued_samples - from_queue)
                write_addr += n
                if write_addr >= RING_BUFFER_END:
                    write_addr = RING_BUFFER_ADDR
                w_head = write_addr

                bytes_prebuffered += n
                if bytes_prebuffered >= prebuffer_bytes:
                    self.nmi.start(adaptive=self.nmi_rate_adaptive)
                    prebuffered = True
                    # R only becomes meaningful now that the NMI consumes;
                    # start the servo integrator + adaptive-rate loop clean
                    # (warm-up gate armed inside — see reset_for_consumer_start).
                    self.servo.reset_for_consumer_start()
                    # Health windows measure the consuming phase only — the
                    # prebuffer fill writes unsplit and has no slots to be late.
                    self._health_last_log = 0.0
                    # Pace the next write one chunk_period out so the
                    # PREBUFFER_CHUNKS slack stays steady instead of
                    # getting eaten up immediately.
                    next_write_time = time.monotonic() + chunk_period
        except Exception:
            # Without this, a thread crash means audio goes silent forever and
            # main loop has no clue why. Mark not-running so callers can detect.
            log.exception("audio worker crashed")
            self.running = False

    def note_playback_disturbance(self) -> None:
        """Re-arm the adaptive NMI-rate loop's warm-up gate after a large playback
        disturbance (the playlist calls this when it snaps the deadline forward and
        drops a big batch of frames — a seek catch-up or a stream rebuffer).

        Holds the latch at its current value while R rides through the disturbance
        and re-settles, so the loop doesn't chase the abnormal bus load and glitch
        the pitch. The EMA is left intact (not re-seeded) so it keeps tracking
        across the gap. Cheap + thread-safe: a single monotonic write. A no-op in
        effect when the rate loop isn't running (open-loop / REU pump / static)."""
        self.servo.note_disturbance()

    # ---- sample tap ----------------------------------------------------------
    def _push_to_analysis(self, mono_floats: np.ndarray) -> None:
        """Feed the pre-DSP analysis sink, if one is installed.

        Called from realtime callbacks, so a failing analyzer must never take
        the audio path down with it: the first exception is logged and the sink
        is dropped for the rest of the run (visuals stop reacting, sound keeps
        playing)."""
        # getattr-guarded like _dsp_active: streamers built via __new__ in tests
        # (no __init__) must read as "no sink" rather than raising in a callback.
        sink: Callable[[np.ndarray], None] | None = getattr(self, "analysis_sink", None)
        if sink is None:
            return
        try:
            sink(mono_floats)
        except Exception:
            if not self._analysis_sink_failed:
                self._analysis_sink_failed = True
                log.exception("audio analysis sink failed — disabling it (playback continues)")
            self.analysis_sink = None

    def _push_to_tap(self, mono_floats: np.ndarray) -> None:
        """Append float samples in [-1, 1] to the FFT tap ring buffer."""
        n = mono_floats.size
        if n == 0:
            return
        if n >= SAMPLE_TAP_SIZE:
            # Source frame is larger than our tap — keep the tail only.
            with self._tap_lock:
                self._tap_buf[:] = mono_floats[-SAMPLE_TAP_SIZE:]
                self._tap_write = 0
            return
        with self._tap_lock:
            end = self._tap_write + n
            if end <= SAMPLE_TAP_SIZE:
                self._tap_buf[self._tap_write : end] = mono_floats
            else:
                split = SAMPLE_TAP_SIZE - self._tap_write
                self._tap_buf[self._tap_write :] = mono_floats[:split]
                self._tap_buf[: end - SAMPLE_TAP_SIZE] = mono_floats[split:]
            self._tap_write = end % SAMPLE_TAP_SIZE

    def get_recent_samples(self, n: int) -> np.ndarray:
        """Return the most recent n float samples, oldest first.

        Returns a freshly-allocated copy so the caller can do whatever it
        wants without racing the writer. n is clamped to SAMPLE_TAP_SIZE."""
        n = min(int(n), SAMPLE_TAP_SIZE)
        out = np.empty(n, dtype=np.float32)
        with self._tap_lock:
            w = self._tap_write
            # The newest sample is at index (w-1) % N; the oldest of our
            # window is (w - n) % N. Two slices handle the wrap.
            start = (w - n) % SAMPLE_TAP_SIZE
            tail = SAMPLE_TAP_SIZE - start
            if n <= tail:
                out[:] = self._tap_buf[start : start + n]
            else:
                out[:tail] = self._tap_buf[start:]
                out[tail:] = self._tap_buf[: n - tail]
        return out

    # ---- host DSP ------------------------------------------------------------
    def _dsp_active(self) -> bool:
        """True when the host DSP chain has at least one enabled stage. Used to
        decide whether the mic path's legacy hard gate is bypassed (the DSP's
        expander replaces it). getattr-guarded so streamers built via __new__
        in tests (without __init__) read as DSP-inactive rather than erroring."""
        dsp: AudioDSP | None = getattr(self, "_dsp", None)
        return dsp is not None and dsp.active

    def set_pre_emphasis(self, amount: float | None) -> None:
        """Override the DSP chain's pre-emphasis for the upcoming scene.

        The AudioStreamer is shared across scenes, so a scene applies its
        per-scene value (or None = source-aware/global default) at setup(). We
        update _dsp_params and rebuild the line chain now; mic scenes rebuild
        with is_mic=True in start_mic() from the updated params, and the REU
        video path reads _dsp_params via process_offline_dsp(). No-op for
        __new__-built test streamers without _dsp_params."""
        params = getattr(self, "_dsp_params", None)
        if params is None:
            return
        self._dsp_params = dataclasses.replace(params, pre_emphasis=amount)
        self._dsp = AudioDSP(self._dsp_params, sample_rate=self.sample_rate, is_mic=False)

    def _apply_dsp(self, floats: np.ndarray) -> np.ndarray:
        """Run the host DSP chain over float samples in [-1, 1] before the DAC
        encode. No-op (returns the input) when DSP is inactive."""
        dsp: AudioDSP | None = getattr(self, "_dsp", None)
        if dsp is not None and dsp.active:
            return dsp.process(floats)
        return floats

    def process_offline_dsp(self, floats: np.ndarray) -> np.ndarray:
        """Run the configured DSP over a COMPLETE offline buffer using a fresh
        line chain (is_mic=False), leaving the realtime streamer's own chain
        state untouched. Used by the REU video pre-encode so REU-staged
        and host-DMA video audio get identical DSP treatment. No-op when
        DSP is disabled."""
        dsp = AudioDSP(self._dsp_params, sample_rate=self.sample_rate, is_mic=False)
        return dsp.process(floats) if dsp.active else floats

    # ---- shared encode + enqueue ---------------------------------------------
    def _encode_and_enqueue(self, floats: np.ndarray, block_on_full: bool = False) -> int:
        """Push float samples in [-1, 1] through the FFT tap and into the
        DAC queue as 4-bit values. Returns the number of samples enqueued.

        Encodes the whole input array to one bytes blob and enqueues it in
        a single put. The previous per-sample loop hit ~88K lock
        acquisitions/sec on a 44.1 kHz PyAV stream; this is one per
        producer call (~10-40/sec).

        block_on_full: if True, block up to 200ms for queue capacity (used
        by the PyAV push path so the demuxer naturally throttles). If
        False, drop the whole blob when full (mic path, where the
        sounddevice callback is real-time and can't block). Backpressure
        is counted in samples (not blobs) against self._max_queued_samples."""
        if floats.size == 0:
            return 0
        # Phase 4 flush epoch: captured at entry. If a transport flush() bumps it
        # while this call is parked in the backpressure spin below, the samples
        # are pre-splice and are dropped before the put (checked just before it).
        epoch = self._flush_epoch
        floats = self._apply_dsp(floats)
        self._push_to_tap(floats.astype(np.float32, copy=False))
        vol = encode_floats_to_dac(floats, dither=self.dither_enabled, curve=self._dac_curve)
        n = int(vol.size)
        payload = vol.tobytes()
        # Sample-count backpressure. Reading _queued_samples without the GIL
        # is racy with the worker decrement, but the worst case is putting
        # one blob over the cap — harmless given the cap is a soft ceiling.
        if self._queued_samples + n > self._max_queued_samples:
            if not block_on_full:
                return 0
            deadline = time.time() + QUEUE_PUT_TIMEOUT_S
            while self._queued_samples + n > self._max_queued_samples and self.running:
                if time.time() >= deadline:
                    return 0
                time.sleep(BACKPRESSURE_SPIN_S)
        # Drop the blob if a transport splice flushed while we were encoding /
        # waiting for capacity — otherwise this stale pre-splice chunk lands in
        # the queue right after the drain. The residual epoch-check→put window is
        # µs against a user-action-rate flush; accepted.
        if self._flush_epoch != epoch:
            return 0
        try:
            if block_on_full:
                self.q.put(payload, timeout=QUEUE_PUT_TIMEOUT_S)
            else:
                self.q.put_nowait(payload)
        except queue.Full:
            return 0
        with self._count_lock:
            self._queued_samples += n
            self._pushed_count += n
        return n

    # ---- input sources -------------------------------------------------------
    def _mic_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status or not self.running:
            return
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = mono * self.sensitivity
        # Analysis tap first: pre-gate, pre-DSP (see _push_to_analysis).
        self._push_to_analysis(mono.astype(np.float32, copy=False))
        # The DSP expander supersedes the legacy hard gate when DSP is on.
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        self._encode_and_enqueue(mono.astype(np.float32, copy=False))

    def _mic_callback_reu(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        """Mic callback for REU-pump mode. Encodes float samples to 4-bit
        DAC codes (same pipeline as host-DMA mode) but REUWRITEs them into
        the REU mic ring instead of queuing for the worker thread. The
        C64-side IRQ pump drains the REU ring into the audio ring at
        match-rate. The REUWRITE is bus-clean — no SID perturbation per
        callback — so we can do it directly from the sounddevice thread
        without a worker hop."""
        if status or not self.running:
            return
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = mono * self.sensitivity
        self._push_to_analysis(mono.astype(np.float32, copy=False))
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        mono = self._apply_dsp(mono.astype(np.float32, copy=False))
        self._push_to_tap(mono)
        vol = encode_floats_to_dac(mono, dither=self.dither_enabled, curve=self._dac_curve)
        self._push_mic_to_reu(vol.tobytes())

    def _push_mic_to_reu(self, encoded: bytes) -> None:
        """REUWRITE `encoded` to the mic ring at `_mic_reu_write_pos`,
        wrapping at REU_MIC_SIZE. Splits the write across the ring boundary
        when needed so the C64 pump always reads a contiguous stream
        (otherwise the wrap-end half of the chunk would be stale silence
        for one ring period)."""
        n = len(encoded)
        if n == 0:
            return
        pos = self._mic_reu_write_pos
        end = pos + n
        if end <= REU_MIC_SIZE:
            self.api.reu_write(REU_MIC_BASE + pos, encoded)
            self._mic_reu_write_pos = end % REU_MIC_SIZE
        else:
            split = REU_MIC_SIZE - pos
            self.api.reu_write(REU_MIC_BASE + pos, encoded[:split])
            self.api.reu_write(REU_MIC_BASE, encoded[split:])
            self._mic_reu_write_pos = n - split
        # Tracking for position_seconds() in REU-mic mode. Each sample
        # produced advances the wall-clock-derived clock the same way the
        # host-DMA path does via _pushed_count → consumed.
        self._pushed_count += n

    def start_mic(
        self,
        device: int | str,
        sensitivity: float,
        noise_gate: float,
        *,
        skip_irq_vector_hook: bool = False,
    ) -> None:
        """Start mic capture. When ``use_reu_pump`` is set on the streamer,
        delegates to the REU-staged mic pump (which respects
        ``skip_irq_vector_hook`` the same way start_for_reu_staged does).
        For the host-DMA mic path the flag has no effect (no $0314 hook
        to skip)."""
        if not AUDIO_AVAILABLE:
            log.warning("sounddevice not installed; mic capture disabled")
            return
        # Resolve a name substring / int-in-string to an index up front, so the
        # log line below and the reu delegation both see a plain int (and
        # _open_input_stream's re-coercion is a no-op on the int).
        device = resolve_audio_input_device(device)
        self.sensitivity = sensitivity
        self.noise_gate = noise_gate
        # Rebuild the DSP chain for a mic source so the AGC stage activates
        # (line sources keep the is_mic=False chain built in __init__). Covers
        # both the host-DMA and REU mic paths since both route through here.
        # getattr-guarded for streamers built via __new__ in tests.
        dsp_params = getattr(self, "_dsp_params", None)
        if dsp_params is not None:
            self._dsp = AudioDSP(dsp_params, sample_rate=self.sample_rate, is_mic=True)
            if self._dsp.active:
                log.info("audio: host DSP active (mic chain)")
        self._listen_mode = False
        if self.use_reu_pump:
            self._start_mic_for_reu_pump(device, skip_irq_vector_hook=skip_irq_vector_hook)
            return
        self._upload_nmi_and_buffers()
        self._pushed_count = 0
        self.running = True
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="audio-worker"
        )
        self._worker_thread.start()
        assert sd is not None
        self.mic_stream = self._open_input_stream(device)
        self.mic_stream.start()
        log.info(
            "audio: mic device=%d %dHz sensitivity=%.2f noise_gate=%.3f",
            device,
            self.sample_rate,
            sensitivity,
            noise_gate,
        )

    # ---- listen-only capture (analysis, no C64 audio output) ----------------
    def _listen_callback(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        """Listen-only capture callback: feed the analysis sink and nothing
        else. No noise gate, no DSP, no DAC encode, no ring — the input drives
        reactive visuals only, so the raw pre-gate signal is exactly what the
        onset detector wants (mirrors the tap point in `_mic_callback`)."""
        if status or not self.running:
            return
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = mono * self.sensitivity
        self._push_to_analysis(mono.astype(np.float32, copy=False))

    def start_listen(
        self, device: int | str, sensitivity: float, *, sample_rate: int | None = None
    ) -> None:
        """Open the input for analysis ONLY — no NMI, no worker thread, no DAC
        or SID writes. The samples reach `analysis_sink` (the music-feature
        analyzer) and stop there, so a generative scene reacts to whatever is
        played into the input without the 4-bit DAC also blasting a lo-fi copy.

        Unlike `start_mic`, nothing downstream is bound to the DAC sample rate,
        so the input opens at `sample_rate` when given (default the DAC rate).
        A higher rate — e.g. 44.1 kHz — hands the analyzer full-bandwidth audio
        (real hi-hat energy above the DAC's 6 kHz Nyquist, cleaner transients).
        The analyzer's feature math is sample-rate-agnostic, so the caller only
        has to build its `AudioFeatureStream` with the matching rate."""
        if not AUDIO_AVAILABLE:
            log.warning("sounddevice not installed; listen capture disabled")
            return
        device = resolve_audio_input_device(device)
        self.sensitivity = sensitivity
        self._listen_mode = True
        rate = int(sample_rate) if sample_rate else self.sample_rate
        self.running = True
        assert sd is not None
        self.mic_stream = self._open_input_stream(
            device, callback=self._listen_callback, sample_rate=rate
        )
        self.mic_stream.start()
        log.info(
            "audio: listen-only capture device=%d %dHz sensitivity=%.2f", device, rate, sensitivity
        )

    # ---- REU-staged mic (live capture, opt-in via use_reu_pump) -------------
    def _start_mic_for_reu_pump(
        self, device: int | str, *, skip_irq_vector_hook: bool = False
    ) -> None:
        """Bring up live mic capture using the REU-staged pump.

        Same C64-side architecture as start_for_reu_staged() but with a
        ring on BOTH sides: the host fills the REU mic ring from the
        sounddevice callback (REUWRITE — bus-clean) and the C64-side IRQ
        pump drains it into the audio ring at the matched CIA-driven
        rate. No host-DMA writes to the audio ring per chunk = no SID
        perturbation from audio refills.

        ``skip_irq_vector_hook``: skip the $0314 → $C100 patch in step 6
        when the display mode's bank-swap dispatcher already owns $0314
        and JMPs to $C100 itself. See start_for_reu_staged for the
        symmetric rationale.

        Order matches start_for_reu_staged: REU prefill → NMI bring-up →
        REU pump install → CIA #1 reprogram → NMI arm → IRQ vector patch.
        """
        # Idempotent on an int (start_mic already resolved before delegating);
        # keeps the device=%d log below correct if ever called with a name.
        device = resolve_audio_input_device(device)
        # 1. Pre-fill the REU mic ring with NEUTRAL so the pump's first
        # ~ring-size worth of reads play silence (not stale FPGA SRAM,
        # which could be loud noise). One REUWRITE slice = 32 KB, so two
        # slices cover the 64 KB ring.
        log.info(
            "audio[reu mic]: prefilling REU ring at $%06X (%d bytes)", REU_MIC_BASE, REU_MIC_SIZE
        )
        pad = bytes([self._neutral_byte] * REU_UPLOAD_SLICE)
        for off in range(0, REU_MIC_SIZE, REU_UPLOAD_SLICE):
            n = min(REU_UPLOAD_SLICE, REU_MIC_SIZE - off)
            self.api.reu_write(REU_MIC_BASE + off, pad[:n])

        # 2. Standard NMI bring-up (handler + ring + digi-boost). NMI
        # consumes from $4000 which we've just filled with NEUTRAL via
        # _upload_nmi_and_buffers, so initial silence reads cleanly.
        self._upload_nmi_and_buffers()

        # 3. Install REU mic IRQ handler at $C100 and seed the main-RAM REU
        # source tracker at $C200 with REU_MIC_BASE. The handler reloads
        # $DF04/$DF05/$DF06 from this tracker every IRQ (working around the
        # $DF06 read-back garbage — see the mic-tracker comment in audio_handlers.py).
        # Init REU regs: dest = RING_BUFFER_ADDR (start of main audio ring),
        # length = REU_PUMP_CHUNK_SIZE, address-control = 0 (both auto-inc,
        # no autoload). The src registers don't need init since the handler
        # writes them on every trigger.
        self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", REU_MIC_IRQ_HANDLER)
        self.api.write_memory(
            f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}",
            f"{REU_MIC_BASE & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 8) & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 16) & 0xFF:02X}",
        )
        self.api.write_memory(
            f"{REU.C64_ADDR_LO:04X}",
            f"{RING_BUFFER_ADDR & 0xFF:02X}{(RING_BUFFER_ADDR >> 8) & 0xFF:02X}",
        )
        self.api.write_memory(
            f"{REU.LENGTH_LO:04X}",
            f"{REU_PUMP_CHUNK_SIZE & 0xFF:02X}{(REU_PUMP_CHUNK_SIZE >> 8) & 0xFF:02X}",
        )
        self.api.write_memory(f"{REU.ADDR_CONTROL:04X}", "00")

        # 4. Reprogram CIA #1 Timer A latch — matched pump rate vs NMI
        # consume rate. Same value (REU_PUMP_CIA1_LATCH = $3FFF) as the
        # video REU path because the ratio (chunk × NMI_period)
        # is independent of CPU clock.
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}",
            f"{REU_PUMP_CIA1_LATCH & 0xFF:02X}{(REU_PUMP_CIA1_LATCH >> 8) & 0xFF:02X}",
        )
        self.api.flush()
        log.info(
            "audio[reu mic]: pump installed at $%04X, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            REU_PUMP_CIA1_LATCH,
        )

        # 5. Arm NMI (CIA #2 Timer A). NMI now consumes the prebuilt
        # NEUTRAL ring at the consume rate.
        self._reu_pump_start_time = time.monotonic()
        self.nmi.start(adaptive=self.nmi_rate_adaptive)
        time.sleep(0.05)  # let NMI catch a few samples before IRQ arms

        # 6. Patch IRQ vector → REU mic pump handler. Pump starts on next
        # kernal IRQ (~16 ms). Initially reads NEUTRAL (because the ring
        # is full of NEUTRAL); after the bootstrap window, reads real mic
        # data written by the sounddevice callback. Skipped when the
        # display mode's bank-swap dispatcher owns $0314 and JMPs to
        # $C100 itself.
        if not skip_irq_vector_hook:
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}",
                REU_PUMP_HANDLER_ADDR & 0xFF,
                (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF,
            )
            self.api.flush()

        # 7. State for callback + teardown.
        self.running = True
        self._reu_pump_armed = True
        self._pushed_count = 0
        # Bootstrap: start the host write head 200 ms ahead of the pump's
        # read head. Steady-state latency = REU_MIC_BOOTSTRAP_BYTES /
        # sample_rate (~133 ms at the 12 kHz default).
        self._mic_reu_write_pos = REU_MIC_BOOTSTRAP_BYTES

        # 8. Open the mic input stream with the REU callback. _open_input_stream
        # currently hardcodes self._mic_callback as the callback; swap in the
        # REU variant for this path.
        self.mic_stream = self._open_input_stream(device, callback=self._mic_callback_reu)
        self.mic_stream.start()
        log.info(
            "audio[reu mic]: device=%d %dHz sensitivity=%.2f noise_gate=%.3f "
            "bootstrap=%dB (%.0fms latency)",
            device,
            self.sample_rate,
            self.sensitivity,
            self.noise_gate,
            REU_MIC_BOOTSTRAP_BYTES,
            1000 * REU_MIC_BOOTSTRAP_BYTES / self.sample_rate,
        )

    def _resolve_input_device(self, device: int | str) -> tuple[int | None, str]:
        """Pick an input-capable device.

        - `device < 0`: use the system default input device (PortAudio
          accepts `None` for that).
        - The configured device exists and has input channels: use it.
        - Otherwise (output-only or unknown): fall back to the system
          default and warn the user that the configured device is unusable.

        Returns (device_or_None, friendly_name).
        """
        assert sd is not None

        # Coerce a name substring / int-in-string to an index first (returns -1
        # for default / no-match), so the rest of this method is plain int logic.
        device = resolve_audio_input_device(device)

        def _default_input() -> tuple[int | None, str]:
            try:
                idx = sd.default.device[0]
                if idx is None or idx < 0:
                    return None, "system default input"
                info = sd.query_devices(idx, "input")
                return int(idx), str(info.get("name", f"device {idx}"))
            except Exception:
                return None, "system default input"

        if device < 0:
            return _default_input()

        try:
            info = sd.query_devices(device, "input")
            if int(info.get("max_input_channels", 0)) > 0:
                return device, str(info.get("name", f"device {device}"))
        except Exception as e:
            # Redundant with the "falling back" warning below — the second
            # message tells the user what happened and how to fix it.
            log.debug("could not query input device %r: %s", device, e)

        fallback, name = _default_input()
        log.warning(
            "audio device %d has no input channels; falling back to "
            "%s. Pass --audio-device N (see -L) or set audio.device = -1 "
            "in your config to silence this warning.",
            device,
            name,
        )
        return fallback, name

    def _open_input_stream(
        self, device: int | str, callback: Any = None, *, sample_rate: int | None = None
    ) -> Any:
        """Open an InputStream with sensible channel-count fallback.

        CoreAudio (and a few ALSA drivers) reject `channels=1` on devices
        that internally only present stereo, with the generic PortAudio
        error code -9998 "Invalid number of channels". Try 1 first (most
        mics want it); fall back to the device's native channel count;
        finally try a few common counts before giving up with a useful
        error that lists alternative input devices.

        `callback` defaults to the host-DMA `_mic_callback`. The REU mic
        path passes `_mic_callback_reu` to redirect samples into the REU
        ring instead of the worker queue; the listen-only path passes
        `_listen_callback`. `sample_rate` defaults to the DAC rate; the
        listen path passes a higher rate for full-bandwidth analysis.
        """
        assert sd is not None
        if callback is None:
            callback = self._mic_callback
        rate = int(sample_rate) if sample_rate else self.sample_rate
        resolved, dev_name = self._resolve_input_device(device)

        try:
            info = (
                sd.query_devices(resolved, "input")
                if resolved is not None
                else sd.query_devices(kind="input")
            )
            max_in = int(info.get("max_input_channels", 0))
        except Exception as e:
            log.warning("could not query resolved input device: %s", e)
            max_in = 0

        if max_in <= 0:
            raise RuntimeError(
                f"no usable audio input device (tried {dev_name!r}). "
                f"Run `c64cast -L` to list devices "
                f"and pick one with --audio-device N."
            )

        seen: set[int] = set()
        candidates: list[int] = []
        for ch in (1, max_in, 2):
            if 1 <= ch <= max_in and ch not in seen:
                seen.add(ch)
                candidates.append(ch)

        last_err: Exception | None = None
        for ch in candidates:
            try:
                stream = sd.InputStream(
                    device=resolved, samplerate=rate, channels=ch, callback=callback
                )
                if ch != 1:
                    log.info("mic: opened %r with channels=%d (downmixing to mono)", dev_name, ch)
                return stream
            except sd.PortAudioError as e:
                last_err = e
                log.debug(
                    "mic: device %r rejected channels=%d sr=%d: %s",
                    dev_name,
                    ch,
                    rate,
                    e,
                )
        raise RuntimeError(
            f"could not open mic on {dev_name!r} at "
            f"{rate} Hz (tried channels {candidates}): "
            f"{last_err}"
        )

    # ---- external-source mode (used by PyAV demuxer) ------------------------
    def start_for_external_source(self) -> None:
        """Bring up NMI + worker without an input thread. Caller feeds samples
        via push_samples()."""
        self._listen_mode = False
        self._upload_nmi_and_buffers()
        self._pushed_count = 0
        self.running = True
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="audio-worker"
        )
        self._worker_thread.start()
        # Report the achieved rate alongside the request: they differ by the
        # CIA latch quantization (NTSC@12k → 12032 Hz), and the achieved one is
        # what everything downstream is actually timed against.
        log.info(
            "audio: external push source → SID @ %dHz requested, %.1fHz actual (%+.2f%%)",
            self.sample_rate,
            self.effective_rate,
            100.0 * (self.effective_rate / self.sample_rate - 1.0) if self.sample_rate else 0.0,
        )

    # ---- REU-staged playback (VideoScene) ------------------------------
    def start_for_reu_staged(
        self,
        audio_4bit: bytes,
        chunk_size: int | None = None,
        *,
        skip_irq_vector_hook: bool = False,
    ) -> None:
        """Bring up audio with the entire track preloaded into REU.

        ``audio_4bit`` is a bytes blob of pre-encoded 4-bit DAC volume codes
        (1 byte = 1 sample). Caller is responsible for the encoding (use the
        same float→4-bit pipeline as ``_encode_and_enqueue`` to stay
        consistent with the host-DMA path).

        ``chunk_size`` overrides the default REU_PUMP_CHUNK_SIZE for scenes
        where the C64 bus is heavily halted (e.g. mhires DMAWRITE). The pump
        production rate is chunk × pump_irq_rate; when NMI consumption drops
        below the configured sample_rate due to bus halts, a smaller chunk keeps
        the ring from overflowing. See REU_PUMP_CHUNK_SIZE_HEAVY_BUS for the measured
        value (4020 Hz NMI under mhires-like halts → ~65 bytes/IRQ).

        ``skip_irq_vector_hook``: when True, skip step 6 (patching
        $0314 → $C100). Used when the display mode owns $0314 — its
        bank-swap dispatcher at $C500 (merged variant) JMPs to $C100 on
        non-raster IRQs, so the audio bytes at $C100 are still reached
        but via the dispatcher rather than directly. The dispatcher
        installer pre-uploads a 3-byte JMP $EA31 stub at $C100 BEFORE
        hooking $0314, so the gap between dispatcher install and this
        method writing real audio bytes is covered.

        Architecture (in order — order matters for clean bring-up):
          1. Upload audio_4bit to REU offset 0 via REUWRITE slices.
          2. Standard NMI bring-up (NMI routine at $C020, ring at $4000
             with first 8 KB of audio pre-filled so NMI starts on real data).
          3. Install REU pump IRQ handler at $C100, initialize REU registers
             ($DF02-$DF0A) for streaming source.
          4. Reprogram CIA #1 Timer A latch ($DC04/$DC05) for matched pump
             rate so write_pos doesn't lap read_pos (eliminates the stale-
             overlap artifact that produces audible "static").
          5. Arm NMI (CIA #2 Timer A enable). NMI starts consuming pre-fill.
          6. Patch IRQ vector $0314 → $C100 (skipped if
             skip_irq_vector_hook). REU pump starts refilling ring
             ~16 ms later when the next kernal IRQ fires.

        No Python worker thread is started — the C64-side IRQ handler is
        the pump. self.running stays True so stop() does proper teardown.
        """
        if not audio_4bit:
            log.warning("audio: start_for_reu_staged called with empty data")
            return
        self._listen_mode = False
        chunk = REU_PUMP_CHUNK_SIZE if chunk_size is None else chunk_size
        # CIA #1 latch: pump period = chunk × NMI period. The NMI period is
        # (NMI latch + 1) cycles — derive it from the actual consumer latch
        # rather than hardcoding 128, so a non-default sample_rate still gets a
        # matched pump rate. (At 8 kHz this is the historical chunk × 128 - 1.)
        nmi_period = self.nmi.nominal_latch() + 1
        cia1_latch = chunk * nmi_period - 1
        self._reu_cia1_latch_nominal = cia1_latch
        # Pump start pointers: seed the write pointer half a ring behind the
        # reader (REU_PUMP_INITIAL_MARGIN) for symmetric jitter headroom.
        # src offset ≡ dst position (mod ring), so the constant sample→position
        # mapping is preserved (see REU_PUMP_INITIAL_MARGIN). Both the plain
        # auto-increment handler (initial $DF02/$DF04 regs) and the tracked
        # handler (seeded $C200 tracker) use these same values.
        initial_src_off = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        initial_dst = RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN
        # 1. Preload audio into REU, padded with ~5 sec of NEUTRAL_SAMPLE
        # beyond source end. Without the pad, when the pump pointer runs
        # past the end of the audio it reads uninitialized FPGA SRAM —
        # could be anything, including high-amplitude noise (audible as a
        # loud hiss at the end of the video). The pad costs ~40 KB of REU
        # for a typical 5-second tail and ensures playback decays cleanly
        # to silence after EOF until the scene tears down on video EOF.
        # Both are real-time durations of what the pump will drain, so they
        # scale by effective_rate (the payload was encoded at it too).
        eof_pad_bytes = round(self.effective_rate * 5)
        log.info(
            "audio: REU upload %d bytes (%.1fs of source) + %d bytes EOF pad",
            len(audio_4bit),
            len(audio_4bit) / self.effective_rate,
            eof_pad_bytes,
        )
        t0 = time.perf_counter()
        for off in range(0, len(audio_4bit), REU_UPLOAD_SLICE):
            self.api.reu_write(REU_AUDIO_BASE + off, audio_4bit[off : off + REU_UPLOAD_SLICE])
        # EOF pad: write NEUTRAL_SAMPLE for the tail so the pump's read-past-
        # end-of-source plays silence instead of garbage.
        pad_payload = bytes([self._neutral_byte] * REU_UPLOAD_SLICE)
        pad_off = len(audio_4bit)
        pad_end = pad_off + eof_pad_bytes
        while pad_off < pad_end:
            chunk_len = min(REU_UPLOAD_SLICE, pad_end - pad_off)
            self.api.reu_write(REU_AUDIO_BASE + pad_off, pad_payload[:chunk_len])
            pad_off += chunk_len
        log.info("audio: REU upload took %.2fs", time.perf_counter() - t0)

        # 2. Standard NMI bring-up (NMI routine + neutral ring + digi-boost).
        self._upload_nmi_and_buffers()

        # 2b. Pre-fill the ring buffer with the first 8 KB of audio so NMI
        # starts on real audio data rather than NEUTRAL silence. Without this,
        # there'd be ~1s of silence before the REU pump catches up.
        prefill = audio_4bit[:RING_BUFFER_SIZE]
        if len(prefill) < RING_BUFFER_SIZE:
            prefill = prefill + bytes([self._neutral_byte] * (RING_BUFFER_SIZE - len(prefill)))
        self.api.write_memory_file(f"{RING_BUFFER_ADDR:04X}", prefill)

        # 3. Install REU pump IRQ handler at $C100 and initialize REU regs.
        # Source = REU offset REU_PUMP_INITIAL_MARGIN, Dest = ring start +
        # REU_PUMP_INITIAL_MARGIN — i.e. the write pointer starts half a ring
        # BEHIND the reader (which begins at ring start on the pre-fill) for
        # symmetric jitter headroom. The first pump DMAs harmlessly re-write
        # the upper half of the pre-fill with identical bytes, then the pump
        # advances steadily ~0.5 s behind NMI. Length = chunk_size. Address
        # control = 0 (both source and dest auto-increment, no autoload).
        #
        # Handler variant: when the display mode owns $0314 (REU bank-swap
        # video on hires/mhires), the bank-swap raster IRQ uses the REC
        # controller too — its DMAs overwrite BOTH src ($DF04-$DF06) AND
        # dst ($DF02-$DF03) between audio IRQs. The plain handler relies
        # on those registers auto-incrementing across triggers and would
        # read from the video REU staging area + write into color RAM
        # after each raster IRQ. The TRACKED variant reloads all 5 regs
        # from a main-RAM tracker ($C200-$C204: src LO/MI/HI, dst LO/HI)
        # every IRQ, immune to inter-IRQ REC contamination. Patch offsets:
        #   plain (37 B):    chunk at offsets 2, 7
        #   tracked (109 B): chunk at offsets 2, 7, 51, 59, 76, 84
        if skip_irq_vector_hook:
            handler = bytearray(REU_IRQ_HANDLER_TRACKED)
            handler[2] = chunk & 0xFF  # length LO
            handler[7] = (chunk >> 8) & 0xFF  # length HI
            handler[51] = chunk & 0xFF  # src advance ADC LO
            handler[59] = (chunk >> 8) & 0xFF  # src advance ADC HI
            handler[76] = chunk & 0xFF  # dst advance ADC LO
            handler[84] = (chunk >> 8) & 0xFF  # dst advance ADC HI
            # Seed src + dst trackers BEFORE uploading the tracked
            # handler bytes — between handler upload and tracker seed,
            # any CIA #1 IRQ via the bank-swap dispatcher would run the
            # handler with stale tracker values and DMA from/to garbage
            # addresses (audible as bursts of static into ring + writes
            # into color RAM). Bank-swap install left the JMP $EA31 stub
            # at $C100 covering the window while we seed the tracker;
            # the upload-handler write then atomically swaps to the real
            # handler now that the tracker is valid.
            self.api.write_memory(
                f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}",
                f"{initial_src_off & 0xFF:02X}"
                f"{(initial_src_off >> 8) & 0xFF:02X}"
                f"{(initial_src_off >> 16) & 0xFF:02X}"
                f"{initial_dst & 0xFF:02X}"
                f"{(initial_dst >> 8) & 0xFF:02X}",
            )
            # Seed tick-divider counter to 1: first IRQ DECs to 0, doesn't
            # branch, reloads to N, chains. Then N-1 lean-exits before the
            # next chain. Without this seed the counter byte is whatever
            # was in main RAM at $C205 (could be 0 → wraps to $FF on DEC
            # → 254 lean-exits before first kernal tail, eating keyboard
            # responsiveness during the first ~2.5 sec of playback).
            self.api.write_memory(f"{REU_PUMP_TICK_COUNTER_ADDR:04X}", "01")
            # Upload the pump-body subroutine at $C180 BEFORE the entry at
            # $C100. The chunked mhires bank-swap dispatcher JSRs to $C180
            # between every per-frame REC chunk; if the entry at $C100 is
            # in place before the body, a CIA #1 IRQ that fires mid-install
            # could end up calling into uninitialized RAM at $C180. Body
            # first means the JSR target is always valid by the time the
            # JMP $EA31 stub at $C100 is replaced with the real handler.
            self.api.write_memory_file(
                f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", REU_PUMP_BODY_SUBROUTINE
            )
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", bytes(handler))
        elif self.reu_pump_governor:
            # Governor handler: 18-byte skip-when-ahead prefix + pump body.
            # The chunk patch sites are shifted by the prefix: the body's
            # LDA #<chunk (plain offset 2) lands at 19, LDA #>chunk (7) at 24.
            handler = bytearray(REU_IRQ_HANDLER_GOVERNOR)
            handler[19] = chunk & 0xFF  # LDA #<chunk → STA $DF07
            handler[24] = (chunk >> 8) & 0xFF  # LDA #>chunk → STA $DF08
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", bytes(handler))
        else:
            handler = bytearray(REU_IRQ_HANDLER)
            handler[2] = chunk & 0xFF  # LDA #<chunk → STA $DF07
            handler[7] = (chunk >> 8) & 0xFF  # LDA #>chunk → STA $DF08
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", bytes(handler))
        self.api.write_memory(
            f"{REU.C64_ADDR_LO:04X}", f"{initial_dst & 0xFF:02X}{(initial_dst >> 8) & 0xFF:02X}"
        )
        self.api.write_memory(
            f"{REU.REU_ADDR_LO:04X}",
            f"{initial_src_off & 0xFF:02X}{(initial_src_off >> 8) & 0xFF:02X}"
            f"{(initial_src_off >> 16) & 0xFF:02X}",
        )
        self.api.write_memory(
            f"{REU.LENGTH_LO:04X}", f"{chunk & 0xFF:02X}{(chunk >> 8) & 0xFF:02X}"
        )
        self.api.write_memory(f"{REU.ADDR_CONTROL:04X}", "00")

        # 4. Reprogram CIA #1 Timer A latch for pump rate. The kernal-default
        # rate (60/50 Hz) underfills the ring at our chunk size and produces
        # an audible stale-data echo. CIA #1 stays in continuous mode (kernal
        # already set CRA bits); only the latch changes. BASIC's TI$ jiffy
        # clock drifts as a side effect — nothing we depend on.
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{cia1_latch & 0xFF:02X}{(cia1_latch >> 8) & 0xFF:02X}"
        )

        self.api.flush()
        log.info(
            "audio: REU pump installed at $%04X, chunk=%d, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            chunk,
            cia1_latch,
        )

        # 5. Arm NMI (CIA #2 Timer A). NMI now consumes the pre-filled ring.
        # Capture the playback-clock origin RIGHT BEFORE NMI starts firing
        # so position_seconds() measures "time since user started hearing
        # audio" rather than "time since IRQ vector was patched 100 ms
        # later" (which would put video sync 100 ms behind audio).
        self._reu_pump_start_time = time.monotonic()
        self.nmi.start(adaptive=self.nmi_rate_adaptive)

        # Brief settle so NMI is already firing before the REU pump arms;
        # otherwise the first pump DMA could overwrite ring positions NMI
        # hasn't yet read, causing a glitch.
        time.sleep(0.05)

        # 6. Patch IRQ vector → REU pump handler. Pump starts on next kernal
        # IRQ (~16 ms after this write). Skipped when the display mode's
        # bank-swap dispatcher owns $0314 and JMPs to $C100 itself.
        if not skip_irq_vector_hook:
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}",
                REU_PUMP_HANDLER_ADDR & 0xFF,
                (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF,
            )
            self.api.flush()

        self.running = True
        self._reu_pump_armed = True
        self._reu_pump_total_samples = len(audio_4bit)
        self._pushed_count = 0
        log.info(
            "audio: REU pump armed; NMI consuming @ %d Hz (vector_hook=%s, governor=%s)",
            self.sample_rate,
            "skipped" if skip_irq_vector_hook else "set",
            "on" if self.reu_pump_governor else "off",
        )

    def _disarm_reu_pump(self) -> None:
        """Restore IRQ vector to kernal default and CIA #1 Timer A to ~60 Hz.

        Idempotent — safe to call from stop() even if the REU pump was never
        armed. Order: vector restore FIRST so the next kernal IRQ doesn't
        fire into a handler we're about to dismantle, then CIA #1 latch
        back to kernal's value, then the normal NMI/SID teardown."""
        if not self._reu_pump_armed:
            return
        try:
            # Restore IRQ vector → $EA31. Use write_regs (coalesced into
            # one DMA) so $0314 and $0315 atomically point at the kernal.
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
            )
            # Restore CIA #1 Timer A latch to the NTSC kernal default
            # (CIA1_TIMER_A_LATCH_KERNAL_NTSC). PAL kernal uses a slightly
            # different value but the timer keeps running either way;
            # the kernal will overwrite this if it needs to.
            latch = CIA1_TIMER_A_LATCH_KERNAL_NTSC
            self.api.write_memory(
                f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
            )
            self.api.flush()
        except Exception as e:
            log.debug("REU pump disarm: %s", e)
        self._reu_pump_armed = False

    def push_samples(self, samples_int16: np.ndarray) -> None:
        """Convert mono int16 → 4-bit volume codes and enqueue. Blocks
        briefly when the queue is full so the PyAV demuxer naturally
        throttles to the audio sample rate."""
        floats = samples_int16.astype(np.float32) / INT16_FULL_SCALE
        # Pre-DSP analysis tap, same as the mic callbacks — this is what lets a
        # decoded file drive reactive visuals through the identical analyzer.
        self._push_to_analysis(floats)
        self._encode_and_enqueue(floats, block_on_full=True)

    def position_seconds(self) -> float:
        """Approximate playback position from the consumer's perspective.

        Host-DMA mode: (samples pushed - samples still queued) / effective_rate.
        REU pump mode: wall-clock seconds since the IRQ pump armed (clamped
        to the total source length so over-runs don't desync video). The C64
        ring buffer adds another ~0.5s of latency past either path, but
        that bias is constant in steady state and therefore harmless for
        relative sync.

        The divisor is `effective_rate` because this is a real-time clock —
        video is slaved to it, and the `clock/wall` gauge that calibrates
        [audio].dac_bitmap_tempo_* reads it against wall time, so dividing by
        the *requested* rate put a standing 0.27% (NTSC@12kHz) in both.
        """
        rate = self.effective_rate
        if not rate:
            return 0.0
        if self._reu_pump_armed:
            elapsed = time.monotonic() - self._reu_pump_start_time
            total_s = self._reu_pump_total_samples / rate
            return max(0.0, min(elapsed, total_s))
        # q.qsize() now counts bytes-blobs, not samples — read the explicit
        # sample-count counter instead.
        consumed = self._pushed_count - self._queued_samples
        return max(0.0, consumed / rate)

    def reset_position(self) -> None:
        self._pushed_count = 0

    def _drain_queue_samples(self) -> int:
        """get_nowait-drain self.q; return the total samples dropped (each blob
        is one byte per sample, see the q comment in __init__). Shared by flush()
        and stop()."""
        drained = 0
        while True:
            try:
                blob = self.q.get_nowait()
            except queue.Empty:
                break
            drained += len(blob)
        return drained

    def flush(self, *, silence_output: bool = False) -> None:
        """Drop all queued (not-yet-ring-written) audio WITHOUT moving
        position_seconds(). Used by VideoScene's transport splice (seek / loop
        wrap / resume) so stale pre-splice audio doesn't play after the demuxer
        re-seeks. ``silence_output`` additionally asks the worker to NEUTRAL-fill
        the unplayed ring region (pause fast mute) — the worker owns write_addr,
        so it executes the ring stomp, not this thread.

        The bump-then-drain order pairs with the epoch checks in the push and
        worker paths: pushers blocked mid-commit and the worker holding an
        in-hand chunk both discard against the new epoch, closing the windows a
        bare queue drain would leave open. Counters are subtracted in pairs under
        _count_lock so ``position = pushed - queued`` is unchanged by the drop.
        No-op in REU-pump mode (no host queue to flush)."""
        if self._reu_pump_armed:
            return
        self._flush_epoch += 1
        drained = self._drain_queue_samples()
        with self._count_lock:
            self._queued_samples = max(0, self._queued_samples - drained)
            self._pushed_count = max(0, self._pushed_count - drained)
        if silence_output:
            self._stomp_requested = True

    def _stomp_ring(self, write_addr: int) -> None:
        """NEUTRAL-fill the unplayed ring region ``(R + guard .. W)`` for the
        pause fast mute. On a bad R read it just returns (the drained queue pads
        the ring to silence within ~1 s regardless). Called only from the worker
        thread, so write_addr is the live worker-local W."""
        r_addr = self.read_consumer_ptr()
        if r_addr is None:
            return
        neutral = bytes([self._neutral_byte])
        for addr, ln in stomp_spans(r_addr, write_addr):
            self.api.write_memory_file(f"{addr:04X}", neutral * ln)

    # ---- shutdown ------------------------------------------------------------
    def stop(self) -> None:
        # Listen-only sessions never touched the NMI/DAC/SID, so skip all of
        # that teardown (writing $D418/NMI vectors would be spurious U64 traffic)
        # — just close the input stream and reset the flag. getattr-guarded for
        # streamers built via __new__ in tests.
        if getattr(self, "_listen_mode", False):
            self.running = False
            self._listen_mode = False
            if self.mic_stream:
                try:
                    self.mic_stream.stop()
                    self.mic_stream.close()
                except Exception as e:
                    log.debug("listen close: %s", e)
                self.mic_stream = None
            return
        # Order matters for clean audio cutoff:
        #  - REU pump (if armed): restore IRQ vector + CIA #1 latch FIRST
        #    so the pump doesn't fire into a teardown-in-progress.
        #  - Then disable the NMI source so no more SID writes land. Without
        #    this, the worker can block up to 2 × chunk_period (~256 ms)
        #    waiting on q.get before noticing running=False — during which
        #    time NMI keeps reading the ring and playing the buffered audio,
        #    audible as a brief echo past the visual end of the clip.
        #  - Then zero SID volume so the DAC isn't clamped at the last NMI
        #    value, and finally restore the KERNAL NMI vector.
        self.running = False
        # REU pump teardown is a no-op if it was never armed (host-DMA mode).
        # The governor lives entirely in the C64-side handler, so disarming the
        # IRQ vector stops it — no host thread to join.
        self._disarm_reu_pump()
        try:
            self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            self.api.write_memory("D418", "00")
            if self.digi_boost:
                self._disable_digi_boost()
            elif self._dac_curve is not None:
                self._disable_mahoney_env()
            self.api.write_regs(
                f"{VECTORS.NMI:04X}", KERNAL.DEFAULT_NMI & 0xFF, (KERNAL.DEFAULT_NMI >> 8) & 0xFF
            )
        except Exception as e:
            log.debug("teardown write failed: %s", e)
        # NMI is already silenced; let the worker / mic threads tear down
        # at their own pace.
        if self.mic_stream:
            try:
                self.mic_stream.stop()
                self.mic_stream.close()
            except Exception as e:
                log.debug("mic close: %s", e)
            self.mic_stream = None
        if self._worker_thread:
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None
        # Drain the queue so subsequent runs start clean.
        self._drain_queue_samples()
        self._pushed_count = 0
        self._queued_samples = 0
        self._stomp_requested = False
        # Clear the timer's pitch-comp/arm state and the servo's watchdog +
        # adaptive-rate state, so the next scene's bring-up re-arms and
        # re-acquires from nominal (the per-mode learned-latch cache survives
        # by design — see NmiTimer.reset_after_stop).
        self.nmi.reset_after_stop()
        self.servo.reset_after_stop()
        # Report underrun telemetry for the run that just ended. Each full
        # underrun is an audible click; partials are less audible but still
        # indicate producer stalls. Deterministic, source-correlated counts
        # (same numbers across reruns of the same video) point at PyAV decode
        # stalls rather than DMA timing.
        #
        # Gated on the worker having actually written to the ring, and worded
        # per *run* rather than per session, because stop() is called once when
        # the scene tears down and again at session teardown — reporting
        # unconditionally printed the real counts and then, from the second
        # call with the counters already cleared, a flat contradiction of them.
        if self._total_slots:
            if self._full_underruns or self._partial_underruns:
                log.warning(
                    "audio: %d full + %d partial underruns this run "
                    "(producer stalled past pace deadline)",
                    self._full_underruns,
                    self._partial_underruns,
                )
            else:
                log.info("audio: clean run (no underruns)")
        self._full_underruns = 0
        self._partial_underruns = 0
        # Late slots: sub-writes that reached their slot with the deadline
        # already gone. A run with a low count kept the spread it was designed
        # to have; a high one collapsed toward one bunched write per chunk
        # period, which is audible as modulation even though every sample was
        # delivered and no underrun was counted.
        if self._total_slots:
            late_pct = 100.0 * self._late_slots / self._total_slots
            log.log(
                logging.WARNING if late_pct >= 10.0 else logging.INFO,
                "audio: %d/%d ring sub-writes late (%.1f%%) — spread %s",
                self._late_slots,
                self._total_slots,
                late_pct,
                "degraded toward bursts" if late_pct >= 10.0 else "held",
            )
        self._late_slots = 0
        self._total_slots = 0
        self._late_worst_s = 0.0
        # Host-DMA servo gap telemetry: confirms the closed loop locked the
        # ring gap near half a ring (4096) and never approached a lap (0) or an
        # underrun (RING_BUFFER_SIZE). The external drift probe can't see this
        # (it assumes a fixed wall-clock W), so this is the non-ears check.
        if self.servo.gap_last >= 0:
            log.info(
                "audio: host-DMA servo gap last=%d min=%d max=%d (target=%d, lap at 0/%d)",
                self.servo.gap_last,
                self.servo.gap_min,
                self.servo.gap_max,
                HOST_DMA_SERVO_TARGET_GAP,
                RING_BUFFER_SIZE,
            )
        self.servo.gap_min = -1
        self.servo.gap_max = -1
        self.servo.gap_last = -1

    def close(self) -> None:
        # AudioStreamer doesn't own its API — it shares the render path's
        # C64Backend (single-connection DMA constraint). The caller closes
        # the API after the final reset; closing it here would strand reset().
        self.stop()
