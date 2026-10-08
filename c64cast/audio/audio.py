"""NMI-driven SID DAC audio via the master volume register ($D418).

A 6502 routine at $C020 (audio_handlers.py) pulls one sample per NMI out of the
8 KB ring at $4000-$5FFF and writes it to $D418; CIA #2 Timer A sets the rate,
and this module's AudioStreamer feeds the ring over Socket DMA.

Ring bytes are 4-bit volume codes only on the `[audio].dac_curve = "linear"`
path. The default resolves to a Mahoney companding table, so the ring carries
the full 0..255 $D418 byte and every prefill, underrun pad and EOF pad here
writes ``self._neutral_byte`` rather than a literal.

See docs/architecture/audio.md#audiopy--audiostreamer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import logging
import queue
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, NamedTuple

import numpy as np

from c64cast._teardown import run_teardown_steps
from c64cast._wire_log import LogThrottle
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    CIA_TIMER_LATCH_MAX,
    KERNAL,
    REU,
    SID,
    VECTORS,
    actual_rate_for_latch,
    halt_quantum_bytes,
    kernal_cia1_latch,
)
from c64cast.hw.delivery import write_confirmed

from .audio_handlers import (
    AUDIO_HEALTH_LOG_INTERVAL_S,
    AUDIO_QUEUE_MAX_BLOBS,
    AUDIO_WRITE_RATE_SHARE,
    BACKPRESSURE_SPIN_S,
    CHUNK_SIZE,
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    INT16_FULL_SCALE,
    MAX_QUEUED_SAMPLES,
    NEUTRAL_SAMPLE,
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    PREBUFFER_CHUNKS,
    QUEUE_PUT_TIMEOUT_S,
    READ_PTR_LO_ADDR,
    REU_AUDIO_BASE,
    REU_AUDIO_DST_TRACKER_ADDR,
    REU_AUDIO_MAX_BYTES,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_GOVERNOR_MAX_CHUNK,
    REU_GOVERNOR_PUMP_OVERDRIVE,
    REU_IRQ_HANDLER,
    REU_IRQ_HANDLER_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_TRACKED,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_PUMP_BODY_SUBROUTINE,
    REU_MIC_RING_LEAD,
    REU_MIC_SIZE,
    REU_PUMP_BODY_SUBROUTINE,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS,
    REU_PUMP_BODY_SUBROUTINE_GOVERNOR,
    REU_PUMP_BODY_SUBROUTINE_GOVERNOR_CHUNK_OFFSETS,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_HANDLER_STUB,
    REU_PUMP_INITIAL_MARGIN,
    REU_PUMP_SETTLE_S,
    REU_PUMP_TICK_COUNTER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
    SAMPLE_TAP_SIZE,
    SID_DIGIBOOST_CONTROL,
    SID_DIGIBOOST_SR,
    SID_GATE_OFF,
    SID_MAHONEY_AD,
    SID_MAHONEY_CONTROL,
    SID_MAHONEY_RES_FILT,
    SID_MAHONEY_SR,
    WORKER_JOIN_TIMEOUT_S,
    encode_floats_to_dac,
    mic_ring_lead_ok,
    mic_ring_seed,
    patch_chunk_size,
    reu_pump_chunk_fits_ring,
    stomp_spans,
)
from .audio_rate import NmiTimer, RateServo
from .audio_servo import (
    HOST_DMA_SERVO_TARGET_GAP,
    NMI_RATE_LOOP_WARMUP_S,
    STALL_INSIDE_LEAD_SLACK,
    STALL_REANCHOR_READ_BUDGET_FRAC,
    stall_lapped,
    stall_reanchor,
)
from .dac_curves import NEUTRAL_INDEX, resolve_dac_curve
from .dsp import INPUT_CEILING, AudioDSP, DSPParams
from .mic_lead import (
    MicLeadServo,
    MicLeadShaper,
    MicRingGovernor,
    TrimWrite,
    read_mic_pump,
    reanchor_fill,
)
from .splice import FlushCut

log = logging.getLogger(__name__)


class StallInsideLead(NamedTuple):
    """What ``AudioStreamer._resync_after_stall`` returns when R never
    reached the write head: ``gap`` is how far W is still ahead of R, less
    R's travel during the read."""

    gap: int


# Any so Pyright doesn't flag every sd.XXX as an attribute of None; the
# intermediate name gives both branches one annotation, which mypy --strict
# needs because `import as` has already bound the name.
try:
    import sounddevice as _sounddevice

    sd: Any = _sounddevice
    AUDIO_AVAILABLE = True
except ImportError:
    sd = None
    AUDIO_AVAILABLE = False


class AudioInputDeviceError(RuntimeError):
    """A configured audio input that cannot be honored. Raised instead of
    opening the system default input in its place: on a laptop that is the
    built-in microphone, and the room it hears would go out through the C64."""


def resolve_audio_input_device(device: int | str) -> int:
    """Map a ``[audio].device`` value (int index, int-in-string, or a device
    *name substring*) to a sounddevice input index.

    Returns ``-1`` ("use the system default input") only for a negative or
    empty value — the one spelling that asks for the default. A name that
    matches no input-capable device, a name given without sounddevice, or a
    device list that cannot be read raises :class:`AudioInputDeviceError`
    rather than falling back to the default, the same fail-closed rule as the
    camera resolver (:func:`c64cast.control.camera.resolve_camera_index`).
    PortAudio exposes no USB VID:PID, so the only string form is a name
    substring, matched case-insensitively against *input-capable* devices
    (first match on a tie, with a warning). Names come from
    ``sd.query_devices()`` — the same listing ``c64cast --list-devices``
    prints."""
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
        raise AudioInputDeviceError(
            f"selecting an audio device by name ({token!r}) needs sounddevice (the 'mic' extra)"
        )

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
        raise AudioInputDeviceError(f"could not list audio devices to find {token!r}: {e}") from e

    if not matches:
        raise AudioInputDeviceError(
            f"no audio input device matched {token!r}; not using the system default "
            "input in its place. Run `c64cast --list-devices` to see names and "
            "indices, or set [audio].device = -1 to ask for the default."
        )
    if len(matches) > 1:
        # Device names come from USB/driver descriptors, so repr-quote them:
        # a name carrying CR/LF can otherwise forge --log-file lines.
        others = ", ".join(f"[{i}] {n!r}" for i, n in matches)
        log.warning(
            "audio device %r matched %d input devices (%s) — using [%d] %r; "
            "narrow it with a more specific name or an index",
            token,
            len(matches),
            others,
            matches[0][0],
            matches[0][1],
        )
    idx, name = matches[0]
    log.info("resolved audio device %r -> index %d (%r)", token, idx, name)
    return idx


def downmix_to_mono(indata: np.ndarray) -> np.ndarray:
    """Collapse a PortAudio capture block to a mono float array.

    sounddevice's numpy InputStream always hands back a (frames, channels)
    2-D block, but a raw-buffer or hand-fed 1-D array is already mono — and
    all three capture callbacks used to spell that fallback ``indata[:, 0]``,
    which can only raise IndexError on the 1-D input it exists for. One
    helper, so the three copies cannot drift apart again.

    It is also where a device's floats enter, so non-finite samples become
    0 / ±1 here and finite ones are held to ±`dsp.INPUT_CEILING`: a NaN from a
    misbehaving driver would otherwise latch the analyzer's level follower and
    the DSP chain's envelopes for the rest of the run, and the DAC encoder
    casts NaN to the bottom rail. A finite sample near float32's limit gets
    there too, overflowing to inf under the caller's sensitivity gain.
    """
    mono = indata.mean(axis=1) if indata.ndim > 1 else indata
    clean = np.asarray(np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0))
    return np.asarray(np.clip(clean, -INPUT_CEILING, INPUT_CEILING))


# Attempts per stage of AudioStreamer._install_tracked_pump before it gives up.
TRACKED_PUMP_INSTALL_TRIES = 3
# How long the entry upload waits after masking CIA #1 under a bank-swap
# dispatcher: longer than the chunked mhires dispatcher's ~18 ms run, so a CIA #1
# IRQ that was already asserted when the mask landed has been serviced (through
# the $C100 stub) before the entry bytes replace it.
TRACKED_PUMP_ENTRY_DRAIN_S = 0.03

# The DAC clock runs between chunk landings at the pace chunks land: the bytes
# landed over the last LANDING_PACE_WINDOW_S or so, measured landing to
# landing, once the window spans LANDING_PACE_MIN_INTERVALS intervals. The
# record keeps at most LANDING_PACE_MAX_LANDINGS landings, so a burst of tiny
# landings cannot grow it.
LANDING_PACE_WINDOW_S = 1.0
LANDING_PACE_MIN_INTERVALS = 2
LANDING_PACE_MAX_LANDINGS = 64


class PumpInstallError(RuntimeError):
    """The tracked REU pump could not be installed with every write confirmed
    delivered. Raised by ``AudioStreamer._install_tracked_pump``; the
    streamer has already put the C64 side back in a safe state."""


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
        dither_seed: int | None = None,
    ):
        # Shares the render path's C64Backend: the U64 DMA service takes one
        # connection at a time, so a second socket would TCP-accept and then
        # never see its IDENTIFY answered. SocketDMAClient is thread-safe and
        # audio ~8/sec + render ~30-60/sec stays under the ~200/sec ceiling.
        self.api = api
        self.sample_rate = sample_rate
        self.system = system
        self.dither_enabled = dither
        # One generator per streamer rather than numpy's process-wide one, so a
        # run's dither is a sequence this object owns and a capture can be
        # reproduced from the seed. np.random.Generator is not thread-safe and
        # both the host-DMA producer and the mic callback reach _encode_dac, so
        # the draw is taken under _dither_lock.
        self.dither_seed = secrets.randbits(64) if dither_seed is None else int(dither_seed)
        self._dither_rng = np.random.default_rng(self.dither_seed)
        self._dither_lock = threading.Lock()
        if dither:
            log.info("audio: TPDF dither seed=%d", self.dither_seed)
        self.digi_boost = digi_boost
        # An active curve is a uint8[256] amplitude→$D418 table (dac_curves.py);
        # "linear" leaves it None. It needs the Mahoney SID env that
        # _upload_nmi_and_buffers installs and is mutually exclusive with
        # digi_boost. A caller may pass dac_table already resolved (per-system
        # calibration, or cli's "auto"/"calibrated"), leaving dac_curve a label.
        table = dac_table if dac_table is not None else resolve_dac_curve(dac_curve)
        if table is not None and digi_boost:
            # Config validation should have caught this. The curve wins:
            # digi_boost's DC bias would corrupt the Mahoney levels.
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
        # Built per input source: line sources get the line chain here, the mic
        # start methods rebuild it with is_mic=True to activate AGC. Disabled
        # params give an identity chain that the encode paths short-circuit.
        self._dsp_params = dsp_params if dsp_params is not None else DSPParams()
        self._dsp = AudioDSP(self._dsp_params, sample_rate=sample_rate, is_mic=False)
        # REU-staged mode: a scene that knows the whole track upfront preloads
        # it into REU and lets a C64-side IRQ pump refill the ring, replacing
        # the host-DMA worker. False = start_for_external_source / start_mic.
        self.use_reu_pump = use_reu_pump
        # Uploads the skip-when-ahead governor pump so it self-throttles with
        # zero host bus writes; False uploads the open-loop one, which drifts
        # into an echo. Applies to both the plain handler and the tracked
        # $C180 body the bank-swap video path runs. The REU mic pump's $C180
        # body has no governed variant, so on that path the flag gates the
        # host-side MicRingGovernor instead.
        self.reu_pump_governor = reu_pump_governor
        # Closed-loop pacing for the host-DMA worker: read R once per chunk and
        # run servo_period's PI controller on the sleep so the ring gap locks
        # near half a ring instead of free-running into a ~26 s lap echo. Host
        # timing only, no C64 writes; the REU pump path is unaffected.
        self.host_dma_servo = host_dma_servo
        # Adaptive NMI-rate compensation: RateServo.update_rate_loop raises the
        # nominal rate until the bus-halt-throttled consumer lands at
        # sample_rate. Mutually exclusive with the static pitch_mult_* path —
        # set_nmi_latch_for_mode no-ops here so pitch_multiplier stays 1.0 and
        # the loop owns the latch from nominal.
        self.nmi_rate_adaptive = nmi_rate_adaptive
        # audio_rate.py collaborators: the CIA #2 latch machinery and the
        # worker-thread closed loops own their own state; the streamer drives.
        self.nmi = NmiTimer(self)
        self.servo = RateServo(self, self.nmi)
        # _reu_pump_start_time is what position_seconds() uses in REU mode: the
        # host never sees the samples, so the queue counter does not apply.
        self._reu_pump_armed = False
        # A failed install's $0314 restore that never confirmed: stop() owes
        # it even though no pump armed (_unwind_pump_install).
        self._irq_vector_restore_owed = False
        # A CIA #1 unmask that never confirmed after a masked $C100 write: the
        # next dispatcher entry upload, pump arm or stop() owes it
        # (_cia1_unmask_step).
        self._cia1_unmask_owed = False
        self._reu_pump_start_time = 0.0
        self._reu_pump_total_samples = 0
        # The matched CIA #1 pump latch this run derived; 0 before any pump
        # arms. Leave it 0 rather than seeding a nominal latch, so a path that
        # forgets to derive one reads as unset instead of plausibly wrong.
        self._reu_cia1_latch_nominal = 0
        # Host's REU write position, wrapping at REU_MIC_SIZE; 0 until
        # _start_mic_for_reu_pump seeds REU_MIC_BOOTSTRAP_BYTES. The error count
        # is this pump's only telemetry — its REUWRITEs go out from the
        # PortAudio callback, which has none of the worker counters below.
        self._mic_reu_write_pos = 0
        self._mic_reu_write_errors = 0
        # The closed loop on that position's lead over the pump (#560): the
        # servo thread measures, the shaper applies its drop fraction on the
        # callback. Both None outside a REU mic session.
        self._mic_lead: MicLeadServo | None = None
        self._mic_shaper: MicLeadShaper | None = None
        # The fence on the mic ring governor's CIA #1 writes: a write goes out
        # under the lock only while its token is current, and the pump disarm
        # bumps the token under the same lock before it restores the kernal
        # latch, so no trim can land after that restore.
        self._pump_trim_lock = threading.Lock()
        self._pump_trim_token = 0
        # Producer missed the pace deadline. full_underruns: the queue was
        # empty, so the whole chunk is NEUTRAL (an audible click at
        # chunk_period). partial_underruns: NEUTRAL padding at the tail only.
        self._full_underruns = 0
        self._partial_underruns = 0
        # Drip-schedule telemetry: a sub-write that reaches its slot late goes
        # out immediately, bunching the rest of the chunk at the end of the
        # period and collapsing back toward the one-write cadence the split
        # exists to escape (4-20 Hz modulation 0.65 spread vs 8.33 bursted).
        # Underrun counts cannot see that, so score the spread from here.
        self._late_slots = 0
        self._total_slots = 0
        self._late_worst_window_s = 0.0
        # Health-line window state: last emission's wall-clock, the counter
        # snapshot taken with it, and the servo gap's excursion since.
        self._health_last_log = 0.0
        self._health_mark: tuple[int, int, int, int] = (0, 0, 0, 0)
        # Each item is (flush epoch, pre-encoded bytes blob, one byte per
        # sample), so the queue costs one lock per chunk rather than per
        # sample. q.qsize() therefore counts blobs: backpressure reads
        # self._queued_samples, and q.full() is unused because the cap below
        # is in bytes, not items. The epoch is the one the producer pushed in,
        # which the worker checks blob by blob (_collect_until).
        self.q: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=AUDIO_QUEUE_MAX_BLOBS)
        self._queued_samples = 0
        # Queued samples in the chunk the worker is writing to the ring: they
        # passed its flush-epoch check, so they play ahead of any post-splice
        # sample, and cut()'s anchor counts them until they land. Set by the
        # worker and read by cut(), each with the epoch under _count_lock.
        self._in_flight_samples = 0
        # cut() and stop() bump _flush_epoch; _encode_and_enqueue and _worker
        # each carry it and discard audio held across a change, so neither a
        # seek/loop/pause splice nor a scene cut-over can leak pre-splice samples
        # from a blocked pusher, the queue or the worker's hand. _count_lock pairs
        # the _pushed_count/_queued_samples mutations so position_seconds()
        # (= pushed - queued) stays invariant across a discard. _stomp_requested asks the worker (which owns
        # write_addr) to NEUTRAL-fill the unplayed ring, keeping ring DMA off the
        # playlist thread and away from the servo.
        self._flush_epoch = 0
        self._count_lock = threading.Lock()
        self._stomp_requested = False
        # MAX_QUEUED_SAMPLES caps the buffer so a stalled consumer cannot
        # accumulate a wall of stale audio.
        self._max_queued_samples = MAX_QUEUED_SAMPLES
        # Set by end_input() once the producer has pushed its last sample for
        # now. A producer that ends short of the prebuffer would otherwise
        # leave the worker waiting for it forever, the NMI never started and
        # the clip never heard. Cleared by every _start_worker and by the next
        # accepted push (a video's demuxer pushes again after a seek back), so
        # it is not a "track finished" signal.
        self._input_ended = False
        self.running = False
        # Bumped by every _start_worker and by stop(); a worker exits when it
        # stops matching.
        self._worker_generation = 0
        self.chunk_size = CHUNK_SIZE
        self.sensitivity = 1.0
        self.noise_gate = 0.05
        self.mic_stream: Any = None
        self._worker_thread: threading.Thread | None = None
        # start_listen(): capture-only, feeding the analysis sink and nothing
        # else. stop() short-circuits its DAC teardown when set; the other
        # start_* methods clear it, since the streamer outlives a scene.
        self._listen_mode = False

        # Audio-master clock bookkeeping (used by PyAV-driven scenes).
        self._pushed_count = 0
        # Where the pad the worker landed sits in the ring's byte stream. The
        # servo's gap counts pad and content alike, and position_seconds() takes
        # out the pad still inside it wherever it sits: a dry tail would
        # otherwise stop the clock a ring short of the last sample, and pad
        # followed by content would step it backward once the content landed.
        # The worker writes them and readers take them, both under the lock,
        # which also makes the high-water mark position_seconds() never drops
        # below a single read-modify-write.
        self._ring_pad_lock = threading.Lock()
        self._ring_landed_total = 0
        self._ring_pads: deque[tuple[int, int]] = deque()
        self._position_floor = 0.0
        # When the clock's last landed count was taken (monotonic): the
        # consumer's start, then each landing. None until the consumer starts.
        self._ring_landed_at: float | None = None
        # (monotonic time, landed total) at each paced landing in the pace
        # window, and the pace the window last measured, which stands while a
        # restarted window fills. The speed the clock runs at between landings.
        self._landings: deque[tuple[float, int]] = deque(maxlen=LANDING_PACE_MAX_LANDINGS)
        self._landing_pace = 0.0
        # One record per interval however often the link stalls.
        self._stall_log = LogThrottle(log)

        # Sample tap for FFT overlays. Lockless write from input threads,
        # locked read from the render thread — readers tolerate a torn frame
        # because the next FFT is ~16 ms away.
        self._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
        self._tap_write = 0
        self._tap_lock = threading.Lock()

        # PRE-DSP analysis sink (audio_features.AnalysisTap.push), set by a
        # reactive source at setup() and cleared at teardown(). Distinct from
        # the tap above and fed before the gate and _apply_dsp, because AGC +
        # compressor + limiter flatten the transients an onset detector reads.
        self._analysis_sink: Callable[[np.ndarray], None] | None = None
        self._analysis_sink_failed = False

    @property
    def analysis_sink(self) -> Callable[[np.ndarray], None] | None:
        return self._analysis_sink

    @analysis_sink.setter
    def analysis_sink(self, sink: Callable[[np.ndarray], None] | None) -> None:
        # The session builds one streamer and every reactive source installs
        # its own analyzer on it at setup(), so a newly installed sink gets its
        # first failure logged even if an earlier one already failed.
        if sink is not None:
            self._analysis_sink_failed = False
        self._analysis_sink = sink

    @property
    def dac_curve(self) -> np.ndarray | None:
        """Active Mahoney companding table (uint8[256] amplitude→$D418), or
        None for the legacy linear 4-bit path. Read by scenes doing offline
        REU pre-encoding so their bytes match the realtime callback paths."""
        return self._dac_curve

    def _upload_nmi_and_buffers(self) -> None:
        self.api.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", NMI_ROUTINE)
        self.api.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", bytes([self._neutral_byte] * RING_BUFFER_SIZE)
        )
        # Disable CIA #2 IRQs + stop Timer A, then point the NMI vector at
        # $C020. _arm_nmi_once re-lands it when the timer arms, so a dropped
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

    def _release_sid_voice_gate(self, voice: int) -> None:
        base = SID.voice_base(voice)
        self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{SID_GATE_OFF:02X}")

    def _release_sid_gates(self) -> None:
        """Release the gate on all 3 voices, undoing whichever DAC bias set it.

        One teardown step per voice: a voice left gated with its TEST bit
        locked holds the SID mixer at a DC bias, so a failed write must not
        cost the other two."""
        run_teardown_steps(
            log,
            type(self).__name__,
            [
                (f"SID voice {v} gate release", functools.partial(self._release_sid_voice_gate, v))
                for v in range(SID.N_VOICES)
            ],
        )

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

    @property
    def effective_rate(self) -> float:
        """The rate the C64 NMI consumer *actually* runs at, in Hz.

        `sample_rate` is a request: the CIA #2 Timer A period is an integer
        cycle count, so the achievable rates are the grid PHI2/(latch+1) and
        this is the nearest point on it. This — not `sample_rate` — is the
        timebase for producer pacing, the adaptive loop's target,
        `position_seconds()`, and the rate file paths resample content to.

        Excludes the mic capture-device open rate and the DSP filter rates, and
        ignores `pitch_mult_*` and the adaptive loop, which are deliberate
        offsets from nominal rather than corrections to it.
        `UltimateAudioSampler` exposes an `effective_rate` too, so a scene can
        read either sink the same way.

        See docs/architecture/audio.md#sample_rate-is-a-request-effective_rate-is-what-you-get.
        """
        return self.nmi.effective_rate

    def _collect_until(
        self,
        chunk_buf: bytearray,
        n: int,
        leftover: bytes,
        deadline: float,
        *,
        generation: int,
        epoch: int,
    ) -> tuple[int, bytes, int]:
        """Fill ``chunk_buf`` from ``leftover`` then the queue until it holds
        ``chunk_size`` bytes or ``deadline`` passes.

        ``chunk_buf[:n]`` is all queued audio (pad is added after the
        collect), and it and ``leftover`` were pushed in flush epoch
        ``epoch``. Every blob carries the epoch its producer pushed it in. A
        blob from an epoch a flush has since retired is discarded. A current
        blob arriving while the chunk holds audio from an earlier epoch means
        a flush landed during the collect: the chunk's audio is pre-splice and
        is discarded, and the chunk restarts from that blob. Judged by an epoch
        read once per iteration instead, a collect that straddled a flush
        dropped the post-splice audio it took after it along with the rest.

        A worker retired while parked in a ring write takes nothing more from
        the queue: those blobs are the next activation's, already counted in
        its queued count, and the retired worker's discard is fenced out of
        that count (see :meth:`_discard_unpushed`), so a blob it took would
        stay counted as queued for the rest of the run.

        Returns ``(new_n, new_leftover, new_epoch)``. Split out of the
        worker so collection can be resumed across several short deadlines —
        the drip schedule calls it once per quantum slot, which is what lets the
        next chunk be gathered *while* the current one is being written out.
        """
        size = self.chunk_size
        if leftover and n < size:
            take = min(len(leftover), size - n)
            chunk_buf[n : n + take] = leftover[:take]
            n += take
            leftover = leftover[take:]
        while n < size and not leftover and self.running and generation == self._worker_generation:
            remaining = deadline - time.monotonic()
            # Past the deadline, still take what is already waiting rather than
            # reporting an underrun over a full queue: one over-long sub-write
            # expires every later drip slot, and without this drain that
            # starves collection and NEUTRAL-pads every chunk.
            try:
                tag, piece = self.q.get(timeout=remaining) if remaining > 0 else self.q.get_nowait()
            except queue.Empty:
                break
            if not piece:
                # end_input()'s wake-up: nothing more is coming to wait for.
                # One left over from an earlier producer, whose end_input()
                # raced its teardown's drain, or from an earlier pass of a
                # video's demuxer that has pushed again since, is not this
                # input's end.
                if self._input_ended:
                    break
                continue
            if tag != self._flush_epoch:
                self._discard_unpushed(len(piece), generation=generation)
                continue
            if n and tag != epoch:
                self._discard_unpushed(n, generation=generation)
                n = 0
            epoch = tag
            take = min(len(piece), size - n)
            chunk_buf[n : n + take] = piece[:take]
            n += take
            if take < len(piece):
                leftover = piece[take:]
        return n, leftover, epoch

    def _drip_chunk(
        self,
        payload: bytes,
        addr: int,
        chunk_buf: bytearray,
        leftover: bytes,
        base_time: float,
        chunk_period: float,
        current: Callable[[], bool],
        *,
        generation: int,
        epoch: int,
    ) -> tuple[int, bytes, int]:
        """Write `payload` into the ring as sub-NMI-period pieces spread evenly
        across `chunk_period`, collecting the *next* chunk in the gaps between
        them. Returns that collection's ``(n, leftover, epoch)``; ``epoch`` is
        that of ``leftover`` going in (see :meth:`_collect_until`).

        Two separate effects, and it is easy to bank only the first. Splitting
        keeps each write's CPU halt inside one NMI period, so it cannot swallow
        a second CIA #2 underflow and lose the tick. Spreading keeps those halts
        from re-bunching into one low-frequency event, in the 4-20 Hz band
        modulation sensitivity peaks in. HW-measured at the 12 kHz NTSC default
        against a 376 Hz carrier: 27.3 Hz of FM deviation for one 1024-byte
        write, 10.8 Hz for 64-byte writes back-to-back, 5.3 Hz spread across the
        period. See docs/architecture/audio.md#the-ring-write-is-split-and-spread.

        Collecting between the writes rather than before them is what keeps the
        producer's full period of collect time — the writes now occupy the
        period that used to be spent asleep waiting for the pace deadline.

        ``current`` is the worker's fence: each piece write can park past
        stop()'s join, and a worker superseded meanwhile neither collects from
        the next session's queue nor writes the rest of the chunk into the
        ring that session is priming. ``generation`` is the value that fence
        tests, handed on to :meth:`_collect_until`.
        """
        quantum = self._halt_quantum() or len(payload)
        slots = max(1, (len(payload) + quantum - 1) // quantum)
        slot_period = chunk_period / slots
        n = 0
        for i in range(slots):
            if not current():
                break
            slot_deadline = base_time + i * slot_period
            n, leftover, epoch = self._collect_until(
                chunk_buf, n, leftover, slot_deadline, generation=generation, epoch=epoch
            )
            # Retired between slots: the rest of the payload is not this
            # worker's to write into a ring the next activation owns.
            if not current():
                break
            sleep_s = slot_deadline - time.monotonic()
            self._total_slots += 1
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                self._late_slots += 1
                self._late_worst_window_s = max(self._late_worst_window_s, -sleep_s)
            piece = payload[i * quantum : (i + 1) * quantum]
            if not piece:
                break
            self.api.write_memory_file(f"{addr:04X}", piece)
            addr += len(piece)
            if addr >= RING_BUFFER_END:
                addr = RING_BUFFER_ADDR
        return n, leftover, epoch

    def stats(self) -> dict[str, int | float]:
        """Snapshot of the pacing/underrun telemetry counters — the public
        seam for tests and reporting. The push/consume pair is read under
        _count_lock so position bookkeeping stays coherent; the worker-owned
        counters are plain reads (single-writer, torn reads impossible on
        ints). The counters themselves stay private so their single-owner
        write discipline is visible at the attribute level.

        ``running`` is the liveness flag the worker's crash handler clears, so
        a caller can tell "audio was set up and the worker died" from "audio is
        streaming" without reaching into the thread.

        Every other counter is cumulative for the run (cleared in stop()) —
        except ``late_worst_window_s``, which _maybe_log_health zeroes on every
        health line. The key says ``window`` so it can't be read as a run
        total beside its neighbors."""
        with self._count_lock:
            pushed = self._pushed_count
            queued = self._queued_samples
        return {
            "pushed_samples": pushed,
            "queued_samples": queued,
            "full_underruns": self._full_underruns,
            "partial_underruns": self._partial_underruns,
            "late_slots": self._late_slots,
            "total_slots": self._total_slots,
            "late_worst_window_s": self._late_worst_window_s,
            "running": self.running,
        }

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
            self._late_worst_window_s * 1000.0,
            d_full,
            d_part,
            d_slots / dt,
            self._halt_quantum(),
            r,
            self.nmi.latch,
        )
        self._health_last_log = now
        self._health_mark = mark
        servo.reset_health_window()
        self._late_worst_window_s = 0.0

    def _halt_quantum(self) -> int:
        """Bytes per ring write, sized so each write's CPU halt fits inside one
        NMI period.

        Derived from the live latch rather than a constant, so it tracks the
        configured rate, PAL vs NTSC, and any pitch-multiplier retune — the
        period it has to fit inside is exactly ``latch + 1`` cycles.

        That halt-derived size is then floored by what the link can actually
        carry, because the quantum sets the write *rate* (chunk_size/quantum
        writes per chunk period) on a socket the render thread shares. Asking
        for more writes than the link sustains runs each past its slot, starving
        collection and NEUTRAL-padding chunks over a full queue: HW-measured, a
        65-byte quantum (188 writes/s) produced 1744 full underruns and lapped
        the ring. Backing off costs little — 4-20 Hz modulation is 1.96 at 128 B
        against 2.41 at 64 B — since what matters is clearing that band at all.
        """
        period_cycles = (self.nmi.latch or self.nmi.compensated_latch()) + 1
        quantum = halt_quantum_bytes(period_cycles)
        # Straight through, no getattr: both names are declared, so a rename
        # fails type-checking here instead of silently yielding max_hz = None
        # and dropping the floor this method's docstring depends on.
        max_hz = self.api.profile.max_write_rate_hz
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

        The host-DMA servo locks playback speed to the NMI consumer R, which
        loses ~1-14% of its ticks to video DMA bus-halts, so playback comes out
        slow. ``calibration`` maps a display-mode name to a playback-rate
        multiplier (``[audio] pitch_mult_*``); >1.0 plays that much faster to
        cancel the slowdown. Call it at scene setup when the display mode
        changes — the servo then tracks the new R on its own.

        Rate and latch are inversely related (the NMI period is latch+1 cycles),
        so the nominal period is divided by the multiplier:

            period = (nominal_latch + 1) / multiplier;  latch = period − 1

        Only applies under the host-DMA servo with a running worker; the REU pump
        has its own C64-side rate governor and open-loop needs no adjustment.
        """
        if not self.host_dma_servo or not self._worker_thread:
            # REU pump has its own governor; open-loop doesn't need adjustment.
            return
        if self.nmi_rate_adaptive:
            # Adaptive mode owns the latch, so the static multiplier stays 1.0;
            # the mode is still recorded so the loop seeds from a close per-mode
            # estimate instead of gliding, and re-seeds on a mid-stream change.
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
        # Stashed for NmiTimer.start: at scene setup the worker is usually still
        # prebuffering, so the timer picks the value up when it first arms.
        self.nmi.pitch_multiplier = multiplier
        requested = self.nmi.requested_compensated_latch()
        if requested != self.nmi.clamp_latch(requested):
            log.warning(
                "audio: pitch multiplier %g for %s needs CIA #2 latch %d, outside the "
                "%d..%d the NMI handler budget and the 16-bit timer allow — arming %d",
                multiplier,
                display_mode,
                requested,
                self.nmi.ceiling_latch(),
                CIA_TIMER_LATCH_MAX,
                self.nmi.clamp_latch(requested),
            )
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

    def _start_worker(self) -> threading.Thread:
        """Start a fresh ring-feeding worker and return its thread.

        The generation bump is what fences a previous worker that outlived
        stop()'s bounded join: it captures the value at start and exits as soon
        as it no longer matches, so it cannot be resurrected by this method
        setting ``running`` back to True (see :meth:`_worker`)."""
        with self._ring_pad_lock:
            self._worker_generation += 1
            generation = self._worker_generation
            self._clear_ring_clock_locked()
        with self._count_lock:
            self._in_flight_samples = 0
        self._input_ended = False
        thread = threading.Thread(
            target=self._worker,
            args=(generation,),
            daemon=True,
            name="audio-worker",
        )
        thread.start()
        return thread

    def _consume_queued(self, n: int, *, generation: int) -> None:
        """Account for ``n`` queued bytes that have now LANDED in the ring.

        Only the queued count drops, so ``position = pushed - queued`` advances
        by exactly ``n`` — the audio clock moves because the audio played. A
        worker that outlived stop()'s bounded join counts nothing: its bytes
        are not in the next activation's counts (see :meth:`_note_ring_landed`)."""
        if not n:
            return
        with self._count_lock:
            if generation != self._worker_generation:
                return
            self._queued_samples = max(0, self._queued_samples - n)
            self._in_flight_samples = 0

    def _claim_ring_write(self, epoch: int, n: int, *, generation: int) -> bool:
        """Whether a chunk captured at ``epoch`` may still go to the ring, and
        if so record its ``n`` queued samples as in flight until
        :meth:`_consume_queued` counts them landed. Under ``_count_lock``,
        where cut() bumps the epoch and reads its anchor: a check made
        unlocked could pass just before a cut that then anchored without
        the chunk, one chunk ahead of where the target's first sample plays."""
        with self._count_lock:
            if self._flush_epoch != epoch:
                return False
            if generation == self._worker_generation:
                self._in_flight_samples = n
            return True

    def _discard_unpushed(self, n: int, *, generation: int | None = None) -> None:
        """Account for ``n`` bytes dropped before they reached the ring.

        Both counts drop — the paired subtract — so the bytes read as never
        pushed and ``position = pushed - queued`` is exactly unchanged across
        the drop. That invariant is what lets flush() splice the transport
        without moving the audio clock, and the only difference from
        :meth:`_consume_queued` is whether the bytes were played: getting the
        two the wrong way round shifts A/V sync at every splice. Both live
        here, once, rather than hand-written at each site.

        ``generation`` is the calling worker's, fenced as in
        :meth:`_consume_queued`."""
        if not n:
            return
        with self._count_lock:
            if generation is not None and generation != self._worker_generation:
                return
            self._queued_samples = max(0, self._queued_samples - n)
            self._pushed_count = max(0, self._pushed_count - n)

    def _count_silence(self, n: int, *, generation: int) -> None:
        """Count ``n`` bytes of pad as pushed and queued, as if a producer had
        pushed silence: from here they are queued audio in the chunk in hand,
        landed, claimed or discarded like any other. Fenced as in
        :meth:`_consume_queued`."""
        with self._count_lock:
            if generation != self._worker_generation:
                return
            self._pushed_count += n
            self._queued_samples += n

    def _neutral_fill_ring(self, addr: int, n: int) -> None:
        """NEUTRAL-fill ``n`` bytes of ring from ``addr``.

        The span never straddles ``RING_BUFFER_END``: every ring write is
        chunk-aligned (the worker pads every short chunk) and chunk_size
        divides RING_BUFFER_SIZE exactly."""
        self.api.write_memory_file(f"{addr:04X}", bytes([self._neutral_byte]) * n)

    def _worker(self, generation: int) -> None:
        """Drain the bytes-blob queue into the C64 ring buffer, paced to
        NMI consumption.

        ``generation`` is the value of ``_worker_generation`` this worker was
        started with, and the loop exits as soon as it no longer matches. The
        shared ``running`` flag is not enough on its own: stop()'s join is
        bounded, so a worker parked in a ring write on a stalled link can
        outlive it, and the next scene's start_* sets ``running`` back to True —
        which the orphan would read as "keep going", leaving two workers
        dripping into one ring with independent write cursors.

        Pacing is required because the producer is not always the rate
        authority: PyAV's demuxer decodes far faster than real time, a mic
        producer is naturally real-time, and the worker cannot tell which it
        has. Per iteration it collects chunk_size bytes by the next pace
        deadline, ships a NEUTRAL chunk if that expires with nothing, pads a
        partial chunk to keep the pace math in chunk-sized steps, sleeps to the
        pace point, and writes.

        The schedule is strict absolute — `next_write_time + chunk_period`,
        never snapped forward on an ordinary overrun. With `host_dma_servo` on
        (default) the increment is `servo.next_pace_increment(...)` instead of
        the bare `chunk_period`, still added to the absolute time, and clamped
        to [0.5, 1.5]·chunk_period so one bad reading cannot stall or sprint
        the schedule. The one exception is a stall longer than the ring lead
        (a DMA link that blocked or redialed): catching that up would sprint
        writes until W lapped R, so the worker re-anchors instead — see
        :meth:`_resync_after_stall`.

        See docs/architecture/audio.md#the-worker-thread-and-its-pacing."""

        def current() -> bool:
            return not self._superseded(generation)

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
            # CIA latch actually yields, and pacing to the request would hand the
            # servo a standing offset to absorb before it could correct anything.
            chunk_period = self.chunk_size / self.effective_rate
            prebuffer_bytes = PREBUFFER_CHUNKS * self.chunk_size
            # Behind schedule by more than this, the consumer has played the
            # whole lead and is replaying the ring's previous lap.
            stall_resync_s = HOST_DMA_SERVO_TARGET_GAP / self.effective_rate
            # Pace + collect deadlines. Zero until NMI starts.
            next_write_time = 0.0
            # Last iteration's chunk, dripped out over this one: one chunk_period
            # of latency for collect/write overlap. The queued-sample count drops
            # only once the bytes land, so position_seconds() stays true.
            pending: bytes | None = None
            pending_addr = RING_BUFFER_ADDR
            pending_from_queue = 0
            pending_pad = 0
            pending_epoch = 0

            # The flush epoch of the chunk being collected: that of the blobs
            # in it (see _collect_until), else the current one, so a chunk of
            # nothing but pad is claimed against the epoch it was padded in.
            epoch = 0

            while current():
                if not leftover:
                    epoch = self._flush_epoch
                pace_deadline = next_write_time if prebuffered else 0.0

                n = 0

                if prebuffered and pending is not None:
                    if not self._claim_ring_write(
                        pending_epoch, pending_from_queue, generation=generation
                    ):
                        # Splice landed after this chunk left the queue: drop it
                        # unplayed, with the paired subtract used below.
                        self._discard_unpushed(pending_from_queue, generation=generation)
                        # write_addr passed this chunk at hand-off and
                        # pending_addr is already beyond it, so nothing would
                        # ever write [pending_addr, +len) and the NMI would
                        # replay it from a lap ago as an echo where the splice
                        # promises silence. Filling it also keeps w_head honest,
                        # so the servo isn't handed a W a chunk behind the head.
                        self._neutral_fill_ring(pending_addr, len(pending))
                        if not current():
                            break
                        self._note_ring_landed(generation, len(pending), len(pending), paced=False)
                        w_head = pending_addr + len(pending)
                        if w_head >= RING_BUFFER_END:
                            w_head -= RING_BUFFER_SIZE
                        pending = None
                        pending_from_queue = 0
                    else:
                        # Pause fast mute: NEUTRAL-fill the unplayed ring ahead of
                        # the read head. Stomping from pending_addr keeps the
                        # chunk about to be written at the front of the span.
                        if self._stomp_requested:
                            self._stomp_ring(pending_addr, current)
                        n, leftover, epoch = self._drip_chunk(
                            pending,
                            pending_addr,
                            chunk_buf,
                            leftover,
                            pace_deadline,
                            chunk_period,
                            current,
                            generation=generation,
                            epoch=epoch,
                        )
                        # Every ring write can park past stop()'s join; past
                        # it, the counters below are the next session's.
                        if not current():
                            break
                        self._note_ring_landed(generation, len(pending), pending_pad)
                        self._consume_queued(pending_from_queue, generation=generation)
                        w_head = pending_addr + len(pending)
                        if w_head >= RING_BUFFER_END:
                            w_head -= RING_BUFFER_SIZE
                        pending = None
                        pending_from_queue = 0

                # Read before the collect: end_input() follows the producer's
                # last push, so a collect that comes back empty after it was
                # seen leaves nothing behind in the queue. Read with the epoch
                # it belongs to, under the lock cut() clears it under.
                with self._count_lock:
                    input_ended = self._input_ended
                    ended_epoch = self._flush_epoch
                if pending is None and n < self.chunk_size:
                    # Priming, or the drip's interleaved slots did not fill the
                    # chunk: fall back to a blocking collect on the same deadline.
                    # A priming collect after the producer ended takes only what
                    # is queued: nothing more is coming to wait for.
                    collect_deadline = (
                        pace_deadline
                        if prebuffered
                        else time.monotonic() + (0.0 if input_ended else chunk_period)
                    )
                    n, leftover, epoch = self._collect_until(
                        chunk_buf, n, leftover, collect_deadline, generation=generation, epoch=epoch
                    )

                if not current():
                    break
                # A collect that crossed a cut holds the post-splice input, and
                # the end read before it was the pre-splice one: its pad
                # counted as silence put the clock that far ahead of the sound.
                # A chunk of an older epoch than the end is retired and dropped
                # at its claim: read as not ended, its pad was an underrun.
                input_ended = input_ended and epoch <= ended_epoch
                # Everything collected so far is queued audio; pad comes next.
                from_queue = n

                pad = 0
                # A pad is an underrun only while the NMI is reading and more
                # input is due. After end_input() the collect above came up
                # short because the queue is drained: the pads that follow
                # are the silence the ring plays out after the last sample.
                stalled = prebuffered and not input_ended
                if n == 0:
                    if not prebuffered and not input_ended:
                        # Idle: no producer data, no NMI to feed.
                        continue
                    # Real underrun: refresh ring with silence. Or, priming, a
                    # producer that ended short of the prebuffer: fill the rest
                    # of it with silence so the NMI starts on what it pushed,
                    # behind the same lead as any other start. That includes a
                    # producer that pushed nothing, such as a video whose
                    # audio stream holds no samples: its picture waits on
                    # this clock.
                    chunk_buf[:] = bytes([self._neutral_byte] * self.chunk_size)
                    n = pad = self.chunk_size
                    if stalled:
                        self._full_underruns += 1
                elif n < self.chunk_size:
                    # Pad every short chunk, including during the prebuffer fill:
                    # a raw-length write takes write_addr off the chunk grid
                    # RING_BUFFER_SIZE is a multiple of, and both wrap guards test
                    # the address only after the increment, so the straddling
                    # write's tail lands in $6000+ (waveform.py's bitmap). See
                    # audio.md#the-worker-thread-and-its-pacing. Pad bytes are NOT
                    # counted in from_queue.
                    pad = self.chunk_size - n
                    chunk_buf[n : n + pad] = bytes([self._neutral_byte]) * pad
                    n = self.chunk_size
                    if stalled:
                        # Consumption-phase only: with no NMI reading yet, a short
                        # prebuffer collect is a slow start, not an underrun.
                        self._partial_underruns += 1
                if pad and input_ended:
                    # Silence after the input ended: counted as pushed audio,
                    # so the clock runs on through it. Left as pad, the clock
                    # stopped at the last sample, and a video whose sound ends
                    # before its picture held that frame for good.
                    self._count_silence(pad, generation=generation)
                    from_queue += pad
                    pad = 0

                # A splice landed while this chunk was in hand: from_queue +
                # leftover are pre-splice (leftover is the rest of the chunk's
                # last blob, so of its epoch), so count them as never pushed
                # (the paired subtract holds position) and skip the write and pace.
                # A chunk handed off below is claimed when it is written, at the
                # top of the loop; a priming chunk is written here.
                if not self._claim_ring_write(
                    epoch, 0 if prebuffered else from_queue, generation=generation
                ):
                    self._discard_unpushed(from_queue + len(leftover), generation=generation)
                    leftover = b""
                    continue

                # Pause fast mute, for a request the pending path above did not
                # take: no chunk was pending (the first iteration after the arm,
                # or one after a splice dropped it), or the request arrived
                # during that path's drip and collect. That path stomps from
                # the chunk about to go out; this one from the chunk in hand.
                if self._stomp_requested and prebuffered:
                    self._stomp_ring(write_addr, current)

                if prebuffered:
                    # Hand off to the next iteration, which drips this into the
                    # ring while collecting its successor. Nothing is written
                    # here, so the queued-sample count stays put.
                    sleep_s = pace_deadline - time.monotonic()
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    pending = bytes(chunk_buf[:n])
                    pending_addr = write_addr
                    pending_from_queue = from_queue
                    pending_pad = pad
                    pending_epoch = epoch
                    write_addr += n
                    if write_addr >= RING_BUFFER_END:
                        write_addr = RING_BUFFER_ADDR
                    next_write_time += self.servo.next_pace_increment(w_head, chunk_period, current)
                    # The pacing read can park past stop()'s join like a ring
                    # write: past it, the resync and the health line below
                    # would act on the next session's ring and counters.
                    if not current():
                        break
                    lag = time.monotonic() - next_write_time
                    if lag > stall_resync_s:
                        outcome = self._resync_after_stall(lag, generation, w_head)
                        # A superseded resync returns None, as an unreadable R
                        # does; past it, the health line is the next session's.
                        if not current():
                            break
                        next_write_time = time.monotonic()
                        if isinstance(outcome, StallInsideLead):
                            # Owe only what refills the target lead, not the
                            # whole stall: catching all of it up from a lead
                            # longer than the target (a slow consumer's)
                            # drove W more than a ring ahead of R.
                            short = max(0, HOST_DMA_SERVO_TARGET_GAP - outcome.gap)
                            next_write_time -= short / self.effective_rate
                        elif outcome is not None:
                            pending_addr = outcome
                            write_addr = outcome + n
                            if write_addr >= RING_BUFFER_END:
                                write_addr = RING_BUFFER_ADDR
                    self._maybe_log_health(time.monotonic())
                    continue

                # Prebuffer fill: the NMI is not consuming yet, so there is no
                # halt to hide from and one unsplit write primes the ring fastest.
                self.api.write_memory_file(f"{write_addr:04X}", bytes(chunk_buf[:n]))
                # Retired while parked in that write: the counts below are the
                # next activation's, and the NMI start would reprogram the timer
                # under it.
                if not current():
                    break
                self._note_ring_landed(generation, n, pad)
                self._consume_queued(from_queue, generation=generation)
                write_addr += n
                if write_addr >= RING_BUFFER_END:
                    write_addr = RING_BUFFER_ADDR
                w_head = write_addr

                bytes_prebuffered += n
                if bytes_prebuffered >= prebuffer_bytes:
                    self.nmi.start(adaptive=self.nmi_rate_adaptive)
                    # The arm reads R and writes the CIA, and can park too.
                    if not current():
                        break
                    prebuffered = True
                    # R only becomes meaningful now that the NMI consumes: start
                    # the servo integrator and rate loop clean (the warm-up gate
                    # arms inside reset_for_consumer_start).
                    self.servo.reset_for_consumer_start(bytes_prebuffered)
                    self._mark_ring_clock()
                    # Health windows measure the consuming phase only — the
                    # prebuffer fill writes unsplit and has no slots to be late.
                    self._health_last_log = 0.0
                    # Pace one chunk_period out so the PREBUFFER_CHUNKS slack
                    # holds instead of being eaten immediately.
                    next_write_time = time.monotonic() + chunk_period
        except Exception:
            # Clearing `running` is what stats()["running"] reports, so a caller
            # can tell a dead worker from a live one rather than inferring it
            # from silence. A superseded worker's write most often ends this
            # way — a parked write on a stalled link raises rather than
            # returns — and by then `running` is the next session's.
            log.exception("audio worker crashed")
            # A retired worker's late write failing is not the next
            # activation's crash: clearing `running` would stop that one.
            if current():
                self.running = False
            # The chunk whose write raised never lands, so nothing would count
            # it landed: left in flight, every later flush anchored it late.
            with self._count_lock:
                if generation == self._worker_generation:
                    self._in_flight_samples = 0

    def _resync_after_stall(
        self, lag: float, generation: int, w_head: int
    ) -> int | StallInsideLead | None:
        """Recover from a worker stall longer than the ring lead; returns the
        chunk-grid address the next chunk lands at, None when R cannot be
        read promptly (the schedule is then only snapped forward), or a
        ``StallInsideLead`` when R turns out not to have reached ``w_head``
        (the live write head) after all.

        The trigger is ``HOST_DMA_SERVO_TARGET_GAP`` of lag, but the lead the
        stall ate can be longer: the ≈5 KiB the consumer starts behind before
        the servo pulls it in, or an open-loop lead grown by the consumer's
        bus-halt deficit. With W still ahead, the span from R to the anchor
        holds audio not yet played, and NEUTRAL-filling it cut a hole in the
        scene. R read here says which case this is (``stall_lapped``), and
        when W is still ahead the ring is not touched; the worker restarts
        its schedule from now, owing only what tops the lead back up to the
        target. The whole stall is not caught up: from a lead grown longer
        than the target, that drove W more than a ring ahead of R, and each
        catch-up iteration still over the trigger read R and judged again,
        until a lag that no longer measured what R ate passed for a lap.

        By now the consumer has played all of the lead and some of the ring's
        previous lap, and nothing written meanwhile can change that. What the
        old schedule would do next is worse: write back-to-back at the link's
        limit until it caught up, more than the audio share of the write
        budget, while W overtook R and overwrote what had not yet played. So
        the write head restarts ``HOST_DMA_SERVO_TARGET_GAP`` ahead of R, with
        the span between them NEUTRAL-filled (it holds a lap-old ring), and
        the schedule restarts from now.

        A live input's backlog is the stall's: played late it would only add
        that much latency for the rest of the session, so it is dropped. A
        decoded source's is kept, and plays late — the picture is slaved to
        the audio clock, which did not advance for what was not played.

        R comes from ``RateServo.read_r_promptly``, against a budget of
        ``STALL_REANCHOR_READ_BUDGET_FRAC`` of the lead: a stall is often a
        slow server, and a read as slow as the one that tripped this would
        leave R stale by more than the lead the anchor puts between them.

        ``generation`` is the worker's own, as in :meth:`_worker`. A worker
        parked in that read, or in a stomp write, can outlive stop()'s bounded
        join, and the stall that parked it is the one that brings it here, so
        it would otherwise stomp the next session's ring, drain its mic queue
        and reset its clock state. Each step that blocks is followed by a
        fence check — the R read's own backoff bookkeeping and each stomp
        write included — and a superseded worker returns None and touches
        nothing more."""

        def current() -> bool:
            return not self._superseded(generation)

        read_started = time.monotonic()
        r_addr = self.servo.read_r_promptly(
            self.chunk_size / self.effective_rate,
            STALL_REANCHOR_READ_BUDGET_FRAC * HOST_DMA_SERVO_TARGET_GAP / self.effective_rate,
            current,
        )
        read_travel = int((time.monotonic() - read_started) * self.effective_rate)
        if self._superseded(generation):
            return None
        dropped = 0
        if self.mic_stream is not None:
            dropped = self._drain_queue_samples()
            self._discard_unpushed(dropped, generation=generation)
        # R's consumption since the missed slot, counting the read: R may be
        # sampled at the read's end, and a read that outlasts the old lead
        # otherwise leaves a lap judged as W a ring's worth ahead.
        behind = int(lag * self.effective_rate) + read_travel
        if r_addr is not None and not stall_lapped(
            r_addr, w_head, behind, read_travel + STALL_INSIDE_LEAD_SLACK
        ):
            gap = (w_head - r_addr) % RING_BUFFER_SIZE - read_travel
            inside_lead = (
                "audio: DAC worker stalled %.2f s, inside its %d-byte lead; "
                "%d bytes still ahead of the C64's playback"
            )
            if dropped:
                # Lost live input is audible, so it is reported at default
                # verbosity, through the re-anchor's throttle.
                self._stall_log.warn(
                    inside_lead + "; dropped %.2f s of live input",
                    lag,
                    gap + behind,
                    gap,
                    dropped / self.effective_rate,
                )
            else:
                log.debug(inside_lead, lag, gap + behind, gap)
            # The refill is a short burst the adaptive loop should not steer on.
            self.servo.note_disturbance()
            return StallInsideLead(gap)
        if r_addr is None:
            self._stall_log.warn(
                "audio: DAC worker stalled %.2f s behind the C64's playback "
                "(a blocked or redialed link); the read pointer was not read inside a "
                "slow read's backoff, or could not be read, or not in time, so the "
                "write head could not be re-anchored",
                lag,
            )
            self.servo.note_disturbance()
            return None
        anchor = stall_reanchor(r_addr, self.chunk_size)
        self._stomp_from(r_addr, anchor, current)
        if self._superseded(generation):
            return None
        lead = (anchor - r_addr) % RING_BUFFER_SIZE
        # The new lead is all pad: recorded as a landing of NEUTRAL, the clock
        # subtracts none of it until content lands behind it. Through the
        # landing record, not the clock: everything landed before the stall
        # has played, so the clock reaches the landed count here, and the
        # high-water mark holds it there while the content behind the pad
        # lands and is played.
        self._note_ring_landed(generation, lead, lead, paced=False)
        self.servo.resync(lead)
        self._stall_log.warn(
            "audio: DAC worker stalled %.2f s behind the C64's playback (a blocked "
            "or redialed link); it replayed its ring meanwhile. Re-anchored the "
            "write head %d bytes ahead of it%s",
            lag,
            lead,
            f" and dropped {dropped / self.effective_rate:.2f} s of live input" if dropped else "",
        )
        return anchor

    def _superseded(self, generation: int) -> bool:
        """True once the worker started as ``generation`` should stop acting:
        stop() cleared ``running``, or a later start_* replaced it."""
        return not self.running or generation != self._worker_generation

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

    def _push_to_analysis(self, mono_floats: np.ndarray) -> None:
        """Feed the pre-DSP analysis sink, if one is installed.

        Called from realtime callbacks, so a failing analyzer must never take
        the audio path down with it: the first exception is logged and the sink
        is dropped until a source installs another (visuals stop reacting,
        sound keeps playing)."""
        sink = self.analysis_sink
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

    def _dsp_active(self) -> bool:
        """True when the host DSP chain has at least one enabled stage. Used to
        decide whether the mic path's legacy hard gate is bypassed (the DSP's
        expander replaces it)."""
        return self._dsp.active

    def set_pre_emphasis(self, amount: float | None) -> None:
        """Override the DSP chain's pre-emphasis for the upcoming scene.

        The AudioStreamer is shared across scenes, so a scene applies its
        per-scene value (or None = source-aware/global default) at setup(). We
        update _dsp_params and rebuild the line chain now; mic scenes rebuild
        with is_mic=True in start_mic() from the updated params, and the REU
        video path reads _dsp_params via process_offline_dsp()."""
        self._dsp_params = dataclasses.replace(self._dsp_params, pre_emphasis=amount)
        self._dsp = AudioDSP(self._dsp_params, sample_rate=self.sample_rate, is_mic=False)

    def _apply_dsp(self, floats: np.ndarray) -> np.ndarray:
        """Run the host DSP chain over float samples in [-1, 1] before the DAC
        encode. No-op (returns the input) when DSP is inactive."""
        return self._dsp.process(floats) if self._dsp.active else floats

    def process_offline_dsp(self, floats: np.ndarray) -> np.ndarray:
        """Run the configured DSP over a COMPLETE offline buffer using a fresh
        line chain (is_mic=False), leaving the realtime streamer's own chain
        state untouched. Used by the REU video pre-encode so REU-staged
        and host-DMA video audio get identical DSP treatment. No-op when
        DSP is disabled."""
        dsp = AudioDSP(self._dsp_params, sample_rate=self.sample_rate, is_mic=False)
        return dsp.process(floats) if dsp.active else floats

    def _encode_dac(self, floats: np.ndarray) -> np.ndarray:
        """Quantize `floats` to DAC bytes through this streamer's dither
        generator. The lock is load-bearing: np.random.Generator is not
        thread-safe, and the host-DMA producer (demuxer or mic callback
        thread) and the REU mic callback both land here."""
        with self._dither_lock:
            return encode_floats_to_dac(
                floats,
                dither=self.dither_enabled,
                rng=self._dither_rng,
                curve=self._dac_curve,
            )

    def _backpressure_wait_s(self, n: int) -> float:
        """How long a blocking push of an ``n``-sample blob waits for room
        before the blob is dropped.

        The worker frees room a whole chunk at a time, one chunk behind its
        collect, so a live consumer needs up to two chunk periods beyond the
        blob's own length to make room for it. A flat QUEUE_PUT_TIMEOUT_S fell
        inside that at startup, while the first chunks after the prebuffer
        were still in hand, and dropped the producer's next blob (about 93 ms
        of a 44.1 kHz WAV at 12 kHz)."""
        return QUEUE_PUT_TIMEOUT_S + (n + 2 * self.chunk_size) / self.effective_rate

    def _encode_and_enqueue(
        self, floats: np.ndarray, block_on_full: bool = False, *, epoch: int | None = None
    ) -> int:
        """Push float samples in [-1, 1] through the FFT tap and into the
        DAC queue as 4-bit values. Returns the number of samples enqueued.

        Encodes the whole input array to one bytes blob and enqueues it in
        a single put. The previous per-sample loop hit ~88K lock
        acquisitions/sec on a 44.1 kHz PyAV stream; this is one per
        producer call (~10-40/sec).

        block_on_full: if True, block for queue capacity, up to the drain bound (used
        by the PyAV push path so the demuxer naturally throttles). If
        False, drop the whole blob when full (mic path, where the
        sounddevice callback is real-time and can't block). Backpressure
        is counted in samples (not blobs) against self._max_queued_samples.

        epoch: the caller's capture, taken before its own ``running`` check,
        or a producer's own (see :meth:`push_samples`). Captured here instead,
        a stop() that lands between that check and this entry bumps it first
        and the blob is queued behind stop()'s drain."""
        if floats.size == 0:
            return 0
        # The producer's, else captured at entry: if a cut bumps it while this
        # call is parked in the backpressure spin below, the samples are
        # pre-splice and are dropped just before the put.
        if epoch is None:
            epoch = self._flush_epoch
        floats = self._apply_dsp(floats)
        self._push_to_tap(floats.astype(np.float32, copy=False))
        vol = self._encode_dac(floats)
        n = int(vol.size)
        payload = vol.tobytes()
        # Reading _queued_samples unlocked races the worker's decrement; the
        # worst case is one blob over what is already a soft cap.
        if self._queued_samples + n > self._max_queued_samples:
            if not block_on_full:
                return 0
            # monotonic, like every other deadline here: a wall-clock step would
            # either expire this wait instantly or park the PyAV demuxer thread
            # for the length of a backward step.
            deadline = time.monotonic() + self._backpressure_wait_s(n)
            # `self._queued_samples and` admits a blob bigger than the whole
            # cap once the queue drains. Without it the condition never clears
            # however empty the queue gets, and the caller returns 0 forever.
            while (
                self._queued_samples
                and self._queued_samples + n > self._max_queued_samples
                and self.running
            ):
                if time.monotonic() >= deadline:
                    return 0
                time.sleep(BACKPRESSURE_SPIN_S)
        # Drop the blob if a splice cut while we encoded or waited for
        # capacity: the worker would only discard it.
        if self._flush_epoch != epoch:
            return 0
        # Counted before the put: a blob in the queue without its count, taken
        # by stop()'s drain or the worker, is subtracted from a queued count
        # that clamps at zero, and the late add then leaves a phantom queued
        # count the ring never consumes.
        with self._count_lock:
            self._queued_samples += n
            self._pushed_count += n
        # Polled rather than a blocking q.put(timeout=...): stop()'s drain
        # frees the very slots a parked put waits on, so the blob would land
        # in the queue right after the drain. The check and the put share
        # _count_lock with the epoch bump, so a put cannot pass its check
        # before a bump and land after the drain.
        put_deadline = time.monotonic() + QUEUE_PUT_TIMEOUT_S
        while True:
            with self._count_lock:
                if self._flush_epoch != epoch:
                    break
                try:
                    self.q.put_nowait((epoch, payload))
                    return n
                except queue.Full:
                    pass
            if not block_on_full or time.monotonic() >= put_deadline:
                break
            time.sleep(BACKPRESSURE_SPIN_S)
        with self._count_lock:
            self._queued_samples = max(0, self._queued_samples - n)
            self._pushed_count = max(0, self._pushed_count - n)
        return 0

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        # Ahead of the running check, which stop()'s bump precedes.
        epoch = self._flush_epoch
        if status or not self.running:
            return
        mono = downmix_to_mono(indata)
        mono = mono * self.sensitivity
        # Analysis tap first: pre-gate, pre-DSP (see _push_to_analysis).
        self._push_to_analysis(mono.astype(np.float32, copy=False))
        # The DSP expander supersedes the legacy hard gate when DSP is on.
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        self._encode_and_enqueue(mono.astype(np.float32, copy=False), epoch=epoch)

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
        mono = downmix_to_mono(indata)
        mono = mono * self.sensitivity
        self._push_to_analysis(mono.astype(np.float32, copy=False))
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        mono = self._apply_dsp(mono.astype(np.float32, copy=False))
        self._push_to_tap(mono)
        lead, shaper = self._mic_lead, self._mic_shaper
        fill = b""
        start: int | None = None
        if lead is not None and shaper is not None:
            anchor = lead.take_reanchor()
            if anchor is not None:
                # Restart the write head REU_MIC_BOOTSTRAP_BYTES past the pump,
                # NEUTRAL over the span the pump reaches first: the overtaken
                # or lapped ring there holds audio from a lap ago.
                start, fill_len = reanchor_fill(anchor)
                fill = bytes([self._neutral_byte]) * fill_len
            mono = shaper.process(mono, lead.drop_frac)
        vol = self._encode_dac(mono)
        self._push_mic_to_reu(fill + vol.tobytes(), start)

    def _push_mic_to_reu(self, encoded: bytes, start: int | None = None) -> None:
        """REUWRITE `encoded` to the mic ring at `start` (default: the write
        head, `_mic_reu_write_pos`), wrapping at REU_MIC_SIZE. The head moves
        to the end of the write only when it succeeds, so a re-anchor whose
        fill fails leaves the head where it was, for the next measurement to
        find still overtaken or lapped. Splits the write across the ring boundary
        when needed so the C64 pump always reads a contiguous stream
        (otherwise the wrap-end half of the chunk would be stale silence
        for one ring period).

        Called on the PortAudio callback thread, so a link failure is caught
        and counted here: an exception leaving a sounddevice callback kills mic
        audio for the rest of the scene, and its traceback goes to stderr via
        PortAudio rather than to the logger — leaving nothing in a log file to
        point at."""
        n = len(encoded)
        if n == 0:
            return
        pos = self._mic_reu_write_pos if start is None else start
        end = pos + n
        try:
            if end <= REU_MIC_SIZE:
                self.api.reu_write(REU_MIC_BASE + pos, encoded)
            else:
                split = REU_MIC_SIZE - pos
                self.api.reu_write(REU_MIC_BASE + pos, encoded[:split])
                self.api.reu_write(REU_MIC_BASE, encoded[split:])
        except Exception:
            self._mic_reu_write_errors += 1
            if self._mic_reu_write_errors == 1:
                log.exception(
                    "audio[reu mic]: REU write failed — mic audio will stutter or "
                    "stop (further failures counted, reported at stop)"
                )
            return
        # (pos + n) mod ring for both branches. A bare `n - split` on the
        # wrapping branch only stays in range while one block is under a ring's
        # worth past the head; beyond that every later call slices negative.
        self._mic_reu_write_pos = end % REU_MIC_SIZE
        # stats()["pushed_samples"] only: position_seconds() uses the wall-clock
        # branch on this path, so this is not the audio clock.
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
        # Resolve a name substring / int-in-string up front so the log line and
        # the REU delegation both see a plain int. A configured device that
        # cannot be honored raises here, before any capture state is touched.
        device = resolve_audio_input_device(device)
        self._resolve_input_device(device)
        self.sensitivity = sensitivity
        self.noise_gate = noise_gate
        # Rebuild for a mic source so the AGC stage activates; line sources keep
        # the is_mic=False chain from __init__.
        self._dsp = AudioDSP(self._dsp_params, sample_rate=self.sample_rate, is_mic=True)
        if self._dsp.active:
            log.info("audio: host DSP active (mic chain)")
        self._listen_mode = False
        if self.use_reu_pump:
            self._start_mic_for_reu_pump(device, skip_irq_vector_hook=skip_irq_vector_hook)
            return
        self._upload_nmi_and_buffers()
        self.reset_position()
        self.running = True
        self._worker_thread = self._start_worker()
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

    def _listen_callback(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        """Listen-only capture callback: feed the analysis sink and nothing
        else. No noise gate, no DSP, no DAC encode, no ring — the input drives
        reactive visuals only, so the raw pre-gate signal is exactly what the
        onset detector wants (mirrors the tap point in `_mic_callback`)."""
        if status or not self.running:
            return
        mono = downmix_to_mono(indata)
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
        self._resolve_input_device(device)
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

    def _program_reu_pump_rate(self, chunk: int, *, overdrive: float = 1.0) -> int:
        """Derive the matched CIA #1 Timer A latch for ``chunk`` bytes per pump
        IRQ, record it as this run's nominal, write it to $DC04/$DC05, and
        return it for the caller's log line.

        ``overdrive`` > 1 shortens the period by that factor, so the pump
        out-produces the consumer. Only a governed pump may ask for it — the
        governor's skip-when-ahead trims the surplus, and without one it laps
        the ring (see REU_GOVERNOR_PUMP_OVERDRIVE).

        The pump period has to be chunk × the NMI period so the C64-side pump
        delivers exactly what the NMI consumer drains; the kernal-default CIA #1
        rate (60/50 Hz) underfills the ring at our chunk size and the NMI
        re-reads a lap-old span as an audible stale-data echo. The latch is
        therefore derived from the live consumer, never hardcoded: it is a ratio
        of periods, system-independent but NOT rate-independent. Both pump
        bring-ups must call this.

        Only the low 16 bits reach the register pair, so a period that does not
        fit is clamped with a warning instead of being silently reduced modulo
        65536 — a truncated latch can land anywhere, including one that fires
        the pump hundreds of times faster than matched. At the default chunk the
        product passes 16 bits below ≈2 kHz, and ``c64.nmi_rate_safety`` bounds
        ``sample_rate`` only to what the NMI's own 16-bit latch can hold.

        CIA #1 stays in continuous mode (the kernal already set CRA); only the
        latch changes. BASIC's TI$ jiffy clock drifts as a side effect —
        nothing we depend on.
        """
        ideal = round(chunk * (self.nmi.nominal_latch() + 1) / overdrive) - 1
        latch = min(ideal, CIA_TIMER_LATCH_MAX)
        if latch != ideal:
            log.warning(
                "audio: a matched REU pump at sample_rate=%d with chunk=%d needs a "
                "CIA #1 latch of %d, past the 16-bit maximum %d — clamping, so the "
                "pump over-produces and the ring laps (audible echo). Raise "
                "[audio].sample_rate.",
                self.sample_rate,
                chunk,
                ideal,
                CIA_TIMER_LATCH_MAX,
            )
        self._reu_cia1_latch_nominal = latch
        self._write_cia1_timer_a_latch(latch)
        return latch

    def _install_tracked_pump(
        self, body: bytes, *, src: int, dst: int, dispatcher_owns_irq: bool
    ) -> None:
        """Seed the $C200 trackers, then upload ``body`` at $C180 and the
        REU_IRQ_HANDLER_TRACKED entry at $C100, in that order, confirming each
        stage delivered before starting the next.

        ``src`` is the 24-bit REU offset of the first chunk and ``dst`` the
        C64 ring address it lands at. Both pumps that reload their REC
        addresses from the trackers come through here: the tracked video pump
        and the REU mic pump. ``dispatcher_owns_irq`` is True when a bank-swap
        dispatcher already owns $0314 and reaches $C100/$C180 itself.

        The order is what keeps a CIA #1 tick that lands mid-install safe. A
        bank-swap dispatcher that owns $0314 can reach $C180 directly (the
        chunked mhires one JSRs it between REC families) and $C100 through its
        fall-through, and its installer leaves an RTS at $C180 and a JMP $EA31
        at $C100 until this runs. Seeding the trackers first means the body
        never runs on stale ones, and uploading the body before the entry means
        the entry never JSRs into a body that is not there yet.

        The order only holds if every write lands, and a write can be lost
        without an error reaching here (``_emit`` swallows transport failures,
        and a redial can drop what the old connection had not confirmed). A
        body running on unseeded trackers DMAs a chunk to whatever C64 address
        the dst tracker held before its wrap check runs — over the body itself,
        or upward from below the ring through zero page and the vectors. So
        each stage is flushed and checked against ``delivery_epoch``, retried
        up to TRACKED_PUMP_INSTALL_TRIES times, and the next stage starts only
        once it held.

        Under a dispatcher, CIA #1 is masked around the entry upload: a DMA can
        stall the 6510 between the stub's JMP opcode and its operands, which
        then read the entry's bytes and jump to $8020. The flag still latches
        while masked, so no tick is lost; it fires on the unmask.

        Raises PumpInstallError when a stage never confirms, after parking an
        RTS at $C180 and, under a dispatcher, unmasking CIA #1 again.
        """

        def seed_trackers() -> None:
            self.api.write_memory(
                f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}",
                f"{src & 0xFF:02X}{(src >> 8) & 0xFF:02X}{(src >> 16) & 0xFF:02X}"
                f"{dst & 0xFF:02X}{(dst >> 8) & 0xFF:02X}",
            )
            # Seed the tick divider to 1 so the first IRQ DECs to 0, reloads N
            # and chains. Unseeded, $C205 holds whatever was in RAM — 0 wraps to
            # $FF on the DEC, costing 254 lean exits (~2.5 s of unresponsive
            # keyboard) before the first kernal tail.
            self.api.write_memory(f"{REU_PUMP_TICK_COUNTER_ADDR:04X}", "01")

        def upload_body() -> None:
            self.api.write_memory_file(f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", body)

        def upload_entry() -> None:
            self._write_pump_entry(REU_IRQ_HANDLER_TRACKED, dispatcher_owns_irq=dispatcher_owns_irq)

        stages = (("trackers", seed_trackers), ("body", upload_body), ("entry", upload_entry))
        for stage, write in stages:
            try:
                self._require_confirmed(stage, write)
            except PumpInstallError:
                self._park_tracked_pump(dispatcher_owns_irq, entry_may_be_up=stage == "entry")
                raise
        if dispatcher_owns_irq:
            # Not left owed to _arm_installed_pump: the entry's confirmed unmask
            # already held, and a second one lost there would unwind the pump.
            self._cia1_unmask_owed = False

    def _write_pump_entry(
        self, code: bytes, *, dispatcher_owns_irq: bool, unmask: bool = True
    ) -> None:
        """Write ``code`` at the $C100 pump entry. Under a dispatcher, CIA #1 is
        masked around it (see _install_tracked_pump), and nothing is written
        when the mask did not confirm; without ``unmask`` the mask stays. Every call masks afresh: a retry follows
        an attempt whose unmask may have landed even though its entry did not."""
        if dispatcher_owns_irq:
            epoch = self.api.delivery_epoch
            self.api.write_memory(f"{CIA1.ICR:04X}", f"{CIA1.ICR_DISABLE_ALL:02X}")
            self.api.flush()
            if self.api.delivery_epoch != epoch:
                return
            time.sleep(TRACKED_PUMP_ENTRY_DRAIN_S)
        self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", code)
        if dispatcher_owns_irq and unmask:
            self.api.write_memory(f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}")

    def _require_confirmed(self, stage: str, write: Callable[[], None]) -> None:
        """``_write_confirmed``, raising PumpInstallError naming ``stage`` when
        no attempt held."""
        if not self._write_confirmed(write):
            raise PumpInstallError(
                f"REU pump install: the {stage} write was not confirmed delivered "
                f"after {TRACKED_PUMP_INSTALL_TRIES} attempts"
            )

    def _reu_write_confirmed(self, what: str, reu_offset: int, data: bytes) -> None:
        """One REUWRITE slice, confirmed like a pump install stage. Every
        staged track lands at the same REU offset, so a slice lost to a lossy
        redial would play the previous track's audio (or stale SRAM noise)
        rather than fail. Raises PumpInstallError when it never confirms."""
        self._require_confirmed(what, lambda: self.api.reu_write(reu_offset, data))

    def _write_irq_vector(self, handler: int) -> None:
        """Point $0314/$0315 at ``handler``. write_regs coalesces into one DMA,
        so both bytes change at once and no IRQ jumps through half a vector."""
        self.api.write_regs(f"{VECTORS.IRQ:04X}", handler & 0xFF, (handler >> 8) & 0xFF)

    def _arm_installed_pump(
        self,
        program_rec_and_rate: Callable[[], None],
        *,
        tracked: bool,
        dispatcher_owns_irq: bool,
    ) -> None:
        """The tail both pump bring-ups share once their code is up: confirm
        ``program_rec_and_rate`` (the REC registers and the CIA #1 latch), arm
        the NMI, and point $0314 at the pump entry unless a dispatcher owns it.

        The playback-clock origin is captured immediately before the NMI
        starts firing: position_seconds() must measure time since audio became
        audible, or video sync trails it by the bring-up cost. The settle lets
        the NMI catch a few samples before the pump arms (REU_PUMP_SETTLE_S);
        the pump then starts on the next CIA #1 IRQ.

        A write that never confirms unwinds the install and the NMI bring-up
        and raises PumpInstallError, with nothing armed."""
        patched = False
        try:
            self._require_confirmed("REC and CIA #1 latch", program_rec_and_rate)
            self._reu_pump_start_time = time.monotonic()
            self.nmi.start(adaptive=self.nmi_rate_adaptive)
            time.sleep(REU_PUMP_SETTLE_S)
            if not dispatcher_owns_irq:
                # Set first: an unconfirmed patch may have landed all the same.
                patched = True
                self._require_confirmed(
                    "IRQ vector", lambda: self._write_irq_vector(REU_PUMP_HANDLER_ADDR)
                )
            if self._cia1_unmask_owed:
                self._cia1_unmask_step()[1]()
        except PumpInstallError as e:
            self._unwind_pump_install(
                restore_irq_vector=patched,
                tracked=tracked,
                dispatcher_owns_irq=dispatcher_owns_irq,
            )
            self._abandon_pump_bring_up(e)
            raise

    def _unwind_pump_install(
        self, *, restore_irq_vector: bool, tracked: bool, dispatcher_owns_irq: bool
    ) -> None:
        """Best-effort undo of a pump install that failed after its code went
        up: $0314 back to the kernal when this install may have patched it,
        the tracked body parked on an RTS for anything still reaching $C180,
        and CIA #1 Timer A back to the kernal latch. A dispatcher's $0314 is
        never touched.

        The vector restore is confirmed like the patch was, and stays owed to
        `_disarm_reu_pump` until it lands: nothing is armed after this, so
        without the debt stop() would leave every CIA #1 tick running a pump
        whose session has ended."""
        if restore_irq_vector:
            self._irq_vector_restore_owed = True
            run_teardown_steps(
                log,
                type(self).__name__,
                [("IRQ vector restore", self._restore_irq_vector_confirmed)],
            )
        if tracked:
            self._park_tracked_pump(dispatcher_owns_irq, entry_may_be_up=True)
        run_teardown_steps(
            log,
            type(self).__name__,
            [
                ("CIA #1 Timer A latch restore", self._restore_cia1_latch),
                ("pump unwind flush", self.api.flush),
            ],
        )

    def _restore_irq_vector_confirmed(self) -> None:
        """$0314 back to the kernal, confirmed; clears the restore debt only
        once it held. Raises PumpInstallError when it never confirms, after
        putting the JMP $EA31 stub at the $C100 entry the vector still names.

        Left as it was, that entry's tick divider chained the kernal on only
        every Nth CIA #1 tick once the latch went back to the kernal's, so the
        jiffy clock, SCNKEY and the cursor ran at a third of their speed until
        a later stop() landed the restore."""
        try:
            self._require_confirmed(
                "IRQ vector restore", lambda: self._write_irq_vector(KERNAL.IRQ_HANDLER)
            )
        except PumpInstallError:
            # A mask already owed was not placed here, and $0314 may name a
            # stale dispatcher rather than $C100, so the stub keeps it.
            self._stub_pump_entry(unmask=not self._cia1_unmask_owed)
            raise
        self._irq_vector_restore_owed = False

    def _stub_pump_entry(self, *, unmask: bool) -> None:
        """The JMP $EA31 stub at $C100, written under a CIA #1 mask as a
        dispatcher's entry is (an IRQ may be running the entry), then CIA #1
        unmasked when ``unmask``; each confirmed, and one that never confirms
        is logged. Without ``unmask`` the mask stays owed."""
        steps = [self._entry_stub_step(unmask=unmask)]
        if unmask:
            steps.append(self._cia1_unmask_step())
        run_teardown_steps(log, type(self).__name__, steps)

    def _entry_stub_step(self, *, unmask: bool = True) -> tuple[str, Callable[[], None]]:
        """The confirmed teardown step that puts the JMP $EA31 stub back at
        $C100 under a CIA #1 mask. The mask may land without the unmask that
        follows it, so the unmask is owed until `_cia1_unmask_step` holds."""

        def stub() -> None:
            self._cia1_unmask_owed = True
            self._require_confirmed(
                "pump entry stub restore",
                lambda: self._write_pump_entry(
                    REU_PUMP_HANDLER_STUB, dispatcher_owns_irq=True, unmask=unmask
                ),
            )

        return ("pump entry stub restore", stub)

    def _cia1_unmask_step(self) -> tuple[str, Callable[[], None]]:
        """The confirmed teardown step that unmasks CIA #1 Timer A; the debt
        (`_cia1_unmask_owed`) clears only once it held, and the next
        `_arm_installed_pump` or `_disarm_reu_pump` writes it again until then
        (a dispatcher's confirmed entry upload in `_install_tracked_pump`
        unmasks too, and pays it).
        A mask left in place stops the kernal's jiffy IRQ outright, SCNKEY
        included, and a pump armed under it never runs."""

        def unmask() -> None:
            self._require_confirmed(
                "CIA #1 unmask",
                lambda: self.api.write_memory(f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"),
            )
            self._cia1_unmask_owed = False

        return ("CIA #1 unmask", unmask)

    def _write_confirmed(self, write: Callable[[], None]) -> bool:
        """``delivery.write_confirmed`` at TRACKED_PUMP_INSTALL_TRIES tries."""
        return write_confirmed(self.api, write, tries=TRACKED_PUMP_INSTALL_TRIES)

    def _park_tracked_pump(self, dispatcher_owns_irq: bool, *, entry_may_be_up: bool) -> None:
        """Best-effort safe state after a failed tracked-pump install: an RTS at
        $C180 so neither the $C100 entry nor a dispatcher's inline JSR runs a
        body on unconfirmed trackers, and CIA #1 unmasked if the install masked
        it. A one-byte write cannot tear an instruction the 6510 is fetching.

        Under a dispatcher, an entry that may have gone up (``entry_may_be_up``)
        also goes back to the JMP $EA31 stub its installer left: the dispatcher
        keeps JMPing to $C100 for the rest of the scene, and once CIA #1 is back
        at the kernal latch the entry's tick divider would chain the kernal on
        only every Nth tick (the jiffy clock, SCNKEY and the cursor blink at a
        third speed).

        Every write here is confirmed like an install stage, since the link
        that lost the install can lose these too, and one that never confirms
        is logged rather than dropped. A lost RTS leaves a torn body where the
        chunked dispatcher JSRs, and a lost unmask after a masked entry upload
        leaves the kernal with no jiffy IRQ at all, keyboard scan included."""

        def confirmed(stage: str, write: Callable[[], None]) -> tuple[str, Callable[[], None]]:
            return (stage, lambda: self._require_confirmed(stage, write))

        steps = [
            confirmed(
                "pump body park",
                lambda: self.api.write_memory(f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", "60"),
            )
        ]
        if dispatcher_owns_irq and entry_may_be_up:
            steps.append(self._entry_stub_step())
        if dispatcher_owns_irq:
            steps.append(self._cia1_unmask_step())
        run_teardown_steps(log, type(self).__name__, steps)

    def _abandon_pump_bring_up(self, err: PumpInstallError) -> None:
        """Undo the NMI bring-up a failed pump install left behind and say so
        loudly; the scene carries on without audio."""
        log.error("audio: %s — this scene plays without audio", err)
        run_teardown_steps(log, type(self).__name__, self._hardware_teardown_steps())
        self.api.note_nmi_consumer(False)

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
        The two sequences are still written out separately, which is how the
        CIA #1 latch came to be derived on one and hardcoded on the other;
        everything the two must agree on now lives in something they share
        (_program_reu_pump_rate, _arm_installed_pump, the handler constants).
        A new field either goes in a shared helper or is a divergence again.
        """
        # Idempotent on an int (start_mic already resolved before delegating);
        # keeps the device=%d log below correct if ever called with a name.
        device = resolve_audio_input_device(device)
        # Pre-fill the REU mic ring with NEUTRAL so the pump's first reads play
        # silence rather than stale FPGA SRAM, which can be loud noise. One
        # REUWRITE slice is 32 KB, so two cover the 64 KB ring.
        log.info(
            "audio[reu mic]: prefilling REU ring at $%06X (%d bytes)", REU_MIC_BASE, REU_MIC_SIZE
        )
        pad = bytes([self._neutral_byte] * REU_UPLOAD_SLICE)
        try:
            for off in range(0, REU_MIC_SIZE, REU_UPLOAD_SLICE):
                n = min(REU_UPLOAD_SLICE, REU_MIC_SIZE - off)
                self._reu_write_confirmed("mic ring prefill", REU_MIC_BASE + off, pad[:n])
        except PumpInstallError as e:
            log.error("audio: %s — this scene plays without audio", e)
            return

        # Standard NMI bring-up (handler + ring + digi-boost). NMI consumes from
        # the $4000 ring _upload_nmi_and_buffers just NEUTRAL-filled.
        self._upload_nmi_and_buffers()

        # Install the tracked pump with the mic body at $C180: src tracker =
        # REU_MIC_BASE, dst tracker = REU_MIC_RING_LEAD into the ring, where
        # the NMI reader (still parked at the ring start) is that far behind.
        # _seed_mic_ring_lead re-measures that lead once both are running.
        # Every tick reloads all five REC addresses from the trackers, so a
        # bank-swap REC DMA or a REU screen push between ticks cannot redirect
        # it (#551). Address control = 0: both sides auto-increment, no
        # autoload.
        try:
            self._install_tracked_pump(
                REU_MIC_PUMP_BODY_SUBROUTINE,
                src=REU_MIC_BASE,
                dst=RING_BUFFER_ADDR + REU_MIC_RING_LEAD,
                dispatcher_owns_irq=skip_irq_vector_hook,
            )
        except PumpInstallError as e:
            self._abandon_pump_bring_up(e)
            return

        # Match the pump rate to the NMI consume rate, derived from the live NMI
        # latch by the same helper the video path uses (_program_reu_pump_rate
        # says why it cannot be a constant). Then arm the NMI, which consumes
        # the prebuilt NEUTRAL ring, and patch $0314 at the mic pump entry
        # unless a bank-swap dispatcher owns it and JMPs to $C100 itself; the
        # pump reads NEUTRAL until the bootstrap window has passed.
        def program_rec_and_rate() -> None:
            self.api.write_memory(f"{REU.ADDR_CONTROL:04X}", "00")
            self._program_reu_pump_rate(REU_PUMP_CHUNK_SIZE)

        try:
            self._arm_installed_pump(
                program_rec_and_rate, tracked=True, dispatcher_owns_irq=skip_irq_vector_hook
            )
        except PumpInstallError:
            return
        log.info(
            "audio[reu mic]: pump installed at $%04X, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            self._reu_cia1_latch_nominal,
        )

        self.running = True
        self._reu_pump_armed = True
        self._pushed_count = 0
        ring_lead = self._seed_mic_ring_lead()
        # Start the host write head ahead of the pump's src tracker. Latency is
        # this lead plus the pump's lead over the NMI in the $4000 ring:
        # (REU_MIC_BOOTSTRAP_BYTES + REU_MIC_RING_LEAD) / sample_rate, ~0.3 s
        # at 12 kHz.
        self._mic_reu_write_pos = REU_MIC_BOOTSTRAP_BYTES

        # _open_input_stream hardcodes self._mic_callback, so swap in the REU
        # variant for this path.
        self.mic_stream = self._open_input_stream(device, callback=self._mic_callback_reu)
        self.mic_stream.start()
        self._start_mic_lead_servo()
        # An unread ring lead is logged as the nominal one it was seeded at.
        ring_bytes = REU_MIC_RING_LEAD if ring_lead is None else ring_lead
        log.info(
            "audio[reu mic]: device=%d %dHz sensitivity=%.2f noise_gate=%.3f "
            "host lead=%dB + C64 ring lead=%dB%s (%.0fms latency)",
            device,
            self.sample_rate,
            self.sensitivity,
            self.noise_gate,
            REU_MIC_BOOTSTRAP_BYTES,
            ring_bytes,
            "" if ring_lead is not None else " (nominal, unread)",
            1000 * (REU_MIC_BOOTSTRAP_BYTES + ring_bytes) / self.sample_rate,
        )

    def _read_mic_ring_phase(self, timeout: float = 1.0) -> tuple[int, int] | None:
        """``(R, W)`` from the lead servo's span read (``read_mic_pump``), so
        the two come from the same instant and their difference carries no
        round-trip skew. None when the read fails or a pointer is outside its
        ring. ``timeout`` is the backend's per-read bound."""
        reading = read_mic_pump(self.api.read_memory, timeout)
        return None if reading is None else (reading.r, reading.w)

    def _seed_mic_ring_lead(self) -> int | None:
        """Put the mic pump's write head ``REU_MIC_RING_LEAD`` ahead of the NMI
        reader in the $4000 ring, once both are running, and return the lead
        read back (bytes), or None when it could not be read.

        The install seeded the dst tracker that far past the ring start, where
        R sits until the NMI arms, but the two do not start together: on the
        solo path the pump starts only at the $0314 patch, after the NMI has
        already played ~1 KB, and under a dispatcher the pump runs from the
        moment its body lands, before the NMI arms. So the lead is measured
        here and, when it is off, the dst tracker is rewritten at a
        chunk-aligned R + REU_MIC_RING_LEAD and read back. The pump advances
        the same tracker every tick, and a host write that lands between its
        load and its store is overwritten, so a read-back that does not show
        the seed is retried. A backend without reads keeps the install seed.

        A read that fails once a seed has gone out is retried within the same
        bound: the tracker no longer holds the install seed, and one read can
        land in the instant either pointer's HI byte sits at the ring end."""
        if not self.api.profile.supports_read:
            return None
        phase: int | None = None
        seeds = 0
        for attempt in range(TRACKED_PUMP_INSTALL_TRIES + 1):
            got = self._read_mic_ring_phase()
            if got is None and seeds == 0:
                log.info(
                    "audio[reu mic]: could not read the C64 ring pointers; the pump's "
                    "lead over the NMI stays at its install seed"
                )
                return None
            if got is None:
                phase = None
                continue
            r, w = got
            phase = (w - r) % RING_BUFFER_SIZE
            if mic_ring_lead_ok(phase):
                return phase
            if attempt == TRACKED_PUMP_INSTALL_TRIES:
                break
            dst = mic_ring_seed(r)
            self.api.write_memory(
                f"{REU_AUDIO_DST_TRACKER_ADDR:04X}", f"{dst & 0xFF:02X}{(dst >> 8) & 0xFF:02X}"
            )
            self.api.flush()
            seeds += 1
        log.warning(
            "audio[reu mic]: the pump's lead over the NMI reads %s B after %d seed(s), "
            "not ~%d B; mic audio may run late or replay lap-old audio",
            phase,
            seeds,
            REU_MIC_RING_LEAD,
        )
        return phase

    def _start_mic_lead_servo(self) -> None:
        """Close the loop on the write head's lead over the pump (#560), or
        say why it stays open: without reads there is nothing to measure."""
        self._mic_shaper = MicLeadShaper(self.sample_rate)
        if not self.api.profile.supports_read:
            log.warning(
                "audio[reu mic]: this backend cannot read C64 memory, so the mic "
                "lead servo is off; latency drifts with the pump's rate"
            )
            self._mic_lead = None
            return
        self._mic_lead = MicLeadServo(
            read_memory=self.api.read_memory,
            write_pos=lambda: self._mic_reu_write_pos,
            sample_rate=self.sample_rate,
            ring_governor=self._new_mic_ring_governor(),
        )
        self._mic_lead.start()

    def _new_mic_ring_governor(self) -> MicRingGovernor | None:
        """The closed loop on the pump's lead over the NMI reader (#580), or
        None with ``reu_pump_governor`` off. Its latch writes are fenced to
        this arm of the pump (see ``_write_mic_pump_latch``)."""
        if not self.reu_pump_governor:
            return None
        with self._pump_trim_lock:
            self._pump_trim_token += 1
            token = self._pump_trim_token
        return MicRingGovernor(
            write_latch=functools.partial(self._write_mic_pump_latch, token),
            matched_latch=self._reu_cia1_latch_nominal,
            sample_rate=self.sample_rate,
        )

    def _write_mic_pump_latch(self, token: int, latch: int) -> TrimWrite:
        """Write the governed pump's CIA #1 latch, flushed and checked against
        ``delivery_epoch``, or refuse without writing once the pump armed
        under ``token`` has been disarmed (or rearmed for a later scene). A
        latch write takes effect at the next underflow and does not restart
        the count, so a trim lands between two pump ticks rather than inside
        one. One attempt per tick: an unconfirmed trim is sent again at the
        governor's next tick rather than retried here under the lock the
        disarm waits on."""
        with self._pump_trim_lock:
            if token != self._pump_trim_token:
                return TrimWrite.REFUSED
            if write_confirmed(self.api, lambda: self._write_cia1_timer_a_latch(latch), tries=1):
                return TrimWrite.DELIVERED
            return TrimWrite.UNCONFIRMED

    def _stop_mic_lead_servo(self) -> None:
        lead, self._mic_lead = self._mic_lead, None
        shaper, self._mic_shaper = self._mic_shaper, None
        if lead is None:
            return
        lead.stop()
        gov = lead.ring_governor
        if gov is not None and gov.lead_min is not None:
            log.info(
                "audio[reu mic]: C64 ring lead %d..%d B (target %d), pump slowed "
                "%.2f..%.2f %%, last CIA #1 latch %d (matched %d), %d failed read(s), "
                "%d unconfirmed trim(s)%s",
                gov.lead_min,
                gov.lead_max,
                REU_MIC_RING_LEAD,
                100.0 * (gov.slow_min or 0.0),
                100.0 * (gov.slow_max or 0.0),
                gov.latch,
                self._reu_cia1_latch_nominal,
                gov.failed_reads,
                gov.unconfirmed_trims,
                ", retired" if gov.retired else "",
            )
        if lead.lead_min is None:
            return
        log.info(
            "audio[reu mic]: lead %d..%d B, %d re-anchor(s) (%d dropped unclaimed), "
            "%d open-loop spell(s), "
            "%d splice(s) skipping %d samples",
            lead.lead_min,
            lead.lead_max,
            lead.reanchors,
            lead.reanchors_dropped,
            lead.open_loop_spells,
            shaper.splices if shaper is not None else 0,
            shaper.skipped_samples if shaper is not None else 0,
        )

    def _resolve_input_device(self, device: int | str) -> tuple[int | None, str]:
        """Pick an input-capable device.

        - `device < 0`: use the system default input device (PortAudio
          accepts `None` for that).
        - The configured device exists and has input channels: use it.
        - Otherwise (output-only or unknown): raise `AudioInputDeviceError`.
          The default input is never substituted for a device the user
          named — on a laptop it is the built-in microphone.

        Returns (device_or_None, friendly_name).
        """
        assert sd is not None

        # Coerce a name substring / int-in-string to an index first (-1 only
        # for an explicit default; a name that matches nothing raises), so the
        # rest of this method is plain int logic.
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
            raise AudioInputDeviceError(
                f"audio device {device} is not an input device ({e}); not using the "
                "system default input in its place. Pass --audio-device N (see -L), "
                "or set [audio].device = -1 to ask for the default."
            ) from e
        raise AudioInputDeviceError(
            f"audio device {device} has no input channels; not using the system "
            "default input in its place. Pass --audio-device N (see -L), or set "
            "[audio].device = -1 to ask for the default."
        )

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

    def start_for_external_source(self) -> None:
        """Bring up NMI + worker without an input thread. Caller feeds samples
        via push_samples()."""
        self._listen_mode = False
        self._upload_nmi_and_buffers()
        self.reset_position()
        self.running = True
        self._worker_thread = self._start_worker()
        # Report the achieved rate too: CIA latch quantization separates them
        # (NTSC@12k → 12032 Hz) and the achieved one is the downstream timebase.
        log.info(
            "audio: external push source → SID @ %dHz requested, %.1fHz actual (%+.2f%%)",
            self.sample_rate,
            self.effective_rate,
            100.0 * (self.effective_rate / self.sample_rate - 1.0) if self.sample_rate else 0.0,
        )

    def _fit_reu_audio_region(self, audio_4bit: bytes, eof_pad_bytes: int) -> bytes:
        """Truncate ``audio_4bit`` so the payload plus its EOF pad stays inside
        the REU audio region, warning when it has to.

        One byte is one sample, so the region a track occupies grows with its
        duration and nothing about the upload bounds itself: at the 12032 Hz
        NTSC default the payload reaches ``REU_AUDIO_MAX_BYTES`` after ~20
        minutes of source, and what lies past it is the video staging region
        the REU bank-swap bitmap path rewrites every frame. Overrunning it
        makes the audio pump DMA bitmap bytes into the ring as full-scale
        garbage while the per-frame video writes shred the audio — with no
        host-side error, on nothing more exotic than a long clip.

        Truncating is the graceful end of that trade: the caller has already
        encoded the whole track for this path, so refusing outright would just
        be silence. Bounding the payload here also bounds the EOF pad loop,
        which starts where the payload ends.
        """
        ceiling = REU_AUDIO_MAX_BYTES - eof_pad_bytes
        if len(audio_4bit) <= ceiling:
            return audio_4bit
        log.warning(
            "audio: REU-staged track is %d bytes (%.1f min of source), past the "
            "%d-byte REU audio region less its %d-byte EOF pad — truncating to "
            "%.1f min so the upload cannot reach the video staging region",
            len(audio_4bit),
            len(audio_4bit) / self.effective_rate / 60.0,
            REU_AUDIO_MAX_BYTES,
            eof_pad_bytes,
            ceiling / self.effective_rate / 60.0,
        )
        return audio_4bit[:ceiling]

    def start_for_reu_staged(
        self,
        audio_4bit: bytes,
        chunk_size: int | None = None,
        *,
        skip_irq_vector_hook: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> None:
        """Bring up audio with the entire track preloaded into REU.

        ``audio_4bit`` is a bytes blob of pre-encoded 4-bit DAC volume codes
        (1 byte = 1 sample). Caller is responsible for the encoding (use the
        same float→4-bit pipeline as ``_encode_and_enqueue`` to stay
        consistent with the host-DMA path).

        ``chunk_size`` overrides the default REU_PUMP_CHUNK_SIZE for scenes
        where the C64 bus is heavily halted (bitmap modes pass
        REU_PUMP_CHUNK_SIZE_HEAVY_BUS). The CIA #1 latch is derived from it, so
        it sets each pump DMA's bus halt rather than the byte rate. Raises
        ValueError unless it divides both RING_BUFFER_SIZE and
        REU_PUMP_INITIAL_MARGIN (reu_pump_chunk_fits_ring): any other chunk
        DMAs past the ring end once per lap. With reu_pump_governor on it must
        also be at most REU_GOVERNOR_MAX_CHUNK, or ValueError. Raises
        PumpInstallError when a write of the pump install never confirms (each
        REUWRITE slice of the track and its EOF pad; the tracked pump's stages in ``_install_tracked_pump``; the plain handler;
        the REC registers and CIA #1 latch; the $0314 patch), with the NMI
        bring-up already undone and nothing armed: the caller plays on without
        audio.

        ``on_progress`` (fraction 0..1 of payload + EOF-pad bytes uploaded) is
        called once per upload slice — the seconds-long upload is the bulk of
        a REU video scene's setup time, and this is what the setup progress
        bar tracks.

        ``skip_irq_vector_hook``: when True, skip the $0314 → $C100 patch. Used
        when the display mode owns $0314 — its bank-swap dispatcher at $C500
        JMPs to $C100 on non-raster IRQs, so $C100 is still reached. The
        dispatcher installer pre-uploads a 3-byte JMP $EA31 stub at $C100 before
        hooking $0314, covering the gap until this method writes real bytes.

        Bring-up order, which matters:
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
        chunk = REU_PUMP_CHUNK_SIZE if chunk_size is None else chunk_size
        if not reu_pump_chunk_fits_ring(chunk):
            raise ValueError(
                f"REU pump chunk_size={chunk} must be a positive divisor of the "
                f"{RING_BUFFER_SIZE}-byte ring and of its {REU_PUMP_INITIAL_MARGIN}-byte "
                "initial margin"
            )
        if self.reu_pump_governor and chunk > REU_GOVERNOR_MAX_CHUNK:
            raise ValueError(
                f"REU pump chunk_size={chunk} is past the governor's "
                f"{REU_GOVERNOR_MAX_CHUNK}-byte maximum: one pump would carry the "
                "write head out of the skip window, which reads as an overtake"
            )
        if not audio_4bit:
            log.warning("audio: start_for_reu_staged called with empty data")
            return
        self._listen_mode = False
        # Seed the write pointer REU_PUMP_INITIAL_MARGIN behind the reader for
        # symmetric jitter headroom, keeping src offset ≡ dst position (mod
        # ring) so the sample→position mapping stays constant. Both the plain
        # and the tracked handler start from these values.
        initial_src_off = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        initial_dst = RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN
        # Preload the audio into REU with a NEUTRAL_SAMPLE tail: past the end of
        # the source the pump would otherwise read uninitialized FPGA SRAM,
        # audible as loud hiss at the end of the video. Both durations are
        # real time for the pump, so they scale by effective_rate, which the
        # payload was encoded at too.
        eof_pad_bytes = round(self.effective_rate * 5)
        audio_4bit = self._fit_reu_audio_region(audio_4bit, eof_pad_bytes)
        log.info(
            "audio: REU upload %d bytes (%.1fs of source) + %d bytes EOF pad",
            len(audio_4bit),
            len(audio_4bit) / self.effective_rate,
            eof_pad_bytes,
        )
        upload_total = len(audio_4bit) + eof_pad_bytes
        t0 = time.perf_counter()
        try:
            for off in range(0, len(audio_4bit), REU_UPLOAD_SLICE):
                self._reu_write_confirmed(
                    "audio upload",
                    REU_AUDIO_BASE + off,
                    audio_4bit[off : off + REU_UPLOAD_SLICE],
                )
                if on_progress is not None:
                    on_progress(min(off + REU_UPLOAD_SLICE, len(audio_4bit)) / upload_total)
            # EOF pad: write NEUTRAL_SAMPLE for the tail so the pump's read-past-
            # end-of-source plays silence instead of garbage.
            pad_payload = bytes([self._neutral_byte] * REU_UPLOAD_SLICE)
            pad_off = len(audio_4bit)
            pad_end = pad_off + eof_pad_bytes
            while pad_off < pad_end:
                chunk_len = min(REU_UPLOAD_SLICE, pad_end - pad_off)
                self._reu_write_confirmed(
                    "EOF pad", REU_AUDIO_BASE + pad_off, pad_payload[:chunk_len]
                )
                pad_off += chunk_len
                if on_progress is not None:
                    on_progress(pad_off / upload_total)
        except PumpInstallError as e:
            log.error("audio: %s — this scene plays without audio", e)
            raise
        log.info("audio: REU upload took %.2fs", time.perf_counter() - t0)

        self._upload_nmi_and_buffers()

        # Pre-fill the ring with the first 8 KB so the NMI starts on real audio;
        # otherwise there is ~1 s of silence before the pump catches up.
        prefill = audio_4bit[:RING_BUFFER_SIZE]
        if len(prefill) < RING_BUFFER_SIZE:
            prefill = prefill + bytes([self._neutral_byte] * (RING_BUFFER_SIZE - len(prefill)))
        self.api.write_memory_file(f"{RING_BUFFER_ADDR:04X}", prefill)

        # Install the pump IRQ handler at $C100 and init the REU regs: src = REU
        # offset REU_PUMP_INITIAL_MARGIN, dst = ring start + the same, so the
        # write pointer trails the reader by that margin. The first pump DMAs
        # re-write the upper half of the pre-fill with identical bytes, then run
        # steadily ~0.5 s behind the NMI. Length = chunk_size, address control =
        # 0 (both auto-increment, no autoload).
        #
        # When the display mode owns $0314 (REU bank-swap video on
        # hires/mhires), its raster IRQ drives the REC controller too and its
        # DMAs overwrite both src ($DF04-$DF06) and dst ($DF02-$DF03) between
        # audio IRQs — the plain handler, which relies on those registers
        # auto-incrementing, would then read video staging and write into color
        # RAM. The TRACKED variant reloads all five from the main-RAM tracker at
        # $C200-$C204 (src LO/MI/HI, dst LO/HI) every IRQ. Its pump code is the
        # $C180 subroutine, which the $C100 entry and the chunked mhires
        # dispatcher both call, so the chunk size and the governor choice land
        # there, once, for both callers.
        #
        # Chunk-operand offsets come from audio_handlers' *_CHUNK_OFFSETS,
        # stated beside the assembly that defines them: a wrong offset writes a
        # length into another instruction's operand and DMAs to a garbage
        # address.
        if skip_irq_vector_hook:
            if self.reu_pump_governor:
                body = patch_chunk_size(
                    REU_PUMP_BODY_SUBROUTINE_GOVERNOR,
                    REU_PUMP_BODY_SUBROUTINE_GOVERNOR_CHUNK_OFFSETS,
                    chunk,
                )
            else:
                body = patch_chunk_size(
                    REU_PUMP_BODY_SUBROUTINE, REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS, chunk
                )
            try:
                self._install_tracked_pump(
                    body, src=initial_src_off, dst=initial_dst, dispatcher_owns_irq=True
                )
            except PumpInstallError as e:
                self._abandon_pump_bring_up(e)
                raise
        else:
            # The governor handler is the skip-when-ahead prefix + the pump body.
            if self.reu_pump_governor:
                handler = patch_chunk_size(
                    REU_IRQ_HANDLER_GOVERNOR, REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS, chunk
                )
            else:
                handler = patch_chunk_size(REU_IRQ_HANDLER, REU_IRQ_HANDLER_CHUNK_OFFSETS, chunk)
            try:
                self._require_confirmed(
                    "handler",
                    lambda: self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", handler),
                )
            except PumpInstallError as e:
                self._abandon_pump_bring_up(e)
                raise

        def program_rec_and_rate() -> None:
            self.api.write_memory(
                f"{REU.C64_ADDR_LO:04X}",
                f"{initial_dst & 0xFF:02X}{(initial_dst >> 8) & 0xFF:02X}",
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
            # Reprogram CIA #1 Timer A latch for the matched pump rate (see
            # _program_reu_pump_rate, which the mic bring-up shares).
            self._program_reu_pump_rate(
                chunk,
                overdrive=REU_GOVERNOR_PUMP_OVERDRIVE if self.reu_pump_governor else 1.0,
            )

        # Arm the NMI on the pre-filled ring and patch $0314 at the pump entry,
        # skipped when the display mode's bank-swap dispatcher owns $0314 and
        # JMPs to $C100 itself.
        self._arm_installed_pump(
            program_rec_and_rate,
            tracked=skip_irq_vector_hook,
            dispatcher_owns_irq=skip_irq_vector_hook,
        )
        log.info(
            "audio: REU pump installed at $%04X, chunk=%d, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            chunk,
            self._reu_cia1_latch_nominal,
        )

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
        armed. Also runs when a failed install's unwind could not confirm its
        vector restore (`_irq_vector_restore_owed`). Order: vector restore
        FIRST so the next kernal IRQ doesn't fire into a handler we're about
        to dismantle, then CIA #1 latch back to kernal's value, then the
        normal NMI/SID teardown.

        The vector restore is confirmed like the unwind's: this is the last
        write that can take the pump off $0314, and one lost on a lossy link
        leaves it running for every scene after. One that never confirms stays
        owed, so the shared streamer's next stop() writes it again.

        A CIA #1 unmask that never confirmed after a masked $C100 write
        (`_cia1_unmask_owed`) is written again here the same way, and only
        behind a $0314 restore that confirmed: a restore that fails has just
        written the stub, and an unmask already owed keeps its mask there.
        Unmasking without one would
        undo the mask `uninstall_bank_swap_irq` leaves when its own restore is
        lost, and vector every jiffy IRQ through the stale in-RAM dispatcher."""
        if not (self._reu_pump_armed or self._irq_vector_restore_owed or self._cia1_unmask_owed):
            return
        # Retire the mic ring governor's latch writes first; one already in
        # flight finishes before this returns, so it lands ahead of the
        # restore below rather than after it.
        with self._pump_trim_lock:
            self._pump_trim_token += 1
        # Cleared by the confirmed restore only once it held.
        self._irq_vector_restore_owed = True
        unmask_label, unmask = self._cia1_unmask_step()
        run_teardown_steps(
            log,
            type(self).__name__,
            [
                ("IRQ vector restore", self._restore_irq_vector_confirmed),
                ("CIA #1 Timer A latch restore", self._restore_cia1_latch),
                (
                    unmask_label,
                    lambda: (
                        unmask()
                        if self._cia1_unmask_owed and not self._irq_vector_restore_owed
                        else None
                    ),
                ),
                ("REU pump disarm flush", self.api.flush),
            ],
        )
        self._reu_pump_armed = False

    def _write_cia1_timer_a_latch(self, latch: int) -> None:
        """Write CIA #1 Timer A's 16-bit latch, LO then HI, in one DMA write."""
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
        )

    def _restore_cia1_latch(self) -> None:
        """Put CIA #1 Timer A back to this machine's kernal default, without
        which the jiffy clock, `SCNKEY` and the cursor blink stay at the REU
        pump's rate. Raises on a `system` that resolves to neither NTSC nor
        PAL."""
        latch = kernal_cia1_latch(self.system)
        self._write_cia1_timer_a_latch(latch)

    def push_samples(self, samples_int16: np.ndarray, *, epoch: int | None = None) -> int:
        """Convert mono int16 → 4-bit volume codes and enqueue. Blocks
        briefly when the queue is full so the PyAV demuxer naturally
        throttles to the audio sample rate. A no-op once stopped, as the
        sampler's is.

        ``epoch`` is the flush epoch (:meth:`current_flush_epoch`) the
        producer read alongside its decision to push, and the blob is tagged
        with it: one read before a cut and pushed after it is dropped, and
        one read after a cut is kept even when the cut's flush() has yet to
        run. Without it, the epoch at entry.

        Returns the samples enqueued: 0 once stopped, or when the queue stayed
        full past ``QUEUE_PUT_TIMEOUT_S`` plus the worker's drain time for the
        blob, and the blob was dropped."""
        # Ahead of the running check, which stop()'s bump precedes: a stop()
        # that lands after the check finds this capture already stale. A
        # producer's own is older still.
        if epoch is None:
            epoch = self._flush_epoch
        if not self.running:
            return 0
        floats = samples_int16.astype(np.float32) / INT16_FULL_SCALE
        # Pre-DSP analysis tap, as in the mic callbacks, so a decoded file
        # drives reactive visuals through the same analyzer. Only audio the
        # queue took: the file source reads the tap at this streamer's played
        # count, which a blob dropped on a backpressure timeout never enters,
        # so tapping it too would leave every later window that far behind
        # the sound.
        accepted = self._encode_and_enqueue(floats, block_on_full=True, epoch=epoch)
        if accepted:
            # A video's demuxer ends its input at EOF and pushes again
            # after a seek back (an A/B loop wrap, a resume near the end).
            self._input_ended = False
            self._push_to_analysis(floats)
        return accepted

    def end_input(self) -> None:
        """The ``push_samples`` producer has ended: start the consumer on what
        it pushed even when that is short of the prebuffer, which otherwise
        never fills and leaves a short clip unplayed. Call it after the last
        push returns. Cleared when the next worker starts, by a splice's cut(), and
        by the next accepted push."""
        self._input_ended = True
        # An empty blob wakes a worker parked in a priming collect, which
        # would otherwise wait out its chunk period for samples that will not
        # come. Nothing else enqueues one. A full queue has no parked worker.
        with contextlib.suppress(queue.Full):
            self.q.put_nowait((self._flush_epoch, b""))

    def position_seconds(self) -> float:
        """Approximate playback position from the consumer's perspective.

        Host-DMA mode: (samples pushed - samples still queued - the ring's
        unplayed content lead + the content played since the last landing) /
        effective_rate.
        REU pump mode: wall-clock seconds since the IRQ pump armed, clamped to
        the total source length so over-runs don't desync video — but only when
        there IS a total. A live REU-mic session has no finite length and never
        sets one, and clamping a wall clock to a zero total pinned it at 0.0 for
        the whole session (or, on a streamer reused after a staged video scene,
        to the previous track's length).

        Host-DMA mode counts a sample once it lands in the C64 ring, which the
        NMI plays a ring gap later: about a third of a second at the servo's
        target. Video slaved to the landed count ran that far ahead of the
        sound, so the clock subtracts the servo's smoothed gap, less the pad
        still inside it, and reads 0 until the consumer starts. It never
        decreases within an activation: an audio-file scene ends when it
        reaches the pushed length.

        The divisor is `effective_rate` because this is a real-time clock —
        video is slaved to it, and the `clock/wall` gauge that calibrates
        [audio].dac_bitmap_tempo_* reads it against wall time, so dividing by
        the *requested* rate put a standing 0.27% (NTSC@12kHz) in both.
        """
        rate = self.effective_rate
        if not rate:
            return 0.0
        if self._reu_pump_armed:
            elapsed = max(0.0, time.monotonic() - self._reu_pump_start_time)
            if not self._reu_pump_total_samples:
                return elapsed
            return min(elapsed, self._reu_pump_total_samples / rate)
        _, heard = self._host_clock_bytes()
        return heard / rate

    @property
    def content_lag_seconds(self) -> float:
        """Always 0. The sampler's figure (`UltimateAudioSampler.content_lag_seconds`)
        is how far its re-anchors put the sound behind a wall clock; this
        clock counts the samples that landed, so it moves with the sound.
        Present so `AudioFileSource` reads either sink's lag as a typed
        attribute, where a rename fails the type check instead of reading 0."""
        return 0.0

    def ring_lead_seconds(self) -> float:
        """Audio landed in the C64 ring but not yet played: a splice's first
        sample is heard at ``position_seconds() + ring_lead_seconds()``, so
        the video waits until then. The splice itself anchors on what
        ``flush()`` returns, which also counts the chunk the worker is
        writing, since it lands after this read.

        It is the landed content less what position_seconds() reports as
        heard, from one read of both, so the anchor is exactly the landed
        count however the clock got there: before the consumer starts nothing
        landed has played, and after it the clock has already taken out the
        pad the smoothed gap counts."""
        rate = self.effective_rate
        if not rate or self._reu_pump_armed:
            return 0.0
        consumed, heard = self._host_clock_bytes()
        return max(0.0, consumed - heard) / rate

    def _host_clock_bytes(self) -> tuple[int, float]:
        """``(landed content, heard content)`` in bytes on the host-DMA path.

        Heard is the landed content less the servo's smoothed ring gap, with
        the pad still inside that gap taken back out (``_unplayed_pad``), plus
        the content played since the last landing (``_played_since_landing``),
        and never below what an earlier read of this activation reported: the gap
        is smoothed, so a widening gap would otherwise walk the clock back.
        Zero before the consumer starts.

        Consumed is read first: the worker records a landing's pad before it
        counts the landing, so a read torn across one lags by that chunk
        rather than leading by it. Locked: the worker's discard of a
        pre-splice chunk drops both counts, and an unlocked read pairing the
        old pushed with the new queued leads by that chunk. Read inside
        ``_ring_pad_lock`` too: a count read before a reset and a floor
        written after it would hold the new activation's clock at the old
        one's position."""
        with self._ring_pad_lock:
            with self._count_lock:
                consumed = max(0, self._pushed_count - self._queued_samples)
            lead = self.servo.ring_lead
            if lead < 0:
                return consumed, 0.0
            content_lead = max(0.0, lead - self._unplayed_pad(lead))
            # Capped rather than trusted to come out under: the gap's content
            # counts from the fractional front and the played span from a whole
            # byte, so with pad across the front they differ by a few ULPs.
            played = min(self._played_since_landing(lead), content_lead)
            heard = max(self._position_floor, consumed - content_lead + played)
            self._position_floor = heard
        return consumed, heard

    def _played_since_landing(self, lead: float) -> float:
        """Content the NMI has played since the last landing, in bytes.
        Caller holds ``_ring_pad_lock``.

        The landed count moves a whole chunk at a time, and the gap is read
        just after a landing, so without this the clock held for a chunk
        period (≈85 ms at 12 kHz) and then jumped by a chunk. An analyzer
        reading the audio-file tap at that clock saw its window jump by its
        own length, and a click near the edge of the one window that held it
        never reached the onset threshold; video slaved to it moved in the
        same steps.

        It runs at the pace chunks land (``_landing_pace_locked``),
        which is the speed the NMI drains them at. What the NMI plays next is
        the front of the gap, so pad there moves
        nothing: content landed behind a dry stretch is not heard until the
        stretch has played. At most one chunk, the next landing's worth, so a
        link that stalls holds the clock rather than running it past what
        landed. Zero until the consumer starts."""
        if self._ring_landed_at is None:
            return 0.0
        elapsed = max(0.0, time.monotonic() - self._ring_landed_at)
        # Whole bytes: the pad record is in whole bytes, so a span of nothing
        # but pad then nets exactly 0. In fractional bytes it netted a few ULPs
        # over, and the clock read content landed behind the pad as heard.
        pace = self._landing_pace_locked()
        span = int(min(elapsed * pace, float(self.chunk_size), lead))
        lo = self._ring_landed_total - int(lead)
        return float(span - self._pad_in(lo, lo + span))

    def _mark_ring_clock(self) -> None:
        """The consumer started: interpolate the clock from now, at the
        nominal rate until landings measure the pace."""
        with self._ring_pad_lock:
            self._ring_landed_at = time.monotonic()
            self._landings.clear()
            self._landing_pace = float(self.effective_rate)

    def _armed_rate(self) -> float:
        """The rate the CIA #2 latch now armed fires at: above
        ``effective_rate`` under a pitch multiplier or the adaptive loop's
        bus-halt compensation, which drain the ring that much faster."""
        rate = self.effective_rate
        latch = self.nmi.latch
        return max(rate, actual_rate_for_latch(latch, self.system)) if latch > 0 else rate

    def _landing_pace_locked(self) -> float:
        """Bytes per second the clock runs at between landings. Caller holds
        ``_ring_pad_lock``.

        The worker lands chunks as fast as the NMI drains them, so the pace
        is the speed the sound is heard at. Run at the nominal rate instead,
        the clock reached the next chunk early whenever bus halts slowed the
        NMI, then held until the landing: under bitmap video it moved in
        bursts and holds, and video slaved to it skipped and froze frames.

        It is the bytes landed across the window over the time they took,
        rather than an average of per-landing rates: a stall and the
        back-to-back landings the worker drips to catch up on it cancel in
        the sum, where per-landing samples had to be told apart and
        filtered, and capped samples read landing jitter as a slower pace.
        Capped at the armed NMI rate, which the drain cannot beat."""
        marks = self._landings
        if len(marks) > LANDING_PACE_MIN_INTERVALS:
            (t0, b0), (t1, b1) = marks[0], marks[-1]
            if t1 > t0:
                self._landing_pace = (b1 - b0) / (t1 - t0)
        if self._landing_pace <= 0:
            return self.effective_rate
        return min(self._landing_pace, self._armed_rate())

    def _note_landing_pace_locked(self, now: float, paced: bool) -> None:
        """Record a landing in the pace window. Caller holds
        ``_ring_pad_lock``.

        The window starts afresh at an unpaced landing, and the next landing
        is its first mark, so neither that landing's bytes nor the interval
        after it is measured. The first landing after the consumer starts is
        a first mark too: the worker hands its first chunk off a pace period
        before dripping it, so that interval spans two to three chunk periods
        for one chunk."""
        marks = self._landings
        if not paced:
            marks.clear()
            return
        marks.append((now, self._ring_landed_total))
        while (
            len(marks) > LANDING_PACE_MIN_INTERVALS + 1
            and marks[1][0] <= now - LANDING_PACE_WINDOW_S
        ):
            marks.popleft()

    def _unplayed_pad(self, lead: float) -> float:
        """The pad bytes among the last ``lead`` bytes landed in the ring.
        Caller holds ``_ring_pad_lock``."""
        return self._pad_in(self._ring_landed_total - lead, self._ring_landed_total)

    def _pad_in(self, lo: float, hi: float) -> float:
        """The pad bytes in ``[lo, hi)`` of the ring's landed byte stream.
        Caller holds ``_ring_pad_lock``. The record is in landing order, so
        the walk is newest first and stops at the first pad wholly behind
        ``lo``."""
        pad_bytes = 0.0
        for end, pad in reversed(self._ring_pads):
            if end <= lo:
                break
            pad_bytes += max(0.0, min(hi, end) - max(lo, end - pad))
        return pad_bytes

    def _note_ring_landed(
        self, generation: int, nbytes: int, pad: int, *, paced: bool = True
    ) -> None:
        """Worker-side: ``nbytes`` reached the ring, the last ``pad`` of them
        padding. Pad further back than a whole ring can no longer be inside
        the gap, so it is dropped, which bounds the record at a ring's worth
        of chunks.

        A worker that outlived stop()'s bounded join records nothing: its
        write returning late would otherwise land in the next activation's
        fresh record, and the high-water mark would hold the clock past what
        that landing moved. :meth:`_start_worker` bumps the generation and
        clears the record under this lock, so no stale landing slips between
        the two.

        ``paced=False`` starts the pace window afresh: the stall re-anchor's
        lead of pad and a splice's NEUTRAL fill are written at once rather
        than drained in, and the worker collects and hands off the next chunk
        before that one lands."""
        with self._ring_pad_lock:
            if generation != self._worker_generation:
                return
            self._ring_landed_total += nbytes
            if self._ring_landed_at is not None:
                now = time.monotonic()
                self._note_landing_pace_locked(now, paced)
                self._ring_landed_at = now
            total = self._ring_landed_total
            if pad > 0:
                self._ring_pads.append((total, pad))
            while self._ring_pads and self._ring_pads[0][0] <= total - RING_BUFFER_SIZE:
                self._ring_pads.popleft()

    def _reset_ring_clock(self) -> None:
        """Start the host-DMA clock's pad record and high-water mark afresh,
        for a new activation."""
        with self._ring_pad_lock:
            self._clear_ring_clock_locked()

    def _clear_ring_clock_locked(self) -> None:
        """:meth:`_reset_ring_clock`'s body. Caller holds ``_ring_pad_lock``."""
        self._ring_landed_total = 0
        self._ring_pads.clear()
        self._position_floor = 0.0
        self._ring_landed_at = None
        self._landings.clear()
        self._landing_pace = 0.0

    def reset_position(self) -> None:
        with self._count_lock:
            self._pushed_count = 0
            self._in_flight_samples = 0
        self._reset_ring_clock()

    def _drain_queue_samples(self) -> int:
        """get_nowait-drain self.q; return the total samples dropped (each blob
        is one byte per sample, see the q comment in __init__). Used by stop()
        and the mic path's stall re-anchor."""
        drained = 0
        while True:
            try:
                _, blob = self.q.get_nowait()
            except queue.Empty:
                break
            drained += len(blob)
        return drained

    def current_flush_epoch(self) -> int:
        """The flush epoch a push made now is tagged with; see
        :meth:`push_samples`."""
        return self._flush_epoch

    def cut(self) -> FlushCut:
        """Retire every sample pushed so far and anchor the splice: the first
        step of :meth:`flush`, split out so a video source can take it under
        the lock that sets its pending seek (see
        :meth:`AVFileSource.request_seek`). Every push tagged with an earlier
        epoch is dropped from then on, wherever it is: in a blocked pusher,
        the queue, or the worker's hand.

        The anchor is the ``position_seconds()`` at which the first sample
        pushed after the cut is heard: the landed count plus the chunk the
        worker is writing, from one read of both. No-op in REU-pump mode."""
        if self._reu_pump_armed:
            return FlushCut(None, self.position_seconds())
        rate = self.effective_rate
        # The landed count, plus the chunk the worker is writing, which plays
        # ahead of every post-splice sample but lands after this read. One
        # read, under the lock the worker claims a ring write and counts a
        # landing under, with the epoch bump: a chunk claimed before it is
        # counted here, and one checked after it is dropped. The heard
        # position never passes the landed count, so the landed count is
        # where the first post-splice sample is heard. The worker discards
        # the retired blobs with the paired subtract, which moves neither.
        with self._count_lock:
            self._flush_epoch += 1
            epoch = self._flush_epoch
            landed = max(0, self._pushed_count - self._queued_samples) + self._in_flight_samples
            # A pass that reached EOF before the seek was requested ended the
            # input, and the post-splice pass has yet to push: left ended, a
            # priming worker pads its prebuffer out with silence and a stall
            # after the splice is not counted. Cleared here rather than in
            # flush(): a post-splice pass short enough to end before flush()
            # runs has its end kept.
            self._input_ended = False
        return FlushCut(epoch, landed / rate if rate else 0.0)

    def flush(self, *, silence_output: bool = False, cut: FlushCut | None = None) -> float:
        """Drop all pre-splice (not-yet-ring-written) audio WITHOUT moving
        position_seconds(). Used by VideoScene's transport splice (seek / loop
        wrap / resume) so stale pre-splice audio doesn't play after the demuxer
        re-seeks. Returns the splice anchor (see :meth:`cut`); ``cut`` is one
        already taken, else this takes it. ``silence_output`` additionally
        asks the worker to NEUTRAL-fill the unplayed ring region (pause fast
        mute) — the worker owns write_addr, so it executes the ring stomp, not
        this thread.

        Nothing is drained here: each blob carries the epoch it was pushed
        in, and the worker discards a retired one when it takes it, through
        :meth:`_discard_unpushed`, whose paired subtract leaves
        ``position = pushed - queued`` unchanged. A drain could not tell the
        retired blobs from post-splice ones a demuxer pushed between the cut
        and this call. No-op in REU-pump mode (no host queue to flush)."""
        if cut is None:
            cut = self.cut()
        if self._reu_pump_armed:
            return cut.anchor_s
        if silence_output:
            self._stomp_requested = True
        return cut.anchor_s

    def _stomp_ring(self, write_addr: int, current: Callable[[], bool]) -> None:
        """NEUTRAL-fill the unplayed ring region ``(R + guard .. W)`` for the
        pause fast mute. On a bad R read it just returns (the drained queue pads
        the ring to silence within ~1 s regardless). Called only from the worker
        thread, so write_addr is the live worker-local W.

        ``current`` is the worker's fence: the R read and each stomp write can
        park past stop()'s join, as the stall re-anchor's can, and a worker
        superseded meanwhile writes nothing more (see :meth:`_stomp_from`).
        It is checked before the request is taken, too: a worker already
        superseded leaves ``_stomp_requested`` alone, because once a later
        start_* has run, a pause that set it is the next session's."""
        if not current():
            return
        self._stomp_requested = False
        r_addr = self.read_consumer_ptr()
        if r_addr is None:
            return
        self._stomp_from(r_addr, write_addr, current)

    def _stomp_from(self, r_addr: int, write_addr: int, current: Callable[[], bool]) -> None:
        """NEUTRAL-fill ``(r_addr + guard .. write_addr)`` — the pause stomp's
        span, and the stall re-anchor's — split at ``RING_BUFFER_END``.

        ``current`` fences the writes: once it is False, the rest are skipped.
        A wrapped span's second write lands at ``RING_BUFFER_ADDR``, where the
        next session's prebuffer starts, so a worker superseded during the
        first must not make it."""
        for addr, ln in stomp_spans(r_addr, write_addr):
            if not current():
                return
            self._neutral_fill_ring(addr, ln)

    def _hardware_teardown_steps(self) -> list[tuple[str, Callable[[], object]]]:
        """The C64-side teardown of a DAC session, in `stop()`'s cutoff order."""
        steps: list[tuple[str, Callable[[], object]]] = [
            (
                "NMI source disable",
                lambda: self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP),
            ),
            (
                "KERNAL NMI vector restore",
                lambda: self.api.write_regs(
                    f"{VECTORS.NMI:04X}",
                    KERNAL.DEFAULT_NMI & 0xFF,
                    (KERNAL.DEFAULT_NMI >> 8) & 0xFF,
                ),
            ),
            ("SID volume mute", lambda: self.api.write_memory("D418", "00")),
        ]
        if self.digi_boost or self._dac_curve is not None:
            steps.append(("DAC bias release", self._release_sid_gates))
        return steps

    def _close_mic_stream(self) -> None:
        stream, self.mic_stream = self.mic_stream, None
        if stream is not None:
            run_teardown_steps(
                log,
                type(self).__name__,
                [("mic stop", stream.stop), ("mic close", stream.close)],
            )

    def stop(self) -> None:
        # Retire the worker's generation first, and here, not only in the next
        # _start_worker: the REU-pump and listen-only starts set running back
        # to True without starting a worker, and start_mic sets it before its
        # _start_worker bumps. Either way an orphan that outlived the join
        # below would otherwise read its fence as current again, and drip into
        # a ring it no longer owns. Under the pad lock, so a landing the orphan
        # records is either counted before the bump or refused after it.
        with self._ring_pad_lock:
            self._worker_generation += 1
        # A listen-only session never touched the NMI/DAC/SID, so writing $D418
        # or the NMI vectors here would be spurious U64 traffic.
        if self._listen_mode:
            self.running = False
            self._listen_mode = False
            self._close_mic_stream()
            return
        # Teardown order, for a clean cutoff:
        #  - REU pump (if armed): restore the IRQ vector + CIA #1 latch FIRST so
        #    the pump cannot fire into a teardown in progress.
        #  - Then disable the NMI source. The worker can sit up to 2 ×
        #    chunk_period (~256 ms) in q.get before it sees running=False, and
        #    the NMI keeps playing the ring through that as an echo past the
        #    visual end of the clip.
        #  - Then restore the KERNAL NMI vector, which is what puts the in-RAM
        #    $D418 writer out of reach — so the SID mute follows it and holds
        #    even when the NMI-source disable is the write that failed.
        #  - The DAC-bias gate release goes last, so the bias collapse it
        #    starts (release=0 under digi-boost) happens at volume 0.
        # Ahead of `running` clearing, which is what releases a producer
        # parked in the backpressure spin: bumped after it, a producer that
        # woke in between found its epoch current and landed its blob. The
        # drain at the bottom only catches one that beats it there. Under
        # _count_lock, where the push path checks it and puts.
        with self._count_lock:
            self._flush_epoch += 1
        self.running = False
        # The callback stops claiming re-anchors at running=False; the servo
        # must stop posting them before the teardown below can stall.
        if self._mic_lead is not None:
            self._mic_lead.request_stop()
        # No-op if the pump was never armed and no $0314 restore or CIA #1
        # unmask is owed. The video pumps' governor lives in the C64-side
        # handler, so disarming the IRQ vector stops it; the mic pump's
        # host-side MicRingGovernor is fenced off by the disarm's trim-token
        # bump.
        self._disarm_reu_pump()
        run_teardown_steps(log, type(self).__name__, self._hardware_teardown_steps())
        self.api.note_nmi_consumer(False)
        # NMI is already silenced; let the worker / mic threads tear down
        # at their own pace.
        self._close_mic_stream()
        self._stop_mic_lead_servo()
        if self._worker_thread:
            # A plain bounded join, not session.join_bounded: a daemon thread
            # joined off the main thread, and the audio layer must not import
            # the app layer.
            self._worker_thread.join(timeout=WORKER_JOIN_TIMEOUT_S)
            if self._worker_thread.is_alive():
                # A ring write on a stalled link can outlast the bounded join,
                # and the counters cleared just below are still being mutated.
                # Dropping the reference is safe: the surviving worker is
                # generation-fenced (see _worker, and the bump at the top of
                # this method), so no later start_* can resurrect it into a
                # second live writer.
                log.warning(
                    "audio: worker did not exit within %.1fs; ring writes may still "
                    "be in flight (it will exit when its write returns)",
                    WORKER_JOIN_TIMEOUT_S,
                )
            self._worker_thread = None
        # Drain so subsequent runs start clean. The epoch bumped above is what
        # covers a producer still between its own capture and its put.
        self._drain_queue_samples()
        # Locked: a producer rolling back a blob the epoch bump refused does a
        # read-modify-write under _count_lock, and an unlocked store here could
        # land inside it and be overwritten with the pre-zero count.
        with self._count_lock:
            self._pushed_count = 0
            self._queued_samples = 0
            self._in_flight_samples = 0
        self._reset_ring_clock()
        self._stomp_requested = False
        # The streamer is reused across scenes: a total outliving its own scene
        # would clamp the next scene's position_seconds clock.
        self._reu_pump_total_samples = 0
        if self._mic_reu_write_errors:
            log.warning(
                "audio[reu mic]: %d REU write failures this run (mic audio dropped "
                "out); see the first traceback above",
                self._mic_reu_write_errors,
            )
        self._mic_reu_write_errors = 0
        # Clear the timer's pitch-comp/mode/arm state and the servo's watchdog +
        # adaptive-rate state so the next scene re-acquires from nominal; the
        # per-mode learned-latch cache survives (NmiTimer.reset_after_stop).
        self.nmi.reset_after_stop()
        self.servo.reset_after_stop()
        # Gated on the worker having actually written to the ring, and worded
        # per run: stop() is called at scene teardown and again at session
        # teardown, and the second call's counters are already cleared.
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
        # Sub-writes that reached their slot after its deadline. A high count
        # means the spread collapsed toward one bunched write per chunk period —
        # audible modulation that no underrun counter registers.
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
        self._late_worst_window_s = 0.0
        # Confirms the closed loop held the ring gap near half a ring (4096) and
        # never approached a lap (0) or an underrun (RING_BUFFER_SIZE). The
        # external drift probe assumes a fixed wall-clock W and cannot see this.
        if self.servo.gap_last >= 0:
            log.info(
                "audio: host-DMA servo gap last=%d min=%d max=%d (target=%d, lap at 0/%d)",
                self.servo.gap_last,
                self.servo.gap_min,
                self.servo.gap_max,
                HOST_DMA_SERVO_TARGET_GAP,
                RING_BUFFER_SIZE,
            )
        self.servo.reset_run_telemetry()

    def close(self) -> None:
        # The API is the render path's shared C64Backend, not ours: the caller
        # closes it after the final reset, and closing it here strands reset().
        self.stop()
